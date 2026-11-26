#!/usr/bin/env python3
"""Shared SIP qualification helpers and retired legacy menu command.

The old parent-entry menu executor fails closed. Current direct called-number
qualification uses run_trunk_did_lab.py and configured Fusion or HA-peer routes.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager, suppress
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import threading
import time
from typing import Any, Callable

import aiohttp
import requests


ROOT = Path(__file__).resolve().parents[1]
TEST_CAPTURE_DIR = ROOT / "test_captures"
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tools"))


HA_BASE = os.environ.get("HA_BASE", "http://127.0.0.1:18123").rstrip("/")
WILDIX_CONFIG = Path(os.environ.get("WILDIX_CONFIG", "/home/codex/.baresip-wildix-426"))
OLD_AUTOMATION = "automation.voip_ha_non_risponde_inoltra_ad_assist"
INBOUND_AUTOMATION = "automation.voip_inbound_trunk_to_rg_casa"
ROUTE_AUTOMATION_ID = "codex_voip_route_override_matrix"
ROUTE_AUTOMATION = f"automation.{ROUTE_AUTOMATION_ID}"
TIMEOUT_AUTOMATION_ID = "codex_voip_no_answer_matrix"
TIMEOUT_AUTOMATION = f"automation.{TIMEOUT_AUTOMATION_ID}"
CALL_EVENT_ENTITY = "event.voip_stack_call"
WS3_AUTO_ANSWER = "switch.cucina_waveshare_s3_audio_auto_answer"
TRUNK_NUMBER = "427"
ROUTE_DESTINATION = "Waveshare S3 Audio"
ASSIST_EXTENSION = "1666"
SECONDARY_EXTENSION = os.environ.get("VOIP_SECONDARY_EXTENSION", "667")
SECONDARY_CALLEE = os.environ.get("VOIP_SECONDARY_CALLEE", "Test")
SECONDARY_EXPECTED_STATE = os.environ.get(
    "VOIP_SECONDARY_EXPECTED_STATE", "ringing"
)
SECONDARY_HOLD_SECONDS = float(os.environ.get("VOIP_SECONDARY_HOLD_SECONDS", "0"))
ROUTE_HOLD_SECONDS = float(os.environ.get("VOIP_ROUTE_HOLD_SECONDS", "0"))


class HomeAssistantApi:
    """Small strict wrapper around the public HA REST API."""

    def __init__(
        self,
        *,
        base_url: str = HA_BASE,
        token: str | None = None,
    ) -> None:
        if token is None:
            lab_credentials = Path(os.environ.get("HA_LAB_CREDENTIALS", ""))
            auth_file = Path(os.environ.get("HA_AUTH_FILE", ""))
            if lab_credentials.is_file():
                from ha_voip_lab.auth import lab_token

                token = lab_token(base_url, lab_credentials)
            elif auth_file.is_file():
                from live_voip_qualification import _refresh_ha_token

                token = _refresh_ha_token(auth_file)
            else:
                from ha_playwright_auth import ha_token

                token = ha_token()
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            }
        )

    def request(
        self,
        method: str,
        path: str,
        data: dict[str, Any] | None = None,
        *,
        allow_missing: bool = False,
    ) -> Any:
        response = self.session.request(
            method,
            f"{self.base_url}{path}",
            json=data,
            timeout=20,
        )
        # HA's automation config endpoint returns 400, rather than 404, when
        # asked to delete an automation ID that is not present.
        if allow_missing and response.status_code in {400, 404}:
            return None
        try:
            response.raise_for_status()
        except requests.HTTPError as error:
            raise RuntimeError(
                f"Home Assistant API {method} {path} failed: {response.text[:2000]}"
            ) from error
        return response.json() if response.content else None

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, data: dict[str, Any] | None = None) -> Any:
        return self.request("POST", path, data or {})

    def delete(self, path: str, *, allow_missing: bool = False) -> Any:
        return self.request("DELETE", path, allow_missing=allow_missing)

    def state(self, entity_id: str) -> dict[str, Any]:
        return self.get(f"/api/states/{entity_id}")

    def optional_state(self, entity_id: str) -> dict[str, Any] | None:
        return self.request(
            "GET", f"/api/states/{entity_id}", allow_missing=True
        )

    def service(
        self, domain: str, name: str, data: dict[str, Any] | None = None
    ) -> Any:
        return self.post(f"/api/services/{domain}/{name}", data or {})


class BareSip:
    """Run one real SIP user agent with deterministic keypad timing."""

    def __init__(
        self,
        config: Path = WILDIX_CONFIG,
        *,
        wait_registered: bool = True,
        working_dir: Path | None = None,
    ) -> None:
        capture_dir = working_dir if working_dir is not None else TEST_CAPTURE_DIR
        capture_dir.mkdir(parents=True, exist_ok=True)
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen(
            ["baresip", "-f", str(config)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            cwd=capture_dir,
        )
        os.close(slave)
        os.set_blocking(self.master, False)
        self.output = ""
        self.wait_for("registered successfully" if wait_registered else "ready", 10)

    def read(self) -> str:
        while True:
            ready, _, _ = select.select([self.master], [], [], 0)
            if not ready:
                break
            try:
                chunk = os.read(self.master, 65536)
            except (BlockingIOError, OSError):
                break
            if not chunk:
                break
            self.output += chunk.decode(errors="replace")
        return self.output

    def wait_for(self, needle: str, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle.lower() in self.read().lower():
                return self.output
            if self.proc.poll() is not None:
                break
            time.sleep(0.03)
        raise RuntimeError(
            f"bareSIP timeout waiting for {needle}: {self.output[-2500:]}"
        )

    def wait_for_any(self, needles: tuple[str, ...], timeout: float) -> str:
        deadline = time.monotonic() + timeout
        wanted = tuple(needle.lower() for needle in needles)
        while time.monotonic() < deadline:
            output = self.read().lower()
            if any(needle in output for needle in wanted):
                return self.output
            if self.proc.poll() is not None:
                break
            time.sleep(0.03)
        raise RuntimeError(
            f"bareSIP timeout waiting for {' or '.join(needles)}: {self.output[-2500:]}"
        )

    def wait_for_dtmf_media(self, timeout: float = 10) -> str:
        """Wait for provisional RFC 4733 media or a confirmed INFO dialog."""
        return self.wait_for_any(("183 Session Progress", "Call established"), timeout)

    def command(self, command: str) -> None:
        os.write(self.master, f"{command}\n".encode())

    def dial(self, target: str = TRUNK_NUMBER) -> float:
        started = time.monotonic()
        self.command(f"/dial {target}")
        return started

    def digits(self, digits: str, interval: float = 0.45) -> None:
        """Send bareSIP menu digits without tool/PTY round-trip latency."""
        for digit in digits:
            if digit not in "0123456789*#ABCD":
                raise ValueError(f"unsupported DTMF digit: {digit}")
            os.write(self.master, digit.encode())
            time.sleep(interval)

    def hangup(self) -> None:
        if self.proc.poll() is None:
            self.command("/hangup")

    def close(self) -> None:
        if self.proc.poll() is None:
            with suppress(Exception):
                self.hangup()
            with suppress(Exception):
                self.command("/quit")
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                with suppress(subprocess.TimeoutExpired):
                    self.proc.wait(timeout=2)
        with suppress(OSError):
            os.close(self.master)


class EventTrace:
    """Capture every backend call event through HA's WebSocket API."""

    def __init__(self, api: HomeAssistantApi) -> None:
        self.api = api
        self.items: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.error = ""
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> EventTrace:
        self._thread.start()
        if not self._ready.wait(8):
            raise RuntimeError("timeout subscribing to VoIP Stack call events")
        if self.error:
            raise RuntimeError(self.error)
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _run(self) -> None:
        try:
            asyncio.run(self._listen())
        except Exception as err:  # noqa: BLE001 - relay to the owning test.
            self.error = f"call-event WebSocket failed: {err}"
            self._ready.set()

    async def _listen(self) -> None:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                f"{self.api.base_url.replace('http', 'ws', 1)}/api/websocket",
                heartbeat=20,
            ) as websocket:
                hello = await websocket.receive_json(timeout=5)
                if hello.get("type") != "auth_required":
                    raise RuntimeError(f"unexpected WebSocket greeting: {hello}")
                await websocket.send_json(
                    {"type": "auth", "access_token": self.api._token}
                )
                authenticated = await websocket.receive_json(timeout=5)
                if authenticated.get("type") != "auth_ok":
                    raise RuntimeError(
                        f"WebSocket authentication failed: {authenticated}"
                    )
                await websocket.send_json(
                    {"id": 1, "type": "voip_stack/subscribe_call_events"}
                )
                subscribed = await websocket.receive_json(timeout=5)
                if not subscribed.get("success"):
                    raise RuntimeError(f"call-event subscription failed: {subscribed}")
                self._ready.set()
                while not self._stop.is_set():
                    try:
                        message = await websocket.receive(timeout=0.1)
                    except asyncio.TimeoutError:
                        continue
                    if message.type is aiohttp.WSMsgType.CLOSED:
                        break
                    if message.type is not aiohttp.WSMsgType.TEXT:
                        continue
                    payload = json.loads(message.data)
                    event = payload.get("event") or {}
                    item = dict(event.get("data") or event)
                    if not item:
                        continue
                    item["observed_at"] = time.time()
                    self.items.append(item)

    def for_call(self, call_id: str) -> list[dict[str, Any]]:
        return [item for item in self.items if item.get("call_id") == call_id]


