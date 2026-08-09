"""Deterministic ownership handoff for browser media WebSockets."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runtime_data import BrowserMediaRuntime


_LOGGER = logging.getLogger(__name__)


class WebSocketOwnerBusyError(RuntimeError):
    """Raised when an existing media WebSocket cannot release ownership."""


@dataclass(slots=True)
class MediaWebSocketOwner:
    """One browser media owner and its deterministic release notification."""

    websocket: Any | None = None
    transport: asyncio.BaseTransport | None = None
    user_id: str = ""
    client_id: str = ""
    token: object = field(default_factory=object)
    handoff_requested: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)

    def revoke(self) -> None:
        """Wake the previous request so it can release RTP resources."""

        self.handoff_requested.set()
        if self.websocket is not None:
            try:
                self.websocket.force_close()
            except Exception:  # noqa: BLE001 - teardown must continue.
                _LOGGER.debug("Failed to force-close replaced media WebSocket", exc_info=True)
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:  # noqa: BLE001 - teardown must continue.
                _LOGGER.debug("Failed to close replaced media transport", exc_info=True)


@dataclass(slots=True)
class _MediaIdentityLock:
    """One short-lived serialization point for a logical call endpoint."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


@asynccontextmanager
async def _media_identity_guard(media: BrowserMediaRuntime, owner_key: str):
    """Serialize one call without head-of-line blocking unrelated calls."""

    locks: dict[str, _MediaIdentityLock] = media.identity_locks
    entry = locks.get(owner_key)
    if entry is None:
        entry = _MediaIdentityLock()
        locks[owner_key] = entry
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if entry.users == 0 and locks.get(owner_key) is entry:
            locks.pop(owner_key, None)


def _call_media_client_id(registry: Any, call_id: str) -> str:
    """Return the browser identity currently pinned to one logical call."""
    session_id = registry.resolve_session_id(str(call_id or "").strip())
    session = registry.sessions.get(session_id)
    session_id_value = str(
        (session.metadata if session is not None else {}).get("media_client_id")
        or ""
    )
    media = registry.resource_for(call_id, "softphone_media") or {}
    return session_id_value or str(media.get("media_client_id") or "")


def media_websocket_owner_status(
    media: BrowserMediaRuntime,
    registry: Any,
    call_id: str,
    endpoint_id: str,
    client_id: str,
) -> str:
    """Return whether this browser owns, may claim, or must observe media."""

    expected_client_id = _call_media_client_id(registry, call_id)
    if expected_client_id and expected_client_id != client_id:
        return "other"
    owner_key = (
        f"{str(endpoint_id or '').strip()}|{str(call_id or '').strip()}"
    )
    live_owners = [
        owner
        for channel in ("audio", "video")
        if (
            owner := media.owners_for(channel).get(owner_key)
        )
        is not None
    ]
    if not live_owners:
        return "available"
    if all(
        isinstance(owner, MediaWebSocketOwner)
        and owner.client_id == client_id
        for owner in live_owners
    ):
        return "self"
    return "other"


def _set_call_media_client_id(registry: Any, call_id: str, client_id: str) -> None:
    """Atomically rebind a disconnected call to a new browser document."""
    session_id = registry.resolve_session_id(str(call_id or "").strip())
    session = registry.sessions.get(session_id)
    if session is None:
        raise WebSocketOwnerBusyError(call_id)
    if session.metadata.get("media_client_id") != client_id:
        session.metadata["media_client_id"] = client_id
        session.revision += 1
    for media_call_id in {call_id, session_id}:
        media = registry.resource_for(media_call_id, "softphone_media")
        if isinstance(media, dict):
            media["media_client_id"] = client_id


async def async_claim_call_media_owner(
    media: BrowserMediaRuntime,
    registry: Any,
    call_id: str,
    endpoint_id: str,
    owner: MediaWebSocketOwner,
    *,
    channel: str,
    timeout: float,
    shutdown_event: asyncio.Event | None = None,
    pin_client_identity: bool = True,
    local_bridge: Any | None = None,
) -> tuple[dict[str, object], asyncio.Lock, str]:
    """Claim one media channel and safely recover a disconnected browser.

    Every audio/video claim for the same endpoint/call is serialized by a
    keyed identity lock. A different browser document may replace the pinned client ID only
    after *all* media sockets for that endpoint/call have disappeared. User
    authorization is deliberately performed by the HTTP view before calling
    this helper, so a takeover remains restricted to the call's sticky HA
    controller.
    """
    if channel not in {"audio", "video"}:
        raise ValueError(f"unsupported media channel {channel!r}")
    call_id = str(call_id or "").strip()
    endpoint_id = str(endpoint_id or "").strip()
    owner_key = f"{endpoint_id}|{call_id}"
    owners = media.owners_for(channel)
    owner_lock = media.owner_lock_for(channel)

    async with _media_identity_guard(media, owner_key):
        live_owners = [
            candidate_owner
            for candidate in ("audio", "video")
            if (
                candidate_owner := media.owners_for(candidate).get(owner_key)
            )
            is not None
        ]
        if pin_client_identity:
            expected_client_id = _call_media_client_id(registry, call_id)
            if expected_client_id != owner.client_id:
                if expected_client_id:
                    raise WebSocketOwnerBusyError(call_id)
                _set_call_media_client_id(registry, call_id, owner.client_id)
        elif local_bridge is not None:
            competing_owner = any(
                not isinstance(candidate, MediaWebSocketOwner)
                or candidate.user_id != owner.user_id
                or candidate.client_id != owner.client_id
                for candidate in live_owners
            )
            if competing_owner:
                raise WebSocketOwnerBusyError(call_id)
            snapshot = local_bridge.get_call(call_id)
            if snapshot is None:
                raise WebSocketOwnerBusyError(call_id)
            if endpoint_id == snapshot.caller_endpoint_id:
                local_owner_id = snapshot.caller_media_owner_id
            elif endpoint_id == snapshot.callee_endpoint_id:
                local_owner_id = snapshot.callee_media_owner_id
            else:
                raise WebSocketOwnerBusyError(call_id)
            if local_owner_id and local_owner_id != owner.client_id:
                if live_owners:
                    raise WebSocketOwnerBusyError(call_id)
                try:
                    local_bridge.rebind_media_owner(
                        call_id,
                        endpoint_id,
                        owner.client_id,
                    )
                except RuntimeError as err:
                    # The dialog may end between get_call() and the atomic
                    # rebind. Surface the race as a normal ownership conflict,
                    # never as an HTTP 500 from the media view.
                    raise WebSocketOwnerBusyError(call_id) from err
        await async_claim_media_owner(
            owners,
            owner_lock,
            owner_key,
            owner,
            timeout=timeout,
            shutdown_event=shutdown_event,
        )
    return owners, owner_lock, owner_key


