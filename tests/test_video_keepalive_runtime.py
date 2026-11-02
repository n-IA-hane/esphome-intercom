"""Real UDP checks for video NAT maintenance across SDP generations."""

import asyncio
import json
import socket
import subprocess
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.ha


async def test_reoffer_wakes_video_nat_refresh_without_waiting_for_period(monkeypatch, socket_enabled):
    from custom_components.voip_stack import video_ws_view as view
    from custom_components.voip_stack.core import rtp, sdp

    sessions = {}
    ended = asyncio.Event()
    store = {"call_id": "video-wakeup"}
    monkeypatch.setattr(view, "require_runtime_data", lambda _: SimpleNamespace(
        media=SimpleNamespace(sessions_for=lambda _: sessions)))
    monkeypatch.setattr(view, "call_projection", lambda _: None)
    monkeypatch.setattr(view, "_ha_softphone_store", lambda *_: store)
    monkeypatch.setattr(view, "_publish_ha_softphone_state", lambda *a, **k: None)
    monkeypatch.setattr(view, "debug_mode", lambda _: False)
    monkeypatch.setattr(view, "listen_for_media_call_end", lambda *a: (ended, lambda: None))
    monkeypatch.setattr(view, "_SYMMETRIC_RTP_KEEPALIVE_ATTEMPTS", 2)
    monkeypatch.setattr(view, "_SYMMETRIC_RTP_KEEPALIVE_INTERVAL", .01)
    monkeypatch.setattr(view, "_SYMMETRIC_RTP_REFRESH_INTERVAL", 10)

    class WebSocket:
        def __init__(self):
            self.messages = asyncio.Queue()
            self.negotiated = asyncio.Queue()

        async def send_json(self, value):
            await self.negotiated.put(value)

        async def send_bytes(self, _):
            pass

        def force_close(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self.messages.get()
            raise StopAsyncIteration

    sockets = []
    for _ in range(3):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        sockets.append(sock)
    local, first, second = sockets
    session = view._VideoMediaSession(
        call_id="video-wakeup", local_rtp_port=local.getsockname()[1],
        rtp_socket=local, remote_rtp_host="127.0.0.1",
        remote_rtp_port=first.getsockname()[1], remote_rtcp_host="127.0.0.1",
        remote_rtcp_port=first.getsockname()[1], remote_rtcp_mux=True,
        video_format=sdp.RtpVideoFormat(payload_type=103), local_direction="recvonly",
    )
    ws = WebSocket()
    task = asyncio.create_task(view._run_video_session(object(), ws, session, endpoint_id="phone"))
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(ws.negotiated.get(), 1)
        for _ in range(2):
            raw = await asyncio.wait_for(loop.sock_recv(first, 2048), 1)
            assert rtp.parse_packet(raw).payload_type != 103
        # The initial probe is finished; the production sender is waiting on
        # its long refresh period. A new SDP destination must wake it now.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(loop.sock_recv(first, 2048), .05)
        session.remote_rtp_port = second.getsockname()[1]
        session.remote_rtcp_port = second.getsockname()[1]
        session.media_generation += 1
        session.update_event.set()
        update = await asyncio.wait_for(ws.negotiated.get(), 1)
        assert update["type"] == "media_update"
        raw = await asyncio.wait_for(loop.sock_recv(second, 2048), .3)
        packet = rtp.parse_packet(raw)
        assert packet.payload_type != 103
        assert not packet.payload
    finally:
        ended.set()
        await asyncio.wait_for(task, 1)
        for sock in sockets:
            sock.close()
    assert not sessions


async def test_parameter_only_h264_packets_are_cached_without_becoming_browser_frames(monkeypatch, socket_enabled):
    from custom_components.voip_stack import video_ws_view as view
    from custom_components.voip_stack.core import rtp, sdp, video_rtp

    sessions = {}
    ended = asyncio.Event()
    store = {"call_id": "parameter-refresh"}
    monkeypatch.setattr(view, "require_runtime_data", lambda _: SimpleNamespace(
        media=SimpleNamespace(sessions_for=lambda _: sessions)))
    monkeypatch.setattr(view, "call_projection", lambda _: None)
    monkeypatch.setattr(view, "_ha_softphone_store", lambda *_: store)
    monkeypatch.setattr(view, "_publish_ha_softphone_state", lambda *a, **k: None)
    monkeypatch.setattr(view, "debug_mode", lambda _: False)
    monkeypatch.setattr(view, "listen_for_media_call_end", lambda *a: (ended, lambda: None))

    class WebSocket:
        def __init__(self):
            self.ready = asyncio.Event()
            self.frames = asyncio.Queue()

        async def send_json(self, _):
            self.ready.set()

        async def send_bytes(self, value):
            self.frames.put_nowait(value)

        def force_close(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

    sockets = []
    for _ in range(2):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        sockets.append(sock)
    local, peer = sockets
    target = local.getsockname()
    session = view._VideoMediaSession(
        call_id=store["call_id"], local_rtp_port=target[1], rtp_socket=local,
        remote_rtp_host="127.0.0.1", remote_rtp_port=peer.getsockname()[1],
        remote_rtcp_host="127.0.0.1", remote_rtcp_port=peer.getsockname()[1],
        remote_rtcp_mux=True, video_format=sdp.RtpVideoFormat(payload_type=103),
        local_direction="recvonly",
    )
    ws = WebSocket()
    task = asyncio.create_task(view._run_video_session(object(), ws, session, endpoint_id="phone"))
    loop = asyncio.get_running_loop()
    sequence = 10

    async def send(nals, timestamp):
        nonlocal sequence
        for index, nal in enumerate(nals):
            raw = rtp.build_packet(rtp.RtpPacket(103, sequence, timestamp, 123, nal,
                marker=index == len(nals) - 1))
            await loop.sock_sendto(peer, raw, target)
            sequence += 1

    sps, pps = b"\x67\x42\xe0\x1f", b"\x68\xce\x06\xe2"
    updated_sps = b"\x67\x42\xe0\x1e"
    try:
        await asyncio.wait_for(ws.ready.wait(), 1)
        await send([sps, pps, b"\x65\xaa"], 9000)
        first = await asyncio.wait_for(ws.frames.get(), 1)
        assert video_rtp.split_annex_b(first[view._VIDEO_HEADER.size:]) == [sps, pps, b"\x65\xaa"]
        await send([updated_sps, pps], 18000)
        await send([b"\x65\xbb"], 27000)
        second = await asyncio.wait_for(ws.frames.get(), 1)
        assert video_rtp.split_annex_b(second[view._VIDEO_HEADER.size:]) == [updated_sps, pps, b"\x65\xbb"]
    finally:
        ended.set()
        await asyncio.wait_for(task, 1)
        for sock in sockets:
            sock.close()
    assert not sessions
    assert store["video_access_units_rx"] == 2


async def test_cif_h264_bootstrap_replay_reaches_ws_and_independent_decoder(monkeypatch, socket_enabled, tmp_path):
    """Preserve the 425 wire shape using owned pixels, never the private camera capture."""
    from custom_components.voip_stack import video_ws_view as view
    from custom_components.voip_stack.core import rtp, sdp, video_rtp

    encoded = tmp_path / "owned-cif.h264"
    made = subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=352x288:rate=10",
        "-frames:v", "6", "-c:v", "libx264", "-profile:v", "baseline", "-level:v", "3.0",
        "-pix_fmt", "yuv420p", "-threads", "1", "-x264-params",
        "repeat-headers=1:keyint=250:min-keyint=250:scenecut=0:bframes=0:slices=1", "-f", "h264", str(encoded),
    ], capture_output=True, text=True)
    assert made.returncode == 0, made.stderr
    nals = video_rtp.split_annex_b(encoded.read_bytes())
    parameters = [n for n in nals if n[0] & 31 in {7, 8}]
    pictures = [n for n in nals if 1 <= n[0] & 31 <= 5]
    assert [n[0] & 31 for n in parameters] == [7, 8]
    assert [n[0] & 31 for n in pictures] == [5, 1, 1, 1, 1, 1]

    sessions, ended, store = {}, asyncio.Event(), {"call_id": "owned-cif-bootstrap"}
    monkeypatch.setattr(view, "require_runtime_data", lambda _: SimpleNamespace(
        media=SimpleNamespace(sessions_for=lambda _: sessions)))
    monkeypatch.setattr(view, "call_projection", lambda _: None)
    monkeypatch.setattr(view, "_ha_softphone_store", lambda *_: store)
    monkeypatch.setattr(view, "_publish_ha_softphone_state", lambda *a, **k: None)
    monkeypatch.setattr(view, "debug_mode", lambda _: False)
    monkeypatch.setattr(view, "listen_for_media_call_end", lambda *a: (ended, lambda: None))

    class WebSocket:
        def __init__(self):
            self.ready, self.frames = asyncio.Event(), asyncio.Queue()

        async def send_json(self, _):
            self.ready.set()

        async def send_bytes(self, value):
            self.frames.put_nowait(value)

        def force_close(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            await asyncio.Event().wait()

    sockets = []
    for _ in range(2):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        sockets.append(sock)
    local, peer = sockets
    target = local.getsockname()
    session = view._VideoMediaSession(
        call_id=store["call_id"], local_rtp_port=target[1], rtp_socket=local,
        remote_rtp_host="127.0.0.1", remote_rtp_port=peer.getsockname()[1],
        remote_rtcp_host="127.0.0.1", remote_rtcp_port=peer.getsockname()[1], remote_rtcp_mux=True,
        video_format=sdp.RtpVideoFormat(payload_type=103, profile_level_id="42801f", packetization_mode=1),
        local_direction="recvonly",
    )
    ws = WebSocket()
    task = asyncio.create_task(view._run_video_session(object(), ws, session, endpoint_id="phone"))
    sequence, received, fragmented_idr = 100, [], False
    loop = asyncio.get_running_loop()

    async def send(nals, timestamp):
        nonlocal sequence
        packets = video_rtp.packetize_annex_b(video_rtp.join_annex_b(nals), payload_type=103,
            sequence=sequence, timestamp=timestamp, ssrc=123, max_payload=600)
        for packet in packets:
            await loop.sock_sendto(peer, rtp.build_packet(packet), target)
        sequence = (sequence + len(packets)) & 65535
        return packets

    # Relative RTP clock from the observed stream, not a wall-clock speed test:
    # standalone SPS/PPS, an IDR, then variable-cadence delta pictures.
    timestamps = (25200, 54900, 80100, 109800, 135000, 164700)
    try:
        await asyncio.wait_for(ws.ready.wait(), 1)
        await send(parameters, 0)
        for index, (nal, timestamp) in enumerate(zip(pictures, timestamps, strict=True)):
            packets = await send([nal], timestamp)
            if index == 0:
                fragmented_idr = len(packets) > 1 and all(p.payload[0] & 31 == 28 for p in packets)
            frame = await asyncio.wait_for(ws.frames.get(), 1)
            _kind, flags, actual_timestamp = view._VIDEO_HEADER.unpack_from(frame)
            data = frame[view._VIDEO_HEADER.size:]
            assert actual_timestamp == timestamp
            assert video_rtp.split_annex_b(data) == ([*parameters, nal] if index == 0 else [nal])
            assert bool(flags & 1) is (index == 0)
            received.append(data)
    finally:
        ended.set()
        await asyncio.wait_for(task, 1)
        for sock in sockets:
            sock.close()
    assert fragmented_idr and not sessions and ws.frames.empty()
    assert store["video_access_units_rx"] == 6
    assert store["video_drop_error"] == store["video_lost_packets"] == 0
    replay = tmp_path / "received.h264"
    replay.write_bytes(b"".join(received))
    decoded = subprocess.run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
        "stream=codec_name,width,height,nb_read_frames", "-of", "json", str(replay),
    ], capture_output=True, text=True)
    assert decoded.returncode == 0, decoded.stderr
    assert json.loads(decoded.stdout)["streams"] == [
        {"codec_name": "h264", "width": 352, "height": 288, "nb_read_frames": "6"}
    ]
    assert store["video_drop_error"] == 0
