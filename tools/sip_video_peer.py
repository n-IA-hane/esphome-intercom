#!/usr/bin/env python3
"""Deterministic SIP audio/video caller for the local VoIP Stack lab.

This is a qualification peer, not a production softphone. It originates one
UDP SIP call, sends continuous codec-native audio plus an FFmpeg test pattern
in the requested RTP video codec, and records the real SIP/media outcome as
JSON.  The optional Dahua profile reproduces the vendor's non-standard
``PCM/16000`` little-endian media contract and User-Agent.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import sys
import time
import types


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _load(name: str):
    """Load protocol helpers without importing the Home Assistant integration."""

    if "custom_components" not in sys.modules:
        package = types.ModuleType("custom_components")
        package.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = package
    if PKG_NAME not in sys.modules:
        package = types.ModuleType(PKG_NAME)
        package.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = package
    full_name = f"{PKG_NAME}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


sip = _load("sip")
sdp = _load("sdp")
rtp = _load("rtp")
video_rtp = _load("video_rtp")
video_rtcp = _load("video_rtcp")


VIDEO_PROFILES = {
    "h264": {
        "payload_type": 102,
        "rtpmap": "H264/90000",
        "fmtp": "profile-level-id=42e01f;packetization-mode=1;level-asymmetry-allowed=1",
        "encoder": "libx264",
        # TinyH264 on ESP32-P4 decodes macroblock-aligned test frames most
        # predictably. Browser interoperability is qualified separately with
        # ordinary cropped resolutions such as 640x360.
        "size": "320x192",
        "args": (
            "-preset",
            "ultrafast",
            "-tune",
            "zerolatency",
            "-profile:v",
            "baseline",
            "-x264-params",
            "repeat-headers=1:keyint=15:min-keyint=15:scenecut=0:slices=1",
            "-threads",
            "1",
        ),
    },
    "h265": {
        "payload_type": 104,
        "rtpmap": "H265/90000",
        "fmtp": "",
        "encoder": "libx265",
        "size": "320x180",
        "args": (
            "-preset",
            "ultrafast",
            "-x265-params",
            "log-level=error:keyint=15:min-keyint=15:bframes=0:no-scenecut=1:repeat-headers=1",
        ),
    },
    "vp8": {
        "payload_type": 103,
        "rtpmap": "VP8/90000",
        "fmtp": "",
        "encoder": "libvpx",
        "size": "320x180",
        "args": ("-deadline", "realtime", "-cpu-used", "8"),
    },
    "jpeg": {
        "payload_type": 26,
        "rtpmap": "JPEG/90000",
        "fmtp": "",
        "encoder": "mjpeg",
        "size": "320x180",
        # RFC 2435 carries the standard Huffman tables implicitly. FFmpeg's
        # default MJPEG encoder optimizes them per frame, which the RTP muxer
        # correctly refuses to packetize.
        "args": (
            "-huffman",
            "default",
            "-force_duplicated_matrix",
            "1",
            "-q:v",
            "5",
        ),
    },
    "h263": {
        "payload_type": 34,
        "rtpmap": "H263/90000",
        "fmtp": "",
        "encoder": "h263",
        "size": "352x288",
        "args": (),
    },
    "h263p": {
        "payload_type": 105,
        "rtpmap": "H263-1998/90000",
        "fmtp": "",
        "encoder": "h263p",
        "size": "352x288",
        "args": (),
    },
}

AUDIO_PROFILES = {
    "pcma": {
        "payload_type": 8,
        "rtpmap": "PCMA/8000",
        "encoding": "PCMA",
        "ptime": 20,
        "sample_rate": 8000,
        "frame_samples": 160,
        "frame_bytes": 160,
        "silence": bytes([0xD5]) * 160,
        "ffmpeg_codec": "pcm_alaw",
        "ffmpeg_format": "alaw",
    },
    "dahua-pcm": {
        "payload_type": 97,
        "rtpmap": "PCM/16000",
        "encoding": "PCM",
        "ptime": 20,
        "sample_rate": 16000,
        "frame_samples": 320,
        "frame_bytes": 640,
        "silence": bytes(640),
        "ffmpeg_codec": "pcm_s16le",
        "ffmpeg_format": "s16le",
    },
    "l16-16k": {
        "payload_type": 96,
        "rtpmap": "L16/16000/1",
        "encoding": "L16",
        "ptime": 16,
        "sample_rate": 16000,
        "frame_samples": 256,
        "frame_bytes": 512,
        "silence": bytes(512),
        "ffmpeg_codec": "pcm_s16le",
        "ffmpeg_format": "s16le",
    },
}


def _local_ip(remote_host: str, remote_port: int) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((remote_host, int(remote_port)))
        return str(sock.getsockname()[0])
    finally:
        sock.close()


def _reserve_udp_socket(local_ip: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sock.bind((local_ip, 0))
    return sock


def _reserve_video_ports(
    local_ip: str,
) -> tuple[int, socket.socket, socket.socket]:
    """Reserve an even RTP port and bind its adjacent RTCP port."""

    for _ in range(128):
        probe = _reserve_udp_socket(local_ip)
        port = int(probe.getsockname()[1])
        probe.close()
        port &= ~1
        if port < 1024 or port >= 65534:
            continue
        rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtp_sock.setblocking(False)
        rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtcp.setblocking(False)
        try:
            rtp_sock.bind((local_ip, port))
            rtcp.bind((local_ip, port + 1))
        except OSError:
            rtp_sock.close()
            rtcp.close()
            continue
        return port, rtp_sock, rtcp
    raise RuntimeError("could not reserve a video RTP/RTCP pair")


def _offer(
    *,
    local_ip: str,
    audio_port: int,
    video_port: int,
    codec: str,
    direction: str,
    video_profile: str,
    audio_direction: str = "sendrecv",
    audio_codec: str = "pcma",
) -> bytes:
    audio = AUDIO_PROFILES[audio_codec]
    audio_payload_type = int(audio["payload_type"])
    lines = [
        "v=0",
        f"o=- 1 1 IN IP4 {local_ip}",
        "s=VoIP Stack video qualification peer",
        f"c=IN IP4 {local_ip}",
        "t=0 0",
        f"m=audio {audio_port} RTP/AVP {audio_payload_type} 101",
        f"a=rtpmap:{audio_payload_type} {audio['rtpmap']}",
        "a=rtpmap:101 telephone-event/8000",
        "a=fmtp:101 0-16",
        f"a=ptime:{audio['ptime']}",
        f"a={audio_direction}",
    ]
    if codec == "audio":
        return ("\r\n".join(lines) + "\r\n").encode()
    profile = VIDEO_PROFILES[codec]
    payload_type = int(profile["payload_type"])
    lines.extend(
        (
            f"m=video {video_port} {video_profile} {payload_type}",
            f"a=rtpmap:{payload_type} {profile['rtpmap']}",
        )
    )
    if profile["fmtp"]:
        lines.append(f"a=fmtp:{payload_type} {profile['fmtp']}")
    if video_profile == "RTP/AVPF" and codec in {"h264", "h265", "vp8"}:
        lines.extend(
            (
                f"a=rtcp-fb:{payload_type} nack pli",
                f"a=rtcp-fb:{payload_type} ccm fir",
            )
        )
    lines.extend((f"a=rtcp:{video_port + 1}", f"a={direction}"))
    return ("\r\n".join(lines) + "\r\n").encode()


def _request_headers(
    *,
    method: str,
    local_ip: str,
    local_port: int,
    local_user: str,
    local_display_name: str = "",
    remote_uri: str,
    call_id: str,
    local_tag: str,
    cseq: int,
    branch: str,
    remote_to: str | None = None,
    content_type: str | None = None,
    user_agent: str = "VoIP-Stack-Video-Lab/1",
) -> list[tuple[str, str]]:
    local_identity = sip.format_name_addr(
        f"sip:{local_user}@{local_ip}:{local_port}",
        local_display_name,
    )
    headers = [
        ("Via", f"SIP/2.0/UDP {local_ip}:{local_port};branch={branch};rport"),
        ("Max-Forwards", "70"),
        ("From", f"{local_identity};tag={local_tag}"),
        ("To", remote_to or f"<{remote_uri}>"),
        ("Call-ID", call_id),
        ("CSeq", f"{cseq} {method}"),
        ("Contact", f"<sip:{local_user}@{local_ip}:{local_port};transport=udp>"),
        ("Allow", "INVITE, ACK, BYE, CANCEL, INFO, OPTIONS, UPDATE"),
        ("Supported", "from-change"),
        ("User-Agent", user_agent),
    ]
    if content_type:
        headers.append(("Content-Type", content_type))
    return headers


def _response_headers(request) -> list[tuple[str, str]]:
    return [
        *(('Via', value) for value in request.header_values("Via")),
        ("From", request.header("From")),
        ("To", request.header("To")),
        ("Call-ID", request.header("Call-ID")),
        ("CSeq", request.header("CSeq")),
    ]


async def _cancel_pending_invite(
    sip_socket: socket.socket,
    destination: tuple[str, int],
    *,
    local_ip: str,
    local_port: int,
    local_user: str,
    local_display_name: str,
    remote_uri: str,
    call_id: str,
    local_tag: str,
    invite_branch: str,
    user_agent: str,
    result: dict,
) -> None:
    """Complete the client side of one pending INVITE cancellation."""

    loop = asyncio.get_running_loop()
    cancel = sip.build_request(
        "CANCEL",
        remote_uri,
        _request_headers(
            method="CANCEL",
            local_ip=local_ip,
            local_port=local_port,
            local_user=local_user,
            local_display_name=local_display_name,
            remote_uri=remote_uri,
            call_id=call_id,
            local_tag=local_tag,
            cseq=1,
            branch=invite_branch,
            user_agent=user_agent,
        ),
    )
    await loop.sock_sendto(sip_socket, cancel, destination)
    deadline = loop.time() + 3.0
    while loop.time() < deadline:
        try:
            raw, _addr = await asyncio.wait_for(
                loop.sock_recvfrom(sip_socket, 65535),
                max(0.05, deadline - loop.time()),
            )
        except TimeoutError:
            break
        message = sip.parse_message(raw)
        if message.header("Call-ID") != call_id or message.status_code is None:
            continue
        cseq = message.header("CSeq").upper()
        status = int(message.status_code)
        if cseq.endswith(" CANCEL"):
            result["cancel_response_status"] = status
        elif cseq.endswith(" INVITE") and status >= 300:
            result["cancel_invite_status"] = status
            ack = sip.build_request(
                "ACK",
                remote_uri,
                _request_headers(
                    method="ACK",
                    local_ip=local_ip,
                    local_port=local_port,
                    local_user=local_user,
                    local_display_name=local_display_name,
                    remote_uri=remote_uri,
                    remote_to=message.header("To"),
                    call_id=call_id,
                    local_tag=local_tag,
                    cseq=1,
                    branch=invite_branch,
                    user_agent=user_agent,
                ),
            )
            await loop.sock_sendto(sip_socket, ack, destination)
            result["cancel_ack_sent"] = True
        if (
            result.get("cancel_response_status") is not None
            and result.get("cancel_invite_status") is not None
        ):
            break
    result["cancel_transaction_complete"] = bool(
        result.get("cancel_response_status") is not None
        and result.get("cancel_invite_status") is not None
        and result.get("cancel_ack_sent")
    )


async def _send_audio_silence(
    sock: socket.socket,
    destination: tuple[str, int],
    stopped: asyncio.Event,
    counters: dict[str, int],
    *,
    payload_type: int,
    frame_samples: int,
    payload: bytes,
) -> None:
    """Send a 20 ms RTP clock with codec-native digital silence."""

    loop = asyncio.get_running_loop()
    sequence = secrets.randbelow(65536)
    timestamp = secrets.randbelow(2**32)
    ssrc = secrets.randbelow(2**32 - 1) + 1
    next_send = loop.time()
    while not stopped.is_set():
        await asyncio.sleep(max(0.0, next_send - loop.time()))
        packet = rtp.build_packet(
            rtp.RtpPacket(payload_type, sequence, timestamp, ssrc, payload)
        )
        await loop.sock_sendto(sock, packet, destination)
        counters["audio_tx_packets"] += 1
        sequence = (sequence + 1) & 0xFFFF
        timestamp = (timestamp + frame_samples) & 0xFFFFFFFF
        next_send += 0.020
        if next_send < loop.time():
            next_send = loop.time() + 0.020


async def _receive_audio(
    sock: socket.socket,
    stopped: asyncio.Event,
    counters: dict[str, int],
) -> None:
    loop = asyncio.get_running_loop()
    while not stopped.is_set():
        try:
            raw, _addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 4096), 0.5)
        except TimeoutError:
            continue
        try:
            packet = rtp.parse_packet(raw)
        except Exception:
            continue
        counters["audio_rx_packets"] += 1
        counters["audio_rx_bytes"] += len(packet.payload)
        counters["audio_rx_last_payload_type"] = packet.payload_type


async def _start_audio_sender(
    audio_file: str,
    duration: float,
    *,
    audio_codec: str,
):
    profile = AUDIO_PROFILES[audio_codec]
    input_args = (
        ["-stream_loop", "-1", "-i", audio_file]
        if audio_file
        else [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate={profile['sample_rate']}",
        ]
    )
    command = [
        shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-nostdin", "-re", *input_args,
        "-t", str(max(2.0, duration + 2.0)), "-vn",
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-ac", "1",
        "-ar", str(profile["sample_rate"]),
        "-c:a", str(profile["ffmpeg_codec"]),
        "-f", str(profile["ffmpeg_format"]),
        "pipe:1",
    ]
    return await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )


async def _send_audio_process(
    process: asyncio.subprocess.Process,
    sock: socket.socket,
    destination: tuple[str, int],
    stopped: asyncio.Event,
    counters: dict[str, int],
    *,
    payload_type: int,
    frame_samples: int,
    frame_bytes: int,
) -> None:
    """Packetize codec-native audio into the negotiated 20 ms RTP cadence."""
    if process.stdout is None:
        raise RuntimeError("FFmpeg audio stdout is unavailable")
    loop = asyncio.get_running_loop()
    sequence = secrets.randbelow(65536)
    timestamp = secrets.randbelow(2**32)
    ssrc = secrets.randbelow(2**32 - 1) + 1
    while not stopped.is_set():
        try:
            payload = await process.stdout.readexactly(frame_bytes)
        except asyncio.IncompleteReadError:
            break
        packet = rtp.build_packet(
            rtp.RtpPacket(
                payload_type,
                sequence,
                timestamp,
                ssrc,
                payload,
            )
        )
        await loop.sock_sendto(sock, packet, destination)
        counters["audio_tx_packets"] += 1
        sequence = (sequence + 1) & 0xFFFF
        timestamp = (timestamp + frame_samples) & 0xFFFFFFFF


async def _receive_rtcp(
    sock: socket.socket,
    stopped: asyncio.Event,
    counters: dict,
) -> None:
    loop = asyncio.get_running_loop()
    while not stopped.is_set():
        try:
            raw, _addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 4096), 0.5)
        except TimeoutError:
            continue
        counters["video_rtcp_rx_packets"] += 1
        try:
            parsed = video_rtcp.parse_compound(raw)
        except video_rtcp.RtcpError:
            counters["video_rtcp_invalid_packets"] += 1
            continue
        counters["video_rtcp_packet_types"].append(
            [[item.packet_type, item.fmt] for item in parsed]
        )


async def _relay_video(
    sock: socket.socket,
    destination: tuple[str, int],
    stopped: asyncio.Event,
    counters: dict[str, int],
    *,
    send_enabled: bool,
    capture_destination: tuple[str, int] | None = None,
    jpeg_recorder=None,
) -> None:
    """Forward generated RTP and count media returned by the HA browser.

    FFmpeg writes its generated stream to the local advertised RTP socket from
    an ephemeral source port. Packets from the negotiated HA media address are
    the opposite direction and must be counted, not reflected back into HA.
    """

    loop = asyncio.get_running_loop()
    while not stopped.is_set():
        try:
            raw, _addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 65535), 0.5)
        except TimeoutError:
            continue
        packet = rtp.parse_packet(raw)
        # The address advertised in SDP can differ from the packet source
        # address when the peer is reached through loopback, NAT or a routed
        # interface. The negotiated RTP source port still identifies the HA
        # return leg in this single-call qualification peer. Comparing the
        # full tuple made legitimate browser video look like local FFmpeg
        # input and reflected it back to HA during loopback tests.
        if int(_addr[1]) == int(destination[1]):
            counters["video_rx_packets"] += 1
            counters["video_rx_bytes"] += len(raw)
            counters["video_rx_last_sequence"] = packet.sequence
            if jpeg_recorder is not None:
                jpeg_recorder.push(packet, counters)
            if capture_destination is not None:
                await loop.sock_sendto(sock, raw, capture_destination)
            continue
        if not send_enabled:
            continue
        counters["video_tx_packets"] += 1
        counters["video_tx_bytes"] += len(raw)
        counters["video_last_sequence"] = packet.sequence
        await loop.sock_sendto(sock, raw, destination)


class _JpegRtpRecorder:
    """Record complete RFC 2435 access units without a second UDP/FFmpeg leg."""

    def __init__(self, output: str, *, payload_type: int = 26) -> None:
        self._payload_type = int(payload_type)
        self._depacketizer = video_rtp.JpegDepacketizer()
        self._stream = Path(output).open("wb")

    def push(self, packet, counters: dict[str, int]) -> None:
        if int(packet.payload_type) != self._payload_type:
            counters["video_rx_capture_invalid_payloads"] += 1
            return
        if packet.marker:
            counters["video_rx_marker_packets"] += 1
        access_unit = self._depacketizer.push(packet)
        if access_unit is None:
            return
        self._stream.write(access_unit.data)
        counters["video_rx_frames"] += 1
        counters["video_rx_frame_bytes"] += len(access_unit.data)

    def close(self, counters: dict[str, int]) -> None:
        self._stream.close()
        counters["video_rx_dropped_access_units"] = (
            self._depacketizer.dropped_access_units
        )


async def _drain_stderr(
    process: asyncio.subprocess.Process,
    tail: list[str],
) -> None:
    if process.stderr is None:
        return
    while line := await process.stderr.readline():
        tail.append(line.decode(errors="replace").rstrip())
        del tail[:-30]


async def _start_video_sender(
    *,
    codec: str,
    destination: tuple[str, int],
    duration: float,
    video_file: str = "",
):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required for the video qualification peer")
    profile = VIDEO_PROFILES[codec]
    source = (
        ["-stream_loop", "-1", "-i", video_file]
        if video_file
        else ["-f", "lavfi", "-i", f"testsrc2=size={profile['size']}:rate=15"]
    )
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-re",
        *source,
        "-t",
        str(max(2.0, duration + 2.0)),
        "-an",
        "-c:v",
        str(profile["encoder"]),
        *profile["args"],
        "-pix_fmt",
        "yuv420p",
        "-g",
        "15",
        "-f",
        "rtp",
        "-payload_type",
        str(profile["payload_type"]),
        f"rtp://{destination[0]}:{destination[1]}?pkt_size=1200",
    ]
    return await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )


async def _start_video_receiver(
    *, codec: str, local_ip: str, local_port: int, output: str,
):
    """Record the browser's returned RTP stream without sharing its SIP socket."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required to record returned video")
    profile = VIDEO_PROFILES[codec]
    sdp_lines = [
        "v=0",
        f"o=- 0 0 IN IP4 {local_ip}",
        "s=VoIP Stack camera capture",
        f"c=IN IP4 {local_ip}",
        "t=0 0",
        f"m=video {local_port} RTP/AVP {profile['payload_type']}",
        f"a=rtpmap:{profile['payload_type']} {profile['rtpmap']}",
    ]
    if profile["fmtp"]:
        sdp_lines.append(f"a=fmtp:{profile['payload_type']} {profile['fmtp']}")
    sdp_lines.extend(("a=recvonly", ""))
    sdp_text = "\n".join(sdp_lines)
    return await asyncio.create_subprocess_exec(
        ffmpeg,
        "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-protocol_whitelist", "pipe,udp,rtp",
        "-f", "sdp", "-i", "pipe:0",
        "-an", "-c:v", "copy", "-y", output,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    ), sdp_text


