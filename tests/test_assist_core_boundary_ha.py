"""The telephone ingress gate precedes Core timing; HA alone owns phrase end."""
from collections import defaultdict
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant.common import async_fire_time_changed_exact

from .test_managed_phones_ha import custom_integration_path as custom_integration_path

pytestmark = pytest.mark.ha


async def test_long_initial_silence_precedes_Core_run_and_preroll_survives_pause(hass, monkeypatch):
    import pymicro_vad
    from homeassistant.components import assist_pipeline
    from custom_components.voip_stack.assist_runtime import AssistConversation, ASSIST_PCM_FORMAT

    class ControlledSpeechProbability:
        def Process10ms(self, chunk):  # noqa: N802 - native API
            return 0.99 if any(chunk) else 0.0

    # Only probability classification and the Core entrypoint are controlled.
    # The R gate and HA VoiceCommandSegmenter are the actual implementations.
    monkeypatch.setattr(pymicro_vad, "MicroVad", ControlledSpeechProbability)
    media = SimpleNamespace(hass=hass, closed=asyncio.Event(), rx_queue=asyncio.Queue(),
        counters=defaultdict(int), _tts_task=None, _accepting_input=False,
        _completed=False, invite=SimpleNamespace(call_id="core-boundary-fixture"),
        _drain_rx=lambda: None, _tts_audio_output=lambda: None, on_complete=AsyncMock())
    conversation = AssistConversation(media, "preferred", "")
    received = []
    entered = asyncio.Event()
    size = ASSIST_PCM_FORMAT.nominal_frame_bytes
    second_syllable = bytes([200]) * size

    async def observed_Core_entrypoint(_hass, **kwargs):
        entered.set()
        assert kwargs["audio_settings"].is_vad_enabled is True
        async for frame in kwargs["stt_stream"]:
            received.append(frame)
            if frame == second_syllable:
                break
        assert received[-1] == second_syllable
        kwargs["event_callback"](SimpleNamespace(type=SimpleNamespace(value="stt-vad-end"), data={}))
        assert media._accepting_input is False
        media.closed.set()

    runner = AsyncMock(side_effect=observed_Core_entrypoint)
    monkeypatch.setattr(assist_pipeline, "async_pipeline_from_audio_stream", runner)
    task = asyncio.create_task(conversation._pipeline_loop())
    try:
        for _ in range(round(20_000 / ASSIST_PCM_FORMAT.frame_ms)):
            media.rx_queue.put_nowait(bytes(size))
        async with asyncio.timeout(2):
            while not media.rx_queue.empty():
                await asyncio.sleep(0)
        # Advance beyond Core's normal command timeout. No Core run exists yet,
        # so there is no per-run no-speech clock to expire before the caller.
        async_fire_time_changed_exact(hass, dt_util.utcnow() + timedelta(seconds=31))
        await hass.async_block_till_done()
        runner.assert_not_awaited()
        first_syllables = [bytes([index]) * size for index in range(1, 16)]
        for frame in first_syllables:
            media.rx_queue.put_nowait(frame)
        for _ in range(round(1200 / ASSIST_PCM_FORMAT.frame_ms)):
            media.rx_queue.put_nowait(bytes(size))
        media.rx_queue.put_nowait(second_syllable)
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(task, 2)
        assert runner.await_count == 1
        assert [frame for frame in received if any(frame)] == [*first_syllables, second_syllable]
        assert media.counters["speech_gate_opens"] == 1
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
