"""Private, config-entry-backed ownership of managed ESPHome SIP accounts."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
import re

from .esphome_native import action_names, device_info, normalize_mac, verified_action_names
from .sip_registrar import SipAccount, validate_managed_username
from .phone_queue_membership import normalize_membership_tracking, normalize_phone_queues

CONF_MANAGED_PHONES = "managed_esphome_phones"
CONF_ESPHOME_ENROLLMENTS = "esphome_sip_enrollments"
MANAGED_ACCOUNT_ID = "ha"
MANAGED_ACTIONS = frozenset({"set_ha_display_name", "set_account", "register_account"})
REGISTER_BY_NODE = "register_ha_account_by_node_name"
REGISTER_BY_FRIENDLY_NAME = "register_ha_account_by_friendly_name"


def managed_registration_action(native) -> str:
    """New firmware advertises the configured name source in its native action."""
    actions = action_names(native)
    if REGISTER_BY_NODE in actions:
        return REGISTER_BY_NODE
    if REGISTER_BY_FRIENDLY_NAME in actions:
        return REGISTER_BY_FRIENDLY_NAME
    return "register_account"


class ManagedNameError(ValueError):
    """A firmware or configuration change is needed before enrollment."""

    def __init__(self, issue, *, value=None):
        super().__init__(issue)
        self.issue = issue
        self.value = value


def check_native_username(action, username):
    """Firmware without the named action sends its SIP URI user unescaped."""
    if action == "register_account" and re.fullmatch(r"[A-Za-z0-9_.-]+", username) is None:
        raise ManagedNameError("esphome_account_firmware")


def managed_device_username(native) -> str:
    info = device_info(native)
    action = managed_registration_action(native)
    username = info.name if action == REGISTER_BY_NODE else info.friendly_name or info.name
    # Legacy DeviceInfo has both effective names but lacks the YAML flag.
    # A literal node name may also end in the physical MAC suffix.
    if (action == "register_account" and username != info.name
            and info.name.endswith("-" + normalize_mac(info.mac_address)[-6:])):
        raise ManagedNameError("esphome_account_firmware")
    try:
        username = validate_managed_username(username)
    except ValueError as exc:
        raise ManagedNameError("esphome_account_name") from exc
    check_native_username(action, username)
    return username


@dataclass(frozen=True, slots=True)
class ManagedPhone:
    """A credential and its physical owner, never a public directory record."""

    device_id: str
    origin_entry_id: str
    mac: str
    password: str = field(repr=False)
    username: str
    endpoint_id: str = ""
    display_name: str = ""
    route_id: str = ""
    api_host: str = ""
    extension: str = ""
    ring_group: str = ""
    queues: tuple[str, ...] = ()
    conference_group: str = ""
    conference_ring: bool = False
    active: bool = True
    membership_pending: dict[str, str] = field(default_factory=dict)
    membership_observed: dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "queues", normalize_phone_queues(self.queues))
        for name in ("membership_pending", "membership_observed"):
            object.__setattr__(self, name, normalize_membership_tracking(getattr(self, name)))
        mac = normalize_mac(self.mac)
        username = validate_managed_username(self.username)
        endpoint_id = self.endpoint_id or f"esphome:{self.device_id}"
        if (not isinstance(self.device_id, str) or not self.device_id
                or not isinstance(self.origin_entry_id, str) or not self.origin_entry_id
                or not isinstance(self.password, str) or not 1 <= len(self.password) <= 128
                or not isinstance(endpoint_id, str)
                or not 1 <= len(endpoint_id) <= 255 or endpoint_id != endpoint_id.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in endpoint_id)):
            raise ValueError("Invalid managed phone ownership")
        object.__setattr__(self, "mac", mac)
        object.__setattr__(self, "username", username)
        object.__setattr__(self, "endpoint_id", endpoint_id)

    def account(self) -> SipAccount:
        return SipAccount(username=self.username, display_name=self.display_name,
            password=self.password, extension=self.extension, ring_group=self.ring_group,
            conference_group=self.conference_group, conference_ring=self.conference_ring)

    def record(self) -> dict:
        record = asdict(self)
        record["queues"] = list(self.queues)
        return record


def managed_account_subentries(entry):
    """Index native SIP accounts whose physical owner is an ESPHome device."""
    return {device_id: row for row in getattr(entry, "subentries", {}).values()
            if row.subentry_type == "phone" and row.data.get("kind") == "esphome"
            and isinstance(device_id := row.data.get("device_id"), str) and device_id}


def managed_records(entry) -> dict[str, ManagedPhone]:
    """Read canonical native accounts and unmigrated records from older releases."""
    candidates = dict(getattr(entry, "data", {}).get(CONF_MANAGED_PHONES, {}))
    for device_id, row in managed_account_subentries(entry).items():
        identity = getattr(row, "unique_id", None)
        if isinstance(identity, str) and identity.startswith("phone:") and identity[6:]:
            candidates[device_id] = {**row.data, "endpoint_id": identity[6:]}
    records = {}
    for key, raw in candidates.items():
        if not isinstance(raw, Mapping):
            continue
        try:
            phone = ManagedPhone(**{name: raw[name] for name in ManagedPhone.__dataclass_fields__ if name in raw})
            if phone.device_id != key:
                continue
            records[key] = phone
        except (TypeError, ValueError):
            continue
    return records


def save_managed_account(hass, entry, phone):
    """The subentry owns the credential. Its native unique ID owns the endpoint ID."""
    from types import MappingProxyType
    from homeassistant.config_entries import ConfigSubentry

    row = managed_account_subentries(entry).get(phone.device_id)
    data = phone.record()
    unique_id = "phone:" + data.pop("endpoint_id")
    data["kind"] = "esphome"
    title = phone.display_name or phone.username
    if row is None:
        row = ConfigSubentry(subentry_type="phone", title=title, unique_id=unique_id, data=MappingProxyType(data))
        hass.config_entries.async_add_subentry(entry, row)
    elif row.data != data or row.unique_id != unique_id:
        # Preserve the title if the administrator renamed this account separately.
        title = title if row.title == (row.data.get("display_name") or row.data.get("username")) else row.title
        hass.config_entries.async_update_subentry(entry, row, title=title, data=data, unique_id=unique_id)

    # HA permits deletion while this integration is unloaded. Keep enough history
    # to distinguish that choice from first discovery and purge the original endpoint.
    receipts = entry.data.get(CONF_ESPHOME_ENROLLMENTS, {})
    receipt = {"origin_entry_id": phone.origin_entry_id, "endpoint_id": phone.endpoint_id}
    if receipts.get(phone.device_id) != receipt:
        hass.config_entries.async_update_entry(entry, data={**entry.data,
            CONF_ESPHOME_ENROLLMENTS: {**receipts, phone.device_id: receipt}})
    return row


def migrate_managed_accounts(hass, entry):
    """Move existing credentials before enrolling any new devices, including offline ones."""
    legacy = entry.data.get(CONF_MANAGED_PHONES)
    if legacy is None:
        return
    records = managed_records(entry)
    remaining = dict(legacy)
    for device_id in legacy:
        if phone := records.get(device_id):
            save_managed_account(hass, entry, phone)
            remaining.pop(device_id)
    data = dict(entry.data)
    if remaining:
        data[CONF_MANAGED_PHONES] = remaining
    else:
        data.pop(CONF_MANAGED_PHONES)
    hass.config_entries.async_update_entry(entry, data=data)


def managed_phone_mode(_hass, origin, device_id: str, mac: str) -> bool | None:
    """Only a complete, MAC-verified native inventory can change firmware mode.

    ESPHome retains this inventory on disconnect. A replacement API owner with
    an incomplete inventory cannot silently reactivate a suspended credential.
    """
    actions = verified_action_names(origin, mac)
    return MANAGED_ACTIONS <= actions if actions is not None else None


def active_managed_accounts(hass, entry) -> list[SipAccount]:
    """A suspended or permanently removed owner cannot regain SIP access."""
    records = managed_records(entry)
    if not records:
        return []
    from homeassistant.helpers import device_registry as dr

    accounts = []
    for phone in records.values():
        origin = hass.config_entries.async_get_entry(phone.origin_entry_id)
        device = dr.async_get(hass).async_get(phone.device_id)
        if (phone.active and origin is not None and origin.domain == "esphome"
                and device is not None and origin.entry_id == device.config_entry_id
                and any(kind == dr.CONNECTION_NETWORK_MAC and normalize_mac(mac) == phone.mac
                        for kind, mac in device.connections)
                and managed_phone_mode(hass, origin, phone.device_id, phone.mac) is not False):
            accounts.append(phone.account())
    return accounts


def managed_route_mappings(entry) -> list[dict]:
    """Reserve physical identities even offline, without exposing credentials."""
    return [{"id": phone.endpoint_id, "name": phone.display_name, "username": phone.username,
             "extension": phone.extension, "ring_group": phone.ring_group,
             "conference_group": phone.conference_group,
             "voicemail": {"enabled": bool(entry.data.get("esphome_mailboxes", {})
                 .get(phone.endpoint_id, {}).get("enabled"))}}
            for phone in managed_records(entry).values()]
