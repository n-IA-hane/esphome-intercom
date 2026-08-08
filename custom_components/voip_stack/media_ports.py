"""RTP port allocation helpers."""

from __future__ import annotations

from dataclasses import dataclass
import socket

from homeassistant.core import HomeAssistant

from .config import transport_config
from .runtime_data import require_runtime_data
from .media_reservation import (
    release_media_reservation as release_media_reservation,
    release_video_media_reservation as release_video_media_reservation,
)

RTP_RELAY_POOL_WIDTH = 400


@dataclass(slots=True)
class RtpPortReservation:
    """Owned RTP relay port pair that must be released or detached."""

    hass: HomeAssistant
    ports: tuple[int, int]
    released: bool = False

    @classmethod
    def allocate(cls, hass: HomeAssistant) -> "RtpPortReservation":
        return cls(hass=hass, ports=allocate_sip_rtp_port_pair(hass))

    def detach(self) -> tuple[int, int]:
        self.released = True
        return self.ports

    def release(self) -> None:
        if self.released:
            return
        release_sip_rtp_port_pair(self.hass, self.ports)
        self.released = True


def reserve_delayed_offer_ports(
    hass: HomeAssistant,
    call_id: str,
) -> RtpPortReservation:
    """Reserve the media pair advertised by one initial delayed offer."""

    runtime = require_runtime_data(hass).sip
    artifacts = runtime.artifacts_for(call_id) if runtime is not None else None
    if artifacts is None:
        raise RuntimeError("SIP call session is unavailable")
    if artifacts.delayed_offer_ports is not None:
        raise RuntimeError("SIP delayed offer already owns media ports")
    reservation = RtpPortReservation.allocate(hass)
    artifacts.delayed_offer_ports = reservation
    return reservation


def take_delayed_offer_ports(
    hass: HomeAssistant,
    call_id: str,
) -> RtpPortReservation | None:
    """Transfer the advertised media pair to the canonical route owner."""

    runtime = require_runtime_data(hass).sip
    artifacts = runtime.artifacts_for(call_id) if runtime is not None else None
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


def bind_sip_rtp_socket(port: int) -> socket.socket:
    """Bind RTP before signaling so an immediate video IDR is retained."""

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
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
    rtp_socket = bind_sip_rtp_socket(int(rtp_port))
    try:
        rtcp_socket = bind_sip_rtp_socket(int(rtp_port) + 1)
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
