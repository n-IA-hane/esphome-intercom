"""HA-anchored SIP conference rooms for VoIP Stack."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import dataclass, field
import logging
import random
import secrets
import time
from typing import Any

import numpy as np

from homeassistant.core import HomeAssistant

from .call_projection import observe_phone_leg_projection
from .core.audio_format import AudioFormat, PcmFormat
from .core.audio_pcm import PcmFrameConverter
from .endpoint_lifecycle import call_registry
from .endpoint_termination import EndpointTerminationHandler
from .endpoint_session import TerminationInitiator, TerminationIntent
from .endpoint_registry import EndpointBusyError
from .fsm import CallState, TerminalReason
from .groups import GROUP_TYPE_CONFERENCE
from .media_ports import RtpPortReservation
from .core.rtp import RtpPacket, build_packet, next_sequence, next_timestamp, parse_packet
from .core.sdp import build_answer_directional
from .core import sdp
from .session_cleanup import async_wait_for_cleanup
from .runtime_data import (
    browser_phone,
    conference_component,
    endpoint_directory,
    sip_endpoint_runtime,
)
from .sip_client import RtpPayloadDecoder, RtpPayloadEncoder
from .sip_listener import SipInvite, SipInviteResult
from .websocket_api import _fire_call_event
from .phone_endpoint import EndpointAvailability, EndpointKind

_LOGGER = logging.getLogger(__name__)

CONFERENCE_FORMAT = AudioFormat(16000, PcmFormat.S16LE, 1, 20)
CONFERENCE_FRAME_BYTES = CONFERENCE_FORMAT.nominal_frame_bytes
CONFERENCE_TICK_S = CONFERENCE_FORMAT.frame_ms / 1000.0
CONFERENCE_INACTIVITY_S = 10.0
MAX_CONFERENCE_LEGS = 8
CONFERENCE_RTP_FORMAT = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
_CONFERENCE_SILENCE = b"\x00" * CONFERENCE_FRAME_BYTES


def _silence() -> bytes:
    return _CONFERENCE_SILENCE


def mix_frames(frames: list[bytes]) -> list[bytes]:
    """Return an N-1 mix for each input 16 kHz s16le mono frame."""
    if not frames:
        return []
    normalized = [frame if len(frame) == CONFERENCE_FRAME_BYTES else _silence() for frame in frames]
    decoded = np.stack([np.frombuffer(frame, dtype="<i2") for frame in normalized]).astype(np.int32)
    mixed = decoded.sum(axis=0, dtype=np.int32)[None, :] - decoded
    peaks = np.maximum(mixed.max(axis=1), -mixed.min(axis=1) - 1)
    outputs: list[bytes] = []
    for row, peak in zip(mixed, peaks, strict=True):
        if peak > 32767:
            row = (row.astype(np.int64) * 32767) // int(peak)
        outputs.append(np.clip(row, -32768, 32767).astype("<i2").tobytes())
    return outputs


@dataclass(slots=True)
class _ConferenceLeg:
    call_id: str
    caller: str
    role: str
    remote_host: str
    remote_port: int
    in_converter: PcmFrameConverter
    out_converter: PcmFrameConverter
    endpoint_id: str = ""
    local_ports: tuple[int, int] = (0, 0)
    transport: asyncio.DatagramTransport | None = None
    decoder: RtpPayloadDecoder | None = None
    encoder: RtpPayloadEncoder | None = None
    local_out: asyncio.Queue[bytes] | None = None
    client: Any | None = None
    port_reservation: RtpPortReservation | None = None
    in_fifo: list[bytes] = field(default_factory=list)
    last_rx: float = field(default_factory=time.monotonic)
    sequence: int = field(default_factory=lambda: random.randrange(0, 0xFFFF))
    timestamp: int = field(default_factory=lambda: random.randrange(0, 0xFFFFFFFF))
    ssrc: int = field(default_factory=lambda: random.randrange(1, 0xFFFFFFFF))
    rx_ssrc: int | None = None
    rx_packets: int = 0
    tx_packets: int = 0
    dropped_frames: int = 0
    can_receive: bool = True
    can_send: bool = True
    connection_held: bool = False
    rx_suppressed: int = 0
    tx_suppressed: int = 0


class _ConferenceRtpProtocol(asyncio.DatagramProtocol):
    def __init__(self, room: "ConferenceRoom", leg_id: str) -> None:
        self.room = room
        self.leg_id = leg_id

    def datagram_received(self, data: bytes, addr) -> None:
        self.room.handle_rtp(self.leg_id, data, addr)


class ConferenceRoom:
    """One active conference focus. Endpoints call the room as a normal SIP URI."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        name: str,
        local_ip: str,
        on_inbound_timeout: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self.hass = hass
        self.name = name
        self.local_ip = local_ip
        self.legs: dict[str, _ConferenceLeg] = {}
        self._task: asyncio.Task | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed = False
        self._ha_softphone_announced: dict[str, str] = {}
        self.on_inbound_timeout = on_inbound_timeout

    async def join(
        self,
        invite: SipInvite,
        *,
        ring_endpoints: tuple[tuple[str, str], ...] = (),
    ) -> SipInviteResult:
        if len(self.legs) >= MAX_CONFERENCE_LEGS:
            return SipInviteResult(486, "Busy Here", to_tag="", decline_reason=TerminalReason.BUSY.value)
        port_reservation = RtpPortReservation.allocate(self.hass)
        local_ports = port_reservation.ports
        transport: asyncio.DatagramTransport | None = None
        leg: _ConferenceLeg | None = None
        try:
            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _ConferenceRtpProtocol(self, invite.call_id),
                local_addr=("0.0.0.0", local_ports[0]),
            )
            # Capacity can change while the socket bind yields to the event
            # loop. Recheck before publishing the leg so concurrent joins
            # cannot exceed the fixed mixer bound.
            if self._closed or (invite.call_id not in self.legs and len(self.legs) >= MAX_CONFERENCE_LEGS):
                transport.close()
                transport = None
                port_reservation.release()
                return SipInviteResult(486, "Busy Here", to_tag="", decline_reason=TerminalReason.BUSY.value)
            was_empty = not self.legs
            leg = _ConferenceLeg(
                call_id=invite.call_id,
                caller=invite.caller,
                role="owner" if was_empty else "manual",
                endpoint_id="",
                remote_host=invite.remote_rtp_host,
                remote_port=int(invite.remote_rtp_port),
                local_ports=local_ports,
                transport=transport,
                port_reservation=port_reservation,
                decoder=RtpPayloadDecoder(invite.recv_format),
                encoder=RtpPayloadEncoder(invite.send_format),
                in_converter=PcmFrameConverter(invite.recv_format.audio_format, CONFERENCE_FORMAT),
                out_converter=PcmFrameConverter(CONFERENCE_FORMAT, invite.send_format.audio_format),
                can_receive=invite.local_audio_direction in {"recvonly", "sendrecv"},
                can_send=(
                    invite.local_audio_direction in {"sendonly", "sendrecv"}
                    and not invite.remote_audio_connection_held
                ),
                connection_held=invite.remote_audio_connection_held,
            )
            self.legs[invite.call_id] = leg
            if self._task is None or self._task.done():
                self._task = self.hass.async_create_task(self._mix_loop())
            if was_empty:
                for endpoint_id, call_id in ring_endpoints:
                    self._set_softphone_ringing(
                        invite,
                        endpoint_id=endpoint_id,
                        call_id=call_id,
                    )
            if was_empty:
                self._fire("conference_started", invite.call_id, count=1)
            self._fire("conference_participant_joined", invite.call_id, caller=invite.caller, count=len(self.legs))
            answer = build_answer_directional(
                self.local_ip,
                self.local_ip,
                local_ports[0],
                invite.send_format,
                invite.recv_format,
                remote_sdp=invite.remote_sdp,
            )
            return SipInviteResult(200, "OK", answer_sdp=answer, to_tag="")
        except BaseException:
            if leg is not None and self.legs.get(invite.call_id) is leg:
                self.legs.pop(invite.call_id, None)
            if transport is not None:
                transport.close()
            port_reservation.release()
            raise

    async def add_client_leg(
        self,
        *,
        call_id: str,
        caller: str,
        client: Any,
        port_reservation: RtpPortReservation,
        role: str = "manual",
    ) -> bool:
        if self._closed:
            port_reservation.release()
            return False
        if call_id not in self.legs and len(self.legs) >= MAX_CONFERENCE_LEGS:
            port_reservation.release()
            return False
        dialog = getattr(client, "dialog", None)
        if dialog is None:
            port_reservation.release()
            return False
        local_ports = port_reservation.ports
        transport: asyncio.DatagramTransport | None = None
        leg: _ConferenceLeg | None = None
        try:
            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _ConferenceRtpProtocol(self, call_id),
                local_addr=("0.0.0.0", int(dialog.local_rtp_port)),
            )
            if self._closed or (call_id not in self.legs and len(self.legs) >= MAX_CONFERENCE_LEGS):
                transport.close()
                transport = None
                port_reservation.release()
                return False
            was_empty = not self.legs
            leg = _ConferenceLeg(
                call_id=call_id,
                caller=caller,
                role=role,
                endpoint_id="",
                remote_host=dialog.remote_rtp_host,
                remote_port=int(dialog.remote_rtp_port),
                local_ports=local_ports,
                transport=transport,
                port_reservation=port_reservation,
                decoder=RtpPayloadDecoder(dialog.recv_format),
                encoder=RtpPayloadEncoder(dialog.send_format),
                in_converter=PcmFrameConverter(dialog.recv_format.audio_format, CONFERENCE_FORMAT),
                out_converter=PcmFrameConverter(CONFERENCE_FORMAT, dialog.send_format.audio_format),
                client=client,
                can_receive=dialog.local_audio_direction in {"recvonly", "sendrecv"},
                can_send=(
                    dialog.local_audio_direction in {"sendonly", "sendrecv"}
                    and not dialog.remote_audio_connection_held
                ),
                connection_held=dialog.remote_audio_connection_held,
            )
            self.legs[call_id] = leg
            if self._task is None or self._task.done():
                self._task = self.hass.async_create_task(self._mix_loop())
            if was_empty:
                self._fire("conference_started", call_id, count=1)
            self._fire("conference_participant_joined", call_id, caller=caller, count=len(self.legs))
            return True
        except BaseException:
            if leg is not None and self.legs.get(call_id) is leg:
                self.legs.pop(call_id, None)
            if transport is not None:
                transport.close()
            port_reservation.release()
            raise

    def handle_rtp(self, call_id: str, data: bytes, addr) -> None:
        leg = self.legs.get(call_id)
        if leg is None:
            return
        if leg.decoder is None:
            return
        if not leg.can_receive:
            leg.rx_suppressed += 1
            return
        if addr[0] != leg.remote_host:
            return
        try:
            packet = parse_packet(data)
            if packet.payload_type != leg.decoder.fmt.payload_type:
                raise ValueError(
                    f"payload type {packet.payload_type} != expected {leg.decoder.fmt.payload_type}"
                )
            if leg.rx_ssrc is not None and packet.ssrc != leg.rx_ssrc:
                raise ValueError(f"SSRC {packet.ssrc} != latched {leg.rx_ssrc}")
            pcm = leg.decoder.decode(packet.payload)
            if not pcm:
                return
            if leg.rx_ssrc is None:
                leg.rx_ssrc = packet.ssrc
                leg.remote_port = int(addr[1])
            elif int(addr[1]) != leg.remote_port:
                leg.remote_port = int(addr[1])
            frames = leg.in_converter.convert(pcm)
        except Exception as err:
            _LOGGER.debug("Conference RTP frame ignored room=%s call_id=%s: %s", self.name, call_id, err)
            return
        leg.last_rx = time.monotonic()
        leg.rx_packets += 1
        for frame in frames:
            if len(leg.in_fifo) >= 3:
                leg.in_fifo.pop(0)
                leg.dropped_frames += 1
            leg.in_fifo.append(frame)

    async def leave(self, call_id: str, reason: str = "remote_hangup") -> bool:
        leg = self.legs.pop(call_id, None)
        if leg is None:
            return False
        origin = asyncio.current_task()
        cleanup = asyncio.create_task(
            self._finish_leave(leg, reason=reason, origin=origin),
            name=f"voip-conference-leave-{self.name}-{call_id}",
        )
        # A service-call or shutdown waiter may be cancelled repeatedly, but
        # ownership has already left the room. Complete teardown first so the
        # detached leg cannot become unreachable.
        await async_wait_for_cleanup(cleanup)
        return True

    async def _finish_leave(
        self,
        leg: _ConferenceLeg,
        *,
        reason: str,
        origin: asyncio.Task[Any] | None,
    ) -> None:
        await self._dispose_leg(leg, reason=reason)
        self._fire(
            "conference_participant_left",
            leg.call_id,
            caller=leg.caller,
            count=len(self.legs),
            reason=reason,
        )
        if not self.legs:
            await self.close(reason=reason, _origin=origin)

    async def close(
        self,
        reason: str = "idle",
        *,
        _origin: asyncio.Task[Any] | None = None,
    ) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(reason=reason, origin=_origin or asyncio.current_task()),
                name=f"voip-conference-close-{self.name}",
            )
        cleanup = self._close_task
        await async_wait_for_cleanup(cleanup)

    async def _close(
        self,
        *,
        reason: str,
        origin: asyncio.Task[Any] | None,
    ) -> None:
        self._closed = True
        legs = list(self.legs.values())
        self.legs.clear()
        if legs:
            await asyncio.gather(*(self._dispose_leg(leg, reason=reason) for leg in legs))
        task = self._task
        self._task = None
        if task is not None and task is not origin and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._set_softphone_idle(reason)
        self._fire("conference_ended", "", count=0, reason=reason)
        manager = conference_component(self.hass)
        if isinstance(manager, ConferenceManager) and manager.rooms.get(self.name) is self:
            manager.rooms.pop(self.name, None)

    async def _dispose_leg(self, leg: _ConferenceLeg, *, reason: str) -> None:
        # Detach the socket from the mixer immediately, but keep the reserved
        # port until dialog teardown has completed.  A late 2xx/RTP packet must
        # never land on a port already reassigned to an unrelated call.
        transport = leg.transport
        leg.transport = None
        if transport is not None:
            transport.close()
        reservation = leg.port_reservation
        leg.port_reservation = None
        client = leg.client
        leg.client = None
        if client is None:
            try:
                if (
                    reason == "media_timeout"
                    and leg.local_out is None
                    and self.on_inbound_timeout is not None
                ):
                    await self.on_inbound_timeout(leg.call_id, reason)
            except Exception:
                _LOGGER.exception(
                    "Conference inbound timeout signaling failed room=%s "
                    "call_id=%s",
                    self.name,
                    leg.call_id,
                )
            finally:
                if reservation is not None:
                    reservation.release()
            return

        async def close_client() -> None:
            if reason != "remote_hangup":
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await client.terminate()
            with contextlib.suppress(Exception, asyncio.CancelledError):
                await client.close()

        async def close_client_and_release() -> None:
            try:
                await close_client()
            finally:
                if reservation is not None:
                    reservation.release()

        cleanup = asyncio.create_task(
            close_client_and_release(),
            name=f"voip-conference-client-close-{self.name}-{leg.call_id}",
        )
        await async_wait_for_cleanup(cleanup)

    async def _mix_loop(self) -> None:
        next_deadline = time.monotonic()
        try:
            while self.legs:
                now = time.monotonic()
                if now < next_deadline:
                    await asyncio.sleep(next_deadline - now)
                else:
                    await asyncio.sleep(0)
                    if now - next_deadline > CONFERENCE_TICK_S:
                        # Never replay missed mixer ticks as a burst. Late audio
                        # is less useful than current audio in a live room.
                        next_deadline = now
                next_deadline += CONFERENCE_TICK_S
                now = time.monotonic()
                for call_id, leg in list(self.legs.items()):
                    if self._rx_inactivity_expired(leg, now):
                        await self.leave(call_id, reason="media_timeout")
                if not self.legs:
                    break
                leg_items = list(self.legs.items())
                input_frames = [leg.in_fifo.pop(0) if leg.in_fifo else _silence() for _call_id, leg in leg_items]
                for (_call_id, leg), mixed in zip(leg_items, mix_frames(input_frames), strict=True):
                    for out_frame in leg.out_converter.convert(mixed):
                        if leg.local_out is not None:
                            if leg.local_out.full():
                                with contextlib.suppress(asyncio.QueueEmpty):
                                    leg.local_out.get_nowait()
                                leg.dropped_frames += 1
                            leg.local_out.put_nowait(out_frame)
                            leg.tx_packets += 1
                            continue
                        if leg.transport is None or leg.encoder is None:
                            continue
                        if not leg.can_send or leg.connection_held:
                            leg.tx_suppressed += 1
                            leg.timestamp = next_timestamp(
                                leg.timestamp,
                                leg.encoder.fmt.rtp_timestamp_step,
                            )
                            continue
                        try:
                            payload = leg.encoder.encode(out_frame)
                            packet = RtpPacket(
                                payload_type=leg.encoder.fmt.payload_type,
                                sequence=leg.sequence,
                                timestamp=leg.timestamp,
                                ssrc=leg.ssrc,
                                payload=payload,
                            )
                            leg.transport.sendto(build_packet(packet), (leg.remote_host, leg.remote_port))
                            leg.sequence = next_sequence(leg.sequence)
                            leg.tx_packets += 1
                        except Exception as err:
                            _LOGGER.debug("Conference RTP send failed room=%s call_id=%s: %s", self.name, leg.call_id, err)
                        finally:
                            # RTP timestamps describe the media clock, not the
                            # number of successfully emitted packets. Preserve
                            # wall-clock audio time across an encode/send drop.
                            leg.timestamp = next_timestamp(
                                leg.timestamp,
                                leg.encoder.fmt.rtp_timestamp_step,
                            )
        except asyncio.CancelledError:
            raise
        finally:
            if not self._closed and not self.legs:
                await self.close(reason="idle")

    @staticmethod
    def _rx_inactivity_expired(leg: _ConferenceLeg, now: float) -> bool:
        """Apply RTP timeout only to a SIP leg expected to transmit audio.

        A local ``sendonly``/``inactive`` answer explicitly tells the peer not
        to send RTP. The browser conference leg may likewise be a deliberate
        listener with no microphone frames. Neither is evidence of a dead SIP
        dialog, and legacy c=0 hold suspends the expectation as well.
        """

        return bool(
            leg.local_out is None
            and leg.can_receive
            and not leg.connection_held
            and now - leg.last_rx > CONFERENCE_INACTIVITY_S
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "legs": len(self.legs),
            "leg_stats": {
                call_id: {
                    "caller": leg.caller,
                    "role": leg.role,
                    "rx_packets": leg.rx_packets,
                    "tx_packets": leg.tx_packets,
                    "dropped_frames": leg.dropped_frames,
                    "can_receive": leg.can_receive,
                    "can_send": leg.can_send,
                    "connection_held": leg.connection_held,
                    "rx_suppressed": leg.rx_suppressed,
                    "tx_suppressed": leg.tx_suppressed,
                }
                for call_id, leg in self.legs.items()
            },
        }

    def add_ha_softphone_leg(
        self,
        *,
        call_id: str,
        endpoint_id: str,
    ) -> asyncio.Queue[bytes] | None:
        existing = self.legs.get(call_id)
        if existing is not None and existing.local_out is not None:
            return existing.local_out
        if len(self.legs) >= MAX_CONFERENCE_LEGS:
            return None
        was_empty = not self.legs
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=8)
        self.legs[call_id] = _ConferenceLeg(
            call_id=call_id,
            caller="HA",
            role="ha",
            endpoint_id=endpoint_id,
            remote_host="",
            remote_port=0,
            in_converter=PcmFrameConverter(CONFERENCE_FORMAT, CONFERENCE_FORMAT),
            out_converter=PcmFrameConverter(CONFERENCE_FORMAT, CONFERENCE_FORMAT),
            local_out=queue,
        )
        if self._task is None or self._task.done():
            self._task = self.hass.async_create_task(self._mix_loop())
        if was_empty:
            self._fire("conference_started", call_id, count=1)
        self._fire("conference_participant_joined", call_id, caller="HA", count=len(self.legs))
        self._ha_softphone_announced[call_id] = endpoint_id
        return queue

    async def remove_ha_softphone_leg(
        self,
        call_id: str,
        *,
        reason: str = "local_hangup",
    ) -> None:
        endpoint_id = self._ha_softphone_announced.get(call_id, "")
        removed = await self.leave(call_id, reason=reason)
        if removed and not self._closed:
            self._set_softphone_idle(reason, call_id=call_id)
        elif endpoint_id:
            self._set_softphone_idle(reason, call_id=call_id)

    def push_ha_audio(self, call_id: str, pcm: bytes) -> None:
        leg = self.legs.get(call_id)
        if leg is None:
            return
        leg.last_rx = time.monotonic()
        leg.rx_packets += 1
        for frame in leg.in_converter.convert(pcm):
            if len(leg.in_fifo) >= 3:
                leg.in_fifo.pop(0)
                leg.dropped_frames += 1
            leg.in_fifo.append(frame)

    def _set_softphone_ringing(
        self,
        invite: SipInvite | None = None,
        *,
        endpoint_id: str,
        call_id: str,
        caller: str = "",
        target: str = "",
    ) -> None:
        endpoint = endpoint_directory(self.hass).get(endpoint_id)
        if endpoint is None or endpoint.kind is not EndpointKind.BROWSER:
            raise ValueError(f"endpoint {endpoint_id!r} is not a browser phone")
        self._ha_softphone_announced[call_id] = endpoint_id
        registry = call_registry(self.hass)
        session = registry.transition(
            call_id,
            state=CallState.RINGING.value,
            caller=str(caller or getattr(invite, "caller", "") or self.name),
            callee=str(target or getattr(invite, "target", "") or self.name),
        )
        if session is None:
            return
        leg_id = f"browser:{endpoint_id}"
        observe_phone_leg_projection(
            self.hass, registry, session, endpoint_id, CallState.RINGING.value,
            leg_id=leg_id,
            peer_name=session.caller, direction="incoming",
            route_kind=GROUP_TYPE_CONFERENCE, sip_status_code=180,
            last_sip_event="INVITE",
        )

    def _set_softphone_idle(
        self,
        reason: str,
        *,
        call_id: str = "",
    ) -> None:
        selected = (
            {call_id: self._ha_softphone_announced.get(call_id, "")}
            if call_id
            else dict(self._ha_softphone_announced)
        )
        endpoint_registry = endpoint_directory(self.hass)
        registry = call_registry(self.hass)
        manager = conference_component(self.hass)
        for softphone_call_id, endpoint_id in selected.items():
            if not endpoint_id:
                continue
            endpoint = endpoint_registry.get(endpoint_id)
            if endpoint is None or endpoint.kind is not EndpointKind.BROWSER:
                continue
            session = registry.get_session(softphone_call_id)
            if session is None:
                continue
            leg_id = f"browser:{endpoint_id}"
            observe_phone_leg_projection(
                self.hass, registry, session, endpoint_id, CallState.IDLE.value,
                leg_id=leg_id,
                intent=TerminationIntent(
                    reason, TerminationInitiator.RUNTIME, CallState.IDLE.value,
                ),
                peer_name=self.name, direction="incoming", reason=reason,
                route_kind=GROUP_TYPE_CONFERENCE, last_sip_event="BYE",
            )
            registry.take_media(softphone_call_id)
            EndpointTerminationHandler(self.hass).request_reason(
                softphone_call_id,
                reason,
                TerminationInitiator.RUNTIME,
            )
            self._ha_softphone_announced.pop(softphone_call_id, None)
            if isinstance(manager, ConferenceManager):
                manager.forget_ha_call(softphone_call_id)

    def _fire(self, event: str, call_id: str, **extra: Any) -> None:
        _fire_call_event(
            self.hass,
            {
                "event": event,
                "state": event,
                "scope": "conference",
                "room": self.name,
                "call_id": call_id,
                **extra,
            },
            "sip",
        )


