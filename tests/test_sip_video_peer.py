"""Contracts for the deterministic SIP media qualification peer."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "sip_video_peer.py"
SOFTPHONE_MATRIX = ROOT / "tools" / "ha_softphone_matrix.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("sip_video_peer", TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load SIP media qualification peer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_audio_offer_can_express_hold_and_resume_directions() -> None:
    peer = _load_tool()
    common = {
        "local_ip": "127.0.0.1",
        "audio_port": 40000,
        "video_port": 0,
        "codec": "audio",
        "direction": "sendrecv",
        "video_profile": "RTP/AVP",
    }

    held = peer._offer(**common, audio_direction="sendonly")
    resumed = peer._offer(**common, audio_direction="sendrecv")

    assert b"a=sendonly\r\n" in held
    assert b"a=sendrecv\r\n" not in held
    assert b"a=sendrecv\r\n" in resumed


def test_dahua_profile_offer_uses_vendor_pcm_and_keeps_video() -> None:
    peer = _load_tool()

    offer = peer._offer(
        local_ip="127.0.0.1",
        audio_port=40000,
        video_port=40002,
        codec="h264",
        direction="sendrecv",
        video_profile="RTP/AVP",
        audio_codec="dahua-pcm",
    )

    assert b"m=audio 40000 RTP/AVP 97 101\r\n" in offer
    assert b"a=rtpmap:97 PCM/16000\r\n" in offer
    assert b"a=ptime:20\r\n" in offer
    assert b"PCMA/8000" not in offer
    assert b"L16/" not in offer
    assert b"m=video 40002 RTP/AVP 102\r\n" in offer
    assert b"a=rtpmap:102 H264/90000\r\n" in offer


def test_dahua_profile_request_identity_is_explicit() -> None:
    peer = _load_tool()

    headers = peer._request_headers(
        method="INVITE",
        local_ip="127.0.0.1",
        local_port=5062,
        local_user="100",
        remote_uri="sip:9901@127.0.0.1:5060",
        call_id="dahua-sim",
        local_tag="from-tag",
        cseq=1,
        branch="z9hG4bKdahua",
        user_agent="Dahua UAC/3.0",
    )

    assert ("User-Agent", "Dahua UAC/3.0") in headers


def test_audio_hold_mode_rejects_video_qualification() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--codec",
            "vp8",
            "--audio-hold-after",
            "1",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )

    assert completed.returncode == 2
    assert "audio hold qualification requires --codec audio" in completed.stderr


def test_softphone_matrix_uses_the_public_device_selector() -> None:
    source = SOFTPHONE_MATRIX.read_text()

    assert '"device_id": phone_device_id' in source
    assert '{"destination": "Codex", "endpoint_id": "default"}' not in source


def test_media_peer_requires_bye_acknowledgement() -> None:
    source = TOOL.read_text()

    assert '"bye_response_status": None' in source
    assert 'raise TimeoutError("SIP BYE was not acknowledged")' in source
