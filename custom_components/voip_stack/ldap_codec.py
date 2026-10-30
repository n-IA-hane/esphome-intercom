"""Bounded LDAPv3 wire codec using public pyasn1 and ldap3 schema types."""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from ldap3.protocol import rfc4511 as schema
from pyasn1.codec.ber.decoder import Decoder
from pyasn1.codec.ber.encoder import Encoder
from pyasn1.error import PyAsn1Error
from pyasn1.type.namedtype import NamedType, NamedTypes
from pyasn1.type.univ import Any

MAX_PDU_BYTES = 16384
MAX_FILTER_DEPTH = 16
MAX_FILTER_NODES = 64
MAX_ATTRIBUTES = 32
MAX_VALUE_BYTES = 4096
_ATTRIBUTE = re.compile(r"(?:[a-zA-Z][a-zA-Z0-9-]*|[0-9]+(?:\.[0-9]+)+)(?:;[a-zA-Z0-9-]+)*\Z")


class LdapProtocolError(ValueError):
    """Invalid or oversized LDAP frame, with no payload in diagnostics."""


@dataclass(frozen=True, slots=True)
class Filter:
    operation: str
    attribute: str = ""
    value: bytes = b""
    children: tuple[Filter, ...] = ()
    substrings: tuple[tuple[str, bytes], ...] = ()
    matching_rule: str = ""
    dn_attributes: bool = False


@dataclass(frozen=True, slots=True)
class Request:
    message_id: int
    operation: str
    version: int = 3
    bind_dn: str = ""
    password: bytes = field(default=b"", repr=False)
    authentication: str = "simple"
    base_dn: str = ""
    scope: int = 0
    size_limit: int = 0
    time_limit: int = 0
    types_only: bool = False
    filter: Filter | None = None
    attributes: tuple[str, ...] = ()
    critical_control: bool = False
    abandon_id: int = 0
    extended_name: str = ""


def _replace_type(container, name, replacement):
    return container.clone(componentType=NamedTypes(*[
        NamedType(item.name, replacement) if item.name == name else item
        for item in container.componentType.namedTypes
    ]))


def _schema():
    # ldap3's client schema initializes recursive choice instances before their
    # child types. Compose one flat level with public constructors instead of
    # changing library classes or expanding a recursive type tree. The visitor
    # below decodes every Any child and enforces the same per-request budget.
    leaves = [item for item in schema.Filter().componentType.namedTypes
              if item.name not in {"and", "or", "notFilter"}]
    filter_schema = schema.Filter(componentType=NamedTypes(
        NamedType("and", schema.And(componentType=Any())),
        NamedType("or", schema.Or(componentType=Any())),
        NamedType("notFilter", schema.Not(componentType=NamedTypes(NamedType("innerNotFilter", Any())))),
        *leaves,
    ))
    search = _replace_type(schema.SearchRequest(), "filter", filter_schema)
    protocol = _replace_type(schema.LDAPMessage()["protocolOp"], "searchRequest", search)
    return _replace_type(schema.LDAPMessage(), "protocolOp", protocol), filter_schema


_MESSAGE_SCHEMA, _FILTER_SCHEMA = _schema()
_DECODER, _ENCODER = Decoder(), Encoder()


def frame_size(prefix: bytes | bytearray) -> int | None:
    """Return the complete PDU size, or None while its header is incomplete."""
    if prefix and prefix[0] != 0x30:
        raise LdapProtocolError("LDAP message needs a sequence")
    if len(prefix) < 2:
        return None
    length = prefix[1]
    header = 2
    if length & 0x80:
        count = length & 0x7F
        if not count or count > 4:
            raise LdapProtocolError("Unsupported BER length")
        if len(prefix) < 2 + count:
            return None
        length = int.from_bytes(prefix[2:2 + count], "big")
        header += count
    total = header + length
    if total > MAX_PDU_BYTES:
        raise LdapProtocolError("LDAP message exceeds its byte budget")
    return total


