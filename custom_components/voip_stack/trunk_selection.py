"""Resolve HA trunk selectors and call defaults without changing registration."""

from dataclasses import dataclass, field

from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr

from .config import normalized_trunk_config, trunk_config, trunk_enabled
from .const import DOMAIN
from .core.sip import SipUri
from .runtime_data import require_runtime_data, sip_trunk
from .store import config_entry
from .trunk_config import trunk_subentries
from .trunk_policy import trunk_available


@dataclass(frozen=True, slots=True)
class TrunkSelection:
    identity: str
    config: dict = field(repr=False)
    client: object = field(repr=False)

    @property
    def available(self) -> bool:
        return trunk_enabled(self.config) and trunk_available(self.client)

    def unavailable_error(self):
        """Explain a failed external route without blocking ordinary local calls."""
        from .service_errors import service_error

        if not self.identity and not self.config.get("trunk_server"):
            return service_error(
                "No outgoing SIP trunk selected. Choose a line in the phone options or set a default trunk.",
                "outgoing_trunk_not_selected",
            )
        return service_error("The selected SIP trunk is unavailable", "outgoing_trunk_unavailable")


def trunk_destination_uri(config, destination) -> SipUri:
    """Construct a logical trunk target independently from its transport hop."""
    return SipUri(str(destination), str(config.get("trunk_domain") or config.get("trunk_server") or ""),
                  int(config.get("trunk_port") or 5060),
                  params=(("transport", str(config.get("trunk_transport") or "udp").lower()),))


def resolve_trunk_selector(hass, value: str) -> str:
    """Accept a native Device selector, stable identity or unique configured name."""
    runtime = require_runtime_data(hass)
    value = str(value or "").strip()
    if not value:
        return ""
    if value in runtime.trunk_configs:
        return value
    device = dr.async_get(hass).async_get(value)
    if device is not None:
        for domain, identifier in device.identifiers:
            if domain == DOMAIN and identifier.startswith("trunk:") and identifier[6:] in runtime.trunk_configs:
                return identifier[6:]
    entry = config_entry(hass)
    matches = [str(item.data.get("trunk_id") or item.subentry_id)
               for item in trunk_subentries(entry) if item.title.casefold() == value.casefold()] if entry else []
    if len(matches) == 1:
        return matches[0]
    raise ServiceValidationError("Select an existing SIP trunk")


def select_call_trunk(hass, *, requested: str = "", endpoint_id: str = "", inherited: str = "") -> TrunkSelection:
    runtime = require_runtime_data(hass)
    endpoint = runtime.endpoints.get(endpoint_id) if endpoint_id else None
    preference = endpoint.policy.outbound_trunk_id if endpoint is not None else ""
    identity = resolve_trunk_selector(hass, requested) if requested else inherited or preference or runtime.default_trunk_id
    if runtime.trunk_configs:
        config = runtime.trunk_configs.get(identity, normalized_trunk_config({}))
        return TrunkSelection(identity, config, sip_trunk(hass, identity))
    if identity:
        return TrunkSelection(identity, normalized_trunk_config({}), None)
    return TrunkSelection("", trunk_config(hass), sip_trunk(hass))


def public_trunk_destination(hass, identity, destination):
    """Build the same public PBX address that any SIP phone can dial."""
    entry = config_entry(hass)
    record = next((item for item in trunk_subentries(entry)
                   if str(item.data.get("trunk_id") or item.subentry_id) == identity), None) if entry else None
    if record is None:
        raise ServiceValidationError("Select a configured SIP trunk")
    return f"trunk.{record.title}.{destination}"


def parse_public_trunk_destination(hass, destination):
    """Resolve an explicit trunk name without private headers or session state."""
    target = str(destination or "").strip()
    runtime = require_runtime_data(hass)
    if runtime.endpoints.by_username(target) is not None:
        return "", target
    entry = config_entry(hass)
    matches = []
    for record in trunk_subentries(entry) if entry else ():
        prefix = f"trunk.{record.title}."
        if target.casefold().startswith(prefix.casefold()) and target[len(prefix):]:
            matches.append((len(prefix), str(record.data.get("trunk_id") or record.subentry_id), target[len(prefix):]))
    if not matches:
        return "", target
    longest = max(item[0] for item in matches)
    selected = [item for item in matches if item[0] == longest]
    if len(selected) != 1:
        raise ServiceValidationError("The selected trunk name is ambiguous")
    return selected[0][1], selected[0][2]
