"""SIP call-state vocabulary shared by HA sessions and bridges."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum


class CallState(StrEnum):
    IDLE = "idle"
    CALLING = "calling"
    REMOTE_RINGING = "remote_ringing"
    RINGING = "ringing"
    CONNECTING = "connecting"
    IN_CALL = "in_call"
    TERMINATING = "terminating"
    BUSY = "busy"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    MEDIA_INCOMPATIBLE = "media_incompatible"
    TRANSPORT_UNREACHABLE = "transport_unreachable"
    AUTH_REQUIRED_UNSUPPORTED = "auth_required_unsupported"


class TerminalReason(StrEnum):
    LOCAL_HANGUP = "local_hangup"
    REMOTE_HANGUP = "remote_hangup"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    FORWARDED = "forwarded"
    TIMEOUT = "timeout"
    BUSY = "busy"
    TRANSPORT_UNREACHABLE = "transport_unreachable"
    MEDIA_INCOMPATIBLE = "media_incompatible"
    AUTH_REQUIRED_UNSUPPORTED = "auth_required_unsupported"
    PROXY_AUTH_REQUIRED_UNSUPPORTED = "proxy_auth_required_unsupported"
    PROTOCOL_ERROR = "protocol_error"


@dataclass(slots=True)
class SipPhoneState:
    """Public SIP phone state exposed by HA and mirrored by ESP snapshots."""

    state: str = CallState.IDLE.value
    call_id: str = ""
    direction: str = ""
    caller: str = ""
    callee: str = ""
    local_uri: str = ""
    remote_uri: str = ""
    contact: str = ""
    sip_transport: str = "udp"
    sip_status_code: int = 0
    terminal_reason: str = ""
    selected_tx_format: str = ""
    selected_rx_format: str = ""
    rtp_tx_packets: int = 0
    rtp_rx_packets: int = 0
    rtp_tx_bytes: int = 0
    rtp_rx_bytes: int = 0
    last_sip_event: str = ""

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)


def sip_phone_state(**values: object) -> dict[str, str | int]:
    """Return a complete SipPhoneState dict, ignoring unknown keys."""

    allowed = SipPhoneState.__dataclass_fields__
    clean = {key: value for key, value in values.items() if key in allowed}
    return SipPhoneState(**clean).as_dict()


def sip_public_state(state: str) -> str:
    """Normalize internal SIP outcomes to the public SipPhoneState state."""
    value = (state or "").strip().lower()
    mapping = {
        "": CallState.IDLE.value,
        "idle": CallState.IDLE.value,
        "calling": CallState.CALLING.value,
        "answered": CallState.IN_CALL.value,
        "in_call": CallState.IN_CALL.value,
        "ringing": CallState.RINGING.value,
        "remote_ringing": CallState.REMOTE_RINGING.value,
        "connecting": CallState.CONNECTING.value,
        "route_requested": "route_requested",
        "terminating": CallState.TERMINATING.value,
        "busy": CallState.BUSY.value,
        "declined": CallState.DECLINED.value,
        "dnd": CallState.DECLINED.value,
        "cancelled": CallState.CANCELLED.value,
        "media_incompatible": CallState.MEDIA_INCOMPATIBLE.value,
        "transport_unreachable": CallState.TRANSPORT_UNREACHABLE.value,
        "auth_required_unsupported": CallState.AUTH_REQUIRED_UNSUPPORTED.value,
        "proxy_auth_required_unsupported": CallState.AUTH_REQUIRED_UNSUPPORTED.value,
        "sip_486": CallState.BUSY.value,
        "sip_603": CallState.DECLINED.value,
        "sip_487": CallState.CANCELLED.value,
        "sip_488": CallState.MEDIA_INCOMPATIBLE.value,
        "sip_401": CallState.AUTH_REQUIRED_UNSUPPORTED.value,
        "sip_407": CallState.AUTH_REQUIRED_UNSUPPORTED.value,
        "local_hangup": CallState.IDLE.value,
        "remote_hangup": CallState.IDLE.value,
        "not_in_call": CallState.IDLE.value,
        "timeout": CallState.TRANSPORT_UNREACHABLE.value,
        "error": CallState.TRANSPORT_UNREACHABLE.value,
        "protocol_error": CallState.TRANSPORT_UNREACHABLE.value,
    }
    if value in mapping:
        return mapping[value]
    if value.startswith("sip_"):
        return CallState.TRANSPORT_UNREACHABLE.value
    return CallState.TRANSPORT_UNREACHABLE.value if value else CallState.IDLE.value


def sip_terminal_reason(result: str, public_state: str | None = None) -> str:
    """Normalize internal SIP outcomes to a terminal reason."""
    value = (result or "").strip().lower()
    reason_mapping = {
        "local_hangup": TerminalReason.LOCAL_HANGUP.value,
        "remote_hangup": TerminalReason.REMOTE_HANGUP.value,
        "not_in_call": TerminalReason.LOCAL_HANGUP.value,
        "sip_486": TerminalReason.BUSY.value,
        "sip_603": TerminalReason.DECLINED.value,
        "sip_487": TerminalReason.CANCELLED.value,
        "sip_488": TerminalReason.MEDIA_INCOMPATIBLE.value,
        "sip_401": TerminalReason.AUTH_REQUIRED_UNSUPPORTED.value,
        "sip_407": TerminalReason.PROXY_AUTH_REQUIRED_UNSUPPORTED.value,
        "dnd": "dnd",
    }
    if value in reason_mapping:
        return reason_mapping[value]
    if value == "timeout":
        return TerminalReason.TIMEOUT.value
    if value in {"error", "protocol_error"}:
        return TerminalReason.PROTOCOL_ERROR.value
    if value.startswith("sip_"):
        return value
    return public_state or sip_public_state(result)


def sip_failure_response(result: str) -> tuple[int, str, str, str]:
    """Map an outbound SIP failure to status, reason, terminal reason and state."""
    public_state = sip_public_state(result)
    terminal_reason = sip_terminal_reason(result, public_state)
    if terminal_reason == "dnd":
        # DND makes this contact temporarily unwilling to take the call; keep
        # it distinct in HA while using the same endpoint-local SIP response
        # as the direct-call path.
        return 486, "Busy Here", terminal_reason, public_state
    if public_state == CallState.BUSY.value:
        return 486, "Busy Here", terminal_reason, public_state
    if public_state == CallState.DECLINED.value:
        return 603, "Decline", terminal_reason, public_state
    if public_state == CallState.CANCELLED.value:
        return 487, "Request Terminated", terminal_reason, public_state
    if public_state == CallState.MEDIA_INCOMPATIBLE.value:
        return 488, "Not Acceptable Here", terminal_reason, public_state
    if terminal_reason == TerminalReason.TIMEOUT.value:
        return 408, "Request Timeout", terminal_reason, public_state
    return 480, "Temporarily Unavailable", terminal_reason, public_state


def sip_terminal_status(reason: str) -> tuple[str, int, str]:
    """Classify an internal terminal reason as HA event class/SIP code/reason."""
    value = (reason or "").strip()
    if value in (TerminalReason.BUSY.value, TerminalReason.DECLINED.value, TerminalReason.CANCELLED.value):
        return ("decline", 0, value)
    if value == TerminalReason.MEDIA_INCOMPATIBLE.value:
        return ("error", 488, value)
    if value == TerminalReason.AUTH_REQUIRED_UNSUPPORTED.value:
        return ("error", 401, value)
    if value == TerminalReason.PROXY_AUTH_REQUIRED_UNSUPPORTED.value:
        return ("error", 407, value)
    if value == TerminalReason.TRANSPORT_UNREACHABLE.value:
        return ("error", 0, value)
    if value == TerminalReason.TIMEOUT.value:
        return ("error", 408, value)
    if value.startswith("sip_"):
        try:
            return ("error", int(value.split("_", 1)[1]), value)
        except ValueError:
            return ("error", 0, value)
    return ("error", 0, value or TerminalReason.PROTOCOL_ERROR.value)
