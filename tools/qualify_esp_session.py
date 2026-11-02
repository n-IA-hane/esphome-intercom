#!/usr/bin/env python3
"""Prepare a grouped session; reusable collectors perform no action on import.

This CLI writes a preparation plan only. A later authorized HIL actor may use
EspObserver with its existing API client. No provisioning, volume setter,
account removal, device restart, demo scene or external publication is included.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode
from urllib.request import urlopen

from PIL import Image, UnidentifiedImageError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qualification.final_session import final_session_plan  # noqa: E402


def validate_screenshot_response(*, status, url, expected_url, mime, etag,
                                 capture_id, payload, expected_size):
    """Bind actual JPEG pixels to the native capture and exact HTTP resource."""
    if status != 200 or url != expected_url:
        raise RuntimeError("Screenshot HTTP response differs from the requested capture")
    if mime.partition(";")[0].strip().lower() != "image/jpeg" or etag != f'"{capture_id}"':
        raise RuntimeError("Screenshot MIME or ETag differs from the fresh capture")
    if not payload.startswith(b"\xff\xd8") or not payload.endswith(b"\xff\xd9"):
        raise RuntimeError("Screenshot JPEG markers are absent or truncated")
    try:
        with Image.open(BytesIO(payload), formats=["JPEG"]) as image:
            image.load()
            if image.size != tuple(expected_size):
                raise RuntimeError("Screenshot geometry differs from the compiled display")
            pixels = image.convert("RGB").tobytes()
            width, height = image.size
    except (UnidentifiedImageError, OSError, ValueError) as error:
        raise RuntimeError("Screenshot JPEG cannot be fully decoded") from error
    return {"capture_id": capture_id, "etag": etag, "bytes": len(payload), "width": width, "height": height,
            "sha256": hashlib.sha256(payload).hexdigest(), "pixels_sha256": hashlib.sha256(pixels).hexdigest()}


class EspObserver:
    """Bounded Q observer, sharing the existing actor's API client and owner."""

    def __init__(self, client):
        self.client = client
        self.entities, self.services = [], {}
        self.values, self.state_revisions = {}, {}
        self.changed = asyncio.Event()
        self.identity_verified = False
        self.unsubscribe = None

    async def open(self, *, expected_mac: str, expected_build: str):
        if not expected_mac or not expected_build:
            raise RuntimeError("A physical target needs exact MAC and candidate build")
        try:
            await self.client.connect(login=True)
            info = await self.client.device_info()
            def norm(value):
                return value.replace(":", "").replace("-", "").casefold()
            if norm(info.mac_address) != norm(expected_mac) or info.compilation_time != expected_build:
                raise RuntimeError("Live MAC/build differs from the approved candidate")
            self.entities, services = await self.client.list_entities_services()
            self.services = {service.name: service for service in services}
            self.unsubscribe = self.client.subscribe_states(self.on_state)
            if asyncio.iscoroutine(self.unsubscribe):
                self.unsubscribe = await self.unsubscribe
            self.identity_verified = True
            return {"mac": info.mac_address, "build": info.compilation_time,
                    "services": sorted(self.services), "physical_qualified": False}
        except BaseException:
            await self.client.disconnect()
            raise

    async def close(self):
        try:
            if self.unsubscribe is not None:
                self.unsubscribe()
        finally:
            self.identity_verified = False
            await self.client.disconnect()

    def on_state(self, state):
        # Entity keys are unique within a domain, never across all domains.
        key = type(state).__name__, state.key
        self.values[key] = getattr(state, "state", None)
        self.state_revisions[key] = self.state_revisions.get(key, 0) + 1
        self.changed.set()

    async def wait(self, predicate, timeout):
        deadline = time.monotonic() + timeout
        while not predicate():
            self.changed.clear()
            if predicate():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Native observation deadline expired")
            await asyncio.wait_for(self.changed.wait(), remaining)

    async def service(self, name, data=None):
        if not self.identity_verified:
            raise RuntimeError("API action refused before candidate identity verification")
        service = self.services.get(name)
        if service is None:
            raise RuntimeError(f"Running firmware does not expose {name}")
        result = self.client.execute_service(service, data or {})
        if asyncio.iscoroutine(result):
            await result

    def _sensor(self, names):
        matches = [entity for entity in self.entities if type(entity).__name__ == "TextSensorInfo"
                   and (entity.object_id in names or getattr(entity, "name", "").casefold() in names)]
        if len(matches) != 1:
            raise RuntimeError("Native screenshot sensor inventory is absent or ambiguous")
        return "TextSensorState", matches[0].key

    async def screenshot(self, base_url, output, *, expected_size, timeout=15, checkpoint=None):
        if len(expected_size) != 2 or any(type(value) is not int or value <= 0 for value in expected_size):
            raise ValueError("Expected display geometry must contain two positive integers")
        id_key = self._sensor({"screenshot_id", "screenshot id"})
        status_key = self._sensor({"screenshot_status", "capture_status", "screenshot status"})
        try:
            await self.wait(lambda: id_key in self.values and status_key in self.values, timeout)
        except TimeoutError as error:
            raise RuntimeError("Screenshot ID and status baselines were not observed") from error
        previous, status = self.values[id_key], str(self.values[status_key]).strip()
        if status in {"Busy", "Capturing"}:
            raise RuntimeError(f"Screenshot capture is busy: {status}")
        if status in {"", "unknown", "unavailable"}:
            raise RuntimeError("Native screenshot status is unavailable")
        revision = self.state_revisions[status_key]
        scene_before = await checkpoint() if checkpoint is not None else None
        def check_scene(scene):
            if (not isinstance(scene, dict) or scene.get("page") in {None, "", "unknown"}
                    or scene.get("transition") != "0" or scene.get("avatar_visible") != "NO"):
                raise RuntimeError("Native UI scene has no identified stable rendered page")
        if checkpoint is not None:
            check_scene(scene_before)

        def complete():
            fresh_status = self.state_revisions[status_key] > revision
            current = str(self.values[status_key]).strip()
            if fresh_status and (current == "Busy" or current == "Error" or current.startswith("Error:")):
                raise RuntimeError(f"Native screenshot capture failed: {current}")
            capture = str(self.values[id_key])
            return (capture != previous and re.fullmatch(r"[A-Za-z0-9_.:-]{1,96}", capture)
                    and capture not in {"unknown", "unavailable"} and fresh_status and current == "Ready")

        await self.service("take_screenshot")
        await self.wait(complete, timeout)
        capture_id = str(self.values[id_key])
        url = base_url.rstrip("/") + "/esp_screenshot/screen/image.jpg?" + urlencode({"capture": capture_id})

        def fetch():
            with urlopen(url, timeout=10) as response:
                payload = response.read()
                result = validate_screenshot_response(status=response.status, url=response.geturl(), expected_url=url,
                    mime=response.headers.get("Content-Type", ""), etag=response.headers.get("ETag"),
                    capture_id=capture_id, payload=payload, expected_size=expected_size)
            return payload, result

        payload, result = await asyncio.to_thread(fetch)
        if self.values[id_key] != capture_id or self.values[status_key] != "Ready":
            raise RuntimeError("Another native capture replaced this screenshot during download")
        scene_after = await checkpoint() if checkpoint is not None else None
        if checkpoint is not None:
            check_scene(scene_after)
        if scene_before != scene_after:
            raise RuntimeError("Native UI scene changed during capture")
        output.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
            stream.write(payload)
        return {**result, "file": output.name, "scene_verified": checkpoint is not None,
                "scene": scene_before, "physical_panel_qualified": False, "acoustic_qualified": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head", required=True)
    parser.add_argument("--candidate-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    payload = final_session_plan(args.head, args.candidate_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as stream:
        json.dump(payload, stream, indent=2)
        stream.write("\n")
    print(json.dumps({"status": "prepared", "physical": "not_run", "acoustic": "not_run"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
