"""Native HA media browsing and playback resolve opt-in diagnostic WAV files."""

import pytest
from homeassistant.components.media_source.local_source import async_get_media_source
from homeassistant.components.media_source.models import MediaSourceItem

from custom_components.voip_stack.audio_ws_view import _DebugAudioCapture
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.mailbox import archive_path
from custom_components.voip_stack.media_paths import debug_capture_path

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("media_id", ["local", "custom"])
async def test_native_media_browser_lists_and_resolves_debug_audio(hass, tmp_path, media_id):
    hass.config.media_dirs = {media_id: str(tmp_path / "media")}
    directory = debug_capture_path(hass)
    assert directory == archive_path(hass) / "debug"
    fmt = AudioFormat(16000, "s16le", 1, 20)
    capture = _DebugAudioCapture("native-media", directory=directory, rx_format=fmt, tx_format=fmt)
    capture.note_rtp_rx(1.0, bytes(fmt.nominal_frame_bytes))
    capture.note_ws_rx(1.0, bytes(fmt.nominal_frame_bytes))
    await hass.async_add_executor_job(capture.write, {})

    source = await async_get_media_source(hass)
    browse = await source.async_browse_media(MediaSourceItem(
        hass, "media_source", f"{media_id}/voip_stack/debug", None))
    assert len(browse.children) == 2
    assert all(child.can_play and child.media_content_type == "audio/x-wav" for child in browse.children)
    for child in browse.children:
        assert child.media_content_id.startswith(f"media-source://media_source/{media_id}/voip_stack/debug/")
        playback = await source.async_resolve_media(MediaSourceItem(
            hass, "media_source", child.identifier, None))
        assert playback.path.parent == directory
        assert playback.url.startswith(f"/media/{media_id}/voip_stack/debug/")
