"""Video I/O acquisition runs actual owners with controlled codec boundaries."""

import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("cancel", [False, True])
async def test_partial_receive_transcoder_setup_releases_port_and_codec(socket_enabled, cancel):
    from custom_components.voip_stack.video_ws_io import async_open_receive_transcoder

    acquired = asyncio.Event()
    instances = []

    class CodecBoundary:
        def __init__(self, **kwargs):
            self.port = kwargs["output_port"]
            self.close_count = 0
            instances.append(self)

        async def async_start(self):
            acquired.set()
            if cancel:
                await asyncio.Event().wait()
            raise RuntimeError("independent codec startup failure")

        async def async_close(self):
            self.close_count += 1

    session = SimpleNamespace(call_id="partial-video", recv_video_format=object())
    task = asyncio.create_task(async_open_receive_transcoder(
        object(), session, transcoder_factory=CodecBoundary, max_packets=3, max_bytes=4096
    ))
    await asyncio.wait_for(acquired.wait(), 1)
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        await task
    await asyncio.sleep(0)
    assert instances[0].close_count == 1
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", instances[0].port))


async def test_failed_rtp_acquisition_closes_both_detached_sockets(socket_enabled):
    from custom_components.voip_stack.video_ws_io import async_open_video_receiver

    rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rtp.bind(("127.0.0.1", 0))
    rtcp.bind(("127.0.0.1", 0))
    rtp.close()
    session = SimpleNamespace(rtp_socket=rtp, rtcp_socket=rtcp, local_rtp_port=0)
    with pytest.raises((OSError, ValueError)):
        await async_open_video_receiver(session, asyncio.DatagramProtocol())
    assert rtp.fileno() == rtcp.fileno() == -1
    assert session.rtp_socket is session.rtcp_socket is None


@pytest.mark.parametrize("cancel", [False, True])
async def test_session_closes_all_io_before_waiting_for_failed_codec_cleanup(monkeypatch, cancel):
    from custom_components.voip_stack import video_ws_view as view

    class TransportBoundary:
        closed = False

        def close(self):
            self.closed = True

        def get_extra_info(self, name):
            assert name == "sockname"
            return ("127.0.0.1", 49152)

    rtp, transcode, rtcp = (TransportBoundary() for _ in range(3))
    startup, cleanup, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class CodecBoundary:
        def __init__(self, **kwargs):
            pass

        async def async_start(self):
            startup.set()
            if cancel:
                await asyncio.Event().wait()
            raise RuntimeError("bounded codec startup failure")

        async def async_close(self):
            cleanup.set()
            await release.wait()

    acquired = []

    async def open_transport(factory, **kwargs):
        protocol = factory()
        transport = rtp if not acquired else transcode
        acquired.append(transport)
        return transport, protocol

    monkeypatch.setattr(asyncio.get_running_loop(), "create_datagram_endpoint", open_transport)
    monkeypatch.setattr(view, "FfmpegVideoTranscoder", CodecBoundary)
    session = SimpleNamespace(remote_rtp_host="192.0.2.1", remote_rtp_port=40000,
        remote_rtcp_host="192.0.2.1", remote_rtcp_port=40001, remote_rtcp_mux=False,
        signaling_host="192.0.2.1", rtp_socket=None, rtcp_socket=rtcp, local_rtp_port=41000,
        browser_receive_format=object(), requires_receive_transcoding=True,
        call_id="cleanup-order", recv_video_format=object())
    websocket = SimpleNamespace(send_json=AsyncMock(), close=AsyncMock())
    task = asyncio.create_task(view._run_video_session(object(), websocket, session, endpoint_id="desk"))
    try:
        await asyncio.wait_for(startup.wait(), 1)
        if cancel:
            task.cancel()
        await asyncio.wait_for(cleanup.wait(), 1)
        assert rtp.closed and rtcp.closed and transcode.closed
        assert not task.done()
    finally:
        release.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await task
