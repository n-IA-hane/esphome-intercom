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


def test_audio_timeline_preserves_drops_across_missing_card_and_context_reset():
    def observed(label, wall, frames, drops, clock):
        return {"label": label, "at_s": wall, "card_state": "in_call", "call_id": "same-dialog",
            "engine_stats": {"frames_in": frames, "frames_drop": drops},
            "audio_clock": {"active": {"state": "running", "current_time": clock}}}

    samples = [observed("hold_001", 1, 100, 0, 1),
        observed("hold_002", 2, 282, 152, 2),
        {"label": "hold_003", "backend_state": {"state": "in_call", "call_id": "same-dialog"}},
        observed("hold_004", 4, 50, 0, .1), observed("active_after_hold", 5, 150, 0, 1.1)]
    report = probe.audio_timeline(samples)
    assert report["loss_totals"]["frames_drop"] == 152
    assert report["calls"]["same-dialog"]["media_generations"] == 2
    assert report["calls"]["same-dialog"]["loss_samples"][0]["label"] == "hold_002"
    assert report["calls"]["same-dialog"]["observation_gaps"] == ["hold_003"]
    with pytest.raises(RuntimeError, match="timeline failed"):
        probe.validate_audio_timeline(report)


def test_audio_timeline_keeps_running_clock_stall_after_later_recovery():
    samples = [{"label": label, "at_s": wall, "card_state": "in_call", "call_id": "clock-dialog",
        "engine_stats": {"frames_drop": 0},
        "audio_clock": {"active": {"state": "running", "current_time": clock}}}
        for label, wall, clock in [("in_call", 0, .023), ("video_flowing", .76, .023),
                                  ("active_after_hold", 2, 1.263)]]
    report = probe.audio_timeline(samples)
    assert report["clock_stalls"] == 1
    assert report["calls"]["clock-dialog"]["clock_stalls"][0]["render_seconds"] == 0
    with pytest.raises(RuntimeError, match="timeline failed"):
        probe.validate_audio_timeline(report)


def test_audio_timeline_counts_loss_deltas_once_per_media_generation():
    samples = [{"card_state": "in_call", "call_id": call, "engine_stats": {"frames_drop": drops}}
        for call, drops in [("first", 3), ("first", 3), ("first", 5), ("first", 0),
                            ("first", 2), ("redial", 0)]]
    report = probe.audio_timeline(samples)
    assert report["loss_totals"]["frames_drop"] == 7
    assert report["calls"]["first"]["media_generations"] == 2
    probe.validate_audio_timeline(probe.audio_timeline([]))


def test_audio_timeline_pairs_render_clock_with_its_browser_observation_time():
    samples = [{"label": str(index), "at_s": observed, "card_state": "in_call", "call_id": "paired",
        "engine_stats": {"frames_drop": 0}, "audio_clock": {"wall_monotonic_ms": paired * 1000,
            "active": {"state": "running", "current_time": rendered}}}
        for index, (observed, paired, rendered) in enumerate([(1, 1, .2), (6, 2, 1.2)])]
    # A slow later CDP request must not be mistaken for a stalled audio clock.
    report = probe.audio_timeline(samples)
    assert report["clock_stalls"] == 0
    probe.validate_audio_timeline(report)


def test_probe_sendrecv_cycle_accepts_dark_frames_only_with_real_receive_and_render():
    sample = _sendonly_sample()
    sample["video_direction"] = "sendrecv"
    sample["engine_stats"]["video"].update(received=20, rendered=19)
    sample["canvas"] = {"width": 320, "height": 240, "hidden": False, "non_black": False}
    probe._validate_sending_video(sample, "JPEG", direction="sendrecv", allow_dark_video=True)
    with pytest.raises(RuntimeError, match="non-black"):
        probe._validate_sending_video(sample, "JPEG", direction="sendrecv")
    for counter in ("received", "rendered"):
        previous = sample["engine_stats"]["video"][counter]
        sample["engine_stats"]["video"][counter] = 0
        with pytest.raises(RuntimeError, match="receive and render"):
            probe._validate_sending_video(sample, "JPEG", direction="sendrecv", allow_dark_video=True)
        sample["engine_stats"]["video"][counter] = previous
    sample["canvas"]["hidden"] = True
    with pytest.raises(RuntimeError, match="visible decoded canvas"):
        probe._validate_sending_video(sample, "JPEG", direction="sendrecv", allow_dark_video=True)


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
