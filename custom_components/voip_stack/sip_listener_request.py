"""Request classification and wire transaction matching for the SIP listener."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

from .core import sip, sip_transfer
from .core.sip_auth import SipDigestCredentials
from .core.sip_dialog import SubscriptionDialog
from .core.sip_transaction import same_request_transaction, transaction_key


@dataclass(frozen=True, slots=True)
class SipRequestResult:
    """Response from an ingress policy or non-call SIP application."""

    status: int = 200
    reason: str = "OK"
    headers: tuple[tuple[str, str], ...] = ()
    to_tag: str = ""
    follow_up: "SipFollowUpRequest | None" = None
    follow_up_lock: asyncio.Lock | None = None
    follow_up_dialog: SubscriptionDialog | None = None
    follow_up_scheduler: Callable[[], None] | None = None
    follow_up_credentials: SipDigestCredentials | None = field(default=None, repr=False)
    follow_up_cseq_changed: Callable[[int], None] | None = None


@dataclass(frozen=True, slots=True)
class SipFollowUpRequest:
    """One request sent in the dialog established by an application method."""

    method: str
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""
    content_type: str = ""
    cseq: int = 1


def request_sequence(request: sip.SipMessage) -> sip.SipCSeq | SipRequestResult:
    """Check the request line, required options and CSeq before routing."""
    if request.method not in sip.SUPPORTED_METHODS:
        if request.method in sip.KNOWN_UNSUPPORTED_METHODS:
            return SipRequestResult(405, "Method Not Allowed")
        return SipRequestResult(501, "Not Implemented")
    if unsupported := sip.unsupported_required_options(request):
        return SipRequestResult(
            420, "Bad Extension", headers=(("Unsupported", ", ".join(unsupported)),)
        )
    try:
        sequence = sip.parse_cseq(request.header("CSeq"))
    except (TypeError, ValueError, sip.SipError):
        return SipRequestResult(400, "Bad Request")
    if sequence.method != request.method:
        return SipRequestResult(400, "Bad Request")
    return sequence


def dialog_extension_error(request: sip.SipMessage) -> SipRequestResult | None:
    """Check Replaces and session timers on requests handled by this endpoint."""
    try:
        sip_transfer.request_replaces(request)
    except sip.SipError:
        return SipRequestResult(400, "Bad Request")
    if request.method in {"INVITE", "UPDATE"}:
        try:
            sip.negotiate_uas_session_timer(request)
        except sip.SipSessionIntervalTooSmall as err:
            return SipRequestResult(
                422,
                "Session Interval Too Small",
                headers=(("Min-SE", str(err.minimum)),),
            )
        except sip.SipError:
            return SipRequestResult(400, "Bad Request")
    return None


def is_application_request(request: sip.SipMessage) -> bool:
    return request.method in {"MESSAGE", "PUBLISH", "SUBSCRIBE"} or (
        request.method == "REFER" and not sip.extract_tag(request.header("To"))
    )


def accepts_capability_sdp(request: sip.SipMessage) -> bool:
    wants_sdp = False
    for item in request.header("Accept", "application/sdp").split(","):
        media_type, parameters = sip._split_semicolon_params(item)
        try:
            quality = float(dict(parameters).get("q", "1"))
        except (TypeError, ValueError):
            continue
        wants_sdp |= (
            media_type.strip().lower() == "application/sdp" and 0 < quality <= 1
        )
    return wants_sdp


def matches_retransmission(
    request: sip.SipMessage,
    original: sip.SipMessage,
    addr: tuple[str, int],
    original_addr: tuple[str, int],
) -> bool:
    """Match a request retransmission while allowing a NAT port rebind."""

    if addr[0] != original_addr[0]:
        return False
    return same_request_transaction(request, original)


def matches_cancel(cancel: sip.SipMessage, invite: sip.SipMessage) -> bool:
    """Compare the method, CSeq and branch of an INVITE and its CANCEL."""
    cancel_key = transaction_key(cancel)
    invite_key = transaction_key(invite)
    return bool(
        cancel_key is not None
        and invite_key is not None
        and cancel_key[0] == "CANCEL"
        and invite_key[0] == "INVITE"
        and cancel_key[2]
        and cancel_key[1:] == invite_key[1:]
    )
