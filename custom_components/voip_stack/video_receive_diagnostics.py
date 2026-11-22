"""Project call-scoped receive counters through the existing HA adapters."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import TYPE_CHECKING, Any

from .video_receive_state import VideoReceiveState
from .video_ws_io import _RtpVideoProtocol

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from .video_ws_view import _VideoMediaSession

_LOGGER = logging.getLogger(f"{__package__}.video_ws_view")

@dataclass(frozen=True, slots=True)
class VideoDiagnosticHooks:
    store: Callable[..., dict[str, Any]]
    debug_enabled: Callable[..., bool]
    merge_debug: Callable[..., None]
    publish: Callable[..., None]
    sync_protocol_drops: Callable[..., None]


@dataclass(slots=True)
class VideoReceiveDiagnostics:
    hass: HomeAssistant
    endpoint_id: str
    session: _VideoMediaSession
    state: VideoReceiveState
    protocol: _RtpVideoProtocol
    rtcp_protocol: _RtpVideoProtocol
    transcode_protocol: _RtpVideoProtocol | None
    loop: asyncio.AbstractEventLoop
    hooks: VideoDiagnosticHooks
    last_counter_event: float = 0.0

    def store_counters(self, *, force: bool = False) -> None:
        """Persist diagnostics without emitting a call lifecycle occurrence."""

        now = self.loop.time()
        interval = 0.5 if self.session.debug_mode else 5.0
        if not force and now - self.last_counter_event < interval:
            return
        self.last_counter_event = now
        self.hooks.sync_protocol_drops(
            self.state.counters,
            self.protocol,
            self.rtcp_protocol,
            self.transcode_protocol,
        )
        store = self.hooks.store(self.hass, self.endpoint_id)
        current_call_id = str(store.get("call_id") or "")
        if current_call_id:
            if current_call_id != self.session.call_id:
                return
        elif str(store.get("last_terminal_call_id") or "") != self.session.call_id:
            return
        store.update(self.state.counters)
        store["video_rtp_dropped_packets"] = self.protocol.dropped_packets + int(
            self.transcode_protocol.dropped_packets if self.transcode_protocol is not None else 0
        )
        if self.hooks.debug_enabled(self.hass):
            self.hooks.merge_debug(
                store,
                call_id=self.session.call_id,
                channel="video",
                values={
                    "local_rtp_port": self.session.local_rtp_port,
                    "remote_rtp_host": self.state.route.remote_host,
                    "remote_rtp_port": self.state.route.remote_port,
                    "remote_rtcp_host": self.state.route.remote_rtcp_host,
                    "remote_rtcp_port": self.state.route.remote_rtcp_port,
                    "format": self.session.recv_video_format.wire_token(),
                    "send_format": self.session.send_video_format.wire_token(),
                    "receive_format": self.session.recv_video_format.wire_token(),
                    "direction": self.session.local_direction,
                    "remote_connection_held": self.session.remote_connection_held,
                    **self.state.counters,
                    "video_rtp_dropped_packets": store["video_rtp_dropped_packets"],
                },
            )
        self.hooks.publish(self.hass, endpoint_id=self.endpoint_id)
