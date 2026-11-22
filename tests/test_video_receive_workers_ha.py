"""Run the extracted receive processors through their production interfaces."""

import asyncio
from dataclasses import fields
import struct
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.voip_stack.core import rtp, sdp, video_rtcp
from custom_components.voip_stack.core.video_rtp import RtpSenderState, VideoAccessUnit
from custom_components.voip_stack.video_receive import VideoReceiveHooks, VideoReceiveWorkers
from custom_components.voip_stack.video_receive_diagnostics import VideoDiagnosticHooks, VideoReceiveDiagnostics
from custom_components.voip_stack.video_receive_generation import VideoGenerationHooks, VideoGenerationUpdates
from custom_components.voip_stack.video_receive_state import VideoReceiveRoute, VideoReceiveState, video_receive_counters
from custom_components.voip_stack.video_rtcp_worker import VideoRtcpHooks, VideoRtcpWorkers
from custom_components.voip_stack.video_ws_io import _ByteBudgetQueue, _RtpVideoProtocol
from custom_components.voip_stack import video_ws_view as view

pytestmark = pytest.mark.ha


class WebSocketBoundary:
    def __init__(self):
        self.messages = []

    async def send_json(self, payload):
        self.messages.append(payload)


class DatagramBoundary:
    def __init__(self):
        self.packets = []
        self.failures = 0

    def sendto(self, packet, address):
        if self.failures:
            self.failures -= 1
            raise OSError("independent datagram send failure")
        self.packets.append((packet, address))


def queue():
    return _ByteBudgetQueue(maxsize=10, max_bytes=4096,
        item_size=lambda item: len(item.data) if isinstance(item, VideoAccessUnit) else len(item[0]))


@pytest.fixture
async def processing():
    session = view._VideoMediaSession(call_id="worker-call", local_rtp_port=41000,
        remote_rtp_host="192.0.2.1", remote_rtp_port=40000,
        remote_rtcp_host="198.51.100.2", remote_rtcp_port=45000, remote_rtcp_mux=False,
        signaling_host="192.0.2.1", video_format=sdp.RtpVideoFormat(payload_type=103, encoding="VP8"), local_direction="sendrecv")
    source = RtpSenderState.create(clock_rate=90000, now=asyncio.get_running_loop().time())
    state = VideoReceiveState(VideoReceiveRoute.from_session(session), session.browser_receive_format,
        view._make_video_depacketizer(session.call_id, session.browser_receive_format, None, ()),
        0, view._video_pipeline_signature(session), video_receive_counters(0))
    return SimpleNamespace(session=session, source=source, state=state, queue=queue(),
        media_queue=queue(), access_units=queue(), closed=asyncio.Event(), ws=WebSocketBoundary(),
        send_lock=asyncio.Lock(), rtp_lock=asyncio.Lock(), updated=asyncio.Event(), stored=Mock())


def generation(lab):
    return VideoGenerationUpdates(lab.session, lab.state, None, (), lab.queue, lab.media_queue,
        lab.access_units, lab.ws, lab.send_lock, lab.rtp_lock, lab.source.clock, lab.closed,
        lab.updated, lab.stored, VideoGenerationHooks(view._video_pipeline_signature,
            view._video_negotiation_payload, view._make_video_depacketizer))


def feedback(lab):
    worker = VideoRtcpWorkers(lab.session, lab.state, lab.source.ssrc, lab.source.clock,
        lab.ws, lab.send_lock, lab.closed, queue(), lab.stored,
        VideoRtcpHooks(video_rtcp.build_pli, video_rtcp.build_fir,
            video_rtcp.build_receiver_compound, video_rtcp.build_sender_compound, video_rtcp.parse_compound),
        asyncio.get_running_loop(), .001, 1.0, 2)
    worker.transport = DatagramBoundary()
    return worker


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(.001)