def schema_defaults(result: dict[str, Any]) -> dict[str, Any]:
    """Return the exact values suggested by one HA config-flow form."""
    values: dict[str, Any] = {}
    for field in result.get("data_schema") or []:
        name = str(field["name"])
        if "default" in field:
            values[name] = field["default"]
        elif (field.get("description") or {}).get("suggested_value") is not None:
            values[name] = field["description"]["suggested_value"]
    return values


class FlowSnapshot:
    """Captured values for every stage of the current VoIP Stack flow."""

    def __init__(
        self,
        *,
        entry_id: str,
        base: dict[str, Any],
        video: dict[str, Any] | None,
        assist: dict[str, Any] | None,
        trunk: dict[str, Any] | None,
    ) -> None:
        self.entry_id = entry_id
        self.base = base
        self.video = video
        self.assist = assist
        self.trunk = trunk

    @classmethod
    def capture(cls, api: HomeAssistantApi) -> FlowSnapshot:
        entries = api.get("/api/config/config_entries/entry")
        entry = next(item for item in entries if item.get("domain") == "voip_stack")
        entry_id = str(entry["entry_id"])
        result = api.post(
            "/api/config/config_entries/flow",
            {
                "handler": "voip_stack",
                "show_advanced_options": True,
                "entry_id": entry_id,
            },
        )
        flow_id = str(result["flow_id"])
        base = schema_defaults(result)
        result = api.post(f"/api/config/config_entries/flow/{flow_id}", base)
        video = None
        if result.get("step_id") == "video":
            video = schema_defaults(result)
            result = api.post(f"/api/config/config_entries/flow/{flow_id}", video)
        assist = None
        if result.get("step_id") == "assist":
            assist = schema_defaults(result)
            result = api.post(f"/api/config/config_entries/flow/{flow_id}", assist)
        if result.get("type") == "abort":
            return cls(
                entry_id=entry_id,
                base=base,
                video=video,
                assist=assist,
                trunk=None,
            )
        if result.get("step_id") != "trunk":
            raise RuntimeError(f"unexpected flow while capturing trunk: {result}")
        trunk = schema_defaults(result)
        # Deliberately do not submit the last form: capture must not reload HA.
        return cls(
            entry_id=entry_id,
            base=base,
            video=video,
            assist=assist,
            trunk=trunk,
        )

    def apply(
        self,
        api: HomeAssistantApi,
        *,
        mode: str | None = None,
        automation: bool | None = None,
        default_target: str | None = None,
        timeout_seconds: int | None = None,
        terminator: str | None = None,
        trunk_override: dict[str, Any] | None = None,
    ) -> None:
        result = api.post(
            "/api/config/config_entries/flow",
            {
                "handler": "voip_stack",
                "show_advanced_options": True,
                "entry_id": self.entry_id,
            },
        )
        flow_id = str(result["flow_id"])
        base = dict(self.base)
        if trunk_override is not None:
            base["trunk_enabled"] = True
        result = api.post(f"/api/config/config_entries/flow/{flow_id}", base)
        if result.get("step_id") == "video":
            result = api.post(
                f"/api/config/config_entries/flow/{flow_id}",
                dict(self.video or {}),
            )
        if result.get("step_id") == "assist":
            result = api.post(
                f"/api/config/config_entries/flow/{flow_id}",
                dict(self.assist or {}),
            )
        if result.get("step_id") == "trunk":
            trunk = schema_defaults(result)
            allowed = set(trunk)
            trunk.update(
                (key, value)
                for key, value in (self.trunk or {}).items()
                if key in allowed
            )
            trunk.update(
                (key, value)
                for key, value in (trunk_override or {}).items()
                if key in allowed
            )
            if mode is not None:
                trunk["trunk_inbound_mode"] = mode
            if automation is not None:
                trunk["automation_routing_enabled"] = automation
            if default_target is not None:
                trunk["trunk_inbound_default_target"] = default_target
            if timeout_seconds is not None:
                trunk["trunk_dtmf_timeout_ms"] = timeout_seconds
            if terminator is not None:
                trunk["trunk_dtmf_terminator"] = terminator
            result = api.post(f"/api/config/config_entries/flow/{flow_id}", trunk)
        if (
            result.get("type") != "abort"
            or result.get("reason") != "reconfigure_successful"
        ):
            raise RuntimeError(f"VoIP Stack reconfigure failed: {result}")
        wait_for(
            lambda: next(
                (
                    item.get("state") == "loaded"
                    for item in api.get("/api/config/config_entries/entry")
                    if item.get("entry_id") == self.entry_id
                ),
                False,
            ),
            15,
            "VoIP Stack reload",
        )
        # async_update_reload_and_abort returns before the integration reload
        # necessarily reaches its entity platforms. ``last_updated`` is not a
        # generation marker when the replacement publishes the same value, so
        # wait on the public readiness state instead.
        wait_for(
            lambda: next(
                (
                    state
                    for state in api.get("/api/states")
                    if str(state.get("entity_id") or "").startswith("sensor.")
                    and str((state.get("attributes") or {}).get("endpoint_id") or "")
                    .strip()
                    .startswith(("default", "browser:"))
                    and str(state.get("state") or "") not in {"unavailable", "unknown"}
                ),
                None,
            ),
            15,
            "VoIP Stack endpoint reload",
        )
        time.sleep(1.0)
        # event.received targets an EventEntity owned by this integration.
        # Re-attach automation triggers after that entity has returned instead
        # of relying on HA's periodic unavailable-target retry.
        api.service("automation", "reload")
        time.sleep(0.5)


