from __future__ import annotations

import importlib.util
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_p4_video_hil", ROOT / "scripts/run_p4_video_hil.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_measured_jpeg_actor_preserves_private_sip_credential_path(tmp_path, monkeypatch):
    credentials = tmp_path / ".secrets" / "peer.json"
    captured = []

    async def measured(args):
        captured.append(args)
        return {"status": "passed"}

    monkeypatch.setenv("HIL_MEASUREMENT_RECEIPT", str(tmp_path / "receipt.json"))
    for key, value in {"HIL_HA_URL": "http://ha.invalid", "HIL_HA_TOKEN_FILE": str(tmp_path / "token"),
                       "HIL_P4_SERIAL_PORT": "/dev/null"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setitem(sys.modules, "run_p4_h264_reinvite_hil", SimpleNamespace(run=measured))
    args = SimpleNamespace(sip_host="127.0.0.1", sip_port=5060, local_ip="127.0.0.1", codec="jpeg",
        target="destination", esp_host="127.0.0.1", duration=30, output=tmp_path / "result.json",
        sip_credentials_file=credentials)
    assert asyncio.run(MODULE.run(args)) == {"status": "passed"}
    (forwarded,) = captured
    assert forwarded.codec == "jpeg" and forwarded.sip_credentials_file == credentials


def valid_result() -> dict[str, object]:
    return {
        "ok": True,
        "sip_statuses": [100, 180, 200],
        "reinvite_status": 200,
        "reinvite_video_direction": "sendrecv",
        "reinvite_negotiated_video": "H264/90000",
        "bye_response_status": 200,
        "audio_tx_packets": 20,
        "audio_rx_packets": 19,
        "video_tx_packets": 10,
        "video_rx_packets": 11,
        "video_rx_marker_packets": 2,
        "video_rx_capture_invalid_payloads": 0,
    }


def test_validate_peer_result_requires_both_media_directions() -> None:
    result = valid_result()
    result["video_rx_packets"] = 0

    with pytest.raises(AssertionError, match="video receive"):
        MODULE.validate_peer_result(result)


def test_validate_peer_result_returns_privacy_safe_metrics() -> None:
    result = valid_result()
    result["call_id"] = "private-call-id"
    result["connected_identity_uri"] = "sip:private@example.test"

    evidence = MODULE.validate_peer_result(result)

    assert evidence["video_rx_packets"] == 11
    assert "call_id" not in evidence
    assert "connected_identity_uri" not in evidence
