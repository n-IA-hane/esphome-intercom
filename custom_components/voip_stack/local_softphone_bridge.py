"""Transport-neutral local media bridge between browser phone endpoints.

The bridge owns only logical call state and in-memory media queues.  Signalling
and media adapters (for example Home Assistant WebSocket views) are expected to
translate their wire protocol at the boundary.  No SIP, SDP, RTP, browser, or
vendor-specific behavior belongs in this module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
import logging
import secrets

from .endpoint_registry import EndpointRegistry
from .fsm import CallState
from .queue_utils import put_drop_oldest


_LOGGER = logging.getLogger(__name__)

DEFAULT_AUDIO_QUEUE_SIZE = 64
DEFAULT_VIDEO_QUEUE_SIZE = 8


class LocalBridgeError(RuntimeError):
    """Base class for local bridge failures."""


class LocalCallNotFoundError(LocalBridgeError):
    """Raised when a local logical call no longer exists."""

    def __init__(self, call_id: object) -> None:
        self.call_id = str(call_id or "").strip()
        super().__init__(f"unknown local call: {self.call_id or '<empty>'}")


class LocalCallCollisionError(LocalBridgeError):
    """Raised when a logical call identifier is already active."""


class LocalCallStateError(LocalBridgeError):
    """Raised when an operation is invalid in the current call state."""


class LocalMediaLeaseError(LocalBridgeError):
    """Base class for media-owner lease failures."""


class LocalMediaLeaseBusyError(LocalMediaLeaseError):
    """Raised when another card already owns an endpoint media leg."""

    def __init__(self, call_id: str, endpoint_id: str, owner_id: str) -> None:
        self.call_id = call_id
        self.endpoint_id = endpoint_id
        self.owner_id = owner_id
        super().__init__(
            f"endpoint {endpoint_id!r} media for call {call_id!r} is already "
            f"owned by {owner_id!r}"
        )


class LocalMediaNotNegotiatedError(LocalBridgeError):
    """Raised when video is used on an audio-only logical call."""


# Public compatibility name; local and SIP/browser calls share one canonical
# state vocabulary.  The bridge uses only the four phases valid for a local
# two-party leg, while identity and serialization come from ``CallState``.
LocalCallState = CallState


class LocalCallEndReason(StrEnum):
    """Terminal reason from the point of view of the logical bridge."""

    DECLINED = "declined"
    CALLER_HANGUP = "caller_hangup"
    CALLEE_HANGUP = "callee_hangup"
    SHUTDOWN = "shutdown"


class LocalBridgeEventType(StrEnum):
    """Observable bridge mutations used by state adapters."""

    STARTED = "started"
    ANSWERED = "answered"
    VIDEO_UPDATED = "video_updated"
    MEDIA_LEASE_ACQUIRED = "media_lease_acquired"
    MEDIA_LEASE_RELEASED = "media_lease_released"
    ENDED = "ended"


class LocalMediaKind(StrEnum):
    """Media streams routed by the local bridge."""

    AUDIO = "audio"
    VIDEO = "video"


@dataclass(frozen=True, slots=True)
class LocalMediaLease:
    """Opaque ownership proof for exactly one endpoint media leg."""

    call_id: str
    endpoint_id: str
    owner_id: str
    token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class LocalEndpointMediaSnapshot:
    """Queue diagnostics for media waiting to be consumed by an endpoint."""

    endpoint_id: str
    audio_queued: int
    video_queued: int
    audio_dropped: int
    video_dropped: int


@dataclass(frozen=True, slots=True)
class LocalCallSnapshot:
    """Immutable public state of a local dual-endpoint call."""

    call_id: str
    caller_endpoint_id: str
    callee_endpoint_id: str
    caller_state: LocalCallState
    callee_state: LocalCallState
    video_requested: bool
    video_enabled: bool
    caller_video_send: bool
    callee_video_send: bool
    answer_owner_id: str
    caller_media_owner_id: str
    callee_media_owner_id: str
    caller_media: LocalEndpointMediaSnapshot
    callee_media: LocalEndpointMediaSnapshot
    end_reason: LocalCallEndReason | None = None

    @property
    def ended(self) -> bool:
        """Return whether both logical endpoint legs are terminal."""
        return (
            self.caller_state is LocalCallState.IDLE
            and self.callee_state is LocalCallState.IDLE
        )

    def state_for(self, endpoint_id: object) -> LocalCallState:
        """Return this call's state for one participant."""
        endpoint = str(endpoint_id or "").strip().casefold()
        if endpoint == self.caller_endpoint_id.casefold():
            return self.caller_state
        if endpoint == self.callee_endpoint_id.casefold():
            return self.callee_state
        raise LocalCallStateError(
            f"endpoint {str(endpoint_id or '').strip()!r} is not in call "
            f"{self.call_id!r}"
        )

    def video_direction_for(self, endpoint_id: object) -> str:
        """Return the negotiated browser-video direction for one endpoint."""
        endpoint = str(endpoint_id or "").strip().casefold()
        if endpoint == self.caller_endpoint_id.casefold():
            can_send = self.caller_video_send
            can_receive = self.callee_video_send
        elif endpoint == self.callee_endpoint_id.casefold():
            can_send = self.callee_video_send
            can_receive = self.caller_video_send
        else:
            raise LocalCallStateError(
                f"endpoint {str(endpoint_id or '').strip()!r} is not in call "
                f"{self.call_id!r}"
            )
        if can_send and can_receive:
            return "sendrecv"
        if can_send:
            return "sendonly"
        if can_receive:
            return "recvonly"
        return "inactive"


