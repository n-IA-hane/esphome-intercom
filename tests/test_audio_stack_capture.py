"""Independent receiver checks for truncated or lossy diagnostic captures."""

import importlib.util
from pathlib import Path
import struct
import wave

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools/capture_audio_stack.py"
spec = importlib.util.spec_from_file_location("capture_audio_stack", TOOL)
capture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(capture)


class FragmentedSocket:
    def __init__(self, data):
        self.data = data

    def recv(self, size):
        size = min(size, 3)
        result, self.data = self.data[:size], self.data[size:]
        return result


def fixture(dropped=0):
    raw = struct.pack("<hhhh", 100, -100, 200, -200)
    packet = capture.HEADER.pack(1, 1, 48000, 2, 16, 2, len(raw), 123000) + raw
    packet += capture.HEADER.pack(3, 2, 48000, 1, 16, 256, 0, 123001)
    stats = capture.STATS.pack(2, dropped, 0, 20)
    packet += capture.HEADER.pack(0, 2, 0, 0, 0, 0, len(stats), 124000) + stats
    return b"ASTCAP1\n" + packet


def test_fragmented_capture_preserves_pcm_and_playback_records(tmp_path):
    result = capture.receive_capture(FragmentedSocket(fixture()), tmp_path)
    assert result["valid"]
    assert result["streams"]["dac_played"]["frames"] == 256
    with wave.open(str(tmp_path / "hardware_rx.wav")) as wav:
        assert wav.getframerate() == 48000 and wav.getnchannels() == 2
        assert struct.unpack("<hhhh", wav.readframes(2)) == (100, -100, 200, -200)


def test_missing_footer_is_not_a_successful_capture(tmp_path):
    with pytest.raises(ValueError, match="complete record/footer"):
        capture.receive_capture(FragmentedSocket(fixture()[:-1]), tmp_path)


def test_observer_overflow_invalidates_audio_qualification(tmp_path):
    with pytest.raises(ValueError, match="overflowed"):
        capture.receive_capture(FragmentedSocket(fixture(dropped=1)), tmp_path)
