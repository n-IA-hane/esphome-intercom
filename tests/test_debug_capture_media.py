"""Diagnostic audio uses the same configured HA media root as recordings."""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from .voip_phase1_support import _load_audio_ws_runtime_module, _load_intercom_module


def test_debug_helpers_import_without_resolving_home():
    source = Path(__file__).resolve().parents[1] / "custom_components/voip_stack/debug_capture.py"
    spec = importlib.util.spec_from_file_location("debug_capture_without_home", source)
    module = importlib.util.module_from_spec(spec)
    with patch.object(Path, "home", side_effect=AssertionError("HA media must not depend on HOME")):
        spec.loader.exec_module(module)


async def test_relay_writes_private_capture_in_selected_media_directory(tmp_path):
    audio = _load_intercom_module("audio_format")
    bridge = _load_intercom_module("sip_rtp_bridge")
    fmt = audio.AudioFormat(16000, "s16le", 1, 20)
    directory = tmp_path / "media" / "voip_stack" / "debug"
    relay = bridge.SipRtpRelay(
        left=bridge.RtpPeer("127.0.0.2", 40000, 96, fmt),
        right=bridge.RtpPeer("127.0.0.3", 41000, 96, fmt),
        left_port=42000, right_port=42002,
        debug_capture=True, capture_directory=directory,
    )
    relay._capture_pcm("left", bytes(fmt.nominal_frame_bytes))
    await relay.stop()
    assert len(list(directory.glob("*.wav"))) == 2
    assert directory.stat().st_mode & 0o777 == 0o700
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.iterdir())


@pytest.mark.parametrize("destination_available", [False, True])
async def test_unavailable_capture_does_not_interrupt_rtp_or_cleanup(tmp_path, destination_available, caplog):
    audio = _load_intercom_module("audio_format")
    bridge = _load_intercom_module("sip_rtp_bridge")
    rtp = _load_intercom_module("rtp")
    captures = _load_intercom_module("debug_capture")
    fmt = audio.AudioFormat(16000, "s16le", 1, 20)
    released = []
    directory = tmp_path / "blocked" if destination_available else None
    relay = bridge.SipRtpRelay(
        left=bridge.RtpPeer("127.0.0.2", 40000, 96, fmt),
        right=bridge.RtpPeer("127.0.0.3", 41000, 96, fmt),
        left_port=42000, right_port=42002,
        debug_capture=True, capture_directory=directory, on_release=released.append,
    )
    transport = Mock()
    relay.right_transport = transport
    relay.handle_packet("left", rtp.build_packet(rtp.RtpPacket(
        96, 1, 0, 123, bytes(fmt.nominal_frame_bytes))), ("127.0.0.2", 40000))
    assert transport.sendto.call_count == 1
    if destination_available:
        # Root-run tests cannot simulate an unwritable directory using chmod.
        with patch.object(captures, "ensure_debug_capture_dir", side_effect=PermissionError("read-only media")):
            await relay.stop()
        assert "debug capture write failed" in caplog.text
    else:
        assert relay.debug_capture is False
        assert not relay._capture_buffers
        await relay.stop()
        assert "no Home Assistant media directory" in caplog.text
    await relay.stop()
    assert released == [(42000, 42002)]
    assert captures.debug_capture_pending_writes() == 0
    assert not list(tmp_path.iterdir())


def test_browser_capture_writes_only_in_selected_media_directory(tmp_path):
    import json
    import wave

    view = _load_audio_ws_runtime_module()
    audio = _load_intercom_module("audio_format")
    fmt = audio.AudioFormat(16000, "s16le", 1, 20)
    directory = tmp_path / "custom-media" / "voip_stack" / "debug"
    with patch.object(Path, "home", side_effect=AssertionError("HOME is not HA media")):
        capture = view._DebugAudioCapture("../../call", directory=directory,
                                          rx_format=fmt, tx_format=fmt)
        payload = bytes(fmt.nominal_frame_bytes)
        capture.note_rtp_rx(1.0, payload)
        capture.note_ws_rx(1.0, payload)
        capture.write({"rtp_rx": 1})
    assert len(list(directory.iterdir())) == 3
    assert directory.stat().st_mode & 0o777 == 0o700
    for path in directory.glob("*.wav"):
        assert path.stat().st_mode & 0o777 == 0o600
        with wave.open(str(path)) as wav:
            assert wav.readframes(wav.getnframes()) == payload
    manifest = json.loads(next(directory.glob("*.json")).read_text())
    assert Path(manifest["rtp_to_browser_wav"]).parent == directory
    assert Path(manifest["browser_to_rtp_wav"]).parent == directory


async def test_unwritable_browser_capture_releases_background_writer(tmp_path, monkeypatch, caplog):
    view = _load_audio_ws_runtime_module()
    captures = _load_intercom_module("debug_capture")
    audio = _load_intercom_module("audio_format")
    fmt = audio.AudioFormat(16000, "s16le", 1, 20)
    capture = view._DebugAudioCapture("unwritable", directory=tmp_path / "media" / "debug",
                                      rx_format=fmt, tx_format=fmt)
    media = SimpleNamespace(debug_capture_tasks=set(), debug_capture_dropped_writes=0)
    monkeypatch.setattr(view, "require_runtime_data", lambda _: SimpleNamespace(media=media))
    hass = SimpleNamespace(async_add_executor_job=lambda target, *args:
                           asyncio.get_running_loop().run_in_executor(None, target, *args))
    monkeypatch.setattr(captures, "ensure_debug_capture_dir", Mock(side_effect=PermissionError("read-only media")))
    view._schedule_debug_capture_write(hass, capture, {})
    tasks = tuple(media.debug_capture_tasks)
    assert len(tasks) == 1
    assert isinstance((await asyncio.gather(*tasks, return_exceptions=True))[0], PermissionError)
    # Future completion schedules the owner's done callback on the next turn.
    await asyncio.sleep(0)
    assert not media.debug_capture_tasks
    assert captures.debug_capture_pending_writes() == 0
    assert "audio debug capture write failed" in caplog.text
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("directories, expected", [
    ({"local": "/media"}, Path("/media/voip_stack")),
    ({"other": "/custom/media", "local": "/config/media"}, Path("/config/media/voip_stack")),
    ({"custom": "/srv/ha-media"}, Path("/srv/ha-media/voip_stack")),
    ({}, None),
])
def test_capture_uses_configured_archive_media_root(directories, expected):
    paths = _load_intercom_module("media_paths")
    hass = SimpleNamespace(config=SimpleNamespace(media_dirs=directories))
    with patch.object(Path, "home", side_effect=AssertionError("HOME must not select HA media")):
        assert paths.archive_path(hass) == expected
        assert paths.debug_capture_path(hass) == (expected / "debug" if expected else None)
