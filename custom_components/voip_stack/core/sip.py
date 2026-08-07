"""Strict SIP/2.0 helpers for the VoIP Stack profile.

This module intentionally implements a small standards-aligned subset of
SIP rather than a proprietary replacement. Unsupported methods/features are
handled by policy at the call layer, but the messages built and parsed here are
ordinary SIP/2.0 messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import re
from secrets import token_hex
from typing import Iterable
from urllib.parse import quote, unquote


CRLF = "\r\n"
SIP_VERSION = "SIP/2.0"
MAX_SIP_MESSAGE_BYTES = 8192
MAX_SIP_BODY_BYTES = 4096
SUPPORTED_METHODS = frozenset(
    {
        "INVITE",
        "ACK",
        "BYE",
        "CANCEL",
        "INFO",
        "OPTIONS",
        "PRACK",
        "REFER",
        "REGISTER",
        "NOTIFY",
        "UPDATE",
    }
)
SUPPORTED_OPTION_TAGS = frozenset({"100rel", "from-change", "replaces", "timer"})
KNOWN_UNSUPPORTED_METHODS = frozenset(
    {
        "MESSAGE",
        "PUBLISH",
        "SUBSCRIBE",
    }
)
_TOKEN_SEPARATORS = set("()<>@,;:\\\"/[]?={} \t")
_QUOTED_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')
_TAG_RE = re.compile(r"(?:^|;)tag=([^;>\s]+)", re.IGNORECASE)
_COMPACT_HEADER_NAMES = {
    "call-id": "i",
    "contact": "m",
    "content-encoding": "e",
    "content-length": "l",
    "content-type": "c",
    "from": "f",
    "subject": "s",
    "supported": "k",
    "to": "t",
    "via": "v",
}
_CANONICAL_HEADER_NAMES = {compact: full for full, compact in _COMPACT_HEADER_NAMES.items()}
_SINGLETON_HEADERS = frozenset({"call-id", "cseq", "from", "to"})


class SipError(ValueError):
    """Malformed or unsupported SIP message."""


class SipSessionIntervalTooSmall(SipError):
    def __init__(self, minimum: int) -> None:
        self.minimum = int(minimum)
        super().__init__(f"session interval is below {self.minimum} seconds")


def normalize_sip_host(value: str) -> str:
    """Return a comparison-safe SIP host without resolving DNS names."""

    host = str(value or "").strip().lower()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        return host.rstrip(".")


def sip_hosts_equal(left: str, right: str) -> bool:
    """Compare SIP hosts without conflating different signaling sockets."""

    left_host = normalize_sip_host(left)
    right_host = normalize_sip_host(right)
    return bool(left_host and right_host and left_host == right_host)


def sip_endpoints_equal(
    left_host: str,
    left_port: int | None,
    right_host: str,
    right_port: int | None,
    *,
    default_port: int = 5060,
) -> bool:
    """Return whether two SIP contacts identify the same signaling socket."""

    return sip_hosts_equal(left_host, right_host) and int(left_port or default_port) == int(
        right_port or default_port
    )


def sip_uri_targets_listener(
    uri: "SipUri | None",
    *,
    listener_hosts: Iterable[str],
    listener_port: int,
    default_port: int = 5060,
) -> bool:
    """Return whether a SIP URI points to this exact local listener."""

    if uri is None or int(uri.port or default_port) != int(listener_port):
        return False
    return any(sip_hosts_equal(uri.host, host) for host in listener_hosts)


def is_sip_token(value: str) -> bool:
    """Return true when *value* is a syntactically valid SIP token."""
    return bool(value) and all(0x21 <= ord(ch) <= 0x7E and ch not in _TOKEN_SEPARATORS for ch in value)


def extract_tag(header: str) -> str:
    clean = _QUOTED_STRING_RE.sub('""', header or "")
    match = _TAG_RE.search(clean)
    return match.group(1) if match else ""


def mark_sip_event(target: object, event: str, status: int = 0, reason: str = "") -> None:
    target.last_sip_event = event
    if status:
        target.last_sip_status_code = int(status)
        target.last_sip_reason = reason or ""


@dataclass(frozen=True, slots=True)
class SipUri:
    user: str
    host: str
    port: int | None = None
    params: tuple[tuple[str, str | None], ...] = ()

    def __str__(self) -> str:
        user = self.user.strip()
        host = self.host.strip()
        if not host:
            raise SipError("SIP URI requires non-empty host")
        if any(ord(ch) < 0x21 or ch in '<>"/@,;?\\' for ch in host):
            raise SipError("SIP URI contains an invalid host")
        safe_user = quote(user, safe="!$&'()*+,-./:;=?_~")
        uri = f"sip:{safe_user}@{host}" if safe_user else f"sip:{host}"
        if self.port is not None:
            if not 1 <= int(self.port) <= 65535:
                raise SipError(f"SIP URI port out of range: {self.port}")
            uri += f":{int(self.port)}"
        for key, value in self.params:
            if not key:
                continue
            if not is_sip_token(key) or (value is not None and any(ord(ch) < 0x20 for ch in value)):
                raise SipError("SIP URI contains an invalid parameter")
            safe_value = None if value is None else quote(value, safe="!$&'()*+,-./:[]_~%")
            uri += f";{key}" if safe_value is None else f";{key}={safe_value}"
        return uri


@dataclass(frozen=True, slots=True)
class SipMessage:
    method: str | None = None
    uri: str = ""
    status_code: int | None = None
    reason: str = ""
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""

    @property
    def is_request(self) -> bool:
        return self.method is not None

    @property
    def is_response(self) -> bool:
        return self.status_code is not None

    def header_values(self, name: str) -> list[str]:
        wanted = name.lower()
        canonical = _CANONICAL_HEADER_NAMES.get(wanted, wanted)
        compact = _COMPACT_HEADER_NAMES.get(canonical)
        return [
            value
            for key, value in self.headers
            if key.lower() == canonical or (compact is not None and key.lower() == compact)
        ]

    def header(self, name: str, default: str = "") -> str:
        values = self.header_values(name)
        return values[-1] if values else default


@dataclass(slots=True)
class SipDialogIds:
    call_id: str
    local_tag: str
    remote_tag: str = ""
    cseq: int = 1
    branch: str = field(default_factory=lambda: make_branch())


@dataclass(frozen=True, slots=True)
class SipVia:
    transport: str
    host: str
    port: int
    branch: str = ""
    rport: int | None = None
    received: str = ""
    params: tuple[tuple[str, str | None], ...] = ()


@dataclass(frozen=True, slots=True)
class SipCSeq:
    number: int
    method: str


@dataclass(frozen=True, slots=True)
class SipSessionExpires:
    seconds: int
    refresher: str = ""


@dataclass(slots=True)
class SipSessionTimer:
    """One dialog-owned RFC 4028 timer state."""

    interval: int = 0
    local_refresher: bool = False
    deadline: float = 0.0
    refresh_at: float = 0.0

    def configure(
        self,
        timer: SipSessionExpires | None,
        *,
        local_role: str,
        now: float = 0.0,
    ) -> None:
        if timer is None:
            self.interval = 0
            self.local_refresher = False
            self.deadline = 0.0
            self.refresh_at = 0.0
            return
        self.interval = timer.seconds
        self.local_refresher = timer.refresher == local_role
        self.deadline = now + timer.seconds if now else 0.0
        self.refresh_at = (
            now + timer.seconds / 2 if now and self.local_refresher else 0.0
        )

    @property
    def expiration_notice_at(self) -> float:
        if not self.deadline:
            return 0.0
        return self.deadline - min(32.0, self.interval / 3)


@dataclass(frozen=True, slots=True)
class SipRAck:
    response_number: int
    cseq_number: int
    method: str


@dataclass(frozen=True, slots=True)
class SipDialogRoute:
    """Routing contract for one request inside an established dialog."""

    request_uri: str
    route_headers: tuple[str, ...]
    next_hop_uri: str


def make_call_id(prefix: str = "voip") -> str:
    return f"{prefix}-{token_hex(12)}"


def make_tag() -> str:
    return token_hex(8)


def make_branch() -> str:
    return "z9hG4bK" + token_hex(10)


def parse_sip_uri(value: str) -> SipUri:
    raw = value.strip()
    if any(ch in "\r\n" for ch in raw):
        raise SipError("SIP URI contains a line break")
    left = raw.find("<")
    right = raw.find(">", left + 1) if left >= 0 else -1
    if left >= 0 and right > left + 1:
        raw = raw[left + 1:right].strip()
    if not raw.lower().startswith("sip:"):
        raise SipError(f"not a sip URI: {value!r}")
    rest = raw[4:]
    if "@" in rest:
        user, host_params = rest.split("@", 1)
    else:
        user, host_params = "", rest
    params_raw: list[str] = []
    if ";" in host_params:
        host_part, params_part = host_params.split(";", 1)
        params_raw = [p for p in params_part.split(";") if p]
    else:
        host_part = host_params
    port: int | None = None
    host = host_part
    if host_part.count(":") == 1 and not host_part.startswith("["):
        host, port_raw = host_part.rsplit(":", 1)
        if port_raw:
            port = int(port_raw)
    params: list[tuple[str, str | None]] = []
    for param in params_raw:
        if "=" in param:
            key, val = param.split("=", 1)
            params.append((key.strip(), val.strip()))
        else:
            params.append((param.strip(), None))
    try:
        user = unquote(user.strip(), errors="strict")
    except UnicodeDecodeError as err:
        raise SipError("SIP URI user has invalid percent encoding") from err
    uri = SipUri(user=user, host=host.strip(), port=port, params=tuple(params))
    str(uri)
    return uri


def contact_target_uri(message: SipMessage) -> str:
    """Return the single Contact URI carried by one SIP message.

    Target-refresh requests are allowed to omit Contact for compatibility with
    older intercom firmware.  When Contact is present, however, accepting a
    list, wildcard, or malformed value would make the subsequent dialog target
    ambiguous.  Commas inside a quoted display name or URI brackets are not
    contact separators.
    """

    values = message.header_values("Contact")
    if not values:
        return ""
    if len(values) != 1:
        raise SipError("SIP dialog Contact must contain exactly one value")
    value = values[0].strip()
    if not value or value == "*":
        raise SipError("SIP dialog Contact must contain one SIP URI")

    contacts = _split_comma_header_values(value)
    if len(contacts) != 1:
        raise SipError("SIP dialog Contact must not contain a contact list")
    return str(parse_sip_uri(contacts[0]))


def _split_comma_header_values(value: str) -> tuple[str, ...]:
    """Split a SIP list header without breaking quoted names or name-addrs."""

    if any(char in "\r\n" for char in value):
        raise SipError("SIP list header contains a line break")
    values: list[str] = []
    start = 0
    quoted = False
    escaped = False
    angle_depth = 0
    for index, char in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quoted and char == "\\":
            escaped = True
            continue
        if char == '"':
            quoted = not quoted
            continue
        if quoted:
            continue
        if char == "<":
            angle_depth += 1
            if angle_depth > 1:
                raise SipError("SIP list header has nested name-address brackets")
        elif char == ">":
            if angle_depth == 0:
                raise SipError("SIP list header has an unmatched closing bracket")
            angle_depth -= 1
        elif char == "," and angle_depth == 0:
            item = value[start:index].strip()
            if not item:
                raise SipError("SIP list header contains an empty value")
            values.append(item)
            start = index + 1
    if quoted or escaped or angle_depth:
        raise SipError("SIP list header is malformed")
    item = value[start:].strip()
    if not item:
        raise SipError("SIP list header contains an empty value")
    values.append(item)
    return tuple(values)


def _name_addr_identity_parts(value: str) -> tuple[str, str]:
    """Return the display name and SIP user from one standard name-address."""

    raw = str(value or "").strip()
    if not raw:
        return "", ""
    try:
        item = _split_comma_header_values(raw)[0]
    except SipError:
        return "", ""
    display = ""
    uri_text = item
    if "<" in item and ">" in item:
        left = item.index("<")
        right = item.index(">", left + 1)
        display = item[:left].strip()
        uri_text = item[left + 1 : right].strip()
    if display:
        if len(display) >= 2 and display[0] == display[-1] == '"':
            quoted = display[1:-1]
            out: list[str] = []
            escaped = False
            for char in quoted:
                if escaped:
                    out.append(char)
                    escaped = False
                elif char == "\\":
                    escaped = True
                else:
                    out.append(char)
            if escaped:
                return "", ""
            display = "".join(out).strip()
    try:
        user = parse_sip_uri(uri_text).user.strip()
    except (TypeError, ValueError, SipError):
        user = ""
    return display, user


def name_addr_display_name(value: str) -> str:
    """Return only the optional display name from a SIP name-address."""

    return _name_addr_identity_parts(value)[0]


def name_addr_identity(value: str) -> str:
    """Return the display name or SIP user from one standard name-address."""

    display, user = _name_addr_identity_parts(value)
    return display or user


def option_tags(message: SipMessage, header_name: str = "Supported") -> frozenset[str]:
    """Return normalized SIP option tags from every occurrence of one header."""

    return frozenset(
        token.strip().lower()
        for value in message.header_values(header_name)
        for token in value.split(",")
        if token.strip()
    )


def supports_option(message: SipMessage, option: str) -> bool:
    """Return whether a SIP message advertises one option tag."""

    return str(option or "").strip().lower() in option_tags(message)


def unsupported_required_options(message: SipMessage) -> tuple[str, ...]:
    """Return required option tags outside the implemented SIP profile."""

    return tuple(sorted(option_tags(message, "Require") - SUPPORTED_OPTION_TAGS))


def format_name_addr(uri: str | SipUri, display_name: str = "") -> str:
    """Render one standards-compliant SIP name-address."""

    uri_text = str(parse_sip_uri(str(uri)))
    display = str(display_name or "").strip()
    if not display:
        return f"<{uri_text}>"
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in display):
        raise SipError("SIP display name contains control characters")
    escaped = display.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}" <{uri_text}>'


def record_route_set(
    message: SipMessage,
    *,
    reverse: bool = False,
) -> tuple[str, ...]:
    """Return a validated route set from all Record-Route field values.

    A UAS retains the request order.  A UAC reverses the order copied into the
    response, as required by RFC 3261.  Original name-address rendering is
    retained so extension parameters survive subsequent ``Route`` fields.
    """

    routes: list[str] = []
    for field_value in message.header_values("Record-Route"):
        for route in _split_comma_header_values(field_value):
            parse_sip_uri(route)
            routes.append(route)
    if reverse:
        routes.reverse()
    return tuple(routes)


def dialog_request_routing(
    remote_target_uri: str,
    route_set: Iterable[str] = (),
) -> SipDialogRoute:
    """Apply RFC 3261 loose/strict routing to an in-dialog request."""

    remote_target = str(parse_sip_uri(remote_target_uri))
    routes = tuple(str(value).strip() for value in route_set)
    if not routes:
        return SipDialogRoute(remote_target, (), remote_target)

    parsed_routes = tuple(parse_sip_uri(value) for value in routes)
    first_uri = str(parsed_routes[0])
    first_is_loose_router = any(
        key.strip().lower() == "lr" for key, _value in parsed_routes[0].params
    )
    if first_is_loose_router:
        return SipDialogRoute(remote_target, routes, first_uri)

    # A strict router occupies the Request-URI. The remote target is appended
    # to Route so the final strict hop restores it before reaching the peer.
    return SipDialogRoute(
        first_uri,
        (*routes[1:], f"<{remote_target}>"),
        first_uri,
    )


def _split_semicolon_params(value: str) -> tuple[str, tuple[tuple[str, str | None], ...]]:
    parts = [part.strip() for part in value.split(";")]
    head = parts[0] if parts else ""
    params: list[tuple[str, str | None]] = []
    for part in parts[1:]:
        if not part:
            continue
        if "=" in part:
            key, val = part.split("=", 1)
            params.append((key.strip().lower(), val.strip()))
        else:
            params.append((part.strip().lower(), None))
    return head, tuple(params)


def parse_via(value: str) -> SipVia:
    head, params = _split_semicolon_params(value.strip())
    bits = head.split()
    if len(bits) != 2 or not bits[0].upper().startswith("SIP/2.0/"):
        raise SipError(f"bad Via header: {value!r}")
    transport = bits[0].rsplit("/", 1)[1].upper()
    if transport not in {"UDP", "TCP"}:
        raise SipError(f"unsupported Via transport {transport!r}")
    sent_by = bits[1]
    host = sent_by
    port = 5060
    if sent_by.count(":") == 1 and not sent_by.startswith("["):
        host, raw_port = sent_by.rsplit(":", 1)
        port = int(raw_port)
    if not host or not 1 <= port <= 65535:
        raise SipError(f"bad Via sent-by: {sent_by!r}")
    param_map = {key: val for key, val in params}
    rport_raw = param_map.get("rport")
    rport = None
    if rport_raw not in (None, ""):
        rport = int(rport_raw)
        if not 1 <= rport <= 65535:
            raise SipError(f"bad Via rport: {rport_raw!r}")
    return SipVia(
        transport=transport,
        host=host,
        port=port,
        branch=param_map.get("branch") or "",
        rport=rport,
        received=param_map.get("received") or "",
        params=params,
    )


def parse_cseq(value: str) -> SipCSeq:
    parts = (value or "").strip().split()
    if len(parts) != 2:
        raise SipError(f"bad CSeq header: {value!r}")
    number = int(parts[0])
    method = parts[1].upper()
    if not 0 <= number <= 0x7FFFFFFF:
        raise SipError(f"bad CSeq number: {value!r}")
    if method not in SUPPORTED_METHODS:
        raise SipError(f"unsupported CSeq method {method}")
    return SipCSeq(number=number, method=method)


def parse_session_expires(value: str) -> SipSessionExpires:
    """Parse RFC 4028 Session-Expires or Min-SE syntax strictly."""

    head, params = _split_semicolon_params(str(value or "").strip())
    try:
        seconds = int(head)
    except ValueError as err:
        raise SipError(f"bad session interval: {value!r}") from err
    if not 1 <= seconds <= 0x7FFFFFFF:
        raise SipError(f"bad session interval: {value!r}")
    refresher = ""
    for key, raw in params:
        if key != "refresher":
            continue
        candidate = str(raw or "").lower()
        if candidate not in {"uac", "uas"} or refresher:
            raise SipError(f"bad refresher parameter: {value!r}")
        refresher = candidate
    return SipSessionExpires(seconds=seconds, refresher=refresher)


def parse_rack(value: str) -> SipRAck:
    """Parse the RFC 3262 response number, CSeq and method tuple."""

    parts = str(value or "").strip().split()
    if len(parts) != 3:
        raise SipError(f"bad RAck header: {value!r}")
    try:
        response_number, cseq_number = (int(parts[0]), int(parts[1]))
    except ValueError as err:
        raise SipError(f"bad RAck header: {value!r}") from err
    method = parts[2]
    if (
        not 1 <= response_number <= 0xFFFFFFFF
        or not 0 <= cseq_number <= 0x7FFFFFFF
        or not is_sip_token(method)
    ):
        raise SipError(f"bad RAck header: {value!r}")
    return SipRAck(response_number, cseq_number, method)


def response_matches_dialog_transaction(
    response: SipMessage,
    ids: SipDialogIds,
    method: str,
) -> bool:
    """Match one response to an exact in-dialog client transaction."""

    if not response.is_response:
        return False
    try:
        cseq = parse_cseq(response.header("CSeq"))
        vias = response.header_values("Via")
        branch = parse_via(vias[0] if vias else "").branch
    except (TypeError, ValueError, SipError):
        return False
    return (
        cseq == SipCSeq(ids.cseq, method.upper())
        and branch == ids.branch
        and response.header("Call-ID") == ids.call_id
        and extract_tag(response.header("From")) == ids.local_tag
        and extract_tag(response.header("To")) == ids.remote_tag
    )


def negotiate_uas_session_timer(
    request: SipMessage,
    *,
    local_minimum: int = 90,
) -> SipSessionExpires | None:
    """Validate an RFC 4028 request and select the response refresher."""

    minimum = max(90, int(local_minimum))
    if raw_minimum := request.header("Min-SE"):
        requested_minimum = parse_session_expires(raw_minimum)
        if requested_minimum.refresher:
            raise SipError("Min-SE cannot select a refresher")
        minimum = max(minimum, requested_minimum.seconds)
    raw_timer = request.header("Session-Expires")
    if not raw_timer:
        return None
    timer = parse_session_expires(raw_timer)
    supports_timer = supports_option(request, "timer")
    if timer.refresher and not supports_timer:
        raise SipError("refresher selection requires timer support")
    if timer.seconds < minimum:
        raise SipSessionIntervalTooSmall(minimum)
    return SipSessionExpires(
        timer.seconds,
        timer.refresher or ("uac" if supports_timer else "uas"),
    )


def session_timer_response_headers(
    request: SipMessage,
    *,
    local_minimum: int = 90,
) -> tuple[tuple[str, str], ...]:
    timer = negotiate_uas_session_timer(
        request,
        local_minimum=local_minimum,
    )
    if timer is None:
        return ()
    headers = [("Session-Expires", f"{timer.seconds};refresher={timer.refresher}")]
    if timer.refresher == "uac" or supports_option(request, "timer"):
        headers.insert(0, ("Require", "timer"))
    return tuple(headers)


def sip_failure_reason(status_code: int) -> str:
    code = int(status_code)
    if code == 401:
        return "auth_required_unsupported"
    if code == 407:
        return "proxy_auth_required_unsupported"
    if code == 486:
        return "busy"
    if code == 487:
        return "cancelled"
    if code == 488:
        return "media_incompatible"
    if code == 603:
        return "declined"
    return f"sip_{code}"


def _split_header_body(data: bytes) -> tuple[str, bytes]:
    if len(data) > MAX_SIP_MESSAGE_BYTES:
        raise SipError("SIP message too large")
    marker = b"\r\n\r\n"
    split = data.find(marker)
    if split < 0:
        raise SipError("SIP message missing CRLF CRLF separator")
    try:
        head = data[:split].decode("utf-8", errors="strict")
    except UnicodeDecodeError as err:
        raise SipError("SIP header is not strict UTF-8") from err
    return head, data[split + len(marker):]


def parse_message(data: bytes) -> SipMessage:
    head, body_tail = _split_header_body(data)
    lines = head.split(CRLF)
    if not lines or not lines[0]:
        raise SipError("empty SIP start line")
    unfolded: list[str] = []
    for line in lines[1:]:
        if not line:
            continue
        if line.startswith((" ", "\t")):
            if not unfolded:
                raise SipError("SIP header continuation has no preceding header")
            unfolded[-1] += " " + line.lstrip(" \t")
            continue
        unfolded.append(line)

    headers: list[tuple[str, str]] = []
    for line in unfolded:
        if ":" not in line:
            raise SipError(f"malformed SIP header: {line!r}")
        key, value = line.split(":", 1)
        key = key.strip()
        if not is_sip_token(key):
            raise SipError("invalid SIP header name")
        value = value.strip()
        if any(ord(ch) < 0x20 and ch != "\t" for ch in value):
            raise SipError("invalid control character in SIP header")
        headers.append((key, value))

    header_counts: dict[str, int] = {}
    for key, _value in headers:
        canonical = _CANONICAL_HEADER_NAMES.get(key.lower(), key.lower())
        header_counts[canonical] = header_counts.get(canonical, 0) + 1
    if any(header_counts.get(name, 0) > 1 for name in _SINGLETON_HEADERS):
        raise SipError("ambiguous duplicate SIP dialog header")

    content_lengths = [v for k, v in headers if k.lower() in {"content-length", "l"}]
    if len(content_lengths) > 1:
        raise SipError("ambiguous SIP Content-Length")
    try:
        content_length = int(content_lengths[0]) if content_lengths else 0
    except ValueError as err:
        raise SipError("invalid SIP Content-Length") from err
    if content_length < 0 or content_length > MAX_SIP_BODY_BYTES:
        raise SipError("invalid SIP Content-Length")
    if len(body_tail) < content_length:
        raise SipError("SIP body shorter than Content-Length")
    body = body_tail[:content_length]

    start = lines[0]
    parts = start.split(" ", 2)
    if start.startswith(SIP_VERSION + " "):
        if len(parts) < 3:
            raise SipError("malformed SIP status line")
        code = int(parts[1])
        if not 100 <= code <= 699:
            raise SipError(f"SIP status code out of range: {code}")
        if any(ord(ch) < 0x20 for ch in parts[2]):
            raise SipError("invalid SIP reason phrase")
        return SipMessage(status_code=code, reason=parts[2], headers=tuple(headers), body=body)

    if len(parts) != 3 or parts[2] != SIP_VERSION:
        raise SipError("malformed SIP request line")
    method = parts[0].upper()
    if not is_sip_token(method):
        raise SipError(f"malformed SIP method {method!r}")
    if "<" in parts[1] or ">" in parts[1]:
        raise SipError("SIP request URI must not use name-address syntax")
    parse_sip_uri(parts[1])
    return SipMessage(method=method, uri=parts[1], headers=tuple(headers), body=body)


def _render_headers(headers: Iterable[tuple[str, str]], body: bytes) -> str:
    out: list[str] = []
    saw_content_length = False
    for key, value in headers:
        if not is_sip_token(str(key)) or any(ord(ch) < 0x20 and ch != "\t" for ch in str(value)):
            raise SipError("invalid SIP header")
        if key.lower() in {"content-length", "l"}:
            saw_content_length = True
            value = str(len(body))
        out.append(f"{key}: {value}")
    if not saw_content_length:
        out.append(f"Content-Length: {len(body)}")
    return CRLF.join(out)


def build_request(method: str, uri: str | SipUri, headers: Iterable[tuple[str, str]], body: bytes = b"") -> bytes:
    method = method.upper()
    if method not in SUPPORTED_METHODS:
        raise SipError(f"unsupported SIP method {method}")
    uri_text = str(uri)
    if "<" in uri_text or ">" in uri_text:
        raise SipError("SIP request URI must not use name-address syntax")
    parse_sip_uri(uri_text)
    body = body or b""
    head = f"{method} {uri_text} {SIP_VERSION}{CRLF}{_render_headers(headers, body)}"
    return head.encode("utf-8") + b"\r\n\r\n" + body


def build_response(status_code: int, reason: str, headers: Iterable[tuple[str, str]], body: bytes = b"") -> bytes:
    if not 100 <= int(status_code) <= 699:
        raise SipError(f"SIP status code out of range: {status_code}")
    if any(ord(ch) < 0x20 for ch in reason):
        raise SipError("invalid SIP reason phrase")
    body = body or b""
    head = f"{SIP_VERSION} {int(status_code)} {reason}{CRLF}{_render_headers(headers, body)}"
    return head.encode("utf-8") + b"\r\n\r\n" + body


def dialog_headers(
    *,
    request_uri: str,
    local_uri: str,
    remote_uri: str,
    dialog: SipDialogIds,
    method: str,
    contact_uri: str,
    max_forwards: int = 70,
    content_type: str | None = None,
    transport: str = "UDP",
    local_display_name: str = "",
    remote_display_name: str = "",
    contact_display_name: str = "",
) -> list[tuple[str, str]]:
    """Build the common headers used by the ESP/HA phase-1 profile."""
    contact = parse_sip_uri(contact_uri)
    sent_by = contact.host
    if contact.port:
        sent_by = f"{sent_by}:{contact.port}"
    via_transport = (transport or "UDP").strip().upper()
    if via_transport not in {"UDP", "TCP"}:
        raise SipError(f"unsupported SIP transport {transport!r}")
    headers = [
        ("Via", f"SIP/2.0/{via_transport} {sent_by};branch={dialog.branch};rport"),
        ("Max-Forwards", str(max_forwards)),
        (
            "From",
            f"{format_name_addr(local_uri, local_display_name)};tag={dialog.local_tag}",
        ),
        (
            "To",
            format_name_addr(remote_uri, remote_display_name)
            + (f";tag={dialog.remote_tag}" if dialog.remote_tag else ""),
        ),
        ("Call-ID", dialog.call_id),
        ("CSeq", f"{dialog.cseq} {method.upper()}"),
        ("Contact", format_name_addr(contact_uri, contact_display_name)),
        ("Allow", ", ".join(sorted(SUPPORTED_METHODS))),
        ("Supported", ", ".join(sorted(SUPPORTED_OPTION_TAGS))),
    ]
    if content_type:
        headers.append(("Content-Type", content_type))
    parse_sip_uri(request_uri)
    return headers
