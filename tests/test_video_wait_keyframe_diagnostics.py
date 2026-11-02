"""Distinguish invalid RTP from valid H264 pictures waiting for an IDR."""

import asyncio
import logging
import socket
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("waiting_pictures,recover", [(0, False), (2, False), (2, True)])
async def test_parse_error_and_keyframe_wait_have_separate_diagnostics(
    monkeypatch, socket_enabled, caplog, waiting_pictures, recover,
):
    from custom_components.voip_stack import video_ws_view as view
    from custom_components.voip_stack.core import rtp, sdp, video_rtp
    from custom_components.voip_stack import websocket_api

    sessions, ended, store = {}, asyncio.Event(), {"call_id": "keyframe-diagnostics"}
    monkeypatch.setattr(view, "require_runtime_data", lambda _: SimpleNamespace(
        media=SimpleNamespace(sessions_for=lambda _: sessions)))
    monkeypatch.setattr(view, "call_projection", lambda _: None)
    monkeypatch.setattr(view, "_ha_softphone_store", lambda *_: store)
    monkeypatch.setattr(view, "_publish_ha_softphone_state", lambda *a, **k: None)
    monkeypatch.setattr(view, "debug_mode", lambda _: False)
    monkeypatch.setattr(view, "listen_for_media_call_end", lambda *a: (ended, lambda: None))
    caplog.set_level(logging.INFO, logger=view.__name__)

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

    processed = 0
    observed = asyncio.Event()
    parse = rtp.parse_packet

    def observe_parse(data):
        nonlocal processed
        try:
            return parse(data)
        finally:
            processed += 1
            observed.set()

    monkeypatch.setattr(rtp, "parse_packet", observe_parse)
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
    try:
        await asyncio.wait_for(ws.ready.wait(), 1)
        # Invalid RTP version, independently of H264 picture/keyframe parsing.
        await loop.sock_sendto(peer, bytes.fromhex("c06700010000000100000001"), target)
        for index in range(waiting_pictures):
            raw = rtp.build_packet(rtp.RtpPacket(
                103, 10 + index, 9000 * (index + 1), 123, b"\x41\xaa", marker=True,
            ))
            await loop.sock_sendto(peer, raw, target)
        if recover:
            raw = rtp.build_packet(rtp.RtpPacket(
                103, 10 + waiting_pictures, 27000, 123, b"\x65\xbb", marker=True,
            ))
            await loop.sock_sendto(peer, raw, target)
            frame = await asyncio.wait_for(ws.frames.get(), 1)
            assert view._VIDEO_HEADER.unpack_from(frame) == (view._VIDEO_ACCESS_UNIT, 1, 27000)
            assert video_rtp.split_annex_b(frame[view._VIDEO_HEADER.size:]) == [b"\x65\xbb"]
        expected = 1 + waiting_pictures + int(recover)
        while processed < expected:
            observed.clear()
            if processed < expected:
                await asyncio.wait_for(observed.wait(), 1)
    finally:
        ended.set()
        await asyncio.wait_for(task, 1)
        for sock in sockets:
            sock.close()

    assert not sessions
    assert store["video_drop_error"] == 1 + waiting_pictures
    assert store.get("video_drop_wait_keyframe") == waiting_pictures
    assert store["video_access_units_rx"] == int(recover)
    assert "video_drop_wait_keyframe" in websocket_api._MEDIA_COUNTER_KEYS
    waiting_logs = [record.message for record in caplog.records
                    if "video waiting for key frame" in record.message]
    assert len(waiting_logs) == int(waiting_pictures > 0)
    if waiting_logs:
        assert "call_id=keyframe-diagnostics" in waiting_logs[0]
        assert "codec=H264" in waiting_logs[0] and "payload_type=103" in waiting_logs[0]
        assert "key_frame=False" in waiting_logs[0] and "nal_types=(1,)" in waiting_logs[0]