def _ber_nodes(data: bytes):
    nodes = 0
    def scan(start, limit, depth):
        nonlocal nodes
        nodes += 1
        if nodes > 1024 or depth > 64 or start >= limit:
            raise LdapProtocolError("BER structure exceeds its budget")
        first = data[start]
        index = start + 1
        number = first & 31
        if number == 31:
            number = 0
            for _ in range(4):
                if index >= limit:
                    raise LdapProtocolError("Incomplete BER tag")
                part = data[index]
                index += 1
                number = (number << 7) | (part & 127)
                if not part & 128:
                    break
            else:
                raise LdapProtocolError("BER tag exceeds its budget")
        if index >= limit:
            raise LdapProtocolError("Incomplete BER length")
        length = data[index]
        index += 1
        if length & 128:
            count = length & 127
            if not count or count > 4 or index + count > limit:
                raise LdapProtocolError("Unsupported BER length")
            length = int.from_bytes(data[index:index + count], "big")
            index += count
        end = index + length
        if end > limit:
            raise LdapProtocolError("Incomplete BER value")
        if first & 0xC0 == 0 and number == 1 and (length != 1 or data[index] not in {0, 255}):
            raise LdapProtocolError("Invalid LDAP boolean")
        if first == 0 or (first & 0xC0 == 0 and number == 4 and first & 32):
            raise LdapProtocolError("LDAP requires primitive strings and definite BER")
        children = []
        if first & 32:
            offset = index
            while offset < end:
                child = scan(offset, end, depth + 1)
                children.append(child)
                offset = child[2]
        return (first, index, end, tuple(children), start)
    node = scan(0, len(data), 0)
    if node[2] != len(data):
        raise LdapProtocolError("Unexpected bytes after LDAP message")
    return node


def _text(value) -> str:
    try:
        raw = value.asOctets()
        if len(raw) > MAX_VALUE_BYTES:
            raise LdapProtocolError("LDAP text exceeds its byte budget")
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise LdapProtocolError("Invalid LDAP UTF-8") from None


def _attribute(value) -> str:
    text = _text(value)
    if len(text) > 128 or not _ATTRIBUTE.fullmatch(text):
        raise LdapProtocolError("Invalid LDAP attribute description")
    return text.casefold()


def _filter(data: bytes) -> Filter:
    count = 0
    def visit(raw, depth):
        nonlocal count
        count += 1
        if count > MAX_FILTER_NODES or depth > MAX_FILTER_DEPTH:
            raise LdapProtocolError("LDAP filter exceeds its budget")
        value, tail = _DECODER(raw, asn1Spec=_FILTER_SCHEMA)
        if tail:
            raise LdapProtocolError("Unexpected filter bytes")
        name = value.getName()
        component = value[name]
        if raw[0] & 0xC0 != 0x80 or bool(raw[0] & 32) != (name != "present"):
            raise LdapProtocolError("Invalid filter encoding")
        if name in {"and", "or"}:
            children = tuple(visit(item.asOctets(), depth + 1) for item in component)
            if not children:
                raise LdapProtocolError("Empty boolean filter")
            return Filter(name, children=children)
        if name == "notFilter":
            return Filter("not", children=(visit(component["innerNotFilter"].asOctets(), depth + 1),))
        if name == "present":
            return Filter(name, _attribute(component))
        if name == "substringFilter":
            parts = tuple((item.getName(), item.getComponent().asOctets()) for item in component["substrings"])
            kinds = [kind for kind, _ in parts]
            if (not parts or len(parts) > MAX_FILTER_NODES or kinds.count("initial") > 1
                    or kinds.count("final") > 1 or ("initial" in kinds and kinds[0] != "initial")
                    or ("final" in kinds and kinds[-1] != "final")
                    or any(len(part) > MAX_VALUE_BYTES for _, part in parts)):
                raise LdapProtocolError("Invalid substring filter")
            return Filter(name, _attribute(component["type"]), substrings=parts)
        if name == "extensibleMatch":
            attribute = _attribute(component["type"]) if component["type"].hasValue() else ""
            rule = _text(component["matchingRule"]) if component["matchingRule"].hasValue() else ""
            assertion = component["matchValue"].asOctets()
            if not attribute and not rule:
                raise LdapProtocolError("Extensible filter needs a type or rule")
            if len(assertion) > MAX_VALUE_BYTES:
                raise LdapProtocolError("Filter assertion exceeds its budget")
            return Filter(name, attribute, assertion, matching_rule=rule, dn_attributes=bool(component["dnAttributes"]))
        assertion = component["assertionValue"].asOctets()
        if len(assertion) > MAX_VALUE_BYTES:
            raise LdapProtocolError("Filter assertion exceeds its budget")
        return Filter(name, _attribute(component["attributeDesc"]), assertion)
    return visit(data, 0)


