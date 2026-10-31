#!/usr/bin/env python3
"""Reuse the maintained JPEG/PCM peer's media helpers behind the isolated NAT.

SIPp owns SIP. This adapter only observes and produces its negotiated RTP;
it implements no registration, call state machine or signaling lifecycle.
"""

from __future__ import annotations

import argparse
from array import array
import asyncio
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import signal
import socket
import sys
import wave


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import sip_video_peer as peer
from ha_voip_lab.sip_nat_lab import CLIENT_IP, SERVER_IP, private_json


async def observe(args):
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    counters = defaultdict(int, codec="jpeg", peer_audio_mode="full_duplex",
        video_tx_required=True, video_rx_required=True, video_rx_capture_backend="rfc2435",
        video_rtcp_packet_types=[])
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stopped.set)
    sockets, tasks, processes = [], [], []
    jpeg = None
    audio = None
    timestamp_steps = Counter()
    last_audio = None

    def bind(port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        sock.bind((args.local_ip, port))
        sockets.append(sock)
        return sock

    def audio_packet(packet):
        nonlocal last_audio
        if packet.payload_type != args.receive_audio_pt or len(packet.payload) != 960:
            raise RuntimeError("Returned PCM does not match negotiated L16/48000/1/10ms")
        if last_audio is not None and packet.sequence == (last_audio.sequence + 1) & 0xFFFF:
            timestamp_steps[(packet.timestamp - last_audio.timestamp) & 0xFFFFFFFF] += 1
        last_audio = packet
        pcm = array("h")
        pcm.frombytes(packet.payload)
        pcm.byteswap()  # RFC3551 network-order L16 to the WAV little-endian container.
        audio.writeframesraw(pcm.tobytes())

    try:
        audio_socket = bind(args.local_audio_port)
        video_socket = bind(args.local_video_port)
        rtcp_socket = bind(args.local_video_port + 1)
        audio_path = args.output / "received-audio.wav"
        audio = wave.open(str(audio_path), "wb")
        audio_path.chmod(0o600)
        audio.setparams((1, 2, 48000, 0, "NONE", "not compressed"))
        video_path = args.output / "received-video.mjpeg"
        jpeg = peer._JpegRtpRecorder(str(video_path))
        video_path.chmod(0o600)
        private_json(args.output / "ready.json", {"media_sockets_bound": True,
            "audio_port": args.local_audio_port, "video_port": args.local_video_port})
        if args.remote_fields_file is not None:
            async with asyncio.timeout(8):
                while True:
                    if stopped.is_set():
                        raise asyncio.CancelledError
                    text = args.remote_fields_file.read_text() if args.remote_fields_file.exists() else ""
                    match = re.search(r"SIP_NAT_MEDIA ra=(\d+) rv=(\d+) tx=(\d+) rx=(\d+)", text)
                    if match:
                        (args.remote_audio_port, args.remote_video_port,
                            args.send_audio_pt, args.receive_audio_pt) = map(int, match.groups())
                        break
                    await asyncio.sleep(.01)
        if (any(port is None or not 1 <= port < 65535 for port in (args.remote_audio_port, args.remote_video_port))
                or any(pt is None or not 96 <= pt <= 127 for pt in (args.send_audio_pt, args.receive_audio_pt))):
            raise ValueError("SIPp did not observe valid negotiated RTP ports and payload types")
        source = await peer._start_audio_sender("", args.duration, audio_codec="l16-16k-10")
        processes.append(source)
        video = await peer._start_video_sender(codec="jpeg", destination=(args.local_ip, args.local_video_port),
            duration=args.duration, framerate=10, video_size=(160, 120))
        processes.append(video)
        workers = [
            asyncio.create_task(peer._send_audio_process(source, audio_socket, (args.remote_ip, args.remote_audio_port),
                stopped, counters, payload_type=args.send_audio_pt, frame_samples=160, frame_bytes=320, frame_ms=10)),
            asyncio.create_task(peer._receive_audio(audio_socket, stopped, counters, packet_observer=audio_packet)),
            asyncio.create_task(peer._relay_video(video_socket, (args.remote_ip, args.remote_video_port), stopped,
                counters, send_enabled=True, jpeg_recorder=jpeg)),
            asyncio.create_task(peer._receive_rtcp(rtcp_socket, stopped, counters)),
        ]
        tasks.extend(workers)
        for process in processes:
            tasks.append(asyncio.create_task(peer._drain_stderr(process, [])))
        timer = asyncio.create_task(asyncio.sleep(args.duration))
        stop_signal = asyncio.create_task(stopped.wait())
        tasks.extend((timer, stop_signal))
        done, _ = await asyncio.wait([timer, stop_signal, *workers], return_when=asyncio.FIRST_COMPLETED)
        if timer not in done and not stopped.is_set():
            for task in done:
                task.result()
            raise RuntimeError("Negotiated media ended before its observation window")
    finally:
        stopped.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for process in processes:
            await peer._stop_process(process)
        if jpeg is not None:
            jpeg.close(counters)
        if audio is not None:
            audio.close()
        for sock in sockets:
            sock.close()

    peer._validate_media_flow(counters)
    if timestamp_steps[480] <= 0 or any(step != 480 for step in timestamp_steps):
        raise RuntimeError("Returned audio does not retain a 48kHz RTP clock at 10ms")
    decoded = await asyncio.create_subprocess_exec("ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_frames,width,height", "-of", "json", str(video_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    raw, errors = await asyncio.wait_for(decoded.communicate(), 10)
    streams = json.loads(raw).get("streams", [])
    if decoded.returncode or errors or not streams or int(streams[0].get("nb_read_frames", 0)) < 3:
        raise RuntimeError("Independent FFmpeg decoding did not produce three returned JPEG frames")
    return {"passed": True, "scope": "isolated software RTP media, not physical ESP or acoustics",
        "send_audio": "L16/16000/1/10ms", "receive_audio": "L16/48000/1/10ms",
        "audio_timestamp_steps": dict(timestamp_steps), "independent_jpeg_decode": streams[0], "counters": dict(counters),
        "encoder_processes": [{"pid": process.pid, "return_code": process.returncode} for process in processes]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-ip", default=CLIENT_IP, choices=[CLIENT_IP])
    parser.add_argument("--remote-ip", default=SERVER_IP, choices=[SERVER_IP])
    parser.add_argument("--local-audio-port", type=int, default=40000)
    parser.add_argument("--local-video-port", type=int, default=40002)
    parser.add_argument("--remote-audio-port", type=int)
    parser.add_argument("--remote-video-port", type=int)
    parser.add_argument("--send-audio-pt", type=int)
    parser.add_argument("--receive-audio-pt", type=int)
    parser.add_argument("--remote-fields-file", type=Path, help="Negotiated port/PT fields emitted by SIPp from the actual INVITE")
    parser.add_argument("--duration", type=float, default=10)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 2 <= args.duration <= 12 or any(not 1 <= port < 65535 for port in (
            args.local_audio_port, args.local_video_port)):
        parser.error("Use a bounded media window and valid UDP ports")
    try:
        result = asyncio.run(observe(args))
    except BaseException as error:
        private_json(args.output / "result.json", {"passed": False, "error_type": type(error).__name__})
        raise
    private_json(args.output / "result.json", result)
    print(json.dumps({"passed": result["passed"], "jpeg_frames": result["independent_jpeg_decode"]["nb_read_frames"]}))


if __name__ == "__main__":
    main()
