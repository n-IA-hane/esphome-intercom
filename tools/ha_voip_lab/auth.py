"""Credential-safe authentication for the isolated Home Assistant lab."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _post(url: str, data: dict[str, object], *, form: bool = False) -> dict:
    payload = urlencode(data).encode() if form else json.dumps(data).encode()
    request = Request(
        url,
        data=payload,
        headers={
            "Content-Type": (
                "application/x-www-form-urlencoded" if form else "application/json"
            )
        },
        method="POST",
    )
    with urlopen(request, timeout=8) as response:  # noqa: S310, controlled lab URL.
        return json.load(response)


def lab_token(base_url: str, credentials_path: Path) -> str:
    """Exchange the private lab username and password for a short-lived token."""

    credentials = dict(
        line.split("=", 1)
        for raw in credentials_path.read_text(encoding="utf-8").splitlines()
        if (line := raw.strip()) and "=" in line
    )
    client_id = "https://home-assistant.io/iOS"
    flow = _post(
        f"{base_url.rstrip('/')}/auth/login_flow",
        {
            "client_id": client_id,
            "handler": ["homeassistant", None],
            "redirect_uri": client_id,
        },
    )
    result = _post(
        f"{base_url.rstrip('/')}/auth/login_flow/{flow['flow_id']}",
        {
            "client_id": client_id,
            "username": credentials["username"],
            "password": credentials["password"],
        },
    )
    token = _post(
        f"{base_url.rstrip('/')}/auth/token",
        {
            "grant_type": "authorization_code",
            "code": result["result"],
            "client_id": client_id,
        },
        form=True,
    )
    return str(token["access_token"])
