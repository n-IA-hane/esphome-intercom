"""Contracts for repeatable authenticated Playwright qualification."""

from __future__ import annotations

import json
from pathlib import Path

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
