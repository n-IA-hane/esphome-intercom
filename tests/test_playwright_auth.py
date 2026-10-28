"""Contracts for repeatable authenticated Playwright qualification."""

from __future__ import annotations

import json
from pathlib import Path
import urllib.error

import pytest

from tools.ha_voip_lab import refresh_playwright_auth as auth
from tools import sip_video_browser_probe as probe


def _storage(path: Path, *origins: str) -> Path:
    path.write_text(
        json.dumps(
            {
                "cookies": [],
                "origins": [
                    {
                        "origin": origin,
                        "localStorage": [
                            {
                                "name": "hassTokens",
                                "value": json.dumps(
                                    {
                                        "clientId": f"{origin}/",
                                        "hassUrl": origin,
                                        "access_token": "old",
                                        "expires": 0,
                                    }
                                ),
                            }
                        ],
                    }
                    for origin in origins
                ],
            }
        )
    )
    return path


def test_storage_origin_is_selected_by_hass_tokens(tmp_path: Path) -> None:
    storage = _storage(tmp_path / "storage.json", "https://ha.example")

    assert auth.playwright_storage_origin(storage) == "https://ha.example"
    assert (
        auth.playwright_storage_origin(
            storage,
            preferred_url="https://ha.example/lovelace/test",
        )
        == "https://ha.example"
    )


def test_storage_origin_rejects_wrong_or_ambiguous_origin(tmp_path: Path) -> None:
    storage = _storage(
        tmp_path / "storage.json",
        "https://ha.example",
        "https://other.example",
    )

    with pytest.raises(ValueError, match="exactly one"):
        auth.playwright_storage_origin(storage)
    with pytest.raises(ValueError, match="no hassTokens"):
        auth.playwright_storage_origin(
            storage,
            preferred_url="http://192.0.2.1:8123/lovelace/test",
        )


def test_probe_derives_full_dashboard_url_from_storage(tmp_path: Path) -> None:
    storage = _storage(tmp_path / "storage.json", "https://ha.example")

    assert (
        probe._dashboard_url("", storage, "/lovelace/test")
        == "https://ha.example/lovelace/test"
    )
    assert (
        probe._dashboard_url("https://ha.example", storage, "lovelace/video")
        == "https://ha.example/lovelace/video"
    )
    assert (
        probe._dashboard_url(
            "https://ha.example/lovelace/custom",
            storage,
            "/lovelace/test",
        )
        == "https://ha.example/lovelace/custom"
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://ha.example/auth/authorize", True),
        ("https://ha.example/auth", True),
        ("https://ha.example/lovelace/test", False),
    ],
)
def test_probe_detects_oauth_redirect(url: str, expected: bool) -> None:
    assert probe._is_ha_auth_url(url) is expected


def test_probe_can_use_a_deterministic_video_capture(tmp_path: Path) -> None:
    video = tmp_path / "complex source.y4m"
    video.touch()

    assert probe._fake_media_args() == [
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
    ]
    assert probe._fake_media_args(video)[-1] == (
        f"--use-file-for-fake-video-capture={video.resolve()}"
    )


def _sendonly_sample() -> dict:
    return {
        "card_state": "in_call",
        "video_direction": "sendonly",
        "video_active": True,
        "engine_video_active": True,
        "video_format": "JPEG/90000",
        "engine_stats": {"sent": 400, "received": 400, "video": {"sent": 18}},
        # A receiver-only peer sends no image to the browser. Its hidden
        # canvas and zero video RX are required to remain a valid witness.
        "canvas": None,
    }


@pytest.mark.parametrize(
    "description",
    ["JPEG/90000", "pt=26:JPEG/90000;framerate=2;rtp-profile=RTP/AVP;direction=recvonly"],
)
def test_probe_accepts_sender_only_video_without_remote_canvas(description: str) -> None:
    sample = _sendonly_sample()
    sample["video_format"] = description
    probe._validate_sendonly_video(sample, "JPEG")


@pytest.mark.parametrize("direction", ["inactive", "recvonly", "sendrecv", ""])
def test_probe_sender_only_gate_rejects_other_negotiated_directions(direction: str) -> None:
    sample = _sendonly_sample()
    sample["video_direction"] = direction
    with pytest.raises(RuntimeError, match="expected browser sendonly"):
        probe._validate_sendonly_video(sample)


@pytest.mark.parametrize("counter", ["sent", "received", "video"])
def test_probe_sender_only_gate_still_requires_real_audio_and_camera(counter: str) -> None:
    sample = _sendonly_sample()
    sample["engine_stats"][counter] = {"sent": 0} if counter == "video" else 0
    with pytest.raises(RuntimeError, match="did not send|bidirectional audio"):
        probe._validate_sendonly_video(sample)


def test_probe_sender_only_gate_rejects_wrong_codec_and_early_hangup() -> None:
    sample = _sendonly_sample()
    with pytest.raises(RuntimeError, match="expected H264"):
        probe._validate_sendonly_video(sample, "H264")
    sample["card_state"] = "idle"
    with pytest.raises(RuntimeError, match="ended before"):
        probe._validate_sendonly_video(sample, "JPEG")


def test_revoked_refresh_falls_back_to_login_flow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _storage(tmp_path / "storage.json", "https://ha.example")
    credentials = tmp_path / "credentials"
    credentials.write_text(
        "username=test-user\npassword=test-password\nrefresh_token=revoked\n"
    )

    def rejected_refresh(_url: str, _fields: dict[str, str]) -> dict[str, object]:
        raise urllib.error.HTTPError("https://ha.example", 400, "bad", {}, None)

    login_calls: list[tuple[str, str]] = []

    def successful_login(
        base_url: str,
        values: dict[str, str],
        client_id: str,
    ) -> dict[str, object]:
        login_calls.append((base_url, client_id))
        assert values["username"] == "test-user"
        return {"access_token": "new-access", "expires_in": 600}

    monkeypatch.setattr(auth, "_request_token", rejected_refresh)
    monkeypatch.setattr(auth, "_login_token", successful_login)

    auth.refresh_playwright_auth(
        token_url="https://ha.example",
        credentials_path=credentials,
        storage_path=storage,
        storage_hass_url="https://ha.example",
    )

    updated = json.loads(storage.read_text())
    token = json.loads(updated["origins"][0]["localStorage"][0]["value"])
    assert token["access_token"] == "new-access"
    assert login_calls == [("https://ha.example", "https://ha.example/")]
