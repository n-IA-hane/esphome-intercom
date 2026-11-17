"""Transport-independent SIP dialog identity helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from . import sip
from ..session_cleanup import async_wait_for_cleanup


_MediaContract = TypeVar("_MediaContract")
_LOGGER = logging.getLogger(__name__)


def notify_media_commit(callback: Callable[[_MediaContract], None] | None, contract: _MediaContract) -> None:
    """Notify observers only after the signaling owner publishes its contract."""
    if callback is not None:
        try:
            callback(contract)
        except Exception:
            _LOGGER.exception("Committed SIP media observer failed")


async def apply_remote_offer_media(
    commit: Callable[[], Awaitable[None]] | None,
    rollback: Callable[[], Awaitable[None]] | None = None,
    finalize: Callable[[], Awaitable[None]] | None = None,
) -> bool:
    """Commit prepared media atomically, rolling it back on local failure."""

    if commit is None:
        return True
    try:
        await commit()
        if finalize is not None:
            await async_wait_for_cleanup(asyncio.create_task(finalize(), name="voip-media-finalize"))
    except BaseException as error:
        if rollback is not None:
            await async_wait_for_cleanup(asyncio.create_task(rollback(), name="voip-media-rollback"))
        if isinstance(error, asyncio.CancelledError):
            raise
        return False
    return True


@dataclass(kw_only=True, slots=True)
class DialogSignalingState:
    """Signaling state shared by UAC and UAS dialog owners."""

    remote_target_uri: str = ""
    route_set: tuple[str, ...] = ()
    local_sdp_session_id: int = 0
    local_sdp_session_version: int = 0
    local_sdp_body: str = ""
    peer_supports_from_change: bool = False
    session_timer: sip.SipSessionTimer = field(default_factory=sip.SipSessionTimer)


@dataclass(frozen=True, slots=True)
class DialogKey:
    """RFC 3261 dialog identity from the local endpoint's perspective.

    A TCP connection, UDP source address, Via branch, and Contact target are
    deliberately not part of this key. They belong to transport bindings or
    transactions and may legitimately change while a dialog remains alive.
    """

    call_id: str
    local_tag: str
    remote_tag: str

    @classmethod
    def for_uas_dialog(
        cls,
        initial_request: sip.SipMessage,
        *,
        local_tag: str,
    ) -> DialogKey:
        """Build the key for a dialog created by an inbound request."""

        return cls(
            call_id=initial_request.header("Call-ID"),
            local_tag=str(local_tag or ""),
            remote_tag=sip.extract_tag(initial_request.header("From")),
        )

    @classmethod
    def from_uas_request(cls, request: sip.SipMessage) -> DialogKey:
        """Build the key carried by a subsequent inbound UAS request."""

        return cls(
            call_id=request.header("Call-ID"),
            local_tag=sip.extract_tag(request.header("To")),
            remote_tag=sip.extract_tag(request.header("From")),
        )

    @property
    def complete(self) -> bool:
        """Return whether all mandatory dialog identifiers are present."""

        return bool(self.call_id and self.local_tag and self.remote_tag)


@dataclass(frozen=True, slots=True)
class DialogRequest:
    """Serialized in-dialog request with its transaction and route identity."""

    raw: bytes
    ids: sip.SipDialogIds
    routing: sip.SipDialogRoute


def build_dialog_request(
    method: str,
    *,
    call_id: str,
    local_tag: str,
    remote_tag: str,
    cseq: int,
    local_uri: str,
    remote_uri: str,
    remote_target_uri: str,
    route_set: tuple[str, ...] = (),
    contact_uri: str = "",
    transport: str = "UDP",
    via_sent_by: tuple[str, int | None] | None = None,
    local_display_name: str = "",
    remote_display_name: str = "",
    extra_headers: tuple[tuple[str, str], ...] = (),
    content_type: str = "",
    body: bytes = b"",
) -> DialogRequest:
    """Build one standards-shaped request shared by every dialog owner."""

    routing = sip.dialog_request_routing(remote_target_uri or remote_uri, route_set)
    ids = sip.SipDialogIds(
        call_id=call_id,
        local_tag=local_tag,
        remote_tag=remote_tag,
        cseq=cseq,
        branch=sip.make_branch(),
    )
    headers = sip.dialog_headers(
        request_uri=routing.request_uri,
        local_uri=local_uri,
        remote_uri=remote_uri,
        dialog=ids,
        method=method,
        contact_uri=contact_uri or local_uri,
        content_type=content_type,
        transport=transport,
        via_sent_by=via_sent_by,
        local_display_name=local_display_name,
        remote_display_name=remote_display_name,
    )
    headers.extend(("Route", value) for value in routing.route_headers)
    headers.extend(extra_headers)
    return DialogRequest(
        sip.build_request(method, routing.request_uri, headers, body),
        ids,
        routing,
    )


def uas_request_matches_dialog(
    request: sip.SipMessage,
    initial_request: sip.SipMessage,
    *,
    local_tag: str,
) -> bool:
    """Match an inbound in-dialog request independently of its transport."""

    expected = DialogKey.for_uas_dialog(initial_request, local_tag=local_tag)
    received = DialogKey.from_uas_request(request)
    return expected.complete and received.complete and received == expected


class DialogIdentityOwner(Protocol):
    @property
    def dialog(self) -> object | None: ...


@dataclass(frozen=True, slots=True)
class PreparedDialogIdentity:
    """Expected identity publication by one prepared media transition."""

    owner: DialogIdentityOwner
    previous: object
    candidate: object

    def current(self, *, committed: bool) -> bool:
        return self.owner.dialog is (self.candidate if committed else self.previous)


@dataclass(slots=True, kw_only=True)
class SubscriptionDialog(DialogSignalingState):
    """Stable dialog routing with independent local and remote sequences."""

    key: DialogKey
    local_uri: str
    remote_uri: str
    remote_cseq: int
    local_cseq: int = 1

    @classmethod
    def from_request(cls, request: sip.SipMessage, local_tag: str):
        return cls(
            key=DialogKey.for_uas_dialog(request, local_tag=local_tag),
            local_uri=sip.name_addr_uri(request.header("To")).split(";tag=", 1)[0],
            remote_uri=sip.name_addr_uri(request.header("From")).split(";tag=", 1)[0],
            remote_target_uri=sip.contact_target_uri(request),
            route_set=tuple(request.header_values("Record-Route")),
            remote_cseq=sip.parse_cseq(request.header("CSeq")).number,
        )

    def refresh(self, request: sip.SipMessage):
        if DialogKey.from_uas_request(request) != self.key:
            raise sip.SipError("subscription dialog does not match")
        sequence = sip.parse_cseq(request.header("CSeq")).number
        if sequence <= self.remote_cseq:
            raise ValueError("subscription CSeq did not advance")
        target = sip.contact_target_uri(request)
        if target:
            self.remote_target_uri = target
        self.remote_cseq = sequence

    def next_cseq(self):
        sequence = self.local_cseq
        self.local_cseq += 1
        return sequence
