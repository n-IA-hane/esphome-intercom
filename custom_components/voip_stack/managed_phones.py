"""Private, config-entry-backed ownership of managed ESPHome SIP accounts."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field

from .esphome_native import device_info, normalize_mac, verified_action_names
from .sip_registrar import SipAccount, validate_managed_username
from .phone_queue_membership import normalize_membership_tracking, normalize_phone_queues

CONF_MANAGED_PHONES = "managed_esphome_phones"
CONF_ESPHOME_ENROLLMENTS = "esphome_sip_enrollments"
MANAGED_ACCOUNT_ID = "ha"
REGISTER_HA_ACCOUNT = "register_ha_account"
MANAGED_ACTIONS = frozenset({"set_ha_display_name", "set_account", REGISTER_HA_ACCOUNT})


class ManagedNameError(ValueError):
    """A firmware or configuration change is needed before enrollment."""

    def __init__(self, issue, *, value=None):
        super().__init__(issue)
        self.issue = issue
        self.value = value


def managed_device_username(native) -> str:
    """Use ESPHome's effective friendly name, whose native suffix is already applied."""
    info = device_info(native)
    try:
        return validate_managed_username(info.friendly_name or info.name)
    except ValueError as exc:
        raise ManagedNameError("esphome_account_name") from exc


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
        endpoint_id = self.endpoint_id or username
        if (not all(isinstance(value, str) for value in (self.display_name, self.extension, self.route_id,
                self.api_host, self.ring_group, self.conference_group))
                or len(self.display_name.encode()) > 1024
                or len(self.extension.encode()) > 1024
                or any(ord(char) < 32 or ord(char) == 127 for char in self.display_name + self.extension)):
            raise ValueError('Invalid managed phone settings')
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
            if row.subentry_type == "phone" and (row.data.get("kind") == "esphome" or row.data.get("managed_esphome"))
            and isinstance(device_id := row.data.get("device_id"), str) and device_id}


def managed_records(entry) -> dict[str, ManagedPhone]:
    """Read canonical native accounts and unmigrated records from older releases."""
    candidates = dict(getattr(entry, "data", {}).get(CONF_MANAGED_PHONES, {}))
    for device_id, row in managed_account_subentries(entry).items():
        identity = getattr(row, "unique_id", None)
        if isinstance(identity, str) and identity.startswith("phone:") and identity[6:]:
            candidates[device_id] = {**row.data, "endpoint_id": identity[6:],
                "display_name": row.data.get("name") or row.data.get("display_name") or row.title,
                "active": bool(row.data.get("enabled", True) and row.data.get("active", True))}
    records = {}
    for key, raw in candidates.items():
        if not isinstance(raw, Mapping):
            continue
        try:
            from .phone_queue_membership import project_phone_memberships
            raw = project_phone_memberships(entry, str(raw.get("endpoint_id") or raw.get("username") or ""), raw)
            phone = ManagedPhone(**{name: raw[name] for name in ManagedPhone.__dataclass_fields__ if name in raw})
            if phone.device_id != key:
                continue
            records[key] = phone
        except (TypeError, ValueError):
            continue
    return records


def save_managed_account(hass, entry, phone, *, username_source=None):
    """The subentry owns the credential. Its native unique ID owns the endpoint ID."""
    from types import MappingProxyType
    from homeassistant.config_entries import ConfigSubentry

    row = managed_account_subentries(entry).get(phone.device_id)
    data = phone.record()
    unique_id = "phone:" + data.pop("endpoint_id")
    data["kind"] = "sip_account"
    data["name"] = phone.display_name or phone.username
    data["managed_esphome"] = True
    data["managed_exclusive"] = bool(row.data.get("managed_exclusive", True)) if row else True
    if row is not None:
        data = {**row.data, **data}
        for field in ('name', 'display_name'):
            if field in row.data:
                data[field] = row.data[field]
    if username_source is not None:
        data['username_source'] = username_source
    from .phone_queue_membership import GROUP_MEMBERSHIP_VERSION
    if entry.data.get(GROUP_MEMBERSHIP_VERSION) == 1:
        data.pop("queues", None)
        data.pop("ring_group", None)
    title = phone.display_name or phone.username
    if row is None:
        row = ConfigSubentry(subentry_type="phone", title=title, unique_id=unique_id, data=MappingProxyType(data))
        hass.config_entries.async_add_subentry(entry, row)
        if phone.active:
            from .phone_config import async_enable_registrar_for_new_account
            async_enable_registrar_for_new_account(hass, entry)
    elif row.data != data or row.unique_id != unique_id:
        # Hardware refreshes update the association, never the account label.
        hass.config_entries.async_update_subentry(entry, row, data=data, unique_id=unique_id)

    # HA permits deletion while this integration is unloaded. Keep enough history
    # to distinguish that choice from first discovery and purge the original endpoint.
    receipts = entry.data.get(CONF_ESPHOME_ENROLLMENTS, {})
    receipt = {"origin_entry_id": phone.origin_entry_id, "endpoint_id": phone.endpoint_id}
    if receipts.get(phone.device_id) != receipt:
        hass.config_entries.async_update_entry(entry, data={**entry.data,
            CONF_ESPHOME_ENROLLMENTS: {**receipts, phone.device_id: receipt}})
    return row


def managed_phone_mode(_hass, origin, device_id: str, mac: str) -> bool | None:
    """Only a complete, MAC-verified native inventory can change firmware mode.

    ESPHome retains this inventory on disconnect. An incomplete replacement
    inventory cannot authorize provisioning RPC; SIP accounts remain independent.
    """
    actions = verified_action_names(origin, mac)
    return MANAGED_ACTIONS <= actions if actions is not None else None


def active_managed_accounts(hass, entry) -> list[SipAccount]:
    """Read retained upgrade input until canonical account subentries replace it."""
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
