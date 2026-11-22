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
    lab.state.route.latched_rtcp_source = ("198.51.100.2", 49998)
    lab.state.extended_sequence.observe(65535)
    lab.state.highest_sequence = 65535
    lab.state.needs_key_frame = False
    lab.state.last_keyframe_feedback = 123.0
    old_depacketizer = lab.state.depacketizer
    for reorder in (lab.state.reorder, lab.state.input_reorder):
        reorder.push(1, b"old", 0)
        reorder.push(3, b"old", 0)
        assert reorder.next_deadline is not None
    for target in (lab.queue, lab.media_queue):
        target.put_nowait((b"old", ("192.0.2.1", 40000)))
    lab.access_units.put_nowait(VideoAccessUnit(b"old", 0, True, "VP8"))
    lab.session.remote_rtp_port = 42000
    lab.session.media_generation = 1
    clock = lab.source.clock
    clock.map_browser(100, clock.origin_time)
    clock_identity = (clock.origin_time, clock.origin_timestamp, clock.clock_rate)
    sequence, ssrc = lab.source.sequence, lab.source.ssrc
    assert await updates.refresh(1)
    assert lab.state.applied_media_generation == 1
    assert lab.state.route.rtp_destination() == ("192.0.2.1", 42000)
    assert lab.state.route.latched_source is lab.state.route.latched_ssrc is None
    assert lab.state.route.latched_rtcp_source is None
    assert lab.state.route.remote_rtcp_host == "198.51.100.2"
    assert lab.state.route.remote_rtcp_port == 45000
    assert lab.state.depacketizer is not old_depacketizer
    assert lab.state.reorder.next_deadline is lab.state.input_reorder.next_deadline is None
    assert lab.state.highest_sequence == 0 and lab.state.needs_key_frame
    assert lab.state.extended_sequence.observe(1) == 1
    assert lab.state.last_keyframe_feedback == 0.0
    assert all(target.empty() for target in (lab.queue, lab.media_queue, lab.access_units))
    assert (lab.source.sequence, lab.source.ssrc) == (sequence, ssrc)
    assert (clock.origin_time, clock.origin_timestamp, clock.clock_rate) == clock_identity
    assert clock.map_browser(10, clock.origin_time + 1) == clock.current(clock.origin_time + 1)
    # Reobserving the same generation must retain newly accepted media and the
    # browser timestamp epoch, rather than resetting processing twice.
    current_frame = VideoAccessUnit(b"current", 1, True, "VP8")
    lab.access_units.put_nowait(current_frame)
    current_depacketizer = lab.state.depacketizer
    assert await updates.refresh(1)
    assert lab.access_units.get_nowait() is current_frame
    assert lab.state.depacketizer is current_depacketizer
    assert clock.map_browser(20, clock.origin_time + 2) == (
        clock.current(clock.origin_time + 1) + 10
    ) & 0xFFFFFFFF
    lab.session.local_video_format = sdp.RtpVideoFormat(payload_type=96, encoding="H263")
    lab.session.media_generation = 2
    assert not await updates.refresh(2)
    assert not await updates.refresh(2)
    assert len(lab.ws.messages) == 1
    assert lab.ws.messages[0]["restart_required"] is True
    assert lab.ws.messages[0]["restart_reason"] == "video_pipeline_changed"
    assert lab.state.counters["video_pipeline_restarts"] == 1


