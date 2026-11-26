"""A registered SIP participant records its call through the other HA owner."""

import asyncio
from dataclasses import replace
import math
import struct
from types import SimpleNamespace
import wave

import numpy as np
import pytest
from aiohttp import WSMsgType
from homeassistant.exceptions import ServiceValidationError

from .test_sip_phone_transfers_ha import registered_phones as registered_phones
from .test_trunk_calls_ha import trunks as trunks, lab as lab
from .test_browser_logical_availability_ha import logical_browser as logical_browser

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("logical_browser", [{"ringing_timeout": 30}], indirect=True)
@pytest.mark.parametrize("attach_first", [False, True])
async def test_registered_caller_records_ha_owned_call_before_and_after_media(
    registered_phones, logical_browser, tmp_path, attach_first,
):
    from custom_components.voip_stack import audio_ws, audio_ws_view
    from custom_components.voip_stack.core.audio_format import parse_audio_format_token
    from custom_components.voip_stack.call_recording import bind_browser_recording

    runtime = logical_browser
    directory = tmp_path / "media"
    directory.mkdir()
    runtime.hass.config.media_dirs = {"local": str(directory)}
    runtime.runtime.endpoints.update("transfer_a", device_id="caller-phone")
    endpoint = runtime.runtime.endpoints.require("casa")

    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)

    class Browser:
        def __init__(self):
            self.messages = asyncio.Queue()
            self.ready = asyncio.Event()

        def force_close(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.messages.get()

        async def send_json(self, value):
            if "tx_format" in value:
                self.contract = value
            self.ready.set()

        async def send_bytes(self, value):
            pass

    await asyncio.to_thread(registered_phones[0].command,
        f'/dial sip:221@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}')
    await wait_for(lambda: runtime.runtime.endpoints.get(endpoint.endpoint_id).active_call_id)
    call_id = runtime.runtime.endpoints.get(endpoint.endpoint_id).active_call_id
    await runtime.hass.services.async_call("voip_stack", "answer",
        {"device_id": endpoint.device_id, "call_id": call_id,
         "media_client_id": "test-media-owner"}, blocking=True)
    await asyncio.to_thread(registered_phones[0].wait_for, "Call established", 3)
    session = runtime.calls.get_session(runtime.calls.resolve_session_id(call_id))
    media = audio_ws_view._active_softphone_media_session(runtime.hass, endpoint.endpoint_id)
    assert media.call_token == session.token
    browser = Browser()
    worker = None
    selector = {"device_id": "caller-phone", "call_id": call_id,
                "expected_generation": session.generation}

    async def attach():
        task = asyncio.create_task(audio_ws_view._run_audio_session(
            runtime.hass, browser, media, endpoint_id=endpoint.endpoint_id))
        await asyncio.wait_for(browser.ready.wait(), 2)
        await wait_for(lambda: media.observe_audio is not None)
        return task

    try:
        if attach_first:
            worker = await attach()
        response = await runtime.hass.services.async_call("voip_stack", "start_recording",
            {**selector, "max_seconds": 30}, blocking=True, return_response=True)
        if not attach_first:
            worker = await attach()
        recording = runtime.calls.resource_for(call_id, "recording")
        assert recording.endpoint.endpoint_id == "transfer_a" and recording.detach is not None
        detach = recording.detach
        observed = []
        original_observe = media.observe_audio

        def observe(*args):
            observed.append(args)
            return original_observe(*args)

        media.observe_audio = observe
        assert not bind_browser_recording(runtime.runtime, media, "transfer_c")
        unrelated = SimpleNamespace(call_id="unrelated-call", call_token=session.token,
                                    observe_audio=observe)
        assert not bind_browser_recording(runtime.runtime, unrelated, endpoint.endpoint_id)
        media.call_token = replace(session.token, generation=session.generation + 1)
        assert not bind_browser_recording(runtime.runtime, media, endpoint.endpoint_id)
        media.call_token = session.token
        assert bind_browser_recording(runtime.runtime, media, endpoint.endpoint_id)
        assert bind_browser_recording(runtime.runtime, media, endpoint.endpoint_id)
        assert recording.detach is detach and not observed
        media.observe_audio = original_observe
        with pytest.raises(ServiceValidationError, match="does not own this call"):
            await runtime.hass.services.async_call("voip_stack", "start_recording",
                {"device_id": "transfer_c", "call_id": call_id,
                 "expected_generation": session.generation}, blocking=True)
        fmt = parse_audio_format_token(browser.contract["tx_format"])
        for index in range(round(800 / fmt.frame_ms)):
            frame = struct.pack(f"<{fmt.nominal_frame_samples}h", *(
                int(5000 * math.sin(2 * math.pi * 650 *
                    (i + index * fmt.nominal_frame_samples) / fmt.sample_rate))
                for i in range(fmt.nominal_frame_samples)))
            await browser.messages.put(SimpleNamespace(type=WSMsgType.BINARY,
                data=audio_ws.encode_audio_frame(frame)))
            await asyncio.sleep(fmt.frame_ms / 1000)
        stopped = await runtime.hass.services.async_call("voip_stack", "stop_recording",
            selector, blocking=True, return_response=True)
        assert "file_path" in stopped, "Accepted SIP-member recording captured no PCM"
        result = await runtime.hass.services.async_call("voip_stack", "get_recording",
            {"device_id": "caller-phone", "recording_id": response["recording_id"]},
            blocking=True, return_response=True)
        assert recording.detach is None and not bind_browser_recording(
            runtime.runtime, media, endpoint.endpoint_id)
        with wave.open(result["file_path"], "rb") as wav:
            rate = wav.getframerate()
            pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2").astype(float)
        spectrum = np.abs(np.fft.rfft(pcm * np.hanning(len(pcm))))
        frequencies = np.fft.rfftfreq(len(pcm), 1 / rate)
        for tone in (450, 650):
            assert spectrum[np.abs(frequencies - tone) < 5].max() > 100_000
        with pytest.raises(ServiceValidationError):
            await runtime.hass.services.async_call("voip_stack", "get_recording",
                {"device_id": endpoint.device_id, "recording_id": response["recording_id"]},
                blocking=True, return_response=True)
        assert session.live
        await asyncio.to_thread(registered_phones[0].hangup)
        await asyncio.wait_for(worker, 2)
        assert not runtime.calls.sessions and not runtime.runtime.rtp_port_pool.get("used", set())
    finally:
        if session.live:
            await runtime.hass.services.async_call("voip_stack", "hangup",
                {"device_id": endpoint.device_id}, blocking=True)
        if worker is not None:
            if not worker.done():
                worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)


