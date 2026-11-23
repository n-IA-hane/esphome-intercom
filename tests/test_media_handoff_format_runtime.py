"""Actual receive handoff and browser concealment retain the PCM contract."""

import asyncio
from collections import defaultdict
import socket
from types import SimpleNamespace

import numpy as np
import pytest

from .voip_phase1_support import _load_audio_ws_runtime_module, _load_intercom_module, audio_format, audio_pcm, rtp, sdp

frames = _load_intercom_module("audio_ws_frames")
receiver_module = _load_intercom_module("rtp_audio_receiver")
handoff_module = _load_intercom_module("rtp_audio_handoff")


@pytest.mark.parametrize("mode", ["deferred-reordered", "no-deferred", "same-quantum", "full-quantum", "normalized"])
def test_native_pcm_handoff_keeps_deferred_partial_quantum(mode):
    fmt = sdp.RtpPcmFormat(96, "L16", 48000, 1, 10, max_frame_ms=20)
    target = audio_format.AudioFormat(8000, "s16le", 1, 10 if mode == "same-quantum" else 20)
    receiver = receiver_module.RtpAudioReceiver(fmt)
    queue = asyncio.Queue()
    source = np.rint(3277 * np.sin(2 * np.pi * 600 * np.arange(480) / 48000)).astype("<i2")

    def packet(sequence):
        return rtp.RtpPacket(96, sequence, (sequence - 1) * 480, 0x123456, source.astype(">i2").tobytes())

    try:
        # The actual receiver releases packet three only when its missing
        # predecessor arrives. The deferred batch is what the WS owner queues.
        assert len(receiver.decode_rtp(packet(1), normalize_frames=mode == "normalized")) == 1
        assert receiver.decode_rtp(packet(3), normalize_frames=mode == "normalized") == []
        reordered = receiver.decode_rtp(packet(2), normalize_frames=mode == "normalized")
        assert reordered == [source.tobytes()] * 2
        ready = reordered if mode == "full-quantum" else reordered[:1]
        if mode != "no-deferred":
            queue.put_nowait((frames.DeferredAudioFrames(receiver, tuple(ready), 0), None))
        state = handoff_module.RtpAudioHandoff(receiver, None, None, None, None, "127.0.0.1", 40000, queue)
        output = state.retarget_receiver(target)
        assert queue.empty()
        assert receiver.output_pcm == target
        assert receiver.receive_state.highest == 3
        if mode != "no-deferred":
            for sequence in range(4, 15):
                output.extend(receiver.decode_rtp(packet(sequence), normalize_frames=True))
            assert output and all(len(pcm) == target.nominal_frame_bytes for pcm in output)
            values = np.frombuffer(b"".join(output), dtype="<i2") / 32768
            assert np.max(np.abs(values)) <= 0.105
            stable = values[-target.sample_rate // 25:]
            phase = 2 * np.pi * 600 * np.arange(len(stable)) / target.sample_rate
            amplitude = 2 * abs(np.sum(stable * np.exp(1j * phase))) / len(stable)
            assert abs(amplitude - 0.1) < 0.004
    finally:
        receiver.close()


def _encode_independent(values, pcm_format):
    bits = 16 if pcm_format == "s16le" else 32 if pcm_format == "s32le" else 24
    integer = np.rint(values * (1 << (bits - 1))).astype("<i4")
    if pcm_format == "s16le":
        return integer.astype("<i2").tobytes()
    if pcm_format == "s24le":
        return b"".join(int(value).to_bytes(3, "little", signed=True) for value in integer.reshape(-1))
    return integer.tobytes()


def test_deferred_twenty_ms_packet_retires_under_ten_ms_receive_preference():
    """maxptime admits a whole 20 ms RTP packet under a 10 ms preference."""
    fmt = sdp.RtpPcmFormat(96, "L16", 48000, 1, 10, max_frame_ms=20)
    receiver = receiver_module.RtpAudioReceiver(fmt)
    queue = asyncio.Queue()
    source = np.rint(3277 * np.sin(2 * np.pi * 600 * np.arange(960) / 48000)).astype("<i2")
    try:
        decoded = receiver.decode_rtp(rtp.RtpPacket(96, 1, 0, 123,
            source.astype(">i2").tobytes()))
        assert decoded == [source.tobytes()]
        queue.put_nowait((frames.DeferredAudioFrames(receiver, tuple(decoded), 0), None))
        state = handoff_module.RtpAudioHandoff(receiver, None, None, None, None, "127.0.0.1", 40000, queue)
        target = audio_format.AudioFormat(8000, "s16le", 1, 20)
        converted = state.retarget_receiver(target)
        assert len(converted) == 1 and len(converted[0]) == target.nominal_frame_bytes
        assert not receiver.has_pending_pcm and queue.empty()
    finally:
        receiver.close()


@pytest.mark.parametrize("cancel", [False, True])
def test_failed_receive_retarget_preserves_decoded_queue_partial_pcm_and_filter(monkeypatch, cancel):
    fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
    output = audio_format.AudioFormat(48000, "s16le", 1, 10)
    receiver = receiver_module.RtpAudioReceiver(fmt, output_pcm=output)
    reference = receiver_module.RtpAudioReceiver(fmt, output_pcm=output)
    queue = asyncio.Queue()
    state = handoff_module.RtpAudioHandoff(receiver, None, None, None, None, "127.0.0.1", 40000, queue)
    pcm = np.full(160, 1234, dtype=">i2").tobytes()
    try:
        for current in (receiver, reference):
            current.decode_rtp(rtp.RtpPacket(96, 1, 0, 123, pcm * 2), normalize_frames=True)
            assert current.decode_rtp(rtp.RtpPacket(96, 2, 320, 123, pcm), normalize_frames=True) == []
        queued = (frames.DeferredAudioFrames(receiver, (bytes(output.nominal_frame_bytes),), 0), None)
        raw = (b"not-yet-decoded", ("127.0.0.1", 40000))
        queue.put_nowait(queued)
        queue.put_nowait(raw)
        converter, pending = receiver._converter, bytes(receiver._pending)
        original_convert = audio_pcm.PcmFrameConverter.convert
        calls = 0
        def fail_after_one_conversion(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                if cancel:
                    raise asyncio.CancelledError()
                raise RuntimeError("injected prepared conversion failure")
            return original_convert(self, *args, **kwargs)
        with monkeypatch.context() as patcher:
            patcher.setattr(audio_pcm.PcmFrameConverter, "convert", fail_after_one_conversion)
            with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
                state.retarget_receiver(audio_format.AudioFormat(8000, "s16le", 1, 20))
        assert receiver.output_pcm == output and receiver._converter is converter
        assert bytes(receiver._pending) == pending
        assert queue.qsize() == 2 and queue.get_nowait() is queued and queue.get_nowait() is raw
        assert state.pending_packets == ()
        packet = rtp.RtpPacket(96, 3, 480, 123, pcm)
        assert receiver.decode_rtp(packet, normalize_frames=True) == reference.decode_rtp(packet, normalize_frames=True)
    finally:
        receiver.close()
        reference.close()


def _decode_independent(data, fmt):
    bits = 16 if fmt.pcm_format == "s16le" else 32 if fmt.pcm_format == "s32le" else 24
    width = fmt.container_bytes_per_sample
    return np.array([int.from_bytes(data[offset:offset + width], "little", signed=True)
                     for offset in range(0, len(data), width)]).reshape(-1, fmt.channels) / (1 << (bits - 1))


@pytest.mark.parametrize("pcm_format", ["s16le", "s24le", "s24le_in_s32", "s32le"])
@pytest.mark.parametrize("channels", [1, 2])
def test_browser_playout_plc_and_recovery_follow_real_sample_format(pcm_format, channels):
    fmt = audio_format.AudioFormat(48000, pcm_format, channels, 10)
    values = np.tile(np.array([0.1, -0.06])[:channels], (fmt.nominal_frame_samples, 1))
    pcm = _encode_independent(values, pcm_format)
    values = _decode_independent(pcm, fmt)
    counters = defaultdict(int)
    playout = frames.BrowserPcmPlayout(fmt, counters)
    for _ in range(playout.target_frames):
        playout.append(pcm)
    for _ in range(playout.target_frames):
        assert playout.next_frame(fmt.sample_rate) == pcm
    concealed = playout.next_frame(fmt.sample_rate)
    assert len(concealed) == fmt.nominal_frame_bytes
    actual = _decode_independent(concealed, fmt)
    expected = values[-1] * (1 - (np.arange(fmt.nominal_frame_samples) + 1)[:, None] / fmt.nominal_frame_samples)
    np.testing.assert_allclose(actual, expected, atol=1 / 32768)
    assert np.max(np.abs(actual)) <= 0.1 + 1 / 32768
    assert np.max(np.abs(actual[0] - values[-1])) < 0.001
    assert not np.any(actual[-1])
    assert playout.next_frame(fmt.sample_rate) == bytes(fmt.nominal_frame_bytes)
    playout.append(pcm)
    recovered = playout.next_frame(fmt.sample_rate)
    assert len(recovered) == fmt.nominal_frame_bytes
    actual = _decode_independent(recovered, fmt)
    fade = fmt.sample_rate * 2 // 1000
    expected = values.copy()
    expected[:fade] *= (np.arange(fade) + 1)[:, None] / fade
    np.testing.assert_allclose(actual, expected, atol=1 / 32768)
    assert np.max(np.abs(actual[0])) < 0.002
    assert recovered[fade * channels * fmt.container_bytes_per_sample:] == pcm[fade * channels * fmt.container_bytes_per_sample:]
    assert counters["tx_playout_plc"] == 2


@pytest.mark.parametrize("failure", ["prepare", "cancel", "protocol"])
async def test_failed_browser_handoff_keeps_live_transport_and_original_owner(monkeypatch, failure):
    from .test_dahua_audio_socket import _Browser

    view = _load_audio_ws_runtime_module()
    browser = _Browser()
    peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    peer.bind(("127.0.0.1", 0))
    peer.setblocking(False)
    bound = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    bound.bind(("127.0.0.1", 0))
    port = bound.getsockname()[1]
    sessions = {}
    hass = SimpleNamespace(store={"call_id": "handoff-failure"})
    monkeypatch.setattr(view, "media_capture_enabled", lambda _: False)
    monkeypatch.setattr(view, "debug_mode", lambda _: False)
    monkeypatch.setattr(view, "_ha_softphone_store", lambda *_args: hass.store)
    monkeypatch.setattr(view, "_publish_ha_softphone_state", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(view, "publish_media_route", lambda *_args: None)
    monkeypatch.setattr(view, "_listen_for_call_end", lambda *_args: (asyncio.Event(), lambda: None))
    monkeypatch.setattr(view, "require_runtime_data", lambda _: SimpleNamespace(
        sip=None, media=SimpleNamespace(sessions_for=lambda _: sessions)))
    fmt = sdp.RtpPcmFormat(96, "L16", 48000, 1, 10)
    session = view._SoftphoneMediaSession(call_id="handoff-failure", local_rtp_port=port,
        remote_rtp_host="127.0.0.1", remote_rtp_port=peer.getsockname()[1],
        send_format=fmt, recv_format=fmt, rtp_socket=bound)
    handoff_requested = asyncio.Event()
    worker = asyncio.create_task(view._run_audio_session(hass, browser, session,
        endpoint_id="default", handoff_requested=handoff_requested))
    transport = None
    try:
        async with asyncio.timeout(2):
            while session.handoff_transport is None:
                await asyncio.sleep(0)
        state = session.audio_state
        original_protocol = None

        def fail(owned_transport, _state):
            nonlocal transport, original_protocol
            transport = owned_transport
            original_protocol = transport.get_protocol()
            if failure == "protocol":
                transport.set_protocol(asyncio.DatagramProtocol())
            if failure == "cancel":
                raise asyncio.CancelledError()
            raise RuntimeError("injected handoff preparation failure")

        with pytest.raises(asyncio.CancelledError if failure == "cancel" else RuntimeError):
            session.handoff_transport(fail)
        assert not state.transferred
        assert not handoff_requested.is_set()
        assert session.audio_state is state
        assert not transport.is_closing()
        assert transport.get_protocol() is original_protocol
        session.validate_handoff()
        data = np.full(480, 1234, dtype=">i2").tobytes()
        await asyncio.get_running_loop().sock_sendto(peer, rtp.build_packet(
            rtp.RtpPacket(96, 10, 4800, 0x123456, data)), ("127.0.0.1", port))
        async with asyncio.timeout(2):
            delivered = await browser.audio.get()
        assert delivered[1:] == np.full(480, 1234, dtype="<i2").tobytes()
        assert not worker.done()
    finally:
        # The original owner must close on normal browser exit even after the
        # failed handoff. Always close a leaked baseline transport as well.
        browser.messages.put_nowait(None)
        await asyncio.wait_for(worker, 2)
        if transport is not None and not transport.is_closing():
            transport.close()
        peer.close()
