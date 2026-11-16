"""Initial speech gate and Core-owned utterance completion on a live call."""

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

    monkeypatch.setattr(pymicro_vad, "MicroVad", Vad)
    size = ASSIST_PCM_FORMAT.nominal_frame_bytes
    silence, speech, second_syllable, last = (bytes([n]) * size for n in range(4))
    initial_speech_frames = 800 if continuous else 50
    pause_frames = [] if continuous else [silence] * 100
    second_frames = [] if continuous else [second_syllable] * 10
    incoming = asyncio.Queue()
    for frame in [*[silence] * 100, *[speech] * initial_speech_frames,
                  *pause_frames, *second_frames, last]:
        incoming.put_nowait(frame)
    media = SimpleNamespace(
        hass=object(), rx_queue=incoming, closed=asyncio.Event(),
        counters={"speech_gate_opens": 0}, _accepting_input=True,
        invite=SimpleNamespace(call_id="speech-test"),
    )
    conversation = AssistConversation(media, "test", "")
    stream = conversation._audio_stream()
    frames = []
    pending = None
    try:
        while not frames or frames[-1] != last:
            frames.append(await asyncio.wait_for(anext(stream), 2))
        assert media.counters["speech_gate_opens"] == 1
        assert frames.count(speech) == initial_speech_frames
        assert not any(frames[0])
        assert media._accepting_input is True
        if continuous:
            assert len(frames) * ASSIST_PCM_FORMAT.frame_ms / 1000 > 16
        else:
            first_tail = frames.index(speech) + initial_speech_frames
            assert frames[first_tail:first_tail + 100] == pause_frames
            assert frames.count(second_syllable) == 10
        # Neither a pause nor the previous local 15-second bound ends input.
        pending = asyncio.create_task(anext(stream))
        await asyncio.sleep(0)
        assert not pending.done() and not media.closed.is_set()
        conversation._pipeline_event(SimpleNamespace(type="stt-vad-end", data={}))
        assert media._accepting_input is False
        assert not media.closed.is_set()
    finally:
        # Core closes its input consumer after its final boundary event; the
        # telephone/media owner remains open for the next conversational turn.
        if pending is not None:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        await stream.aclose()
    assert not media.closed.is_set()
