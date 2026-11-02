"""Private, config-entry-backed ownership of managed ESPHome SIP accounts."""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
import re

from .sip_registrar import SipAccount, normalize_username

CONF_MANAGED_PHONES = "managed_esphome_phones"
MANAGED_ACCOUNT_ID = "ha"
MANAGED_ACTIONS = frozenset({"set_ha_display_name", "set_account", "register_account"})


def normalize_mac(value: object) -> str:
    """Accept only a complete MAC, independently of its presentation."""
    mac = str(value or "").replace(":", "").replace("-", "").casefold()
    if re.fullmatch(r"[0-9a-f]{12}", mac) is None:
        raise ValueError("Managed phones require a complete MAC")
    return mac


@dataclass(frozen=True, slots=True)
class ManagedPhone:
    """A credential and its physical owner, never a public directory record."""

    device_id: str
    origin_entry_id: str
    mac: str
    password: str = field(repr=False)
    username: str = ""
    endpoint_id: str = ""
    display_name: str = ""
    route_id: str = ""
    api_host: str = ""
    extension: str = ""
    ring_group: str = ""
    conference_group: str = ""
    conference_ring: bool = False
    active: bool = True

    def __post_init__(self):
        mac = normalize_mac(self.mac)
        username = normalize_username(self.username or f"esp_{mac}")
        endpoint_id = self.endpoint_id or f"esphome:{self.device_id}"
        if (not isinstance(self.device_id, str) or not self.device_id
                or not isinstance(self.origin_entry_id, str) or not self.origin_entry_id
                or not isinstance(self.password, str) or not 1 <= len(self.password) <= 128
                or len(username.encode()) > 64 or not isinstance(endpoint_id, str)
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
        return asdict(self)


def managed_records(entry) -> dict[str, ManagedPhone]:
    """Read the owning config entry without another credential database."""
    records = {}
    for key, raw in entry.data.get(CONF_MANAGED_PHONES, {}).items():
        if not isinstance(raw, Mapping):
            continue
        try:
            phone = ManagedPhone(**{name: raw[name] for name in ManagedPhone.__dataclass_fields__ if name in raw})
            if phone.device_id != key or not isinstance(raw.get("username"), str) or not raw["username"]:
                continue
            records[key] = phone
        except (TypeError, ValueError):
            continue
    return records


def managed_phone_mode(_hass, origin, device_id: str, mac: str) -> bool | None:
    """Only a complete, MAC-verified native inventory can change firmware mode.

    ESPHome retains this inventory on disconnect. A replacement API owner with
    an incomplete inventory cannot silently reactivate a suspended credential.
    """
    native = getattr(origin, "runtime_data", None)
    complete = getattr(native, "first_connect_done", None)
    info = getattr(native, "device_info", None)
    if complete is None or not complete.is_set() or info is None:
        return None
    try:
        if normalize_mac(info.mac_address) != normalize_mac(mac):
            return None
    except ValueError:
        return None
    services = getattr(native, "services", None)
    if not isinstance(services, Mapping):
        return None
    return MANAGED_ACTIONS <= {service.name for service in services.values()}


def active_managed_accounts(hass, entry) -> list[SipAccount]:
    """A suspended or permanently removed owner cannot regain SIP access."""
    from homeassistant.helpers import device_registry as dr

    accounts = []
    for phone in managed_records(entry).values():
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
