"""Prepare NAT bindings on the existing trunk call's owned media sockets."""

from __future__ import annotations

import asyncio
from ipaddress import ip_address

from .core.media_address import MediaEndpoint


def media_nat_mode(config) -> str:
    """Preserve explicitly configured legacy overrides during migration."""
    return getattr(config, "media_nat_mode", "") or (
        "manual" if config.outbound_media_address else "auto"
    )


def borrow_pending_media(client, reservation, video_relay=None) -> None:
    """Keep descriptor ownership with the existing bridge reservation/relay."""
    def sockets():
        reservation.bind_audio_sockets(ports=(client.local_rtp_port,))
        result = {"audio": reservation.audio_socket(client.local_rtp_port)}
        if video_relay is not None:
            result.update(video_relay.pending_media_sockets("right"))
        return result
    client.media_socket_provider = sockets


async def discover_pending_media(sockets, get_servers):
    from .core.media_stun import discover_media_endpoints
    mapped = await discover_media_endpoints(sockets, get_servers() if get_servers else (), timeout=3)
    if mapped.keys() != sockets.keys():
        raise OSError("Unable to discover public RTP/RTCP destinations for this trunk")
    return {key: MediaEndpoint(sock.getsockname()[1], *mapped[key]) for key, sock in sockets.items()}


async def prepare_trunk_media(client, config, remote_host: str, get_servers) -> None:
    mode = media_nat_mode(config)
    if mode == "manual":
        if not config.outbound_media_address:
            raise ValueError("Manual trunk media requires an advertised IPv4 address")
        client.media_ip = config.outbound_media_address
        return
    client.media_ip = client.local_ip
    if mode == "direct":
        return
    try:
        remote = ip_address(remote_host)
    except ValueError:
        loop = asyncio.get_running_loop()
        import socket
        resolved = await asyncio.wait_for(loop.getaddrinfo(remote_host, None, family=socket.AF_INET), 3)
        remote = ip_address(resolved[0][4][0])
    # Routed local/VPN peers keep their existing host destinations. No
    # internet service participates in an entirely private call path.
    if not remote.is_global or remote.version != 4:
        return
    from .media_ports import bind_sip_audio_socket, bind_sip_video_sockets

    if client.media_socket_provider is not None:
        sockets = client.media_socket_provider()
    else:
        sockets = {}
    if "audio" not in sockets and client.audio_rtp_socket is None:
        reservation = client.media_reservation
        if reservation is not None and client.local_rtp_port in reservation.ports:
            reservation.bind_audio_sockets(ports=(client.local_rtp_port,))
            client.audio_rtp_socket = reservation.take_audio_socket(client.local_rtp_port)
        else:
            client.audio_rtp_socket = bind_sip_audio_socket(client.local_rtp_port)
    sockets.setdefault("audio", client.audio_rtp_socket)
    if "video" not in sockets and client.local_video_rtp_port and client.video_formats:
        if client.video_rtp_socket is None:
            client.video_rtp_socket, client.video_rtcp_socket = bind_sip_video_sockets(client.local_video_rtp_port)
        sockets["video"] = client.video_rtp_socket
        sockets["video_rtcp"] = client.video_rtcp_socket
    client.media_endpoints = await discover_pending_media(sockets, get_servers)