async def test_generation_resets_processing_and_announces_pipeline_restart_once(processing):
    lab = processing
    updates = generation(lab)
    lab.state.route.latched_source = ("192.0.2.1", 49999)
    lab.state.route.latched_ssrc = 55
    lab.state.extended_sequence.observe(65535)
    lab.state.highest_sequence = 65535
    lab.state.needs_key_frame = False
    for target in (lab.queue, lab.media_queue):
        target.put_nowait((b"old", ("192.0.2.1", 40000)))
    lab.access_units.put_nowait(VideoAccessUnit(b"old", 0, True, "VP8"))
    lab.session.remote_rtp_port = 42000
    lab.session.media_generation = 1
    sequence, ssrc = lab.source.sequence, lab.source.ssrc
    assert await updates.refresh(1)
    assert lab.state.applied_media_generation == 1
    assert lab.state.route.rtp_destination() == ("192.0.2.1", 42000)
    assert lab.state.route.latched_source is lab.state.route.latched_ssrc is None
    assert lab.state.highest_sequence == 0 and lab.state.needs_key_frame
    assert all(target.empty() for target in (lab.queue, lab.media_queue, lab.access_units))
    assert (lab.source.sequence, lab.source.ssrc) == (sequence, ssrc)
    lab.session.local_video_format = sdp.RtpVideoFormat(payload_type=96, encoding="H263")
    lab.session.media_generation = 2
    assert not await updates.refresh(2)
    assert not await updates.refresh(2)
    assert len(lab.ws.messages) == 1
    assert lab.ws.messages[0]["restart_required"] is True
    assert lab.ws.messages[0]["restart_reason"] == "video_pipeline_changed"
    assert lab.state.counters["video_pipeline_restarts"] == 1