@pytest.mark.parametrize("limit", ["items", "bytes"])
@pytest.mark.parametrize("incoming_key_frame", [False, True], ids=["delta", "key-frame"])
async def test_h264_backpressure_discards_stale_frames_and_recovers_on_key_frame(
    processing, limit, incoming_key_frame,
):
    lab = processing
    lab.session.video_format = sdp.RtpVideoFormat(payload_type=103, encoding="H264")
    lab.state.browser_format = lab.session.browser_receive_format
    lab.state.depacketizer = view._make_video_depacketizer(
        lab.session.call_id, lab.state.browser_format, None, (),
    )
    lab.state.needs_key_frame = False
    access_units = _ByteBudgetQueue(maxsize=2 if limit == "items" else 10,
        max_bytes=4096 if limit == "items" else 12, item_size=lambda item: len(item.data))
    for timestamp in (1, 2):
        access_units.put_nowait(VideoAccessUnit(b"stale!", timestamp, False, "H264"))
    requests = []
    receiver = VideoReceiveWorkers(lab.session, lab.state, lab.queue, lab.media_queue,
        access_units, lab.closed, None, None, generation(lab).refresh, requests.append,
        lab.stored, VideoReceiveHooks(view.decode_browser_video_packet,
            view._video_loss_requires_key_frame, view._sync_video_reorder_counters), 1024, 2)

    def receive(sequence, key_frame):
        receiver.consume_ordered(rtp.RtpPacket(payload_type=103, sequence=sequence,
            timestamp=sequence * 3000, ssrc=55, marker=True,
            payload=b"\x65\xaa" if key_frame else b"\x41\xbb"), float(sequence))

    receive(1, incoming_key_frame)
    assert lab.state.counters["video_access_unit_queue_drops"] == (2 if incoming_key_frame else 3)
    assert lab.state.needs_key_frame is not incoming_key_frame
    assert access_units.qsize() == int(incoming_key_frame)
    assert requests == ([] if incoming_key_frame else [1.0])
    receive(2, False)
    assert lab.state.counters["video_drop_wait_keyframe"] == int(not incoming_key_frame)
    assert access_units.qsize() == (2 if incoming_key_frame else 0)
    receive(3, True)
    recovered = access_units.get_nowait()
    assert recovered.key_frame and recovered.timestamp == 9000
    assert recovered.data == b"\x00\x00\x00\x01\x65\xaa"
    assert access_units.empty() and not lab.state.needs_key_frame


async def test_browser_key_frame_control_uses_the_existing_feedback_callback(processing):
    from aiohttp import WSMsgType

    lab = processing
    request_key_frame = Mock()

    class BrowserControls:
        def __aiter__(self):
            return self.messages()

        async def messages(self):
            for message in ('{"type":"unrelated"}', '{"type":"request_key_frame"}'):
                yield SimpleNamespace(type=WSMsgType.TEXT, data=message)

    sender = view.BrowserVideoSender(lab.source, DatagramBoundary(), lab.rtp_lock,
        lab.state.route.rtp_destination, SimpleNamespace(time=lambda: 123.0))
    await sender.run(BrowserControls(), lab.session, closed=lab.closed,
        call_ended=asyncio.Event(), counters=lab.state.counters,
        refresh_media_state=generation(lab).refresh, request_key_frame=request_key_frame,
        store_counters=lab.stored, jpeg_normalizer=None)
    request_key_frame.assert_called_once_with(123.0)
    assert lab.state.counters["video_browser_keyframe_requests"] == 1


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


async def test_same_generation_source_change_uses_probation_and_rearms_recovery(processing):
    lab = processing
    decoded, requests = [], []

    def decode(_decoder, packet, *_args):
        decoded.append((packet.ssrc, packet.sequence))
        return VideoAccessUnit(b"frame", packet.timestamp, True, "VP8")

    receiver = VideoReceiveWorkers(lab.session, lab.state, lab.queue, lab.media_queue,
        lab.access_units, lab.closed, None, None, generation(lab).refresh,
        lambda _now: requests.append(lab.state.route.latched_ssrc), lab.stored,
        VideoReceiveHooks(decode, view._video_loss_requires_key_frame, view._sync_video_reorder_counters),
        1024, 2)
    payload_type = lab.state.browser_format.payload_type
    for ssrc, sequence, host, pt in ((99, 0, "192.0.2.99", payload_type),
        (99, 0, "192.0.2.1", 120), (55, 1, "192.0.2.1", payload_type),
        (55, 1, "192.0.2.1", payload_type), (77, 10, "192.0.2.1", payload_type),
        (77, 11, "192.0.2.1", payload_type), (55, 2, "192.0.2.1", payload_type)):
        raw = rtp.build_packet(rtp.RtpPacket(pt, sequence, sequence * 3000, ssrc, b"payload"))
        lab.media_queue.put_nowait((raw, (host, 49999)))
    task = asyncio.create_task(receiver.rtp_to_access_units())
    try:
        await until(lambda: lab.media_queue.empty() and len(decoded) == 2)
        assert decoded == [(55, 1), (77, 11)]
        assert lab.state.route.latched_ssrc == 77
        assert requests[-1] == 77
        assert lab.state.counters["video_drop_payload_type"] == 1
        assert lab.state.counters["video_duplicate_packets"] >= 1
    finally:
        lab.closed.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