class ConferenceManager:
    def __init__(
        self,
        hass: HomeAssistant,
        *,
        local_ip: str,
        on_inbound_timeout: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self.hass = hass
        self.local_ip = local_ip
        self.rooms: dict[str, ConferenceRoom] = {}
        self.ha_calls: dict[str, tuple[str, str]] = {}
        self._close_task: asyncio.Task[None] | None = None
        self._closing = False
        self._closed = False
        self.on_inbound_timeout = on_inbound_timeout

    def _reserve_ha_call(
        self,
        room_name: str,
        endpoint_id: str,
        *,
        call_id: str = "",
        state: str = CallState.RINGING.value,
    ) -> str:
        endpoint = browser_phone(self.hass, endpoint_id)
        if endpoint is None:
            raise ValueError("a Home Assistant phone is required")
        endpoint_id = endpoint.endpoint_id
        room_name = str(room_name or "").strip()
        registry = call_registry(self.hass)
        candidate = str(call_id or "").strip()
        if not candidate:
            candidate = f"conference:{secrets.token_hex(16)}"
        existing = self.ha_calls.get(candidate)
        if existing is not None:
            if existing != (room_name, endpoint_id):
                raise ValueError(f"conference call_id {candidate!r} is already bound")
            return candidate
        registry.upsert(
            candidate,
            state=state,
            owner="ha_softphone",
            caller=room_name,
            callee=endpoint.name,
            route_kind=GROUP_TYPE_CONFERENCE,
            endpoint_id=endpoint_id,
            conference_room=room_name,
            session_device_id=endpoint.device_id,
        )
        try:
            registry.claim_endpoint(candidate, endpoint_id, role="ha_softphone")
        except BaseException:
            EndpointTerminationHandler(self.hass).request_reason(
                candidate,
                TerminalReason.TRANSPORT_UNREACHABLE.value,
                TerminationInitiator.RUNTIME,
            )
            raise
        self.ha_calls[candidate] = (room_name, endpoint_id)
        return candidate

    def _release_ha_reservations(
        self,
        reservations: list[tuple[str, str]] | tuple[tuple[str, str], ...],
        *,
        room: ConferenceRoom | None = None,
        reason: str = TerminalReason.TRANSPORT_UNREACHABLE.value,
        state: str = CallState.TRANSPORT_UNREACHABLE.value,
    ) -> None:
        """Release provisional browser legs even when publication failed.

        Conference setup spans the endpoint registry, the call registry and
        the browser state adapter.  Keep teardown idempotent so an exception
        at any point cannot leave a logical phone busy forever.
        """
        registry = call_registry(self.hass)
        for _endpoint_id, call_id in reservations:
            registry.release_endpoint_claims(call_id)
            try:
                if room is not None and call_id in room._ha_softphone_announced:
                    room._set_softphone_idle(reason, call_id=call_id)
            except Exception:  # pragma: no cover - defensive state adapter isolation
                _LOGGER.exception(
                    "Failed to publish conference browser cleanup call_id=%s",
                    call_id,
                )
            finally:
                registry.take_media(call_id)
                EndpointTerminationHandler(self.hass).request_reason(
                    call_id,
                    reason,
                    TerminationInitiator.RUNTIME,
                )
                self.forget_ha_call(call_id)
                if room is not None:
                    room._ha_softphone_announced.pop(call_id, None)

    def resolve_ha_call(self, call_id: str) -> tuple[str, str] | None:
        return self.ha_calls.get(str(call_id or "").strip())

    def forget_ha_call(self, call_id: str) -> None:
        self.ha_calls.pop(str(call_id or "").strip(), None)

    def ring_ha_endpoints(
        self,
        room_name: str,
        endpoint_ids: tuple[str, ...],
        *,
        caller: str,
    ) -> tuple[str, ...]:
        room_key = str(room_name or "").strip()
        room = self.rooms.get(room_key)
        if room is None or room._closed:
            return ()
        endpoint_registry = endpoint_directory(self.hass)
        call_ids: list[str] = []
        reservations: list[tuple[str, str]] = []
        try:
            for endpoint_id in endpoint_ids:
                endpoint = endpoint_registry.get(endpoint_id)
                if endpoint is None or (
                    endpoint.kind is not EndpointKind.BROWSER
                    or endpoint.dnd
                    or endpoint.availability
                    is not EndpointAvailability.AVAILABLE
                ):
                    continue
                try:
                    call_id = self._reserve_ha_call(room_key, endpoint_id)
                except EndpointBusyError:
                    continue
                reservations.append((endpoint_id, call_id))
                room._set_softphone_ringing(
                    endpoint_id=endpoint_id,
                    call_id=call_id,
                    caller=caller,
                    target=room_key,
                )
                call_ids.append(call_id)
        except BaseException:
            self._release_ha_reservations(reservations, room=room)
            raise
        return tuple(call_ids)

    async def join(
        self,
        invite: SipInvite,
        entry: Any,
        *,
        ring_endpoint_ids: tuple[str, ...] = (),
    ) -> SipInviteResult:
        if self._closing or self._closed:
            return SipInviteResult(503, "Service Unavailable", to_tag="")
        room_name = str(getattr(entry, "name", "") or getattr(entry, "id", "") or invite.target)
        room = self.rooms.get(room_name)
        if room is None or room._closed:
            room = ConferenceRoom(
                self.hass,
                name=room_name,
                local_ip=self.local_ip,
                on_inbound_timeout=self.on_inbound_timeout,
            )
            self.rooms[room_name] = room
        endpoint_registry = endpoint_directory(self.hass)
        ring_endpoints: list[tuple[str, str]] = []
        try:
            for endpoint_id in ring_endpoint_ids:
                endpoint = endpoint_registry.get(endpoint_id)
                if endpoint is None or (
                    endpoint.kind is not EndpointKind.BROWSER
                    or endpoint.dnd
                    or endpoint.availability
                    is not EndpointAvailability.AVAILABLE
                ):
                    continue
                try:
                    call_id = self._reserve_ha_call(room_name, endpoint_id)
                except EndpointBusyError:
                    continue
                ring_endpoints.append((endpoint_id, call_id))
            result = await room.join(
                invite,
                ring_endpoints=tuple(ring_endpoints),
            )
        except BaseException:
            self._release_ha_reservations(ring_endpoints, room=room)
            if not room.legs and self.rooms.get(room_name) is room:
                self.rooms.pop(room_name, None)
            raise
        if result.status != 200:
            self._release_ha_reservations(
                ring_endpoints,
                room=room,
                reason=result.decline_reason
                or TerminalReason.TRANSPORT_UNREACHABLE.value,
            )
            if not room.legs and self.rooms.get(room_name) is room:
                self.rooms.pop(room_name, None)
        return result

    async def add_client_leg(
        self,
        room_name: str,
        *,
        call_id: str,
        caller: str,
        client: Any,
        port_reservation: RtpPortReservation,
        role: str = "manual",
    ) -> bool:
        if self._closing or self._closed:
            port_reservation.release()
            return False
        room_key = str(room_name or "").strip()
        room = self.rooms.get(room_key)
        if room is None or room._closed:
            if role == "auto_invited":
                port_reservation.release()
                return False
            room = ConferenceRoom(
                self.hass,
                name=room_key,
                local_ip=self.local_ip,
                on_inbound_timeout=self.on_inbound_timeout,
            )
            self.rooms[room_key] = room
        return await room.add_client_leg(
            call_id=call_id,
            caller=caller,
            client=client,
            port_reservation=port_reservation,
            role=role,
        )

    async def leave_call(self, call_id: str, reason: str = "remote_hangup") -> bool:
        for name, room in list(self.rooms.items()):
            if await room.leave(call_id, reason=reason):
                if room._closed:
                    self.rooms.pop(name, None)
                return True
        return False

    def join_ha_softphone(
        self,
        room_name: str,
        *,
        endpoint_id: str = "",
        call_id: str = "",
    ) -> tuple[str, asyncio.Queue[bytes]] | None:
        if self._closing or self._closed:
            return None
        room = self.rooms.get(str(room_name or "").strip())
        if room is None or room._closed:
            return None
        try:
            resolved_call_id = self._reserve_ha_call(
                room_name,
                endpoint_id,
                call_id=call_id,
                state=CallState.IN_CALL.value,
            )
        except EndpointBusyError:
            return None
        resolved_endpoint_id = self.ha_calls[resolved_call_id][1]
        reservation = [(resolved_endpoint_id, resolved_call_id)]
        try:
            queue = room.add_ha_softphone_leg(
                call_id=resolved_call_id,
                endpoint_id=resolved_endpoint_id,
            )
        except BaseException:
            self._release_ha_reservations(reservation, room=room)
            raise
        if queue is None:
            self._release_ha_reservations(
                reservation,
                room=room,
                reason=TerminalReason.BUSY.value,
                state=CallState.BUSY.value,
            )
            return None
        return resolved_call_id, queue

    def start_ha_softphone(
        self,
        room_name: str,
        *,
        endpoint_id: str = "",
    ) -> tuple[str, asyncio.Queue[bytes]] | None:
        if self._closing or self._closed:
            return None
        room_key = str(room_name or "").strip()
        room = self.rooms.get(room_key)
        if room is None or room._closed:
            room = ConferenceRoom(self.hass, name=room_key, local_ip=self.local_ip)
            self.rooms[room_key] = room
        try:
            joined = self.join_ha_softphone(room_key, endpoint_id=endpoint_id)
        except BaseException:
            if not room.legs and self.rooms.get(room_key) is room:
                self.rooms.pop(room_key, None)
            raise
        if joined is None and not room.legs and self.rooms.get(room_key) is room:
            self.rooms.pop(room_key, None)
        return joined

    def push_ha_audio(self, call_id: str, pcm: bytes) -> None:
        resolved = self.resolve_ha_call(call_id)
        room = self.rooms.get(resolved[0]) if resolved is not None else None
        if room is not None and not room._closed:
            room.push_ha_audio(call_id, pcm)

    async def leave_ha_softphone(
        self,
        room_name: str,
        *,
        call_id: str,
        reason: str = "local_hangup",
    ) -> None:
        room_key = str(room_name or "").strip()
        room = self.rooms.get(room_key)
        if room is not None:
            await room.remove_ha_softphone_leg(call_id, reason=reason)
            if room._closed and self.rooms.get(room_key) is room:
                self.rooms.pop(room_key, None)
        else:
            registry = call_registry(self.hass)
            registry.take_media(call_id)
            await EndpointTerminationHandler(self.hass).terminate_reason(
                call_id,
                reason,
                TerminationInitiator.LOCAL_USER,
            )
            self.forget_ha_call(call_id)

    async def decline_ha_softphone(
        self,
        call_id: str,
        endpoint_id: str,
        *,
        reason: str = TerminalReason.DECLINED.value,
    ) -> bool:
        resolved = self.resolve_ha_call(call_id)
        if resolved is None or resolved[1] != endpoint_id:
            return False
        room = self.rooms.get(resolved[0])
        if room is not None:
            room._set_softphone_idle(reason, call_id=call_id)
        else:
            await EndpointTerminationHandler(self.hass).terminate_reason(
                call_id,
                reason,
                TerminationInitiator.LOCAL_USER,
            )
            self.forget_ha_call(call_id)
        return True

    async def close(self, reason: str = "local_hangup") -> None:
        self._closing = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._close(reason=reason),
                name="voip-conference-manager-close",
            )
        cleanup = self._close_task
        await async_wait_for_cleanup(cleanup)

    async def _close(self, *, reason: str) -> None:
        try:
            rooms = list(self.rooms.values())
            self.rooms.clear()
            if rooms:
                await asyncio.gather(*(room.close(reason=reason) for room in rooms))
        finally:
            self._closed = True
            self.ha_calls.clear()

    def snapshot(self) -> dict[str, Any]:
        return {name: room.snapshot() for name, room in self.rooms.items()}


def conference_manager(
    hass: HomeAssistant,
    *,
    local_ip: str,
    on_inbound_timeout: Callable[[str, str], Awaitable[None]] | None = None,
) -> ConferenceManager:
    manager = conference_component(hass)
    if not isinstance(manager, ConferenceManager) or manager.local_ip != local_ip:
        manager = ConferenceManager(
            hass,
            local_ip=local_ip,
            on_inbound_timeout=on_inbound_timeout,
        )
        pbx_runtime = sip_endpoint_runtime(hass)
        if pbx_runtime is None:
            raise RuntimeError("VoIP Stack SIP runtime is unavailable")
        pbx_runtime.adopt_component(
            "conference_manager",
            manager,
            closer=manager.close,
        )
    elif on_inbound_timeout is not None:
        manager.on_inbound_timeout = on_inbound_timeout
        for room in manager.rooms.values():
            room.on_inbound_timeout = on_inbound_timeout
    return manager