def decode_request(data: bytes) -> Request:
    try:
        if frame_size(data) != len(data):
            raise LdapProtocolError("Incomplete LDAP message")
        raw = _ber_nodes(data)
        message, tail = _DECODER(data, asn1Spec=_MESSAGE_SCHEMA)
        if tail or not 1 <= int(message["messageID"]) <= 2147483647:
            raise LdapProtocolError("Invalid LDAP message identity")
        operation = message["protocolOp"].getName()
        body = message["protocolOp"][operation]
        if operation not in {"bindRequest", "searchRequest", "abandonRequest", "unbindRequest", "extendedReq",
                             "modifyRequest", "addRequest", "delRequest", "modDNRequest", "compareRequest"}:
            raise LdapProtocolError("Expected an LDAP request")
        common = {"message_id": int(message["messageID"]), "operation": operation,
                  "critical_control": any(bool(control["criticality"]) for control in message["controls"])
                  if message["controls"].hasValue() else False}
        if operation == "bindRequest":
            method = body["authentication"].getName()
            if method == "simple" and raw[3][1][3][2][0] & 32:
                raise LdapProtocolError("Simple password must be primitive")
            password = body["authentication"]["simple"].asOctets() if method == "simple" else b""
            if len(password) > MAX_VALUE_BYTES:
                raise LdapProtocolError("Password exceeds its byte budget")
            return Request(**common, version=int(body["version"]), bind_dn=_text(body["name"]),
                           password=password, authentication=method)
        if operation == "searchRequest":
            if int(body["scope"]) not in {0, 1, 2} or int(body["derefAliases"]) not in {0, 1, 2, 3}:
                raise LdapProtocolError("Invalid search scope")
            attributes = tuple(_text(value).casefold() for value in body["attributes"])
            if len(attributes) > MAX_ATTRIBUTES or any(value not in {"*", "+", "1.1"}
                    and (len(value) > 128 or not _ATTRIBUTE.fullmatch(value)) for value in attributes):
                raise LdapProtocolError("Invalid attribute selection")
            wire_filter = raw[3][1][3][6]
            return Request(**common, base_dn=_text(body["baseObject"]), scope=int(body["scope"]),
                           size_limit=int(body["sizeLimit"]), time_limit=int(body["timeLimit"]),
                           types_only=bool(body["typesOnly"]), attributes=attributes,
                           filter=_filter(data[wire_filter[4]:wire_filter[2]]))
        if operation == "abandonRequest":
            return Request(**common, abandon_id=int(body))
        if operation == "extendedReq":
            return Request(**common, extended_name=_text(body["requestName"]))
        return Request(**common)
    except (PyAsn1Error, IndexError, KeyError, ValueError, TypeError) as error:
        if isinstance(error, LdapProtocolError):
            raise
        raise LdapProtocolError("Malformed LDAP message") from None


def _message(identity, operation):
    result = _MESSAGE_SCHEMA.clone()
    result["messageID"] = identity
    result["protocolOp"].setComponentByName(operation)
    return result, result["protocolOp"][operation]


def encode_result(identity: int, operation: str, code: int = 0, *, diagnostic: str = "", notice: bool = False) -> bytes:
    response, body = _message(identity, operation)
    body["resultCode"] = code
    body["matchedDN"] = ""
    body["diagnosticMessage"] = diagnostic
    if notice:
        body["responseName"] = "1.3.6.1.4.1.1466.20036"
    return _ENCODER(response, defMode=True, maxChunkSize=0)


def encode_entry(identity: int, dn: str, attributes: dict[str, tuple[bytes, ...]], *, types_only: bool = False) -> bytes:
    response, body = _message(identity, "searchResEntry")
    body["object"] = dn
    for index, (name, values) in enumerate(attributes.items()):
        attribute = body["attributes"][index]
        attribute["type"] = name
        attribute["vals"].extend(() if types_only else values)
    encoded = _ENCODER(response, defMode=True, maxChunkSize=0)
    if len(encoded) > MAX_PDU_BYTES:
        raise LdapProtocolError("LDAP result entry exceeds its byte budget")
    return encoded
