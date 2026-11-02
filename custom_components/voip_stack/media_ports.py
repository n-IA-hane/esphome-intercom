"""RTP port allocation helpers."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import socket
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from .config import transport_config
from .runtime_data import require_runtime_data

if TYPE_CHECKING:
    from .pbx_runtime import SipEndpointRuntime
from .media_reservation import (
    release_media_reservation as release_media_reservation,
    release_video_media_reservation as release_video_media_reservation,
)

RTP_RELAY_POOL_WIDTH = 400
_RTP_IP_TOS = 0xB8
_VIDEO_RTP_IP_TOS = 0x88


@dataclass(slots=True)
class RtpPortReservation:
    """Owned RTP relay ports and optional pre-bound audio sockets."""

    hass: HomeAssistant
    ports: tuple[int, int]
    released: bool = False
    _audio_sockets: tuple[socket.socket, socket.socket] | None = field(default=None, repr=False)

    @classmethod
    def allocate(cls, hass: HomeAssistant) -> "RtpPortReservation":
        return cls(hass=hass, ports=allocate_sip_rtp_port_pair(hass))

    @classmethod
    def merge(cls, hass: HomeAssistant, previous_pairs, retained_pair) -> "RtpPortReservation":
        """Retain the two bound legs after their old relay owners detach."""
        used = require_runtime_data(hass).rtp_port_pool.get("used", set())
        previous = {int(port) for pair in previous_pairs for port in pair}
        retained = tuple(int(port) for port in retained_pair)
        if len(retained) != 2 or len(set(retained)) != 2 or not set(retained) <= previous or not previous <= used:
            raise RuntimeError("RTP reservation ownership changed before merge")
        used.difference_update(previous - set(retained))
        return cls(hass=hass, ports=retained)

    @classmethod
    def adopt_bound_source(cls, hass: HomeAssistant, source_port: int) -> "RtpPortReservation":
        """Reserve a spare leg beside a browser's existing, unpooled RTP socket."""
        used = require_runtime_data(hass).rtp_port_pool.get("used", set())
        if source_port in used:
            raise RuntimeError("The source RTP port already belongs to another reservation")
        temporary = cls.allocate(hass)
        used = require_runtime_data(hass).rtp_port_pool["used"]
        used.add(source_port)
        used.remove(temporary.ports[1])
        temporary.detach()
        return cls(hass, (source_port, temporary.ports[0]))

    def detach(self) -> tuple[int, int]:
        self._close_audio_sockets()
        self.released = True
        return self.ports

    def release(self) -> None:
        self._close_audio_sockets()
        if self.released:
            return
        release_sip_rtp_port_pair(self.hass, self.ports)
        self.released = True

    def bind_audio_sockets(self) -> None:
        """Own the advertised ports before signaling, without starting media."""
        if self.released:
            raise RuntimeError("RTP reservation has already been released")
        if self._audio_sockets is not None:
            return
        sockets = []
        try:
            for port in self.ports:
                sock = bind_sip_audio_socket(port)
                sockets.append(sock)
        except BaseException:
            for sock in sockets:
                with contextlib.suppress(OSError):
                    sock.close()
            self.release()
            raise
        self._audio_sockets = (sockets[0], sockets[1])

    def transfer_audio_sockets(self, relay) -> None:
        """Relinquish the same sockets only after synchronous relay validation."""
        if self._audio_sockets is None:
            return
        relay.adopt_audio_sockets(self._audio_sockets)
        self._audio_sockets = None

    def _close_audio_sockets(self) -> None:
        sockets, self._audio_sockets = self._audio_sockets, None
        for sock in sockets or ():
            with contextlib.suppress(OSError):
                sock.close()


def reserve_delayed_offer_ports(
    hass: HomeAssistant,
    runtime: SipEndpointRuntime,
    call_id: str,
) -> RtpPortReservation:
    """Reserve the media pair advertised by one initial delayed offer."""

    artifacts = runtime.artifacts_for(call_id)
    if artifacts is None:
        raise RuntimeError("SIP call session is unavailable")
    if artifacts.delayed_offer_ports is not None:
        raise RuntimeError("SIP delayed offer already owns media ports")
    reservation = RtpPortReservation.allocate(hass)
    artifacts.delayed_offer_ports = reservation
    return reservation


def take_delayed_offer_ports(
    runtime: SipEndpointRuntime,
    call_id: str,
) -> RtpPortReservation | None:
    """Transfer the advertised media pair to the canonical route owner."""

    artifacts = runtime.artifacts_for(call_id)
    if artifacts is None:
        return None
    reservation = artifacts.delayed_offer_ports
    artifacts.delayed_offer_ports = None
    return reservation


def rtp_port_available(port: int) -> bool:
    if not 1 <= int(port) <= 65535:
        return False
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", int(port)))
        return True
    except (OSError, OverflowError):
        return False
    finally:
        sock.close()


def bind_sip_audio_socket(port: int) -> socket.socket:
    """Bind the relay's existing audio socket policy before or after signaling."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setblocking(False)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, _RTP_IP_TOS)
        sock.bind(("0.0.0.0", int(port)))
        return sock
    except BaseException:
        sock.close()
        raise


def bind_sip_rtp_socket(port: int, *, ip_tos: int = _RTP_IP_TOS) -> socket.socket:
    """Pre-bind RTP with audio EF by default and an explicit video policy."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, ip_tos)
        sock.setblocking(False)
        sock.bind(("0.0.0.0", int(port)))
        return sock
    except BaseException:
        sock.close()
        raise