def wait_for(predicate: Callable[[], Any], timeout: float, label: str) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.05)
    raise RuntimeError(f"timeout waiting for {label}; last={last!r}")


def browser_call_state_entity(api: HomeAssistantApi) -> str:
    """Return the explicitly selected browser phone call-state sensor."""

    endpoint_id = os.environ.get("VOIP_QUALIFICATION_POLICY_ENDPOINT_ID", "default")

    def resolve() -> str:
        candidates = [
            state
            for state in api.get("/api/states")
            if str(state.get("entity_id") or "").startswith("sensor.")
            and str((state.get("attributes") or {}).get("endpoint_id") or "").strip()
            == endpoint_id
            and "ringing" in ((state.get("attributes") or {}).get("options") or ())
        ]
        return str(candidates[0]["entity_id"]) if len(candidates) == 1 else ""

    return str(
        wait_for(resolve, 8, f"browser call-state sensor for endpoint {endpoint_id}")
    )


def call_state(api: HomeAssistantApi) -> dict[str, Any]:
    state = api.state(browser_call_state_entity(api))
    return {"state": state["state"], **dict(state.get("attributes") or {})}


def event_state(api: HomeAssistantApi) -> dict[str, Any]:
    """Return the transport-neutral call occurrence projected by HA."""
    state = api.state(CALL_EVENT_ENTITY)
    return dict(state.get("attributes") or {})


