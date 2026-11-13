"""Bounded BroadSoft DND XML, using the existing authenticated event dialog."""

from xml.etree import ElementTree as ET

from .core import sip

CONTENT_TYPE = "application/x-as-feature-event+xml"
NAMESPACE = "http://www.ecma-international.org/standards/ecma-323/csta/ed3"
_NAMESPACES = {NAMESPACE, NAMESPACE[:-1] + "5"}


def requested_dnd(body, username):
    """Read an explicit ON/OFF, never a toggle or an inferred busy status."""
    if len(body) > 4096 or b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise ValueError("invalid feature document")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as error:
        raise ValueError("invalid feature document") from error
    namespace, _, name = root.tag.removeprefix("{").partition("}")
    if namespace not in _NAMESPACES or name != "SetDoNotDisturb":
        raise ValueError("unsupported feature")
    if len(root.findall(f"{{{namespace}}}doNotDisturbOn")) != 1:
        raise ValueError("missing feature value")
    value = root.findtext(f"{{{namespace}}}doNotDisturbOn", "").strip()
    if value not in {"true", "false", "1", "0"}:
        raise ValueError("invalid feature value")
    devices = root.findall(f"{{{namespace}}}device")
    if len(devices) > 1:
        raise ValueError("ambiguous feature device")
    if devices:
        device = devices[0].findtext(f"{{{namespace}}}deviceIdentifier") or devices[0].text or ""
        try:
            user = sip.parse_sip_uri(device.strip()).user
        except (ValueError, sip.SipError) as error:
            raise ValueError("invalid feature device") from error
        if user.casefold() != username.casefold():
            raise PermissionError("foreign feature device")
    return value in {"true", "1"}


def dnd_document(uri, enabled):
    root = ET.Element("DoNotDisturbEvent", {"xmlns": NAMESPACE})
    ET.SubElement(root, "device").text = uri
    ET.SubElement(root, "doNotDisturbOn").text = "true" if enabled else "false"
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