@pytest.mark.parametrize("transcoding", [False, True])
async def test_receive_timeout_publishes_loss_without_requiring_a_later_packet(processing, transcoding):
    lab = processing
    if transcoding:
        lab.session.local_video_format = sdp.RtpVideoFormat(payload_type=96, encoding="H263")
    decoded, forwarded, requests = [], [], []
    def decode(_decoder, packet, *_args):
        decoded.append(packet.sequence)
        return VideoAccessUnit(bytes([packet.sequence]), packet.timestamp, True, "VP8")
    receiver = VideoReceiveWorkers(lab.session, lab.state, lab.queue, lab.media_queue,
        lab.access_units, lab.closed, None, SimpleNamespace(send_rtp=forwarded.append),
        generation(lab).refresh, requests.append, lab.stored,
        VideoReceiveHooks(decode, view._video_loss_requires_key_frame, view._sync_video_reorder_counters),
        1024, 2)
    # This test isolates reorder timing; the existing socket-gap test supplies
    # independent FFmpeg decoding for the complete codec path.
    target = lab.queue if transcoding else lab.media_queue
    payload_type = lab.session.recv_video_format.payload_type
    for sequence in (1, 3):
        packet = rtp.build_packet(rtp.RtpPacket(payload_type=payload_type, sequence=sequence,
            timestamp=sequence * 3000, ssrc=55, payload=b"negotiated payload"))
        target.put_nowait((packet, ("192.0.2.1", 49999)))
    task = asyncio.create_task(receiver.rtp_to_transcoder() if transcoding else receiver.rtp_to_access_units())
    try:
        await until(lambda: lab.state.counters["video_lost_packets"] == 1)
        assert lab.state.highest_sequence == 3
        assert lab.state.route.rtp_destination() == ("192.0.2.1", 49999)
        assert lab.state.route.remote_rtcp_host == "198.51.100.2"
        assert lab.state.route.remote_rtcp_port == 45000
        assert requests
        if transcoding:
            assert len(forwarded) == 2
        else:
            assert decoded == [1, 3]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_rtcp_send_only_report_survives_send_failure_and_uses_current_clock(processing):
    lab = processing
    worker = feedback(lab)
    worker.outbound_clock = SimpleNamespace(current=lambda _now: 0x12345678)
    worker.transport.failures = 1
    lab.state.counters.update(video_rtp_tx_packets=2, video_rtp_tx_payload_bytes=100)
    task = asyncio.create_task(worker.rtcp_reports())
    try:
        await until(lambda: bool(worker.transport.packets))
        raw, address = worker.transport.packets[0]
        report = video_rtcp.parse_compound(raw)[0]
        assert report.packet_type == 200 and report.fmt == 0
        assert struct.unpack_from("!I", report.payload, 0)[0] == worker.ssrc
        assert struct.unpack_from("!I", report.payload, 12)[0] == 0x12345678
        assert address == ("198.51.100.2", 45000)
        assert lab.state.route.latched_ssrc is None
        assert lab.state.counters["video_rtcp_send_errors"] == 1
        assert lab.state.counters["video_rtcp_tx_packets"] >= 1
        assert not task.done() and not lab.closed.is_set()
        lab.stored.assert_called()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_rtcp_feedback_latches_separate_host_only_after_valid_target(processing):
    lab = processing
    worker = feedback(lab)
    worker.rtcp_queue.put_nowait((video_rtcp.build_pli(12, worker.ssrc ^ 1), ("198.51.100.2", 46000)))
    worker.rtcp_queue.put_nowait((video_rtcp.build_pli(12, worker.ssrc), ("198.51.100.2", 47000)))
    task = asyncio.create_task(worker.rtcp_to_browser_feedback())
    try:
        await until(lambda: bool(lab.ws.messages))
        assert lab.ws.messages == [{"type": "force_key_frame", "feedback": "pli"}]
        assert lab.state.route.latched_rtcp_source == ("198.51.100.2", 47000)
        assert lab.state.route.rtp_destination() == ("192.0.2.1", 40000)
        assert lab.state.counters["video_rtcp_drop_error"] == 1
        assert lab.state.counters["video_rtcp_rx_packets"] == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_final_counter_projection_drains_rtcp_drops_without_call_events(processing):
    lab = processing
    store = {"call_id": lab.session.call_id}
    publish = Mock()
    protocols = [_RtpVideoProtocol(queue(), source_allowed=lambda _host: True, min_datagram_bytes=4) for _ in range(2)]
    protocols[1]._record_drop("queue")
    diagnostics = VideoReceiveDiagnostics(object(), "desk", lab.session, lab.state,
        *protocols, None, asyncio.get_running_loop(),
        VideoDiagnosticHooks(lambda *_args: store, lambda *_args: False, Mock(), publish,
            view._sync_video_protocol_drop_counters))
    diagnostics.store_counters(force=True)
    assert store["video_rtcp_drop_queue"] == 1
    assert store["video_rtp_dropped_packets"] == 0
    publish.assert_called_once_with(diagnostics.hass, endpoint_id="desk")
    assert "event_type" not in store and "state" not in store
    assert not any(field.name in {"tasks", "transport", "session", "generation", "token"} for field in fields(lab.state))


async def test_nonfatal_keepalive_failure_is_counted_and_keeps_the_shared_source(processing):
    from custom_components.voip_stack.video_rtp_keepalive import run_symmetric_video_keepalive

    lab = processing
    transport = DatagramBoundary()
    transport.failures = 2
    sender = SimpleNamespace(sequence=lab.source.sequence, last_regular_rtp_tx_at=0)
    observations = []
    def store():
        observations.append(lab.state.counters["video_keepalive_task_errors"])
        if len(observations) == 2:
            lab.closed.set()
    await run_symmetric_video_keepalive(lab.session, closed=lab.closed,
        keepalive_updated=lab.updated, counters=lab.state.counters, sender=sender,
        rtp_send_lock=lab.rtp_lock, rtp_source=lab.source, transport=transport,
        destination=lab.state.route.rtp_destination, store_counters=store,
        probe_interval=.001, probe_attempts=2, refresh_interval=.01)
    assert observations == [1, 2]
    assert sender.sequence == lab.source.sequence
    assert lab.source.keepalives == 2
    assert lab.state.counters["video_symmetric_rtp_keepalives"] == 0