def wait_call_state(
    api: HomeAssistantApi,
    expected: str,
    timeout: float = 15,
    *,
    expected_remote_party: str = "",
    expected_local_party: str = "",
) -> dict[str, Any]:
    def _match() -> dict[str, Any] | None:
        state = call_state(api)
        if state["state"] != expected:
            return None
        if expected_remote_party and expected_remote_party not in {
            str(state.get("callee") or ""),
            str(state.get("peer_name") or ""),
        }:
            return None
        if expected_local_party and expected_local_party != str(
            state.get("local_name") or ""
        ):
            return None
        return state

    return wait_for(
        _match,
        timeout,
        f"{browser_call_state_entity(api)}={expected}/{expected_remote_party}",
    )


def wait_event_state(
    api: HomeAssistantApi,
    expected: str,
    timeout: float = 15,
    *,
    expected_remote_party: str = "",
    expected_local_party: str = "",
) -> dict[str, Any]:
    def _match() -> dict[str, Any] | None:
        state = event_state(api)
        if str(state.get("state") or "") != expected:
            return None
        if expected_remote_party and expected_remote_party not in {
            str(state.get("callee") or ""),
            str(state.get("peer_name") or ""),
        }:
            return None
        if expected_local_party and expected_local_party != str(
            state.get("local_name") or ""
        ):
            return None
        return state

    return wait_for(
        _match,
        timeout,
        f"{CALL_EVENT_ENTITY}={expected}/{expected_remote_party}",
    )


