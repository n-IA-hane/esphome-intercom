"""Real UDP checks for video NAT maintenance across SDP generations."""

import asyncio
import socket
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
    assert store["video_drop_error"] == 0