def bind_sip_video_sockets(rtp_port: int) -> tuple[socket.socket, socket.socket]:
    """Reserve the advertised RTP/RTCP pair before SIP signaling completes."""

    if not 1 <= int(rtp_port) < 65535:
        raise OSError("invalid video RTP/RTCP port pair")
    rtp_socket = bind_sip_rtp_socket(int(rtp_port), ip_tos=_VIDEO_RTP_IP_TOS)
    try:
        rtcp_socket = bind_sip_rtp_socket(int(rtp_port) + 1, ip_tos=_VIDEO_RTP_IP_TOS)
    except BaseException:
        rtp_socket.close()
        raise
    return rtp_socket, rtcp_socket


def reserve_sip_video_media(
    hass: HomeAssistant,
    *,
    attempts: int = 64,
) -> tuple[RtpPortReservation, socket.socket, socket.socket]:
    """Allocate audio/video ports and pre-bind the video's RTP/RTCP pair.

    A single occupied RTCP port must not silently downgrade an otherwise
    valid call to audio-only while the bounded media pool still has room.
    """

    last_error: OSError | None = None
    for _ in range(max(1, int(attempts))):
        reservation = RtpPortReservation.allocate(hass)
        try:
            rtp_socket, rtcp_socket = bind_sip_video_sockets(reservation.ports[1])
        except OSError as err:
            last_error = err
            reservation.release()
            continue
        return reservation, rtp_socket, rtcp_socket
    raise OSError("SIP video RTP/RTCP port allocation exhausted") from last_error


def reserve_sip_video_relay_media(
    hass: HomeAssistant,
    *,
    attempts: int = 64,
) -> tuple[
    RtpPortReservation,
    tuple[socket.socket, socket.socket, socket.socket, socket.socket],
]:
    """Allocate and pre-bind RTP/RTCP sockets for both video relay legs."""

    last_error: OSError | None = None
    for _ in range(max(1, int(attempts))):
        reservation = RtpPortReservation.allocate(hass)
        sockets: list[socket.socket] = []
        try:
            sockets.extend(bind_sip_video_sockets(reservation.ports[0]))
            sockets.extend(bind_sip_video_sockets(reservation.ports[1]))
        except OSError as err:
            last_error = err
            for sock in sockets:
                sock.close()
            reservation.release()
            continue
        return reservation, (sockets[0], sockets[1], sockets[2], sockets[3])
    raise OSError("SIP video relay RTP/RTCP port allocation exhausted") from last_error


def allocate_sip_rtp_port(hass: HomeAssistant, *, step: int = 2) -> int:
    cfg = transport_config(hass)
    runtime = require_runtime_data(hass)
    base_port = int(cfg["rtp_port"])
    if rtp_port_available(base_port):
        runtime.next_rtp_port = base_port + int(step)
        return base_port
    candidate = int(
        runtime.next_rtp_port or base_port + int(step)
    )
    for _ in range(64):
        if candidate == base_port:
            candidate += int(step)
            continue
        if rtp_port_available(candidate):
            runtime.next_rtp_port = candidate + int(step)
            return candidate
        candidate += int(step)
    raise RuntimeError("SIP RTP port allocation exhausted")


def _relay_pool(hass: HomeAssistant, step: int) -> tuple[dict, int, int, int]:
    cfg = transport_config(hass)
    runtime = require_runtime_data(hass)
    base = int(cfg["rtp_port"]) + 2
    if base % 2:
        base += 1
    width = min(RTP_RELAY_POOL_WIDTH, max(0, 65534 - base + 1))
    width -= width % 2
    if width < 4:
        raise RuntimeError("SIP RTP relay port pool is too small")
    pool = runtime.rtp_port_pool
    pool.setdefault("next", base)
    pool.setdefault("used", set())
    return pool, base, base + width, int(step)


def _wrap_pool_port(port: int, base: int, end: int, step: int) -> int:
    width = end - base
    if width <= 0:
        return base
    offset = (int(port) - base) % width
    if offset % step:
        offset += step - (offset % step)
    return base + (offset % width)


def allocate_sip_rtp_port_pair(hass: HomeAssistant, *, step: int = 2) -> tuple[int, int]:
    """Allocate two RTP relay ports from the bounded HA SIP pool."""
    pool, base, end, step = _relay_pool(hass, step)
    used: set[int] = pool["used"]
    candidate = _wrap_pool_port(int(pool.get("next", base)), base, end, step)
    attempts = max(1, (end - base) // step)
    for _ in range(attempts):
        first = candidate
        second = _wrap_pool_port(candidate + step, base, end, step)
        if first not in used and second not in used and rtp_port_available(first) and rtp_port_available(second):
            used.add(first)
            used.add(second)
            pool["next"] = _wrap_pool_port(second + step, base, end, step)
            return first, second
        candidate = _wrap_pool_port(candidate + step, base, end, step)
    raise RuntimeError("SIP RTP relay port pool exhausted")


def release_sip_rtp_port_pair(hass: HomeAssistant, ports: tuple[int, int] | list[int]) -> None:
    pool = require_runtime_data(hass).rtp_port_pool
    if not isinstance(pool, dict):
        return
    used = pool.get("used")
    if not isinstance(used, set):
        return
    for port in ports:
        used.discard(int(port))
