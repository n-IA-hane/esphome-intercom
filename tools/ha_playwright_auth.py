#!/usr/bin/env python3
"""Shared authenticated Home Assistant context for live browser tests."""

from __future__ import annotations

from functools import lru_cache
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

try:
    from tools.ha_voip_lab.refresh_playwright_auth import (
        playwright_storage_origin,
        refresh_playwright_auth,
    )
except ModuleNotFoundError:
    from ha_voip_lab.refresh_playwright_auth import (
        playwright_storage_origin,
        refresh_playwright_auth,
    )


ROOT = Path(__file__).resolve().parents[1]
PRIVATE_ROOT = Path("/home/codex/.secrets/esphome-intercom")


def _origin(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _requested_origin() -> str:
    return _origin(os.environ.get("HA_URL") or os.environ.get("HA_BASE") or "")


def _storage_candidates() -> tuple[Path, ...]:
    configured = os.environ.get("PLAYWRIGHT_STORAGE_STATE", "")
    if configured:
        return (Path(configured).expanduser(),)
    return (
        PRIVATE_ROOT / "ha_playwright_storage.json",
        ROOT / ".secrets" / "ha_playwright_storage.json",
    )


def _storage_path() -> Path:
    expected_origin = _requested_origin()
    existing = tuple(path for path in _storage_candidates() if path.is_file())
    for path in existing:
        try:
            if playwright_storage_origin(path) == expected_origin:
                return path
        except (OSError, ValueError, json.JSONDecodeError):
            continue
    if not expected_origin and existing:
        return existing[0]
    if expected_origin:
        raise RuntimeError(
            f"no Playwright storage state is available for {expected_origin}"
        )
    raise RuntimeError("no Playwright storage state is available")


def _credentials_path() -> Path:
    configured = os.environ.get("HA_PLAYWRIGHT_REFRESH_CREDENTIALS", "")
    if configured:
        return Path(configured).expanduser()
    return PRIVATE_ROOT / "ha_home_auth.json"


@lru_cache(maxsize=4)
def _prepare_storage(path_value: str, origin: str) -> Path:
    path = Path(path_value)
    credentials = _credentials_path()
    if not credentials.is_file():
        raise RuntimeError(f"Playwright refresh credentials do not exist: {credentials}")
    refresh_playwright_auth(
        token_url=os.environ.get("HA_PLAYWRIGHT_REFRESH_URL", ""),
        credentials_path=credentials,
        storage_path=path,
        storage_hass_url=origin,
    )
    return path


def _prepared_storage() -> tuple[Path, str]:
    path = _storage_path()
    origin = _requested_origin() or playwright_storage_origin(path)
    return _prepare_storage(str(path), origin), origin


def context_kwargs() -> dict[str, str]:
    """Return a freshly authenticated Playwright browser context."""

    path, _origin_value = _prepared_storage()
    return {"storage_state": str(path)}


def ha_token() -> str:
    """Return the refreshed access token without persisting it elsewhere."""

    path, origin = _prepared_storage()
    storage = json.loads(path.read_text())
    for item in storage.get("origins", []):
        if str(item.get("origin") or "") != origin:
            continue
        for local in item.get("localStorage", []):
            if local.get("name") != "hassTokens":
                continue
            token = str(json.loads(local.get("value") or "{}").get("access_token") or "")
            if token:
                return token
    raise RuntimeError(f"Playwright storage state has no access token for {origin}")