async def async_release_local_media_if_unowned(
    media: BrowserMediaRuntime,
    bridge: Any,
    lease: Any,
) -> bool:
    """Release a local-call lease after its last audio/video socket closes."""
    owner_key = f"{lease.endpoint_id}|{lease.call_id}"
    async with _media_identity_guard(media, owner_key):
        if any(
            media.owners_for(channel).get(owner_key) is not None
            for channel in ("audio", "video")
        ):
            return False
        try:
            return bool(
                bridge.release_media(
                    lease.call_id,
                    lease.endpoint_id,
                    lease.token,
                )
            )
        except Exception:  # noqa: BLE001 - the call may have ended first.
            _LOGGER.debug(
                "Failed to release unowned local media call_id=%s endpoint_id=%s",
                lease.call_id,
                lease.endpoint_id,
                exc_info=True,
            )
            return False


async def async_claim_media_owner(
    owners: dict[str, object],
    owner_lock: asyncio.Lock,
    call_id: str,
    owner: MediaWebSocketOwner,
    *,
    timeout: float,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Replace a stale browser owner only after its teardown completes."""

    async with owner_lock:
        if shutdown_event is not None and shutdown_event.is_set():
            raise WebSocketOwnerBusyError(call_id)
        previous_owner = owners.get(call_id)

    if isinstance(previous_owner, MediaWebSocketOwner):
        # Media contains private microphone/camera/call audio.  A reconnect is
        # a handoff only when it comes from the exact authenticated browser
        # instance that already owns the stream.  Merely having general
        # control permission must never allow another user or tab to evict it.
        if (
            previous_owner.user_id != owner.user_id
            or previous_owner.client_id != owner.client_id
        ):
            raise WebSocketOwnerBusyError(call_id)
        previous_owner.revoke()
        try:
            await asyncio.wait_for(previous_owner.released.wait(), timeout=timeout)
        except TimeoutError as err:
            raise WebSocketOwnerBusyError(call_id) from err
    elif previous_owner is not None:
        # Compatibility with an owner created before an integration reload.
        raise WebSocketOwnerBusyError(call_id)

    async with owner_lock:
        if shutdown_event is not None and shutdown_event.is_set():
            raise WebSocketOwnerBusyError(call_id)
        if call_id in owners:
            # Another reconnect won the race while this request was waiting.
            raise WebSocketOwnerBusyError(call_id)
        owners[call_id] = owner


async def async_release_media_owner(
    owners: dict[str, object],
    owner_lock: asyncio.Lock,
    call_id: str,
    owner: MediaWebSocketOwner,
) -> None:
    """Release only this request's ownership and wake every waiter."""

    async def release() -> None:
        try:
            async with owner_lock:
                if owners.get(call_id) is owner:
                    owners.pop(call_id, None)
        finally:
            # Handoff waiters must always wake, including shutdown/cancellation
            # while this release is queued behind the ownership lock.
            owner.released.set()

    task = asyncio.create_task(
        release(),
        name=f"voip-media-owner-release-{call_id}",
    )
    caller_cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            caller_cancelled = True
    if caller_cancelled:
        raise asyncio.CancelledError


async def async_revoke_media_owners(
    owners: dict[str, MediaWebSocketOwner],
    owner_lock: asyncio.Lock,
    *,
    timeout: float,
) -> set[str]:
    """Request teardown of every owner and report unconfirmed releases."""

    async with owner_lock:
        snapshot = dict(owners)
    for owner in snapshot.values():
        owner.revoke()

    async def _wait(call_id: str, owner: MediaWebSocketOwner) -> str | None:
        try:
            await asyncio.wait_for(owner.released.wait(), timeout=timeout)
        except TimeoutError:
            return call_id
        return None

    results = await asyncio.gather(
        *(_wait(call_id, owner) for call_id, owner in snapshot.items()),
    )
    return {call_id for call_id in results if call_id is not None}
