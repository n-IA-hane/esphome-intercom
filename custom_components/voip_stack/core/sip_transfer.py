"""RFC 3515 REFER and RFC 3891 Replaces value objects."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, unquote, urlencode

from . import sip


@dataclass(frozen=True, slots=True)
class SipReplaces:
    """One dialog identifier carried by a Replaces header."""

    call_id: str
    to_tag: str
    from_tag: str
    early_only: bool = False

    def __str__(self) -> str:
        parts = [self.call_id, f"to-tag={self.to_tag}", f"from-tag={self.from_tag}"]
        if self.early_only:
            parts.append("early-only")
        return ";".join(parts)


@dataclass(frozen=True, slots=True)
class SipReferTarget:
    """Normalized target of one REFER request."""

    uri: str
    replaces: SipReplaces | None = None

    def as_header(self) -> str:
        query = ""
        if self.replaces is not None:
            query = "?" + urlencode(
                {"Replaces": str(self.replaces)},
                quote_via=quote,
                safe="",
            )
        return f"<{self.uri}{query}>"


def parse_replaces(value: str) -> SipReplaces:
    """Parse a Replaces value with mandatory dialog tags."""

    parts = [part.strip() for part in unquote(value or "").split(";")]
    call_id = parts[0] if parts else ""
    params: dict[str, str] = {}
    flags: set[str] = set()
    for part in parts[1:]:
        if not part:
            continue
        if "=" in part:
            key, item = part.split("=", 1)
            params[key.strip().lower()] = item.strip()
        else:
            flags.add(part.lower())
    if not call_id or not params.get("to-tag") or not params.get("from-tag"):
        raise sip.SipError("Replaces requires call-id, to-tag and from-tag")
    return SipReplaces(
        call_id=call_id,
        to_tag=params["to-tag"],
        from_tag=params["from-tag"],
        early_only="early-only" in flags,
    )


def parse_refer_to(value: str) -> SipReferTarget:
    """Parse one Refer-To name-address and its optional Replaces header."""

    raw = str(value or "").strip()
    if raw.startswith("<") and ">" in raw:
        raw = raw[1 : raw.index(">")].strip()
    uri_text, separator, query = raw.partition("?")
    uri = str(sip.parse_sip_uri(uri_text))
    replaces: SipReplaces | None = None
    if separator:
        for key, item in parse_qsl(query, keep_blank_values=True):
            if key.casefold() == "replaces":
                if replaces is not None:
                    raise sip.SipError("Refer-To contains duplicate Replaces")
                replaces = parse_replaces(item)
    return SipReferTarget(uri=uri, replaces=replaces)


def parse_sipfrag_status(body: bytes | str) -> int:
    """Return the status code from a message/sipfrag status line."""

    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    first_line = text.replace("\r\n", "\n").split("\n", 1)[0].strip()
    parts = first_line.split(None, 2)
    if len(parts) < 2 or parts[0].upper() != "SIP/2.0":
        raise sip.SipError("invalid message/sipfrag status line")
    try:
        status = int(parts[1])
    except ValueError as err:
        raise sip.SipError("invalid message/sipfrag status code") from err
    if not 100 <= status <= 699:
        raise sip.SipError("message/sipfrag status is outside SIP range")
    return status