@contextmanager
def temporary_switch(api: HomeAssistantApi, entity_id: str, enabled: bool):
    """Apply one test-owned switch state and restore the user's value."""

    original = api.state(entity_id)["state"] == "on"
    if original != enabled:
        api.service(
            "switch",
            "turn_on" if enabled else "turn_off",
            {"entity_id": entity_id},
        )
    try:
        yield
    finally:
        if original != enabled:
            api.service(
                "switch",
                "turn_on" if original else "turn_off",
                {"entity_id": entity_id},
            )


def automation_last_triggered(api: HomeAssistantApi, entity_id: str) -> str:
    state = api.state(entity_id)
    return str((state.get("attributes") or {}).get("last_triggered") or "")


def create_automations(api: HomeAssistantApi) -> None:
    call_state_entity = browser_call_state_entity(api)
    for automation_id in (ROUTE_AUTOMATION_ID, TIMEOUT_AUTOMATION_ID):
        api.delete(f"/api/config/automation/config/{automation_id}", allow_missing=True)
    api.post(
        f"/api/config/automation/config/{ROUTE_AUTOMATION_ID}",
        {
            "id": ROUTE_AUTOMATION_ID,
            "alias": "Codex VoIP route override matrix",
            "description": "Temporary native EventEntity qualification automation",
            "triggers": [
                {
                    "trigger": "event.received",
                    "target": {"entity_id": CALL_EVENT_ENTITY},
                    "options": {"event_type": ["route_requested"]},
                }
            ],
            "conditions": [
                {
                    "condition": "state",
                    "entity_id": CALL_EVENT_ENTITY,
                    "attribute": "ingress",
                    "state": "trunk",
                }
            ],
            "actions": [
                {
                    "action": "voip_stack.select_inbound_destination",
                    "data": {
                        "destination": ROUTE_DESTINATION,
                    },
                }
            ],
            "mode": "parallel",
            "max": 10,
        },
    )
    api.post(
        f"/api/config/automation/config/{TIMEOUT_AUTOMATION_ID}",
        {
            "id": TIMEOUT_AUTOMATION_ID,
            "alias": "Codex VoIP no-answer matrix",
            "description": "Temporary native state-for qualification automation",
            "triggers": [
                {
                    "trigger": "state",
                    "entity_id": call_state_entity,
                    "to": "ringing",
                    "for": "00:00:02",
                }
            ],
            "conditions": [
                {
                    "condition": "state",
                    "entity_id": call_state_entity,
                    "attribute": "direction",
                    "state": "incoming",
                }
            ],
            "actions": [
                {
                    "action": "voip_stack.forward",
                    "data": {
                        "destination": ASSIST_EXTENSION,
                        "on_failure": "resume",
                    },
                }
            ],
            "mode": "parallel",
            "max": 10,
        },
    )
    api.service("automation", "reload")
    wait_for(lambda: api.state(ROUTE_AUTOMATION), 10, ROUTE_AUTOMATION)
    wait_for(lambda: api.state(TIMEOUT_AUTOMATION), 10, TIMEOUT_AUTOMATION)


