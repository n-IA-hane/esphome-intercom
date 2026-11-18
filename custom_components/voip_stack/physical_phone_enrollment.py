"""Time-limited PnP bootstrap on the administrator's directly attached phone LAN."""

import hashlib
import ipaddress
import time

from .core import sip
from .physical_phone import matches_announced_model


def _model_matches(profile_id, vendor, model):
    brand, _, name = profile_id.partition("/")
    return matches_announced_model(brand, name, vendor, model)


def phone_dhcp_host(hass, phone):
    """Return a unique, current HA address for the selected handset and model."""
    if "dhcp" not in getattr(getattr(hass, "config", None), "components", ()):
        return ""
    from homeassistant.components.dhcp import async_discovered_service_info
    from .physical_phone_discovery import dhcp_identity

    def mac_of(info):
        value = getattr(info, "macaddress", "")
        return value.lower().replace(":", "").replace("-", "") if isinstance(value, str) else ""

    records = async_discovered_service_info(hass)
    host = ""
    for info in records:
        if mac_of(info) != phone.get("mac"):
            continue
        try:
            _, address, vendor, model = dhcp_identity(info)
        except ValueError:
            return ""
        if (host and host != address) or not _model_matches(phone.get("profile_id", ""), vendor, model):
            return ""
        host = address
    if host and any(getattr(other, "ip", None) == host and mac_of(other) != phone["mac"] for other in records):
        return ""
    return host


def phone_enrollment_identity(phone):
    """Bind a provisioning exchange to the same physical record and credential."""
    values = (phone["phone_id"], phone["mac"], phone["profile_id"], phone["provisioning_token"])
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


def arm_dhcp_enrollment(hass, phone):
    """Called only on confirmation of discovery or an explicit admin retry."""
    if phone.get("enabled", True) and (host := phone_dhcp_host(hass, phone)):
        phone["pnp_enrollment"] = {"issued": time.time(), "host": host, "identity": phone_enrollment_identity(phone)}


def enrollment_matches(hass, phone, source, vendor, model):
    grant = phone.get("pnp_enrollment") or {}
    return (isinstance(grant, dict) and grant.get("identity") == phone_enrollment_identity(phone)
            and isinstance(grant.get("issued"), (int, float)) and isinstance(grant.get("host"), str)
            and 0 <= time.time() - grant["issued"] < 3600
            and _model_matches(phone["profile_id"], vendor, model)
            and phone_dhcp_host(hass, phone) == source)


async def async_same_phone_lan(hass, granted_host, source):
    """The address can change within the on-link network seen at enrollment."""
    from homeassistant.components.network import async_get_adapters

    addresses = (ipaddress.IPv4Address(granted_host), ipaddress.IPv4Address(source))
    for adapter in await async_get_adapters(hass):
        if adapter["enabled"]:
            for assignment in adapter["ipv4"]:
                interface = ipaddress.ip_interface(f"{assignment['address']}/{assignment['network_prefix']}")
                if all(ip in interface.network and ip not in (interface.ip, interface.network.broadcast_address,
                       interface.network.network_address) for ip in addresses):
                    return True
    return False


def direct_pnp_contact(request, addr, transport):
    """A provisioning NOTIFY may return only to this UDP handset's socket."""
    if transport != "UDP" or request.header_values("Record-Route"):
        return False
    try:
        contact = sip.parse_sip_uri(sip.name_addr_uri(request.header("Contact")))
    except (ValueError, sip.SipError):
        return False
    return (contact.scheme == "sip" and (contact.host, sip.sip_default_port(contact)) == addr
            and all(name.casefold() == "transport" and str(value).casefold() == "udp"
                    for name, value in contact.params))