async def test_browser_descriptor_retains_generation_across_prepare_await(lab, monkeypatch):
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import audio_ws_view
    from custom_components.voip_stack.core.sdp import RtpPcmFormat
    from custom_components.voip_stack.media_call_lifetime import ActiveMediaCall

    session = lab.calls.upsert("reused-recording-id", state="in_call", owner="ha_softphone",
        endpoint_id="test-browser", source_endpoint_id="bedroom0")
    fmt = RtpPcmFormat(96, "L16", 16000, 1, 20)
    lab.calls.own_resource(session.call_id, f"softphone_media:{session.call_id}", {
        "endpoint_id": "test-browser", "rtp_loopback": True,
        "remote_rtp_host": "127.0.0.1", "remote_rtp_port": 25000,
        "send_format": fmt, "recv_format": fmt,
    }, None, generation=session.generation)
    monkeypatch.setattr(audio_ws_view, "active_media_call",
        lambda *_args: ActiveMediaCall(session.call_id, {}, lab.calls))
    media = audio_ws_view._active_softphone_media_session(lab.hass, "test-browser")
    assert media.call_token == session.token
    await lab.calls.terminate_call_wait(session.call_id, reason="remote_hangup")
    replacement = lab.calls.upsert(session.call_id, state="in_call", owner="ha_softphone",
        endpoint_id="test-browser", source_endpoint_id="bedroom0")
    assert replacement.token != media.call_token
    open_audio = AsyncMock(side_effect=AssertionError("Stale descriptor opened RTP"))
    monkeypatch.setattr(asyncio.get_running_loop(), "create_datagram_endpoint", open_audio)
    await audio_ws_view._run_audio_session(lab.hass, SimpleNamespace(), media,
        endpoint_id="test-browser")
    assert media.call_token == session.token and media.observe_audio is None
    open_audio.assert_not_called()
    assert not lab.runtime.media.sessions_for("audio")
    await lab.calls.terminate_call_wait(replacement.call_id, reason="local_hangup")