def set_automation(api: HomeAssistantApi, entity_id: str, enabled: bool) -> None:
    api.service(
        "automation",
        "turn_on" if enabled else "turn_off",
        {"entity_id": entity_id, **({"stop_actions": True} if not enabled else {})},
    )
    if enabled:
        # event.received may have been unavailable while VoIP Stack reloaded.
        # A reload after restoring the automation to on attaches that entity
        # trigger immediately instead of waiting for HA's retry interval.
        api.service("automation", "reload")
        api.service("automation", "turn_on", {"entity_id": entity_id})
    wait_for(
        lambda: (
            api.state(entity_id)
            if api.state(entity_id).get("state") == ("on" if enabled else "off")
            else None
        ),
        10,
        f"{entity_id} {'on' if enabled else 'off'}",
    )
    if enabled:
        # Give HA one loop turn to attach the restored trigger before placing
        # the SIP INVITE that is supposed to exercise it.
        time.sleep(0.25)


def cleanup_automations(api: HomeAssistantApi) -> None:
    for entity_id in (ROUTE_AUTOMATION, TIMEOUT_AUTOMATION):
        with suppress(requests.RequestException):
            set_automation(api, entity_id, False)
    for automation_id in (ROUTE_AUTOMATION_ID, TIMEOUT_AUTOMATION_ID):
        with suppress(requests.RequestException):
            api.delete(
                f"/api/config/automation/config/{automation_id}",
                allow_missing=True,
            )
    with suppress(requests.RequestException):
        api.service("automation", "reload")


def trace_types(trace: EventTrace, call_id: str) -> list[str]:
    types: list[str] = []
    for item in trace.for_call(call_id):
        explicit = str(item.get("event_type") or item.get("event") or "")
        state = str(item.get("state") or "")
        if explicit:
            types.append(explicit)
        elif item.get("route_request"):
            types.append("route_requested")
        elif state == "in_call":
            types.append(
                "answered" if item.get("direction") != "outgoing" else "connected"
            )
        elif state:
            types.append(state)
    return types


def main() -> int:
    parser = argparse.ArgumentParser(description="Retired universal trunk menu qualification")
    parser.add_argument("--only", action="append", help="Retired case selector; execution is refused")
    parser.add_argument("--out", help="Retired output option")
    parser.add_argument("--result-name", help="Retired result label")
    parser.parse_args()
    parser.error("The legacy inbound menu matrix is retired; use scripts/run_trunk_did_lab.py with an explicitly configured Fusion or HA-peer profile")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
