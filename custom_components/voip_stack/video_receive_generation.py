"""Apply committed SDP generations without acquiring another media owner."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aiohttp import web

from .core import sdp
from .core.video_rtp import RtpReorderBuffer, RtpTimestampClock
from .queue_utils import drain_queue
from .video_receive_state import VideoDepacketizer, VideoReceiveState
from .video_ws_io import _ByteBudgetQueue

if TYPE_CHECKING:
    from .pbx_runtime import SipEndpointRuntime
    from .video_ws_view import _VideoMediaSession


@dataclass(frozen=True, slots=True)
class VideoGenerationHooks:
    pipeline_signature: Callable[[_VideoMediaSession], tuple[object, ...]]
    negotiation_payload: Callable[..., dict[str, Any]]
    make_depacketizer: Callable[[str, sdp.RtpVideoFormat, SipEndpointRuntime | None,
        tuple[bytes, ...]], VideoDepacketizer | None]


@dataclass(slots=True)
class VideoGenerationUpdates:
    session: _VideoMediaSession
    state: VideoReceiveState
    registry: SipEndpointRuntime | None
    cached_parameter_sets: tuple[bytes, ...]
    queue: _ByteBudgetQueue
    media_queue: _ByteBudgetQueue
    access_units: _ByteBudgetQueue
    ws: web.WebSocketResponse
    ws_send_lock: asyncio.Lock
    rtp_send_lock: asyncio.Lock
    outbound_clock: RtpTimestampClock
    closed: asyncio.Event
    keepalive_updated: asyncio.Event
    store_counters: Callable[..., None]
    hooks: VideoGenerationHooks
    media_reset_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def refresh(self, generation: int) -> bool:
        """Atomically discard packets and codec state from an older SDP generation."""

        if generation == self.state.applied_media_generation:
            return True
        async with self.media_reset_lock:
            if generation == self.state.applied_media_generation:
                return True
            if self.hooks.pipeline_signature(self.session) != self.state.active_pipeline_signature:
                if self.state.restart_notified_generation != generation:
                    payload = self.hooks.negotiation_payload(
                        self.session, message_type="media_update"
                    )
                    payload["restart_required"] = True
                    payload["restart_reason"] = "video_pipeline_changed"
                    async with self.ws_send_lock:
                        await self.ws.send_json(payload)
                    self.state.restart_notified_generation = generation
                    self.state.counters["video_pipeline_restarts"] += 1
                    self.store_counters()
                return False
            self.state.browser_format = self.session.browser_receive_format
            self.state.route.remote_host = str(self.session.remote_rtp_host)
            self.state.route.remote_port = int(self.session.remote_rtp_port)
            self.state.route.remote_rtcp_host = str(self.session.remote_rtcp_host or self.state.route.remote_host)
            self.state.route.remote_rtcp_port = int(self.session.remote_rtcp_port)
            self.state.route.remote_rtcp_host_explicit = bool(
                self.session.remote_rtcp_host
                and self.session.remote_rtcp_host != self.session.remote_rtp_host
            )
            self.state.route.remote_rtcp_offset = (
                0 if self.session.remote_rtcp_mux else self.state.route.remote_rtcp_port - self.state.route.remote_port
            )
            self.state.route.latched_source = None
            self.state.route.latched_ssrc = None
            self.state.route.latched_rtcp_source = None
            self.state.depacketizer = self.hooks.make_depacketizer(
                self.session.call_id,
                self.state.browser_format,
                self.registry,
                self.cached_parameter_sets,
            )
            self.state.reorder = RtpReorderBuffer()
            self.state.input_reorder = RtpReorderBuffer()
            drain_queue(self.queue)
            if self.media_queue is not self.queue:
                drain_queue(self.media_queue)
            drain_queue(self.access_units)
            self.state.needs_key_frame = True
            self.state.extended_sequence.reset()
            self.state.highest_sequence = 0
            self.state.last_keyframe_feedback = 0.0
            async with self.rtp_send_lock:
                self.outbound_clock.reset_browser()
            self.state.applied_media_generation = generation
            return True

    async def run(self) -> None:
        """Apply committed re-INVITEs and notify the attached browser."""

        observed_generation = int(self.session.media_generation)
        while not self.closed.is_set():
            await self.session.update_event.wait()
            self.session.update_event.clear()
            generation = int(self.session.media_generation)
            if generation == observed_generation:
                continue
            if self.session.removed:
                return
            if not await self.refresh(generation):
                return
            observed_generation = generation
            self.keepalive_updated.set()
            async with self.ws_send_lock:
                await self.ws.send_json(
                    self.hooks.negotiation_payload(self.session, message_type="media_update")
                )