@dataclass(frozen=True, slots=True)
class LocalAnswerResult:
    """Atomic answer result, including the winning card's media lease."""

    call: LocalCallSnapshot
    media_lease: LocalMediaLease | None


@dataclass(frozen=True, slots=True)
class LocalBridgeEvent:
    """One immutable bridge notification."""

    event_type: LocalBridgeEventType
    call: LocalCallSnapshot
    endpoint_id: str = ""


LocalBridgeListener = Callable[[LocalBridgeEvent], None]
TokenFactory = Callable[[], str]


@dataclass(slots=True)
class _MediaLeaseState:
    lease: LocalMediaLease
    released: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(slots=True)
class _EndpointMedia:
    endpoint_id: str
    audio: asyncio.Queue[bytes]
    video: asyncio.Queue[bytes]
    video_control: asyncio.Queue[str]
    audio_dropped: int = 0
    video_dropped: int = 0
    lease: _MediaLeaseState | None = None

    def snapshot(self) -> LocalEndpointMediaSnapshot:
        return LocalEndpointMediaSnapshot(
            endpoint_id=self.endpoint_id,
            audio_queued=self.audio.qsize(),
            video_queued=self.video.qsize(),
            audio_dropped=self.audio_dropped,
            video_dropped=self.video_dropped,
        )

    def clear_queued_media(self) -> None:
        """Discard payloads owned by a browser document that went away."""
        for queue in (self.audio, self.video, self.video_control):
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break


@dataclass(slots=True)
class _LocalCall:
    call_id: str
    caller_endpoint_id: str
    callee_endpoint_id: str
    caller_state: LocalCallState
    callee_state: LocalCallState
    video_requested: bool
    video_enabled: bool
    caller_video_send: bool
    callee_video_send: bool
    answer_owner_id: str
    caller_media: _EndpointMedia
    callee_media: _EndpointMedia
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    def media_for(self, endpoint_id: str) -> _EndpointMedia:
        if endpoint_id.casefold() == self.caller_endpoint_id.casefold():
            return self.caller_media
        if endpoint_id.casefold() == self.callee_endpoint_id.casefold():
            return self.callee_media
        raise LocalCallStateError(
            f"endpoint {endpoint_id!r} is not in call {self.call_id!r}"
        )

    def peer_media_for(self, endpoint_id: str) -> _EndpointMedia:
        if endpoint_id.casefold() == self.caller_endpoint_id.casefold():
            return self.callee_media
        if endpoint_id.casefold() == self.callee_endpoint_id.casefold():
            return self.caller_media
        raise LocalCallStateError(
            f"endpoint {endpoint_id!r} is not in call {self.call_id!r}"
        )

    def state_for(self, endpoint_id: str) -> LocalCallState:
        if endpoint_id.casefold() == self.caller_endpoint_id.casefold():
            return self.caller_state
        if endpoint_id.casefold() == self.callee_endpoint_id.casefold():
            return self.callee_state
        raise LocalCallStateError(
            f"endpoint {endpoint_id!r} is not in call {self.call_id!r}"
        )

    def snapshot(
        self, *, end_reason: LocalCallEndReason | None = None
    ) -> LocalCallSnapshot:
        caller_lease = self.caller_media.lease
        callee_lease = self.callee_media.lease
        return LocalCallSnapshot(
            call_id=self.call_id,
            caller_endpoint_id=self.caller_endpoint_id,
            callee_endpoint_id=self.callee_endpoint_id,
            caller_state=self.caller_state,
            callee_state=self.callee_state,
            video_requested=self.video_requested,
            video_enabled=self.video_enabled,
            caller_video_send=self.caller_video_send,
            callee_video_send=self.callee_video_send,
            answer_owner_id=self.answer_owner_id,
            caller_media_owner_id=(
                caller_lease.lease.owner_id if caller_lease is not None else ""
            ),
            callee_media_owner_id=(
                callee_lease.lease.owner_id if callee_lease is not None else ""
            ),
            caller_media=self.caller_media.snapshot(),
            callee_media=self.callee_media.snapshot(),
            end_reason=end_reason,
        )


