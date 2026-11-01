"""Persisted ownership of automatically configured ESPHome SIP accounts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from collections.abc import Mapping

from .sip_registrar import SipAccount, normalize_username

CONF_MANAGED_PHONES = "managed_esphome_phones"
MANAGED_ACCOUNT_ID = "ha"
AUTO_REGISTER_ACTION = "set_ha_display_name"


@dataclass(frozen=True, slots=True)
class ManagedPhone:
    """One physical phone owns one credential and one logical endpoint."""

    device_id: str
    origin_entry_id: str
    mac: str
    username: str
    password: str = field(repr=False)
    display_name: str = ""
    route_id: str = ""
    api_host: str = ""
    extension: str = ""
    sip_transport: str = "udp"

    @property
    def endpoint_id(self) -> str:
        return f"esphome:{self.device_id}"

    def account(self) -> SipAccount:
        return SipAccount(
            username=self.username, display_name=self.display_name or self.username,
            password=self.password, extension=self.extension, endpoint_id=self.endpoint_id,
        )

    def record(self) -> dict:
        return asdict(self)

    @classmethod
    def from_record(cls, value: Mapping) -> ManagedPhone:
        required = ("device_id", "origin_entry_id", "mac", "username", "password")
        if any(not isinstance(value.get(key), str) or not value[key] for key in required):
            raise ValueError("Incomplete managed phone ownership")
        username = normalize_username(value["username"])
        if len(username) > 64 or len(value["password"]) > 128:
            raise ValueError("Managed phone identity exceeds the firmware bounds")
        return cls(**{
            key: str(value.get(key, descriptor.default))
            for key, descriptor in cls.__dataclass_fields__.items()
        })


def managed_records(entry) -> dict[str, ManagedPhone]:
    """Read only valid records, retaining no additional account database."""
    records = {}
    for key, value in entry.data.get(CONF_MANAGED_PHONES, {}).items():
        if not isinstance(value, Mapping):
            continue
        try:
            phone = ManagedPhone.from_record(value)
        except (TypeError, ValueError):
            continue
        if key == phone.device_id:
            records[key] = phone
    return records


def active_managed_accounts(hass, entry) -> list[SipAccount]:
    """An origin removed while we were unloaded must never regain access."""
    return [
        phone.account() for phone in managed_records(entry).values()
        if (origin := hass.config_entries.async_get_entry(phone.origin_entry_id)) is not None
        and origin.domain == "esphome"
        and managed_phone_mode(hass, origin, phone.device_id, phone.mac) is not False
    ]


def managed_phone_mode(hass, origin, device_id: str, mac: str, *, require_inventory=False) -> bool | None:
    """Derive ownership from ESPHome's last complete, MAC-verified inventory.

    ESPHome retains this inventory on disconnect. Bootstrap of a replacement
    API owner keeps the provisioner's last verified decision. Neither case is
    a firmware change. There is no second persisted account database.
    """
    from .runtime_data import runtime_data

    def normalize_mac(value):
        return str(value).replace(":", "").replace("-", "").casefold()

    runtime = runtime_data(hass)
    modes = getattr(getattr(runtime, "phone_provisioning", None), "_firmware_modes", {})
    key = (getattr(origin, "entry_id", ""), device_id, normalize_mac(mac))
    previous = None if require_inventory else modes.get(key)
    native = getattr(origin, "runtime_data", None)
    complete = getattr(native, "first_connect_done", None)
    if complete is None or not complete.is_set():
        return previous
    info = getattr(native, "device_info", None)
    if info is None or not mac or normalize_mac(info.mac_address) != normalize_mac(mac):
        return previous
    services = getattr(native, "services", None)
    if not isinstance(services, Mapping):
        return previous
    names = {service.name for service in services.values()}
    mode = {AUTO_REGISTER_ACTION, "set_account"} <= names
    if not require_inventory or mode or key in modes:
        modes[key] = mode
    return mode
