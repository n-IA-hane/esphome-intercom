"""Utterance ownership and bounds with Home Assistant's real segmenter."""

import asyncio
from types import SimpleNamespace

import pytest
import pymicro_vad

from custom_components.voip_stack.assist_runtime import AssistConversation, ASSIST_PCM_FORMAT

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("continuous", [False, True])
async def test_utterance_ends_without_closing_the_call(monkeypatch, continuous):
    class Vad:
        def Process10ms(self, audio):
            return 1.0 if any(audio) else 0.0

    class Input:
        frames = 0

        async def get(self):
            self.frames += 1
            speech = continuous or 101 <= self.frames <= 150
            return bytes([int(speech)]) * ASSIST_PCM_FORMAT.nominal_frame_bytes

    monkeypatch.setattr(pymicro_vad, "MicroVad", Vad)
    media = SimpleNamespace(
        hass=object(), rx_queue=Input(), closed=asyncio.Event(),
        counters={"speech_gate_opens": 0}, _accepting_input=True,
        invite=SimpleNamespace(call_id="speech-test"),
    )
    conversation = AssistConversation(media, "test", "")
    frames = [frame async for frame in conversation._audio_stream()]
    seconds = len(frames) * ASSIST_PCM_FORMAT.frame_ms / 1000
    assert media.counters["speech_gate_opens"] == 1
    assert media._accepting_input is False
    assert not media.closed.is_set()
    if continuous:
        assert 15 <= seconds <= 16
    else:
        assert seconds < 3
        assert not any(frames[0])
        assert any(any(frame) for frame in frames)
        assert not any(frames[-1])
