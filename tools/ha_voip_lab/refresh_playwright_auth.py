#!/usr/bin/env python3
"""Refresh the local HA lab token stored in a Playwright storage-state file."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


LAB_ROOT = Path(os.environ.get("HA_VOIP_LAB_ROOT", Path.home() / "ha-voip-lab"))


def _origin(value: str) -> str:
    parsed = urlsplit(str(value or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _credentials(path: Path) -> dict[str, str]:
    if path.suffix.lower() == ".json":
        raw = json.loads(path.read_text())
        if not isinstance(raw, dict):
            raise ValueError("JSON credentials must be an object")
        return {str(key): str(value) for key, value in raw.items()}
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip():
            values[key.strip()] = value.strip()
    return values


def playwright_storage_origin(
    storage_path: Path,
    *,
    preferred_url: str = "",
) -> str:
    """Return the HA origin that owns hassTokens in a storage-state file."""

    preferred_origin = _origin(preferred_url)
    storage = json.loads(storage_path.read_text())
    candidates = [
        str(item.get("origin") or "")
        for item in storage.get("origins", [])
        if any(
            local.get("name") == "hassTokens"
            for local in item.get("localStorage", [])
        )
    ]
    if preferred_origin:
        if preferred_origin not in candidates:
            raise ValueError(
                f"storage state has no hassTokens for origin {preferred_origin}"
            )
        return preferred_origin
    if len(candidates) != 1:
        raise ValueError(
            "storage state must contain exactly one Home Assistant hassTokens origin"
        )
    return candidates[0]


def refresh_playwright_auth(
    *,
    token_url: str,
    credentials_path: Path,
    storage_path: Path,
    client_id_override: str = "",
    storage_hass_url: str = "",
) -> None:
    """Refresh HA browser auth while preserving its origin-scoped storage."""

    credentials = _credentials(credentials_path)
    refresh_token = credentials.get("refresh_token", "")
    if not refresh_token:
        raise ValueError("credentials file has no refresh_token")

    desired_hass_url = str(storage_hass_url or "").rstrip("/")
    desired_origin = playwright_storage_origin(
        storage_path,
        preferred_url=desired_hass_url,
    )
    token_base_url = str(
        token_url or credentials.get("hass_url") or desired_origin
    ).rstrip("/")
    if not _origin(token_base_url):
        raise ValueError("token URL must be an absolute HTTP(S) URL")

    storage = json.loads(storage_path.read_text())
    hass_tokens = None
    token_item = None
    for origin in storage.get("origins", []):
        if desired_origin and str(origin.get("origin") or "") != desired_origin:
            continue
        for item in origin.get("localStorage", []):
            if item.get("name") == "hassTokens":
                hass_tokens = json.loads(item.get("value") or "{}")
                token_item = item
                break
        if hass_tokens is not None:
            break
    if hass_tokens is None or token_item is None:
        suffix = f" for origin {desired_origin}" if desired_origin else ""
        raise ValueError(f"storage state has no hassTokens localStorage item{suffix}")

    client_id = str(
        client_id_override
        or credentials.get("client_id")
        or hass_tokens.get("clientId")
        or f"{desired_origin}/"
    )
    body = urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
    ).encode()
    request = Request(
        f"{token_base_url}/auth/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:  # noqa: S310 - explicit HA URL.
        refreshed = json.load(response)
    access_token = str(refreshed.get("access_token") or "")
    if not access_token:
        raise RuntimeError("Home Assistant token refresh returned no access token")

    hass_tokens.update(
        {
            "hassUrl": desired_hass_url
            or str(hass_tokens.get("hassUrl") or desired_origin).rstrip("/"),
            "clientId": client_id,
            "access_token": access_token,
            "expires": int(
                (time.time() + int(refreshed.get("expires_in") or 1800)) * 1000
            ),
        }
    )
    token_item["value"] = json.dumps(hass_tokens, separators=(",", ":"))
    temporary = storage_path.with_suffix(storage_path.suffix + ".tmp")
    temporary.write_text(json.dumps(storage, separators=(",", ":")))
    os.chmod(temporary, 0o600)
    temporary.replace(storage_path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18123")
    parser.add_argument(
        "--client-id",
        default="",
        help="OAuth client ID; defaults to the lab URL with a trailing slash",
    )
    parser.add_argument(
        "--credentials",
        default=str(LAB_ROOT / ".credentials"),
    )
    parser.add_argument(
        "--storage-state",
        default=str(LAB_ROOT / "playwright-storage.json"),
    )
    parser.add_argument(
        "--storage-hass-url",
        default="",
        help=(
            "HA URL retained in browser localStorage; it may differ from the "
            "token endpoint URL"
        ),
    )
    args = parser.parse_args()
    storage_path = Path(args.storage_state).expanduser()
    try:
        refresh_playwright_auth(
            token_url=args.url,
            credentials_path=Path(args.credentials).expanduser(),
            storage_path=storage_path,
            client_id_override=args.client_id,
            storage_hass_url=args.storage_hass_url,
        )
    except ValueError as err:
        parser.error(str(err))
    print(
        "Refreshed Playwright authentication for "
        f"{str(args.storage_hass_url or args.url).rstrip('/')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