async def async_main(args: argparse.Namespace) -> int:
    add_video_mid_dialog = args.add_video_after >= 0
    if add_video_mid_dialog and args.codec == "audio":
        raise ValueError("--add-video-after requires a video codec")
    initial_codec = "audio" if add_video_mid_dialog else args.codec
    audio_profile = AUDIO_PROFILES[args.audio_codec]
    peer_user_agent = (
        args.user_agent
        or ("Dahua UAC/3.0" if args.audio_codec == "dahua-pcm" else "")
        or "VoIP-Stack-Video-Lab/1"
    )
    local_ip = args.local_ip or _local_ip(args.host, args.port)
    sip_socket = _reserve_udp_socket(local_ip)
    audio_socket = _reserve_udp_socket(local_ip)
    if args.codec == "audio":
        video_port, video_socket, rtcp_socket = 0, None, None
    else:
        video_port, video_socket, rtcp_socket = _reserve_video_ports(local_ip)
    loop = asyncio.get_running_loop()
    sip_port = int(sip_socket.getsockname()[1])
    audio_port = int(audio_socket.getsockname()[1])
    call_id = f"video-lab-{secrets.token_hex(10)}@{local_ip}"
    local_tag = secrets.token_hex(8)
    remote_uri = f"sip:{args.target}@{args.host}:{args.port};transport=udp"
    invite_branch = f"z9hG4bK{secrets.token_hex(8)}"
    invite = sip.build_request(
        "INVITE",
        remote_uri,
        _request_headers(
            method="INVITE",
            local_ip=local_ip,
            local_port=sip_port,
            local_user=args.user,
            local_display_name=args.display_name,
            remote_uri=remote_uri,
            call_id=call_id,
            local_tag=local_tag,
            cseq=1,
            branch=invite_branch,
            content_type="application/sdp",
            user_agent=peer_user_agent,
        ),
        _offer(
            local_ip=local_ip,
            audio_port=audio_port,
            video_port=video_port,
            codec=initial_codec,
            direction=args.direction,
            video_profile=args.video_profile,
            audio_codec=args.audio_codec,
        ),
    )
    result: dict = {
        "ok": False,
        "codec": args.codec,
        "audio_codec": args.audio_codec,
        "user_agent": peer_user_agent,
        "local_display_name": args.display_name,
        "initial_codec": initial_codec,
        "add_video_after": args.add_video_after,
        "audio_hold_after": args.audio_hold_after,
        "audio_hold_seconds": args.audio_hold_seconds,
        "video_profile": args.video_profile,
        "call_id": call_id,
        "local_sip": f"{local_ip}:{sip_port}",
        "local_audio_rtp": audio_port,
        "local_video_rtp": video_port,
        "sip_statuses": [],
        "audio_tx_packets": 0,
        "audio_rx_packets": 0,
        "audio_rx_bytes": 0,
        "audio_rx_last_payload_type": None,
        "video_tx_packets": 0,
        "video_tx_bytes": 0,
        "video_rx_packets": 0,
        "video_rx_bytes": 0,
        "video_rx_frames": 0,
        "video_rx_frame_bytes": 0,
        "video_rx_marker_packets": 0,
        "video_rx_dropped_access_units": 0,
        "video_rx_capture_invalid_payloads": 0,
        "video_rtcp_rx_packets": 0,
        "video_rtcp_invalid_packets": 0,
        "video_rtcp_packet_types": [],
        "bye_response_status": None,
        "cancel_response_status": None,
        "cancel_invite_status": None,
        "cancel_ack_sent": False,
        "cancel_transaction_complete": False,
        "connected_identity_update_received": False,
        "connected_identity_name": "",
        "connected_identity_uri": "",
    }
    video_process = None
    video_receiver_process = None
    jpeg_recorder = None
    audio_process = None
    tasks: list[asyncio.Task] = []
    stopped = asyncio.Event()
    answered = False
    remote_bye = False
    bye_sent = False
    remote_to = ""
    next_cseq = 2
    started = time.monotonic()
    audio_stderr: list[str] = []
    video_receiver_stderr: list[str] = []
    try:
        await loop.sock_sendto(sip_socket, invite, (args.host, args.port))
        response = None
        try:
            async with asyncio.timeout(args.answer_timeout):
                while response is None:
                    # Keep one receive coroutine alive. Repeatedly cancelling
                    # sock_recvfrom on a timer boundary can race a final 200
                    # OK on the same event-loop turn.
                    raw, _addr = await loop.sock_recvfrom(sip_socket, 65535)
                    message = sip.parse_message(raw)
                    if message.status_code is None or message.header("Call-ID") != call_id:
                        continue
                    result["sip_statuses"].append(int(message.status_code))
                    print(f"SIP {message.status_code} {message.reason}", flush=True)
                    if message.status_code >= 300:
                        # RFC 3261 17.1.1.3: every non-2xx final INVITE
                        # response is ACKed inside the INVITE transaction.
                        # Without this, a teardown/redial stress failure leaves
                        # the DUT retransmitting that response for 64*T1 and
                        # contaminates every following iteration.
                        failure_ack = sip.build_request(
                            "ACK",
                            remote_uri,
                            _request_headers(
                                method="ACK",
                                local_ip=local_ip,
                                local_port=sip_port,
                                local_user=args.user,
                                local_display_name=args.display_name,
                                remote_uri=remote_uri,
                                remote_to=message.header("To"),
                                call_id=call_id,
                                local_tag=local_tag,
                                cseq=1,
                                branch=invite_branch,
                                user_agent=peer_user_agent,
                            ),
                        )
                        await loop.sock_sendto(
                            sip_socket, failure_ack, (args.host, args.port)
                        )
                        result["failure_ack_sent"] = True
                        raise RuntimeError(
                            f"SIP call failed: {message.status_code} {message.reason}"
                        )
                    if 200 <= message.status_code < 300:
                        response = message
        except TimeoutError as err:
            raise TimeoutError("SIP call was not answered") from err
        if response is None:
            raise TimeoutError("SIP call was not answered")
        answered = True
        remote_to = response.header("To")
        result["answer_cseq"] = response.header("CSeq")
        result["answer_sdp"] = response.body.decode(errors="replace")

        answer_audio = sdp.parse_sdp(response.body)
        answer_audio_formats = sdp.offered_pcm_formats(
            response.body,
            allow_dahua_pcm=args.audio_codec == "dahua-pcm",
        )
        if not answer_audio_formats:
            raise RuntimeError("HA answered without a supported audio format")
        selected_audio = answer_audio_formats[0]
        expected_audio = str(audio_profile["encoding"])
        if selected_audio.encoding != expected_audio:
            raise RuntimeError(
                "HA selected unexpected audio format: "
                f"{selected_audio.encoding}, expected {expected_audio}"
            )
        result["negotiated_audio"] = selected_audio.wire_token()
        answer_video = sdp.offered_video_formats(response.body)
        if (
            args.codec != "audio"
            and not add_video_mid_dialog
            and not answer_video
            and not args.allow_audio_only
        ):
            raise RuntimeError("HA answered without an active video media section")
        parsed_video = sdp.parse_video_sdp(response.body) if answer_video else None
        result["remote_audio_rtp"] = int(answer_audio["media_port"])
        if answer_video and parsed_video is not None:
            selected_video = answer_video[0]
            result.update(
                {
                    "negotiated_video": selected_video.wire_token(),
                    "answer_video_direction": selected_video.direction,
                    "remote_video_rtp": int(parsed_video["media_port"]),
                }
            )
        else:
            result["negotiated_video"] = ""
            result["answer_video_direction"] = "inactive"
            result["remote_video_rtp"] = 0
        ack = sip.build_request(
            "ACK",
            remote_uri,
            _request_headers(
                method="ACK",
                local_ip=local_ip,
                local_port=sip_port,
                local_user=args.user,
                local_display_name=args.display_name,
                remote_uri=remote_uri,
                remote_to=remote_to,
                call_id=call_id,
                local_tag=local_tag,
                cseq=1,
                branch=f"z9hG4bK{secrets.token_hex(8)}",
                user_agent=peer_user_agent,
            ),
        )
        await loop.sock_sendto(sip_socket, ack, (args.host, args.port))

        async def _handle_connected_identity_update(message, addr) -> bool:
            if message.method != "UPDATE":
                return False
            response = sip.build_response(
                200,
                "OK",
                [
                    *_response_headers(message),
                    (
                        "Contact",
                        f"<sip:{args.user}@{local_ip}:{sip_port};transport=udp>",
                    ),
                    ("Supported", "from-change"),
                ],
            )
            await loop.sock_sendto(sip_socket, response, addr)
            result["connected_identity_update_received"] = True
            result["connected_identity_name"] = sip.name_addr_display_name(
                message.header("From")
            )
            try:
                result["connected_identity_uri"] = str(
                    sip.parse_sip_uri(message.header("From"))
                )
            except (TypeError, ValueError, sip.SipError):
                result["connected_identity_uri"] = ""
            return True

        audio_destination = (str(answer_audio["connection_ip"]), int(answer_audio["media_port"]))
        audio_process = await _start_audio_sender(
            args.audio_file,
            args.duration,
            audio_codec=args.audio_codec,
        )
        tasks = [
            asyncio.create_task(
                _send_audio_process(
                    audio_process,
                    audio_socket,
                    audio_destination,
                    stopped,
                    result,
                    payload_type=selected_audio.payload_type,
                    frame_samples=int(audio_profile["frame_samples"]),
                    frame_bytes=int(audio_profile["frame_bytes"]),
                )
            ),
            asyncio.create_task(_receive_audio(audio_socket, stopped, result)),
            asyncio.create_task(_drain_stderr(audio_process, audio_stderr)),
        ]
        video_stderr: list[str] = []

        async def _start_negotiated_video(parsed: dict | None) -> None:
            nonlocal video_process, video_receiver_process, jpeg_recorder
            if parsed is None or video_socket is None or rtcp_socket is None:
                return
            capture_destination = None
            if args.video_rx_file:
                if args.codec == "jpeg":
                    if jpeg_recorder is not None:
                        raise RuntimeError("JPEG recorder already active")
                    jpeg_recorder = _JpegRtpRecorder(args.video_rx_file)
                    result["video_rx_capture_backend"] = "rfc2435"
                else:
                    capture_probe = _reserve_udp_socket(local_ip)
                    capture_port = int(capture_probe.getsockname()[1])
                    capture_probe.close()
                    video_receiver_process, capture_sdp = await _start_video_receiver(
                        codec=args.codec,
                        local_ip=local_ip,
                        local_port=capture_port,
                        output=args.video_rx_file,
                    )
                    assert video_receiver_process.stdin is not None
                    video_receiver_process.stdin.write(capture_sdp.encode())
                    await video_receiver_process.stdin.drain()
                    video_receiver_process.stdin.close()
                    capture_destination = (local_ip, capture_port)
                    tasks.append(
                        asyncio.create_task(
                            _drain_stderr(
                                video_receiver_process,
                                video_receiver_stderr,
                            )
                        )
                    )
            tasks.append(
                asyncio.create_task(_receive_rtcp(rtcp_socket, stopped, result))
            )
            tasks.append(
                asyncio.create_task(
                    _relay_video(
                        video_socket,
                        (
                            str(parsed["connection_ip"]),
                            int(parsed["media_port"]),
                        ),
                        stopped,
                        result,
                        send_enabled=args.direction in {"sendonly", "sendrecv"},
                        capture_destination=capture_destination,
                        jpeg_recorder=jpeg_recorder,
                    )
                )
            )
            if args.direction in {"sendonly", "sendrecv"}:
                video_process = await _start_video_sender(
                    codec=args.codec,
                    destination=(local_ip, video_port),
                    duration=args.duration,
                    video_file=args.video_file,
                )
                tasks.append(
                    asyncio.create_task(
                        _drain_stderr(video_process, video_stderr)
                    )
                )

        await _start_negotiated_video(parsed_video)

        call_deadline = loop.time() + args.duration
        reinvite_at = loop.time() + max(0.0, args.add_video_after)
        reinvite_done = not add_video_mid_dialog
        hold_enabled = args.audio_hold_after >= 0
        hold_at = loop.time() + max(0.0, args.audio_hold_after)
        resume_at = float("inf")
        hold_done = not hold_enabled
        resume_done = not hold_enabled

        async def _add_video() -> None:
            nonlocal next_cseq, remote_bye
            branch = f"z9hG4bK{secrets.token_hex(8)}"
            request = sip.build_request(
                "INVITE",
                remote_uri,
                _request_headers(
                    method="INVITE",
                    local_ip=local_ip,
                    local_port=sip_port,
                    local_user=args.user,
                    local_display_name=args.display_name,
                    remote_uri=remote_uri,
                    remote_to=remote_to,
                    call_id=call_id,
                    local_tag=local_tag,
                    cseq=next_cseq,
                    branch=branch,
                    content_type="application/sdp",
                    user_agent=peer_user_agent,
                ),
                _offer(
                    local_ip=local_ip,
                    audio_port=audio_port,
                    video_port=video_port,
                    codec=args.codec,
                    direction=args.direction,
                    video_profile=args.video_profile,
                    audio_codec=args.audio_codec,
                ),
            )
            await loop.sock_sendto(sip_socket, request, (args.host, args.port))
            statuses: list[int] = []
            final_response = None
            async with asyncio.timeout(args.answer_timeout):
                while final_response is None:
                    raw, addr = await loop.sock_recvfrom(sip_socket, 65535)
                    message = sip.parse_message(raw)
                    if message.header("Call-ID") != call_id:
                        continue
                    if await _handle_connected_identity_update(message, addr):
                        continue
                    if message.method == "BYE":
                        await loop.sock_sendto(
                            sip_socket,
                            sip.build_response(
                                200,
                                "OK",
                                _response_headers(message),
                            ),
                            addr,
                        )
                        remote_bye = True
                        return
                    if message.status_code is None:
                        continue
                    if message.header("CSeq") != f"{next_cseq} INVITE":
                        continue
                    status = int(message.status_code)
                    statuses.append(status)
                    print(f"re-INVITE SIP {status} {message.reason}", flush=True)
                    if status >= 200:
                        final_response = message
            result["reinvite_statuses"] = statuses
            result["reinvite_status"] = int(final_response.status_code)
            result["reinvite_answer_sdp"] = final_response.body.decode(
                errors="replace"
            )
            ack = sip.build_request(
                "ACK",
                remote_uri,
                _request_headers(
                    method="ACK",
                    local_ip=local_ip,
                    local_port=sip_port,
                    local_user=args.user,
                    local_display_name=args.display_name,
                    remote_uri=remote_uri,
                    remote_to=final_response.header("To") or remote_to,
                    call_id=call_id,
                    local_tag=local_tag,
                    cseq=next_cseq,
                    branch=branch,
                    user_agent=peer_user_agent,
                ),
            )
            await loop.sock_sendto(sip_socket, ack, (args.host, args.port))
            next_cseq += 1
            expected = int(args.expect_reinvite_status or 0)
            if expected and int(final_response.status_code) != expected:
                raise RuntimeError(
                    "unexpected re-INVITE response: "
                    f"{final_response.status_code}, expected {expected}"
                )
            if int(final_response.status_code) >= 300:
                result["reinvite_negotiated_video"] = ""
                result["reinvite_video_direction"] = "inactive"
                result["reinvite_remote_video_rtp"] = 0
                return
            formats = sdp.offered_video_formats(final_response.body)
            parsed = sdp.parse_video_sdp(final_response.body) if formats else None
            if not formats or parsed is None:
                result["reinvite_negotiated_video"] = ""
                result["reinvite_video_direction"] = "inactive"
                result["reinvite_remote_video_rtp"] = 0
                if not args.allow_audio_only:
                    raise RuntimeError(
                        "HA accepted the re-INVITE without active video"
                    )
                return
            selected = formats[0]
            result["reinvite_negotiated_video"] = selected.wire_token()
            result["reinvite_video_direction"] = selected.direction
            result["reinvite_remote_video_rtp"] = int(parsed["media_port"])
            await _start_negotiated_video(parsed)

        async def _change_audio_direction(direction: str, prefix: str) -> None:
            nonlocal next_cseq, remote_bye
            branch = f"z9hG4bK{secrets.token_hex(8)}"
            request = sip.build_request(
                "INVITE",
                remote_uri,
                _request_headers(
                    method="INVITE",
                    local_ip=local_ip,
                    local_port=sip_port,
                    local_user=args.user,
                    local_display_name=args.display_name,
                    remote_uri=remote_uri,
                    remote_to=remote_to,
                    call_id=call_id,
                    local_tag=local_tag,
                    cseq=next_cseq,
                    branch=branch,
                    content_type="application/sdp",
                    user_agent=peer_user_agent,
                ),
                _offer(
                    local_ip=local_ip,
                    audio_port=audio_port,
                    video_port=0,
                    codec="audio",
                    direction=args.direction,
                    video_profile=args.video_profile,
                    audio_direction=direction,
                    audio_codec=args.audio_codec,
                ),
            )
            await loop.sock_sendto(sip_socket, request, (args.host, args.port))
            statuses: list[int] = []
            final_response = None
            async with asyncio.timeout(args.answer_timeout):
                while final_response is None:
                    raw, addr = await loop.sock_recvfrom(sip_socket, 65535)
                    message = sip.parse_message(raw)
                    if message.header("Call-ID") != call_id:
                        continue
                    if await _handle_connected_identity_update(message, addr):
                        continue
                    if message.method == "BYE":
                        await loop.sock_sendto(
                            sip_socket,
                            sip.build_response(200, "OK", _response_headers(message)),
                            addr,
                        )
                        remote_bye = True
                        return
                    if message.status_code is None:
                        continue
                    if message.header("CSeq") != f"{next_cseq} INVITE":
                        continue
                    status = int(message.status_code)
                    statuses.append(status)
                    print(
                        f"{prefix} re-INVITE SIP {status} {message.reason}",
                        flush=True,
                    )
                    if status >= 200:
                        final_response = message
            result[f"{prefix}_statuses"] = statuses
            result[f"{prefix}_status"] = int(final_response.status_code)
            result[f"{prefix}_answer_sdp"] = final_response.body.decode(
                errors="replace"
            )
            ack = sip.build_request(
                "ACK",
                remote_uri,
                _request_headers(
                    method="ACK",
                    local_ip=local_ip,
                    local_port=sip_port,
                    local_user=args.user,
                    local_display_name=args.display_name,
                    remote_uri=remote_uri,
                    remote_to=final_response.header("To") or remote_to,
                    call_id=call_id,
                    local_tag=local_tag,
                    cseq=next_cseq,
                    branch=branch,
                    user_agent=peer_user_agent,
                ),
            )
            await loop.sock_sendto(sip_socket, ack, (args.host, args.port))
            next_cseq += 1
            if int(final_response.status_code) >= 300:
                raise RuntimeError(
                    f"{prefix} re-INVITE failed: {final_response.status_code}"
                )
            result[f"{prefix}_answer_direction"] = str(
                sdp.parse_sdp(final_response.body)["direction"]
            )

        while loop.time() < call_deadline:
            if not reinvite_done and loop.time() >= reinvite_at:
                await _add_video()
                reinvite_done = True
                if remote_bye:
                    break
            if not hold_done and loop.time() >= hold_at:
                await _change_audio_direction("sendonly", "hold")
                hold_done = True
                resume_at = loop.time() + args.audio_hold_seconds
                if remote_bye:
                    break
            if hold_done and not resume_done and loop.time() >= resume_at:
                await _change_audio_direction("sendrecv", "resume")
                resume_done = True
                if remote_bye:
                    break
            if (
                video_process is not None
                and video_process.returncode is not None
                and video_process.returncode != 0
            ):
                raise RuntimeError(
                    f"FFmpeg video sender failed ({video_process.returncode}): "
                    + "\n".join(video_stderr)
                )
            try:
                raw, addr = await asyncio.wait_for(
                    loop.sock_recvfrom(sip_socket, 65535),
                    min(0.5, max(0.05, call_deadline - loop.time())),
                )
            except TimeoutError:
                continue
            message = sip.parse_message(raw)
            if message.header("Call-ID") != call_id:
                continue
            if await _handle_connected_identity_update(message, addr):
                continue
            if message.method == "BYE":
                await loop.sock_sendto(
                    sip_socket,
                    sip.build_response(200, "OK", _response_headers(message)),
                    addr,
                )
                remote_bye = True
                break
        result["remote_bye"] = remote_bye

        if not remote_bye:
            bye = sip.build_request(
                "BYE",
                remote_uri,
                _request_headers(
                    method="BYE",
                    local_ip=local_ip,
                    local_port=sip_port,
                    local_user=args.user,
                    local_display_name=args.display_name,
                    remote_uri=remote_uri,
                    remote_to=remote_to,
                    call_id=call_id,
                    local_tag=local_tag,
                    cseq=next_cseq,
                    branch=f"z9hG4bK{secrets.token_hex(8)}",
                    user_agent=peer_user_agent,
                ),
            )
            await loop.sock_sendto(sip_socket, bye, (args.host, args.port))
            bye_sent = True
            try:
                async with asyncio.timeout(3.0):
                    while result["bye_response_status"] is None:
                        raw, addr = await loop.sock_recvfrom(sip_socket, 65535)
                        message = sip.parse_message(raw)
                        if message.header("Call-ID") != call_id:
                            continue
                        if await _handle_connected_identity_update(message, addr):
                            continue
                        if message.method == "BYE":
                            # A simultaneous remote hangup is a separate
                            # transaction. Acknowledge it, then keep waiting
                            # for the final response to our own BYE.
                            await loop.sock_sendto(
                                sip_socket,
                                sip.build_response(
                                    200, "OK", _response_headers(message)
                                ),
                                addr,
                            )
                            remote_bye = True
                            continue
                        if (
                            message.status_code is not None
                            and message.header("CSeq").upper().endswith(" BYE")
                        ):
                            result["bye_response_status"] = int(
                                message.status_code
                            )
            except TimeoutError as err:
                raise TimeoutError("SIP BYE was not acknowledged") from err
            if not 200 <= int(result["bye_response_status"]) < 300:
                raise RuntimeError(
                    "SIP BYE failed: "
                    f"{result['bye_response_status']} {message.reason}"
                )
        if not reinvite_done:
            raise RuntimeError("call ended before the video re-INVITE was sent")
        if not hold_done or not resume_done:
            raise RuntimeError("call ended before the audio hold/resume cycle completed")
        if result["audio_tx_packets"] <= 0 or result["audio_rx_packets"] <= 0:
            raise RuntimeError(
                "established dialog did not retain bidirectional audio RTP"
            )
        expected_connected_name = str(args.expect_connected_name or "").strip()
        if expected_connected_name and (
            result["connected_identity_name"] != expected_connected_name
        ):
            raise RuntimeError(
                "unexpected connected identity: "
                f"{result['connected_identity_name']!r}, "
                f"expected {expected_connected_name!r}"
            )
        result["ok"] = True
    except BaseException as err:
        result["error"] = f"{type(err).__name__}: {err}"
        raise
    finally:
        stopped.set()
        if not answered and not any(
            int(status) >= 200 for status in result.get("sip_statuses", [])
        ):
            with contextlib.suppress(OSError):
                await _cancel_pending_invite(
                    sip_socket,
                    (args.host, args.port),
                    local_ip=local_ip,
                    local_port=sip_port,
                    local_user=args.user,
                    local_display_name=args.display_name,
                    remote_uri=remote_uri,
                    call_id=call_id,
                    local_tag=local_tag,
                    invite_branch=invite_branch,
                    user_agent=peer_user_agent,
                    result=result,
                )
        elif answered and not remote_bye and not bye_sent and remote_to:
            bye = sip.build_request(
                "BYE",
                remote_uri,
                _request_headers(
                    method="BYE",
                    local_ip=local_ip,
                    local_port=sip_port,
                    local_user=args.user,
                    local_display_name=args.display_name,
                    remote_uri=remote_uri,
                    remote_to=remote_to,
                    call_id=call_id,
                    local_tag=local_tag,
                    cseq=next_cseq,
                    branch=f"z9hG4bK{secrets.token_hex(8)}",
                    user_agent=peer_user_agent,
                ),
            )
            with contextlib.suppress(OSError):
                await loop.sock_sendto(sip_socket, bye, (args.host, args.port))
                await asyncio.sleep(0.05)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if video_process is not None and video_process.returncode is None:
            video_process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(video_process.wait(), 2.0)
            if video_process.returncode is None:
                video_process.kill()
                await video_process.wait()
        if video_process is not None:
            result["video_sender_returncode"] = video_process.returncode
        if audio_process is not None and audio_process.returncode is None:
            audio_process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(audio_process.wait(), 2.0)
            if audio_process.returncode is None:
                audio_process.kill()
                await audio_process.wait()
        if audio_process is not None:
            result["audio_sender_returncode"] = audio_process.returncode
        if video_receiver_process is not None and video_receiver_process.returncode is None:
            video_receiver_process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(video_receiver_process.wait(), 2.0)
            if video_receiver_process.returncode is None:
                video_receiver_process.kill()
                await video_receiver_process.wait()
        if video_receiver_process is not None:
            result["video_receiver_returncode"] = video_receiver_process.returncode
            result["video_rx_file"] = args.video_rx_file
            if video_receiver_stderr:
                result["video_receiver_stderr_tail"] = video_receiver_stderr
        if jpeg_recorder is not None:
            jpeg_recorder.close(result)
            result["video_rx_file"] = args.video_rx_file
        if audio_stderr:
            result["audio_sender_stderr_tail"] = list(audio_stderr)
        if "video_stderr" in locals() and video_stderr:
            result["video_sender_stderr_tail"] = list(video_stderr)
        result["elapsed_s"] = round(time.monotonic() - started, 3)
        Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False))
        sip_socket.close()
        audio_socket.close()
        if video_socket is not None:
            video_socket.close()
        if rtcp_socket is not None:
            rtcp_socket.close()
        print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=15060)
    parser.add_argument(
        "--target",
        default=os.environ.get("SIP_VIDEO_TARGET", "2600"),
        help="SIP destination; defaults to the stable HA lab extension 2600",
    )
    parser.add_argument("--user", default="video-lab-peer")
    parser.add_argument(
        "--display-name",
        default="",
        help="standards-based display name carried in the SIP From header",
    )
    parser.add_argument(
        "--expect-connected-name",
        default="",
        help="require this RFC 4916 connected identity after answer",
    )
    parser.add_argument(
        "--user-agent",
        default="",
        help=(
            "SIP User-Agent; defaults to Dahua UAC/3.0 for --audio-codec "
            "dahua-pcm and to the qualification peer identity otherwise"
        ),
    )
    parser.add_argument("--local-ip", default="")
    parser.add_argument(
        "--audio-codec",
        choices=tuple(AUDIO_PROFILES),
        default="pcma",
        help="audio wire profile used by the simulated SIP peer",
    )
    parser.add_argument("--codec", choices=("audio", *sorted(VIDEO_PROFILES)), required=True)
    parser.add_argument(
        "--direction",
        choices=("sendonly", "recvonly", "sendrecv"),
        default="sendonly",
        help="SDP direction advertised by the qualification caller",
    )
    parser.add_argument(
        "--video-profile",
        choices=("RTP/AVP", "RTP/AVPF"),
        default="RTP/AVP",
        help="video RTP profile; feedback attributes are emitted only for AVPF",
    )
    parser.add_argument("--answer-timeout", type=float, default=60.0)
    parser.add_argument(
        "--allow-audio-only",
        action="store_true",
        help="accept an audio-only answer when an offered video codec is rejected",
    )
    parser.add_argument(
        "--add-video-after",
        type=float,
        default=-1,
        help=(
            "start audio-only, then add the selected video codec with an "
            "in-dialog re-INVITE after this many seconds"
        ),
    )
    parser.add_argument(
        "--expect-reinvite-status",
        type=int,
        choices=(200, 488),
        default=0,
        help="require this final response to the video-adding re-INVITE",
    )
    parser.add_argument(
        "--audio-hold-after",
        type=float,
        default=-1,
        help="send an audio sendonly hold re-INVITE after this many seconds",
    )
    parser.add_argument(
        "--audio-hold-seconds",
        type=float,
        default=2,
        help="resume audio with sendrecv after this many seconds on hold",
    )
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument(
        "--audio-file",
        default="",
        help="optional looping audio source; defaults to a generated 440 Hz qualification tone",
    )
    parser.add_argument("--video-file", default="")
    parser.add_argument(
        "--video-rx-file", default="",
        help="record video returned by the HA browser to this media file",
    )
    parser.add_argument("--out", default="/tmp/sip_video_peer.json")
    args = parser.parse_args()
    if args.audio_hold_after >= 0 and (
        args.codec != "audio" or args.add_video_after >= 0
    ):
        parser.error("audio hold qualification requires --codec audio without video re-INVITE")
    if args.audio_hold_seconds <= 0:
        parser.error("--audio-hold-seconds must be greater than zero")
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
