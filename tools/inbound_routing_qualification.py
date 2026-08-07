#!/usr/bin/env python3
"""Qualify direct, DTMF and automation-overridden inbound SIP routing.

This runner uses only Home Assistant's public config-flow, automation and
service APIs. It snapshots the current VoIP Stack flow values, creates two
temporary automations, exercises the real Wildix trunk with bareSIP, then
restores the original integration configuration and automation states even
when a case fails.
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
sys.path.insert(0, str(ROOT / "test_runs"))


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


class HomeAssistantApi:
    """Small strict wrapper around the public HA REST API."""

    def __init__(self) -> None:
        from ha_playwright_auth import ha_token

        self._token = ha_token()
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
            f"{HA_BASE}{path}",
            json=data,
            timeout=20,
        )
        # HA's automation config endpoint returns 400, rather than 404, when
        # asked to delete an automation ID that is not present.
        if allow_missing and response.status_code in {400, 404}:
            return None
        response.raise_for_status()
        return response.json() if response.content else None

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, data: dict[str, Any] | None = None) -> Any:
        return self.request("POST", path, data or {})

    def delete(self, path: str, *, allow_missing: bool = False) -> Any:
        return self.request("DELETE", path, allow_missing=allow_missing)

    def state(self, entity_id: str) -> dict[str, Any]:
        return self.get(f"/api/states/{entity_id}")

    def service(
        self, domain: str, name: str, data: dict[str, Any] | None = None
    ) -> Any:
        return self.post(f"/api/services/{domain}/{name}", data or {})


class BareSip:
    """Run one real SIP user agent with deterministic keypad timing."""

    def __init__(self, config: Path = WILDIX_CONFIG) -> None:
        TEST_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen(
            ["baresip", "-f", str(config)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            cwd=TEST_CAPTURE_DIR,
        )
        os.close(slave)
        os.set_blocking(self.master, False)
        self.output = ""
        self.wait_for("registered successfully", 10)

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
            f"bareSIP timeout waiting for {' or '.join(needles)}: "
            f"{self.output[-2500:]}"
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
                f"{HA_BASE.replace('http', 'ws', 1)}/api/websocket",
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
        result = api.post(f"/api/config/config_entries/flow/{flow_id}", dict(self.base))
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
        if result.get("step_id") != "trunk" or self.trunk is None:
            raise RuntimeError(f"unexpected flow while applying trunk: {result}")
        trunk = dict(self.trunk)
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
    """Return the preferred browser phone call-state sensor."""

    candidates = [
        state
        for state in api.get("/api/states")
        if str(state.get("entity_id") or "").startswith("sensor.")
        and str((state.get("attributes") or {}).get("endpoint_id") or "").strip()
        == "default"
        and "ringing" in ((state.get("attributes") or {}).get("options") or ())
    ]
    if len(candidates) != 1:
        raise RuntimeError(
            f"expected one preferred browser call-state sensor, found {len(candidates)}"
        )
    return str(candidates[0]["entity_id"])


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
    callee: str = "",
) -> dict[str, Any]:
    def _match() -> dict[str, Any] | None:
        state = call_state(api)
        if state["state"] != expected:
            return None
        if callee and str(state.get("callee") or "") != callee:
            return None
        return state

    return wait_for(
        _match,
        timeout,
        f"{browser_call_state_entity(api)}={expected}/{callee}",
    )


def wait_event_state(
    api: HomeAssistantApi,
    expected: str,
    timeout: float = 15,
    *,
    callee: str = "",
) -> dict[str, Any]:
    def _match() -> dict[str, Any] | None:
        state = event_state(api)
        if str(state.get("state") or "") != expected:
            return None
        if callee and str(state.get("callee") or "") != callee:
            return None
        return state

    return wait_for(_match, timeout, f"{CALL_EVENT_ENTITY}={expected}/{callee}")


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
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out", default=str(ROOT / "test_runs" / "inbound_routing_matrix.json")
    )
    parser.add_argument("--only", action="append", default=[])
    args = parser.parse_args()
    api = HomeAssistantApi()
    snapshot = FlowSnapshot.capture(api)
    old_automation_was_on = api.state(OLD_AUTOMATION)["state"] == "on"
    inbound_automation_was_on = api.state(INBOUND_AUTOMATION)["state"] == "on"
    results: list[dict[str, Any]] = []
    active: list[BareSip] = []

    def case(name: str, run: Callable[[], dict[str, Any]]) -> None:
        if args.only and name not in args.only:
            return
        started = time.monotonic()
        try:
            detail = run()
            results.append(
                {
                    "name": name,
                    "status": "pass",
                    "seconds": round(time.monotonic() - started, 3),
                    **detail,
                }
            )
        except Exception as err:  # noqa: BLE001 - complete matrix before reporting.
            results.append(
                {
                    "name": name,
                    "status": "fail",
                    "seconds": round(time.monotonic() - started, 3),
                    "error": str(err),
                }
            )
        finally:
            while active:
                active.pop().close()
            with suppress(Exception):
                wait_call_state(api, "idle", 8)
            time.sleep(0.35)

    def caller() -> BareSip:
        item = BareSip()
        active.append(item)
        return item

    create_automations(api)
    set_automation(api, OLD_AUTOMATION, False)
    set_automation(api, INBOUND_AUTOMATION, False)
    set_automation(api, ROUTE_AUTOMATION, False)
    set_automation(api, TIMEOUT_AUTOMATION, False)
    try:

        def direct_default() -> dict[str, Any]:
            snapshot.apply(api, mode="direct", automation=False, default_target="HA")
            sip = caller()
            with EventTrace(api) as trace:
                started = sip.dial()
                ringing = wait_call_state(api, "ringing", 10)
                elapsed = time.monotonic() - started
                time.sleep(0.15)
            if elapsed >= 1.3:
                raise RuntimeError(f"direct route waited {elapsed:.3f}s")
            if "route_requested" in trace_types(
                trace, str(ringing.get("call_id") or "")
            ):
                raise RuntimeError(
                    "direct route emitted route_requested while disabled"
                )
            return {"elapsed": round(elapsed, 3), "call": ringing}

        case("direct_default_without_automation_window", direct_default)

        def direct_ring_group_waits_for_member_answer() -> dict[str, Any]:
            with temporary_switch(api, WS3_AUTO_ANSWER, False):
                snapshot.apply(
                    api,
                    mode="direct",
                    automation=False,
                    default_target="RG Casa",
                )
                sip = caller()
                with EventTrace(api) as trace:
                    sip.dial()
                    ringing = wait_event_state(
                        api, "ringing", 10, callee="RG Casa"
                    )
                    # Reproduce issue #74 exactly: an immediate-mode trunk
                    # route must remain provisional until a group member
                    # explicitly answers.
                    time.sleep(2.0)
                    still_ringing = event_state(api)
            if still_ringing.get("state") != "ringing":
                raise RuntimeError(
                    f"ring group did not remain provisional: {still_ringing}"
                )
            types = trace_types(trace, str(ringing.get("call_id") or ""))
            if "answered" in types or "in_call" in types:
                raise RuntimeError(f"ring group auto-answered: {types}")
            if "Call established" in sip.read():
                raise RuntimeError("trunk received a final answer without a winner")
            return {"events": types, "call": still_ringing}

        case(
            "direct_ring_group_waits_for_member_answer",
            direct_ring_group_waits_for_member_answer,
        )

        def direct_window_no_match() -> dict[str, Any]:
            snapshot.apply(api, mode="direct", automation=True, default_target="HA")
            set_automation(api, ROUTE_AUTOMATION, False)
            sip = caller()
            with EventTrace(api) as trace:
                started = sip.dial()
                ringing = wait_call_state(api, "ringing", 10)
                elapsed = time.monotonic() - started
                time.sleep(0.15)
            types = trace_types(trace, str(ringing.get("call_id") or ""))
            if elapsed < 1.3:
                raise RuntimeError(
                    f"enabled route window ended too early: {elapsed:.3f}s"
                )
            if "route_requested" not in types:
                raise RuntimeError(f"route_requested missing: {types}")
            return {"elapsed": round(elapsed, 3), "events": types, "call": ringing}

        case("direct_enabled_no_matching_override_uses_default", direct_window_no_match)

        def direct_override() -> dict[str, Any]:
            snapshot.apply(api, mode="direct", automation=True, default_target="HA")
            set_automation(api, ROUTE_AUTOMATION, True)
            previous = automation_last_triggered(api, ROUTE_AUTOMATION)
            sip = caller()
            with temporary_switch(api, WS3_AUTO_ANSWER, True):
                with EventTrace(api) as trace:
                    sip.dial()
                    connected = wait_event_state(
                        api, "in_call", 15, callee=ROUTE_DESTINATION
                    )
                    time.sleep(0.15)
            triggered = automation_last_triggered(api, ROUTE_AUTOMATION)
            if not triggered or triggered == previous:
                raise RuntimeError("native event.received automation did not trigger")
            types = trace_types(trace, str(connected.get("call_id") or ""))
            if "route_requested" not in types or "forwarding" not in types:
                raise RuntimeError(f"incomplete override trace: {types}")
            return {"events": types, "call": connected}

        case("direct_native_event_override", direct_override)
        set_automation(api, ROUTE_AUTOMATION, False)

        def dtmf_no_digits_default() -> dict[str, Any]:
            snapshot.apply(
                api,
                mode="dtmf",
                automation=False,
                default_target="HA",
                timeout_seconds=2,
            )
            sip = caller()
            with EventTrace(api) as trace:
                started = sip.dial()
                ringing = wait_call_state(api, "ringing", 15)
                elapsed = time.monotonic() - started
                time.sleep(0.15)
            types = trace_types(trace, str(ringing.get("call_id") or ""))
            if not 9.5 <= elapsed <= 12.5:
                raise RuntimeError(f"DTMF first-digit timeout was {elapsed:.3f}s")
            if "route_requested" in types:
                raise RuntimeError(
                    f"disabled automation emitted route request: {types}"
                )
            return {"elapsed": round(elapsed, 3), "events": types, "call": ringing}

        case("dtmf_no_digits_uses_default", dtmf_no_digits_default)

        def dtmf_no_digits_override() -> dict[str, Any]:
            snapshot.apply(
                api,
                mode="dtmf",
                automation=True,
                default_target="HA",
                timeout_seconds=2,
            )
            set_automation(api, ROUTE_AUTOMATION, True)
            previous = automation_last_triggered(api, ROUTE_AUTOMATION)
            sip = caller()
            with temporary_switch(api, WS3_AUTO_ANSWER, True):
                with EventTrace(api) as trace:
                    started = sip.dial()
                    connected = wait_event_state(
                        api, "in_call", 15, callee=ROUTE_DESTINATION
                    )
                    elapsed = time.monotonic() - started
                    time.sleep(0.15)
            triggered = automation_last_triggered(api, ROUTE_AUTOMATION)
            if not triggered or triggered == previous:
                raise RuntimeError("no-digits native override did not trigger")
            if elapsed < 1.7:
                raise RuntimeError(f"automation bypassed DTMF window: {elapsed:.3f}s")
            types = trace_types(trace, str(connected.get("call_id") or ""))
            return {"elapsed": round(elapsed, 3), "events": types, "call": connected}

        case("dtmf_no_digits_then_native_override", dtmf_no_digits_override)

        def dtmf_valid_digits() -> dict[str, Any]:
            snapshot.apply(
                api,
                mode="dtmf",
                automation=True,
                default_target="HA",
                timeout_seconds=5,
            )
            set_automation(api, ROUTE_AUTOMATION, True)
            previous = automation_last_triggered(api, ROUTE_AUTOMATION)
            sip = caller()
            with EventTrace(api) as trace:
                started = sip.dial()
                sip.wait_for_dtmf_media()
                sip.digits(ASSIST_EXTENSION)
                connected = wait_event_state(api, "in_call", 12, callee="Troiaio")
                elapsed = time.monotonic() - started
                time.sleep(0.15)
            triggered = automation_last_triggered(api, ROUTE_AUTOMATION)
            if triggered != previous:
                raise RuntimeError("explicit DTMF incorrectly triggered automation")
            types = trace_types(trace, str(connected.get("call_id") or ""))
            if "route_requested" in types:
                raise RuntimeError(f"explicit DTMF emitted route_requested: {types}")
            if elapsed >= 4.5:
                raise RuntimeError(
                    f"exact extension did not terminate collection: {elapsed:.3f}s"
                )
            return {"elapsed": round(elapsed, 3), "events": types, "call": connected}

        case("dtmf_valid_extension_bypasses_automation", dtmf_valid_digits)

        def dtmf_invalid_digits() -> dict[str, Any]:
            snapshot.apply(
                api,
                mode="dtmf",
                automation=True,
                default_target="HA",
                timeout_seconds=3,
            )
            set_automation(api, ROUTE_AUTOMATION, True)
            previous = automation_last_triggered(api, ROUTE_AUTOMATION)
            sip = caller()
            with EventTrace(api) as trace:
                sip.dial()
                sip.wait_for_dtmf_media()
                sip.digits("9999")
                terminal = wait_for(
                    lambda: (
                        item
                        if (item := api.state(CALL_EVENT_ENTITY)["attributes"]).get(
                            "terminal_reason"
                        )
                        == "route_not_found"
                        else None
                    ),
                    8,
                    "route_not_found",
                )
                time.sleep(0.15)
            triggered = automation_last_triggered(api, ROUTE_AUTOMATION)
            if triggered != previous:
                raise RuntimeError(
                    "invalid explicit DTMF incorrectly triggered automation"
                )
            types = trace_types(trace, str(terminal.get("call_id") or ""))
            if "route_requested" in types:
                raise RuntimeError(f"invalid DTMF fell back to automation: {types}")
            return {"events": types, "terminal": terminal}

        case("dtmf_invalid_extension_is_terminal", dtmf_invalid_digits)
        set_automation(api, ROUTE_AUTOMATION, False)

        def no_answer_state_for() -> dict[str, Any]:
            snapshot.apply(api, mode="direct", automation=False, default_target="HA")
            set_automation(api, TIMEOUT_AUTOMATION, True)
            previous = automation_last_triggered(api, TIMEOUT_AUTOMATION)
            sip = caller()
            with EventTrace(api) as trace:
                sip.dial()
                ringing = wait_call_state(api, "ringing", 10)
                connected = wait_event_state(api, "in_call", 12, callee="Troiaio")
                time.sleep(0.15)
            triggered = automation_last_triggered(api, TIMEOUT_AUTOMATION)
            if not triggered or triggered == previous:
                raise RuntimeError("native state-for automation did not trigger")
            if connected.get("call_id") != ringing.get("call_id"):
                raise RuntimeError("state-for forwarding changed logical call_id")
            types = trace_types(trace, str(connected.get("call_id") or ""))
            return {"events": types, "ringing": ringing, "connected": connected}

        case("native_state_for_no_answer_to_assist", no_answer_state_for)
        set_automation(api, TIMEOUT_AUTOMATION, False)

        def cancel_during_dtmf() -> dict[str, Any]:
            snapshot.apply(
                api,
                mode="dtmf",
                automation=True,
                default_target="HA",
                timeout_seconds=3,
            )
            set_automation(api, ROUTE_AUTOMATION, True)
            previous = automation_last_triggered(api, ROUTE_AUTOMATION)
            previous_call_id = str(event_state(api).get("call_id") or "")
            sip = caller()
            with EventTrace(api) as trace:
                sip.dial()
                sip.wait_for_dtmf_media()
                time.sleep(0.35)
                sip.hangup()
                terminal = wait_for(
                    lambda: (
                        item
                        if (
                            (item := event_state(api)).get("call_id")
                            != previous_call_id
                            and str(item.get("state") or "")
                            not in {"ringing", "connecting", "in_call"}
                            and str(item.get("terminal_reason") or "")
                            in {"cancelled", "remote_hangup"}
                        )
                        else None
                    ),
                    8,
                    "cancelled DTMF call terminal event",
                )
                time.sleep(3.2)
            triggered = automation_last_triggered(api, ROUTE_AUTOMATION)
            if triggered != previous:
                raise RuntimeError("cancelled DTMF call later triggered routing")
            live = [
                item
                for item in trace.items
                if item.get("state") in {"ringing", "in_call", "remote_ringing"}
                and item.get("observed_at", 0) > time.time() - 2.5
            ]
            if live:
                raise RuntimeError(f"cancelled call resurrected: {live}")
            return {"state": terminal}

        case("caller_cancel_during_dtmf_window", cancel_during_dtmf)
    finally:
        while active:
            active.pop().close()
        cleanup_automations(api)
        with suppress(Exception):
            snapshot.apply(api)
        with suppress(Exception):
            set_automation(api, OLD_AUTOMATION, old_automation_was_on)
        with suppress(Exception):
            set_automation(api, INBOUND_AUTOMATION, inbound_automation_was_on)
        for path in TEST_CAPTURE_DIR.glob("dump-sip:*.wav"):
            path.unlink(missing_ok=True)

    Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 1 if any(item["status"] != "pass" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