def _required_text(value: object, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise LocalBridgeError(f"{field_name} must not be empty")
    return text


def _positive_queue_size(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise LocalBridgeError(f"{field_name} must be a positive integer")
    if isinstance(value, float) and not value.is_integer():
        raise LocalBridgeError(f"{field_name} must be a positive integer")
    try:
        size = int(value)
    except (TypeError, ValueError) as err:
        raise LocalBridgeError(f"{field_name} must be a positive integer") from err
    if size < 1:
        raise LocalBridgeError(f"{field_name} must be a positive integer")
    return size


class LocalSoftphoneBridge:
    """Own transport-neutral local calls between logical phone endpoints.

    Public mutations are synchronous and contain no ``await``.  This makes
    endpoint claims, first-answer arbitration, and media-lease acquisition
    atomic when called on Home Assistant's event-loop thread.
    """

    def __init__(
        self,
        endpoint_registry: EndpointRegistry,
        *,
        audio_queue_size: int = DEFAULT_AUDIO_QUEUE_SIZE,
        video_queue_size: int = DEFAULT_VIDEO_QUEUE_SIZE,
        token_factory: TokenFactory | None = None,
    ) -> None:
        self._registry = endpoint_registry
        self._audio_queue_size = _positive_queue_size(
            audio_queue_size, "audio_queue_size"
        )
        self._video_queue_size = _positive_queue_size(
            video_queue_size, "video_queue_size"
        )
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self._calls: dict[str, _LocalCall] = {}
        self._listeners: set[LocalBridgeListener] = set()

    @property
    def calls(self) -> tuple[LocalCallSnapshot, ...]:
        """Return an insertion-order snapshot of active local calls."""
        return tuple(call.snapshot() for call in self._calls.values())

    def subscribe(self, listener: LocalBridgeListener) -> Callable[[], None]:
        """Subscribe to logical state changes and return an unsubscribe hook."""
        self._listeners.add(listener)

        def unsubscribe() -> None:
            self._listeners.discard(listener)

        return unsubscribe

    def start_call(
        self,
        caller_endpoint_id: object,
        callee_endpoint_id: object,
        *,
        call_id: object | None = None,
        request_video: bool = False,
        enable_caller_video_send: bool = False,
        caller_owner_id: object = "",
    ) -> LocalCallSnapshot:
        """Claim two idle endpoints and create calling/ringing logical legs.

        Outgoing video is opt-in. When supplied, ``caller_owner_id``
        atomically pins the originating browser instance before any bridge
        event is emitted.
        """
        caller = self._registry.require(caller_endpoint_id)
        callee = self._registry.require(callee_endpoint_id)
        if caller.endpoint_id.casefold() == callee.endpoint_id.casefold():
            raise LocalBridgeError("a local call requires two different endpoints")

        logical_call_id = (
            _required_text(call_id, "call_id")
            if call_id is not None
            else f"local-{secrets.token_hex(16)}"
        )
        if logical_call_id in self._calls:
            raise LocalCallCollisionError(
                f"local call {logical_call_id!r} is already active"
            )
        # EndpointRegistry claims are intentionally idempotent for repeated
        # callbacks belonging to an existing call.  A newly constructed local
        # call must not mistake such an external/surviving claim for ownership.
        for endpoint in (caller, callee):
            if endpoint.active_call_id == logical_call_id:
                raise LocalCallCollisionError(
                    f"endpoint {endpoint.endpoint_id!r} already owns call "
                    f"{logical_call_id!r} outside this bridge"
                )

        video_requested = bool(request_video)
        video_enabled = (
            video_requested
            and caller.supports(LocalMediaKind.VIDEO)
            and callee.supports(LocalMediaKind.VIDEO)
        )

        self._registry.claim_call(caller.endpoint_id, logical_call_id)
        try:
            self._registry.claim_call(callee.endpoint_id, logical_call_id)
        except Exception:
            self._registry.release_call(caller.endpoint_id, logical_call_id)
            raise

        caller_media_acquired = False
        try:
            call = _LocalCall(
                call_id=logical_call_id,
                caller_endpoint_id=caller.endpoint_id,
                callee_endpoint_id=callee.endpoint_id,
                caller_state=LocalCallState.CALLING,
                callee_state=LocalCallState.RINGING,
                video_requested=video_requested,
                video_enabled=video_enabled,
                caller_video_send=bool(
                    video_enabled and enable_caller_video_send
                ),
                callee_video_send=False,
                answer_owner_id="",
                caller_media=self._new_endpoint_media(caller.endpoint_id),
                callee_media=self._new_endpoint_media(callee.endpoint_id),
            )
            owner = str(caller_owner_id or "").strip()
            if owner:
                _lease, caller_media_acquired = self._acquire_media(
                    call, caller.endpoint_id, owner
                )
            self._calls[logical_call_id] = call
        except Exception:
            self._registry.release_call(callee.endpoint_id, logical_call_id)
            self._registry.release_call(caller.endpoint_id, logical_call_id)
            raise

        snapshot = call.snapshot()
        self._emit(LocalBridgeEvent(LocalBridgeEventType.STARTED, snapshot))
        if caller_media_acquired:
            self._emit(
                LocalBridgeEvent(
                    LocalBridgeEventType.MEDIA_LEASE_ACQUIRED,
                    snapshot,
                    caller.endpoint_id,
                )
            )
        return snapshot

    def get_call(self, call_id: object) -> LocalCallSnapshot | None:
        """Return an active call snapshot when it exists."""
        call = self._calls.get(str(call_id or "").strip())
        return call.snapshot() if call is not None else None

    def require_call(self, call_id: object) -> LocalCallSnapshot:
        """Return an active call snapshot or raise a typed error."""
        return self._require_internal(call_id).snapshot()

    def answer(
        self,
        call_id: object,
        endpoint_id: object,
        owner_id: object = "",
        *,
        enable_video_send: bool = False,
    ) -> LocalAnswerResult:
        """Atomically let the first callee card answer and own its media leg."""
        call = self._require_internal(call_id)
        endpoint = self._canonical_participant(call, endpoint_id)
        if endpoint != call.callee_endpoint_id:
            raise LocalCallStateError("only the ringing callee can answer")
        if call.callee_state not in {LocalCallState.RINGING, LocalCallState.IN_CALL}:
            raise LocalCallStateError(
                f"cannot answer call {call.call_id!r} in state "
                f"{call.callee_state.value!r}"
            )

        owner = str(owner_id or "").strip()
        if (
            call.callee_state is LocalCallState.IN_CALL
            and call.answer_owner_id
            and owner != call.answer_owner_id
        ):
            raise LocalMediaLeaseBusyError(
                call.call_id,
                endpoint,
                call.answer_owner_id,
            )
        if owner and not call.answer_owner_id:
            call.answer_owner_id = owner
        lease = None
        acquired = False
        if owner:
            lease, acquired = self._acquire_media(call, endpoint, owner)
        if call.callee_state is LocalCallState.RINGING:
            call.caller_state = LocalCallState.IN_CALL
            call.callee_state = LocalCallState.IN_CALL
            call.callee_video_send = bool(
                call.video_enabled and enable_video_send
            )
            snapshot = call.snapshot()
            self._emit(
                LocalBridgeEvent(
                    LocalBridgeEventType.ANSWERED,
                    snapshot,
                    endpoint,
                )
            )
        else:
            snapshot = call.snapshot()
        if acquired:
            self._emit(
                LocalBridgeEvent(
                    LocalBridgeEventType.MEDIA_LEASE_ACQUIRED,
                    snapshot,
                    endpoint,
                )
            )
        return LocalAnswerResult(snapshot, lease)

    def set_video_send(
        self,
        call_id: object,
        endpoint_id: object,
        enabled: bool,
    ) -> LocalCallSnapshot:
        """Change one participant's camera direction on the active call."""

        call = self._require_internal(call_id)
        endpoint = self._canonical_participant(call, endpoint_id)
        if call.state_for(endpoint) is not LocalCallState.IN_CALL:
            raise LocalCallStateError("video direction requires an active call")
        caller = self._registry.require(call.caller_endpoint_id)
        callee = self._registry.require(call.callee_endpoint_id)
        if enabled and not (
            caller.supports(LocalMediaKind.VIDEO)
            and callee.supports(LocalMediaKind.VIDEO)
        ):
            raise LocalBridgeError("both endpoints must support video")
        if enabled:
            call.video_requested = True
            call.video_enabled = True
        if endpoint.casefold() == call.caller_endpoint_id.casefold():
            call.caller_video_send = bool(enabled and call.video_enabled)
        else:
            call.callee_video_send = bool(enabled and call.video_enabled)
        snapshot = call.snapshot()
        self._emit(
            LocalBridgeEvent(
                LocalBridgeEventType.VIDEO_UPDATED,
                snapshot,
                endpoint,
            )
        )
        return snapshot

    def acquire_media(
        self,
        call_id: object,
        endpoint_id: object,
        owner_id: object,
    ) -> LocalMediaLease:
        """Acquire an idle media leg without changing call signalling state."""
        call = self._require_internal(call_id)
        endpoint = self._canonical_participant(call, endpoint_id)
        state = call.state_for(endpoint)
        if state is LocalCallState.RINGING:
            raise LocalCallStateError("the callee must answer to acquire media")
        if state is LocalCallState.IDLE:
            raise LocalCallStateError("an ended call cannot acquire media")
        lease, acquired = self._acquire_media(call, endpoint, owner_id)
        if acquired:
            self._emit(
                LocalBridgeEvent(
                    LocalBridgeEventType.MEDIA_LEASE_ACQUIRED,
                    call.snapshot(),
                    endpoint,
                )
            )
        return lease

    def rebind_media_owner(
        self,
        call_id: object,
        endpoint_id: object,
        owner_id: object,
    ) -> LocalMediaLease:
        """Replace a disconnected browser document's local media lease.

        The HTTP adapter calls this only after authenticating the sticky HA
        controller and proving that neither audio nor video has a live socket.
        Keeping this explicit avoids weakening ordinary ``acquire_media``:
        that method remains fail-closed against competing cards.
        """
        call = self._require_internal(call_id)
        endpoint = self._canonical_participant(call, endpoint_id)
        state = call.state_for(endpoint)
        if state is LocalCallState.RINGING:
            raise LocalCallStateError("the callee must answer to acquire media")
        if state is LocalCallState.IDLE:
            raise LocalCallStateError("an ended call cannot acquire media")
        owner = _required_text(owner_id, "owner_id")
        media = call.media_for(endpoint)
        previous = media.lease
        if previous is not None and previous.lease.owner_id == owner:
            return previous.lease
        if previous is not None:
            media.lease = None
            media.clear_queued_media()
            if (
                endpoint == call.callee_endpoint_id
                and call.answer_owner_id == previous.lease.owner_id
            ):
                call.answer_owner_id = ""
            previous.released.set()
            self._emit(
                LocalBridgeEvent(
                    LocalBridgeEventType.MEDIA_LEASE_RELEASED,
                    call.snapshot(),
                    endpoint,
                )
            )
        lease, acquired = self._acquire_media(call, endpoint, owner)
        if acquired:
            self._emit(
                LocalBridgeEvent(
                    LocalBridgeEventType.MEDIA_LEASE_ACQUIRED,
                    call.snapshot(),
                    endpoint,
                )
            )
        return lease

    def release_media(
        self,
        call_id: object,
        endpoint_id: object,
        lease_token: object,
    ) -> bool:
        """Release only the matching lease so a stale card cannot evict an owner."""
        call = self._require_internal(call_id)
        endpoint = self._canonical_participant(call, endpoint_id)
        media = call.media_for(endpoint)
        lease_state = media.lease
        if lease_state is None or not secrets.compare_digest(
            lease_state.lease.token, str(lease_token or "")
        ):
            return False
        media.lease = None
        media.clear_queued_media()
        if (
            endpoint == call.callee_endpoint_id
            and call.answer_owner_id == lease_state.lease.owner_id
        ):
            # Signalling remains answered, but the browser document no longer
            # owns media. A reload may now acquire a fresh lease; HA user
            # authorization stays enforced by the HTTP media endpoint.
            call.answer_owner_id = ""
        lease_state.released.set()
        self._emit(
            LocalBridgeEvent(
                LocalBridgeEventType.MEDIA_LEASE_RELEASED,
                call.snapshot(),
                endpoint,
            )
        )
        return True

    def validate_media_lease(
        self,
        call_id: object,
        endpoint_id: object,
        lease_token: object,
    ) -> bool:
        """Return whether a token still owns this active endpoint media leg."""
        call = self._calls.get(str(call_id or "").strip())
        if call is None:
            return False
        try:
            endpoint = self._canonical_participant(call, endpoint_id)
        except LocalCallStateError:
            return False
        lease_state = call.media_for(endpoint).lease
        return lease_state is not None and secrets.compare_digest(
            lease_state.lease.token, str(lease_token or "")
        )

    def decline(self, call_id: object, endpoint_id: object) -> LocalCallSnapshot:
        """Decline a ringing callee leg and clean up both endpoint claims."""
        call = self._require_internal(call_id)
        endpoint = self._canonical_participant(call, endpoint_id)
        if endpoint != call.callee_endpoint_id:
            raise LocalCallStateError("only the ringing callee can decline")
        if call.callee_state is not LocalCallState.RINGING:
            raise LocalCallStateError("only a ringing call can be declined")
        return self._finish(call, LocalCallEndReason.DECLINED)

    def hangup(self, call_id: object, endpoint_id: object) -> LocalCallSnapshot:
        """End an active local call from either logical endpoint."""
        call = self._require_internal(call_id)
        endpoint = self._canonical_participant(call, endpoint_id)
        reason = (
            LocalCallEndReason.CALLER_HANGUP
            if endpoint == call.caller_endpoint_id
            else LocalCallEndReason.CALLEE_HANGUP
        )
        return self._finish(call, reason)

    def close(self) -> tuple[LocalCallSnapshot, ...]:
        """Terminate every active local call, suitable for integration shutdown."""
        return tuple(
            self._finish(call, LocalCallEndReason.SHUTDOWN)
            for call in tuple(self._calls.values())
        )

    def send_audio(
        self,
        call_id: object,
        endpoint_id: object,
        lease_token: object,
        payload: bytes | bytearray | memoryview,
    ) -> bool:
        """Queue one audio frame for the peer; return whether one was dropped."""
        return self._send_media(
            call_id,
            endpoint_id,
            lease_token,
            LocalMediaKind.AUDIO,
            payload,
        )

    def send_video(
        self,
        call_id: object,
        endpoint_id: object,
        lease_token: object,
        payload: bytes | bytearray | memoryview,
    ) -> bool:
        """Queue one negotiated video frame for the peer."""
        return self._send_media(
            call_id,
            endpoint_id,
            lease_token,
            LocalMediaKind.VIDEO,
            payload,
        )

    def send_video_control(
        self,
        call_id: object,
        endpoint_id: object,
        lease_token: object,
        control: object,
    ) -> bool:
        """Queue one transport-neutral video control message for the peer."""
        call = self._require_internal(call_id)
        endpoint = self._canonical_participant(call, endpoint_id)
        self._require_media_lease(call, endpoint, lease_token)
        if call.state_for(endpoint) is not LocalCallState.IN_CALL:
            raise LocalCallStateError("video control is available only after answer")
        if not call.video_enabled:
            raise LocalMediaNotNegotiatedError(
                f"video is not negotiated for call {call.call_id!r}"
            )
        message = _required_text(control, "video control")
        return put_drop_oldest(call.peer_media_for(endpoint).video_control, message)

    def receive_audio_nowait(
        self,
        call_id: object,
        endpoint_id: object,
        lease_token: object,
    ) -> bytes:
        """Read one queued audio frame for the owning card without waiting."""
        return self._receive_media_nowait(
            call_id, endpoint_id, lease_token, LocalMediaKind.AUDIO
        )

    def receive_video_nowait(
        self,
        call_id: object,
        endpoint_id: object,
        lease_token: object,
    ) -> bytes:
        """Read one queued video frame for the owning card without waiting."""
        return self._receive_media_nowait(
            call_id, endpoint_id, lease_token, LocalMediaKind.VIDEO
        )

    async def receive_audio(
        self,
        call_id: object,
        endpoint_id: object,
        lease_token: object,
    ) -> bytes:
        """Wait for one audio frame while the call and media lease remain valid."""
        return await self._receive_media(
            call_id, endpoint_id, lease_token, LocalMediaKind.AUDIO
        )

    async def receive_video(
        self,
        call_id: object,
        endpoint_id: object,
        lease_token: object,
    ) -> bytes:
        """Wait for one video frame while the call and media lease remain valid."""
        return await self._receive_media(
            call_id, endpoint_id, lease_token, LocalMediaKind.VIDEO
        )

    async def receive_video_control(
        self,
        call_id: object,
        endpoint_id: object,
        lease_token: object,
    ) -> str:
        """Wait for a peer video-control message while the lease is valid."""
        call = self._require_internal(call_id)
        endpoint = self._canonical_participant(call, endpoint_id)
        lease_state = self._require_media_lease(call, endpoint, lease_token)
        if not call.video_enabled:
            raise LocalMediaNotNegotiatedError(
                f"video is not negotiated for call {call.call_id!r}"
            )
        queue = call.media_for(endpoint).video_control
        control_task = asyncio.create_task(queue.get())
        released_task = asyncio.create_task(lease_state.released.wait())
        closed_task = asyncio.create_task(call.closed.wait())
        tasks = (control_task, released_task, closed_task)
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if call.closed.is_set():
                raise LocalCallNotFoundError(call.call_id)
            if lease_state.released.is_set() or not self.validate_media_lease(
                call.call_id, endpoint, lease_token
            ):
                raise LocalMediaLeaseError(
                    f"media lease for endpoint {endpoint!r} was released"
                )
            if control_task.done():
                return control_task.result()
            raise LocalBridgeError("video-control wait ended without data")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def media_stats(
        self, call_id: object, endpoint_id: object
    ) -> LocalEndpointMediaSnapshot:
        """Return queue/drop diagnostics for one receiving endpoint."""
        call = self._require_internal(call_id)
        endpoint = self._canonical_participant(call, endpoint_id)
        return call.media_for(endpoint).snapshot()

    async def wait_closed(self, call_id: object) -> None:
        """Wait until a local call ends; an already-ended call returns at once."""
        call = self._calls.get(str(call_id or "").strip())
        if call is not None:
            await call.closed.wait()

    def _new_endpoint_media(self, endpoint_id: str) -> _EndpointMedia:
        return _EndpointMedia(
            endpoint_id=endpoint_id,
            audio=asyncio.Queue(maxsize=self._audio_queue_size),
            video=asyncio.Queue(maxsize=self._video_queue_size),
            video_control=asyncio.Queue(maxsize=self._video_queue_size),
        )

    def _require_internal(self, call_id: object) -> _LocalCall:
        logical_call_id = str(call_id or "").strip()
        call = self._calls.get(logical_call_id)
        if call is None:
            raise LocalCallNotFoundError(logical_call_id)
        return call

    @staticmethod
    def _canonical_participant(call: _LocalCall, endpoint_id: object) -> str:
        endpoint = _required_text(endpoint_id, "endpoint_id")
        if endpoint.casefold() == call.caller_endpoint_id.casefold():
            return call.caller_endpoint_id
        if endpoint.casefold() == call.callee_endpoint_id.casefold():
            return call.callee_endpoint_id
        raise LocalCallStateError(
            f"endpoint {endpoint!r} is not in call {call.call_id!r}"
        )

    def _acquire_media(
        self, call: _LocalCall, endpoint_id: str, owner_id: object
    ) -> tuple[LocalMediaLease, bool]:
        owner = _required_text(owner_id, "owner_id")
        if endpoint_id == call.callee_endpoint_id:
            if call.answer_owner_id and call.answer_owner_id != owner:
                raise LocalMediaLeaseBusyError(
                    call.call_id,
                    endpoint_id,
                    call.answer_owner_id,
                )
            if not call.answer_owner_id and call.callee_state is LocalCallState.IN_CALL:
                call.answer_owner_id = owner
        media = call.media_for(endpoint_id)
        lease_state = media.lease
        if lease_state is not None:
            if lease_state.lease.owner_id == owner:
                return lease_state.lease, False
            raise LocalMediaLeaseBusyError(
                call.call_id,
                endpoint_id,
                lease_state.lease.owner_id,
            )
        # Payloads queued while no browser owned this endpoint are stale by
        # definition. A reload/rebind must resume at the live edge of the call.
        media.clear_queued_media()
        token = _required_text(self._token_factory(), "lease token")
        lease = LocalMediaLease(call.call_id, endpoint_id, owner, token)
        media.lease = _MediaLeaseState(lease)
        return lease, True

    def _require_media_lease(
        self,
        call: _LocalCall,
        endpoint_id: str,
        lease_token: object,
    ) -> _MediaLeaseState:
        lease_state = call.media_for(endpoint_id).lease
        if lease_state is None or not secrets.compare_digest(
            lease_state.lease.token, str(lease_token or "")
        ):
            raise LocalMediaLeaseError(
                f"invalid media lease for endpoint {endpoint_id!r} in call "
                f"{call.call_id!r}"
            )
        return lease_state

    def _send_media(
        self,
        call_id: object,
        endpoint_id: object,
        lease_token: object,
        kind: LocalMediaKind,
        payload: bytes | bytearray | memoryview,
    ) -> bool:
        call = self._require_internal(call_id)
        endpoint = self._canonical_participant(call, endpoint_id)
        self._require_media_lease(call, endpoint, lease_token)
        if call.state_for(endpoint) is not LocalCallState.IN_CALL:
            raise LocalCallStateError("media is available only after answer")
        if kind is LocalMediaKind.VIDEO and not call.video_enabled:
            raise LocalMediaNotNegotiatedError(
                f"video is not negotiated for call {call.call_id!r}"
            )
        if kind is LocalMediaKind.VIDEO:
            can_send = (
                call.caller_video_send
                if endpoint == call.caller_endpoint_id
                else call.callee_video_send
            )
            if not can_send:
                raise LocalMediaNotNegotiatedError(
                    f"endpoint {endpoint!r} is not authorized to send video "
                    f"for call {call.call_id!r}"
                )
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise TypeError("media payload must be bytes-like")

        peer_media = call.peer_media_for(endpoint)
        if peer_media.lease is None:
            # Do not build latency while the peer has no browser media owner.
            if kind is LocalMediaKind.AUDIO:
                peer_media.audio_dropped += 1
            else:
                peer_media.video_dropped += 1
            return True
        queue = peer_media.audio if kind is LocalMediaKind.AUDIO else peer_media.video
        dropped = put_drop_oldest(queue, bytes(payload))
        if dropped:
            if kind is LocalMediaKind.AUDIO:
                peer_media.audio_dropped += 1
            else:
                peer_media.video_dropped += 1
        return dropped

    def _receive_media_nowait(
        self,
        call_id: object,
        endpoint_id: object,
        lease_token: object,
        kind: LocalMediaKind,
    ) -> bytes:
        call = self._require_internal(call_id)
        endpoint = self._canonical_participant(call, endpoint_id)
        self._require_media_lease(call, endpoint, lease_token)
        if kind is LocalMediaKind.VIDEO and not call.video_enabled:
            raise LocalMediaNotNegotiatedError(
                f"video is not negotiated for call {call.call_id!r}"
            )
        media = call.media_for(endpoint)
        queue = media.audio if kind is LocalMediaKind.AUDIO else media.video
        return queue.get_nowait()

    async def _receive_media(
        self,
        call_id: object,
        endpoint_id: object,
        lease_token: object,
        kind: LocalMediaKind,
    ) -> bytes:
        call = self._require_internal(call_id)
        endpoint = self._canonical_participant(call, endpoint_id)
        lease_state = self._require_media_lease(call, endpoint, lease_token)
        if kind is LocalMediaKind.VIDEO and not call.video_enabled:
            raise LocalMediaNotNegotiatedError(
                f"video is not negotiated for call {call.call_id!r}"
            )
        media = call.media_for(endpoint)
        queue = media.audio if kind is LocalMediaKind.AUDIO else media.video
        media_task = asyncio.create_task(queue.get())
        released_task = asyncio.create_task(lease_state.released.wait())
        closed_task = asyncio.create_task(call.closed.wait())
        tasks = (media_task, released_task, closed_task)
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if call.closed.is_set():
                raise LocalCallNotFoundError(call.call_id)
            if lease_state.released.is_set() or not self.validate_media_lease(
                call.call_id, endpoint, lease_token
            ):
                raise LocalMediaLeaseError(
                    f"media lease for endpoint {endpoint!r} was released"
                )
            if media_task.done():
                return media_task.result()
            raise LocalBridgeError("media wait ended without data")
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _finish(
        self, call: _LocalCall, reason: LocalCallEndReason
    ) -> LocalCallSnapshot:
        self._calls.pop(call.call_id, None)
        call.caller_state = LocalCallState.IDLE
        call.callee_state = LocalCallState.IDLE
        call.closed.set()
        for media in (call.caller_media, call.callee_media):
            if media.lease is not None:
                media.lease.released.set()
        for endpoint_id in (
            call.caller_endpoint_id,
            call.callee_endpoint_id,
        ):
            if self._registry.get(endpoint_id) is not None:
                self._registry.release_call(endpoint_id, call.call_id)
        snapshot = call.snapshot(end_reason=reason)
        self._emit(LocalBridgeEvent(LocalBridgeEventType.ENDED, snapshot))
        return snapshot

    def _emit(self, event: LocalBridgeEvent) -> None:
        for listener in tuple(self._listeners):
            try:
                listener(event)
            except Exception:  # pragma: no cover - defensive adapter isolation
                _LOGGER.exception("Local softphone bridge listener failed")
