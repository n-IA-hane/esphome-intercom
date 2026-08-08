#!/usr/bin/env python3
"""Live HA + ESP qualification matrix for VoIP Stack.

This is intentionally a real-system runner, not a simulator. It drives the
Home Assistant integration through REST/websocket and drives ESP devices through
the native ESPHome API, then asserts both sides converge to the expected state.
Use it after deploying HA/ESP firmware changes that touch routing, signaling,
phonebook sync, card-visible state, ring groups, or conference groups.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
import contextlib
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
import importlib.util
import json
import os
from pathlib import Path
import ssl
import subprocess
import time
from typing import Any
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit

try:
    from aioesphomeapi import APIClient
except (
    ModuleNotFoundError
):  # pragma: no cover - dependency-light CI only imports contracts.
    APIClient = None

try:
    import websockets
except (
    ModuleNotFoundError
):  # pragma: no cover - dependency-light CI only imports contracts.
    websockets = None


DEFAULT_HA_URL = os.environ.get("HA_URL", "http://127.0.0.1:18123").rstrip("/")
DEFAULT_TOKEN_FILE = Path("/home/codex/.secrets/esphome-intercom/ha_token_codex")
DEFAULT_AUTH_FILE = Path("/home/codex/.secrets/esphome-intercom/ha_home_auth.json")
OUT = Path("test_runs/live_voip_qualification")
ROOT = Path(__file__).resolve().parents[1]


def candidate_revision() -> dict[str, object]:
    """Identify the exact source revision exercised by a live artifact."""

    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
    ).strip()
    dirty = bool(
        subprocess.check_output(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=ROOT,
            text=True,
        ).strip()
    )
    return {"commit": commit, "dirty": dirty}


def _refresh_ha_token(auth_file: Path) -> str:
    """Exchange the durable local refresh credential without printing it."""

    auth = json.loads(auth_file.read_text(encoding="utf-8"))
    token_url = f"{str(auth['hass_url']).rstrip('/')}/auth/token"
    body = urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": auth["refresh_token"],
            "client_id": auth["client_id"],
        }
    ).encode()
    request = urllib.request.Request(token_url, data=body, method="POST")
    with urllib.request.urlopen(request, timeout=14) as response:
        return str(json.load(response)["access_token"]).strip()


def qualification_token(args: argparse.Namespace) -> str:
    """Load a live-test token without persisting a refreshed credential."""
    if args.token:
        return str(args.token).strip()
    helper_path = (
        Path(__file__).resolve().parents[1] / "test_runs/ha_playwright_auth.py"
    )
    if not helper_path.is_file():
        if args.auth_file.is_file():
            return _refresh_ha_token(args.auth_file)
        return args.token_file.read_text(encoding="utf-8").strip()
    spec = importlib.util.spec_from_file_location(
        "_voip_live_ha_auth",
        helper_path,
    )
    if spec is None or spec.loader is None:
        if args.auth_file.is_file():
            return _refresh_ha_token(args.auth_file)
        return args.token_file.read_text(encoding="utf-8").strip()
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return str(module.ha_token()).strip()


def normalize_ha_url(value: str) -> str:
    url = str(value or "").strip().rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("HA URL must be an absolute http:// or https:// URL")
    return url


def ha_ssl_context(base_url: str, *, insecure: bool) -> ssl.SSLContext | None:
    if urlsplit(base_url).scheme != "https":
        return None
    return (
        ssl._create_unverified_context() if insecure else ssl.create_default_context()
    )


def norm(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value or "").strip().lower().replace(" ", "_")


async def maybe_await(result: Any) -> None:
    if hasattr(result, "__await__"):
        await result


@dataclass(frozen=True)
class EspDevice:
    key: str
    name: str
    host: str
    port: int = 6053
    password: str = ""
    action_prefix: str = ""
    ha_state_entity: str = ""
    runtime_entity: str = ""


DEFAULT_ESPS = {
    "p4": EspDevice(
        "p4",
        "Waveshare P4 Touch",
        "192.168.1.45",
        action_prefix="waveshare_p4_touch",
        ha_state_entity="sensor.waveshare_p4_touch_voip_state",
        runtime_entity="sensor.waveshare_p4_touch_runtime_snapshot",
    ),
    "ws3": EspDevice(
        "ws3",
        "Waveshare S3 Audio",
        "192.168.1.47",
        action_prefix="cucina_waveshare_s3_audio",
        ha_state_entity="sensor.cucina_waveshare_s3_audio_voip_state",
        runtime_entity="sensor.cucina_waveshare_s3_audio_runtime_snapshot",
    ),
    "spotpear": EspDevice(
        "spotpear",
        "Spotpear Ball v2",
        "192.168.1.31",
        action_prefix="casa_spotpear_ball_v2",
        ha_state_entity="sensor.casa_spotpear_ball_v2_voip_state",
        runtime_entity="sensor.casa_spotpear_ball_v2_runtime_snapshot",
    ),
}


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    requires: frozenset[str]
    assertions: frozenset[str]
    run: Callable[["LiveContext"], Awaitable[None]]


class HaRest:
    def __init__(self, base_url: str, token: str, *, insecure: bool = False) -> None:
        self.base_url = normalize_ha_url(base_url)
        self.token = token
        self.ssl_context = ha_ssl_context(self.base_url, insecure=insecure)

    def _request(
        self, method: str, path: str, data: dict[str, Any] | None = None
    ) -> Any:
        raw = None
        headers = {"Authorization": f"Bearer {self.token}"}
        if data is not None:
            raw = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=raw, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(
                req, timeout=14, context=self.ssl_context
            ) as resp:
                body = resp.read().decode()
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")
            raise AssertionError(
                f"HA {method} {path} failed: {err.code} {detail}"
            ) from err
        return json.loads(body) if body else None

    async def state(self, entity_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, "GET", f"/api/states/{entity_id}")

    async def service(
        self, domain: str, service: str, data: dict[str, Any] | None = None
    ) -> Any:
        return await asyncio.to_thread(
            self._request, "POST", f"/api/services/{domain}/{service}", data or {}
        )


class HaWs:
    def __init__(self, base_url: str, token: str, *, insecure: bool = False) -> None:
        self.base_url = normalize_ha_url(base_url)
        self.token = token
        self.ssl_context = ha_ssl_context(self.base_url, insecure=insecure)
        self.ws: Any = None
        self._next_id = 1
        self.events: list[dict[str, Any]] = []

    async def __aenter__(self) -> "HaWs":
        if websockets is None:
            raise RuntimeError(
                "websockets is required to run live HA websocket qualification"
            )
        url = (
            self.base_url.replace("https://", "wss://").replace("http://", "ws://")
            + "/api/websocket"
        )
        self.ws = await websockets.connect(url, ssl=self.ssl_context)
        hello = json.loads(await self.ws.recv())
        if hello.get("type") != "auth_required":
            raise AssertionError(f"unexpected HA websocket hello: {hello}")
        await self.ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        auth = json.loads(await self.ws.recv())
        if auth.get("type") != "auth_ok":
            raise AssertionError(f"HA websocket auth failed: {auth}")
        await self.command(
            {"type": "subscribe_events", "event_type": "voip_stack.call_event"}
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.ws is not None:
            await self.ws.close()

    async def command(
        self, msg: dict[str, Any], timeout: float = 8.0
    ) -> dict[str, Any]:
        assert self.ws is not None
        msg_id = self._next_id
        self._next_id += 1
        await self.ws.send(json.dumps({"id": msg_id, **msg}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(
                self.ws.recv(), timeout=max(0.1, deadline - time.monotonic())
            )
            packet = json.loads(raw)
            if packet.get("type") == "event":
                self.events.append(packet)
                continue
            if packet.get("id") == msg_id:
                if packet.get("success") is False:
                    raise AssertionError(f"HA websocket command failed: {packet}")
                return packet
        raise AssertionError(f"HA websocket command timed out: {msg}")

    async def softphone_state(self) -> dict[str, Any]:
        msg = await self.command({"type": "voip_stack/ha_softphone_state"})
        return dict(msg.get("result") or {})

    async def drain_events(self, seconds: float) -> None:
        assert self.ws is not None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(
                    self.ws.recv(), timeout=max(0.05, deadline - time.monotonic())
                )
            except asyncio.TimeoutError:
                return
            packet = json.loads(raw)
            if packet.get("type") == "event":
                self.events.append(packet)


class EspApi:
    def __init__(self, spec: EspDevice) -> None:
        if APIClient is None:
            raise RuntimeError(
                "aioesphomeapi is required to run live ESP qualification"
            )
        self.spec = spec
        self.client = APIClient(spec.host, spec.port, spec.password)
        self.entities: dict[str, Any] = {}
        self.services: dict[str, Any] = {}
        self.values: dict[str, Any] = {}
        self._object_by_key: dict[int, str] = {}
        self._updates = asyncio.Event()

    async def __aenter__(self) -> "EspApi":
        await self.client.connect(login=True)
        entities, services = await self.client.list_entities_services()
        self.entities = {
            str(getattr(entity, "object_id", "")): entity for entity in entities
        }
        self.services = {
            str(getattr(service, "name", "")): service for service in services
        }
        self._object_by_key = {
            int(getattr(entity, "key", -1)): object_id
            for object_id, entity in self.entities.items()
        }
        await maybe_await(self.client.subscribe_states(self._on_state))
        await asyncio.sleep(0.6)
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.client.disconnect()

    def _on_state(self, state: Any) -> None:
        key = int(getattr(state, "key", -1))
        object_id = self._object_by_key.get(key)
        if not object_id:
            return
        value = getattr(state, "state", None)
        if value is None:
            value = getattr(state, "value", None)
        self.values[object_id] = value
        self._updates.set()

    async def service(self, name: str, data: dict[str, Any] | None = None) -> None:
        service = self.services.get(name)
        if service is None:
            raise AssertionError(f"{self.spec.key}: ESP service {name!r} not exposed")
        await maybe_await(self.client.execute_service(service, data or {}))

    async def button(self, object_id: str) -> None:
        entity = self.entities.get(object_id)
        if entity is None:
            raise AssertionError(
                f"{self.spec.key}: ESP button {object_id!r} not exposed"
            )
        await maybe_await(self.client.button_command(entity.key))

    async def switch(self, object_id: str, value: bool) -> None:
        entity = self.entities.get(object_id)
        if entity is None:
            raise AssertionError(
                f"{self.spec.key}: ESP switch {object_id!r} not exposed"
            )
        await maybe_await(self.client.switch_command(entity.key, value))
        await self.wait(object_id, {"on" if value else "off"}, timeout=5)

    async def text(self, object_id: str, value: str) -> None:
        entity = self.entities.get(object_id)
        if entity is None:
            raise AssertionError(f"{self.spec.key}: ESP text {object_id!r} not exposed")
        await maybe_await(self.client.text_command(entity.key, value))
        await self.wait(object_id, {value}, timeout=6, exact=True)

    async def wait(
        self,
        object_id: str,
        wanted: set[str],
        *,
        timeout: float = 10.0,
        exact: bool = False,
    ) -> Any:
        deadline = time.monotonic() + timeout
        wanted_norm = {norm(value) for value in wanted}
        while time.monotonic() < deadline:
            current = self.values.get(object_id)
            if exact:
                if str(current or "") in wanted:
                    return current
            elif norm(current) in wanted_norm:
                return current
            try:
                await asyncio.wait_for(self._updates.wait(), timeout=0.2)
                self._updates.clear()
            except asyncio.TimeoutError:
                pass
        raise AssertionError(
            f"{self.spec.key}: {object_id} expected {sorted(wanted)}, current={self.values.get(object_id)!r}"
        )

    async def wait_predicate(
        self,
        predicate: Callable[[], bool],
        description: str,
        *,
        timeout: float = 10.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            try:
                await asyncio.wait_for(self._updates.wait(), timeout=0.2)
                self._updates.clear()
            except asyncio.TimeoutError:
                pass
        raise AssertionError(
            f"{self.spec.key}: timed out waiting for {description}; snapshot={self.snapshot()}"
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "device": self.spec.key,
            "state": self.values.get("voip_state"),
            "caller": self.values.get("voip_caller"),
            "destination": self.values.get("voip_destination"),
            "last_reason": self.values.get("voip_last_reason"),
            "endpoint": self.values.get("voip_endpoint"),
            "contacts": self.values.get("voip_contacts"),
            "extension": self.values.get("voip_extension"),
            "ring_groups": self.values.get("voip_ring_groups"),
            "conference_groups": self.values.get("voip_conference_groups"),
            "ring_on_conference": self.values.get("voip_ring_on_conference"),
            "dnd": self.values.get("do_not_disturb"),
            "auto_answer": self.values.get("auto_answer"),
        }


@dataclass
class LiveContext:
    ha: HaRest
    ws: HaWs
    esp: EspApi
    args: argparse.Namespace
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    def _call_resources_are_quiescent(self, snapshot: dict[str, Any]) -> bool:
        resources = dict(
            snapshot.get("runtime_resources")
            or (snapshot.get("media_debug") or {}).get("runtime_resources")
            or {}
        )
        return (
            int(snapshot.get("active_dialogs") or 0) == 0
            and not snapshot.get("pending_call_ids")
            and norm(self.esp.values.get("voip_state")) == "idle"
            and resources.get("call_scoped_quiescent") is True
        )

    async def cleanup(self) -> None:
        for _ in range(2):
            await self.ha.service("voip_stack", "hangup", {})
            await self.ha.service(
                "voip_stack",
                "decline",
                {"reason": "cleanup", "decline_reason": "cleanup"},
            )
        with contextlib.suppress(Exception):
            await self.esp.service("decline_call", {"reason": "cleanup"})
        deadline = time.monotonic() + 8.0
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            last = await self.ws.softphone_state()
            if self._call_resources_are_quiescent(last):
                await asyncio.sleep(0.8)
                confirmed = await self.ws.softphone_state()
                if self._call_resources_are_quiescent(confirmed):
                    return
            await asyncio.sleep(0.1)
        raise AssertionError(f"call resources did not quiesce: {last}")

    def capture(self, label: str) -> None:
        self.artifacts.append(
            {
                "label": label,
                "t": time.monotonic(),
                "esp": self.esp.snapshot(),
            }
        )


async def wait_phonebook_contains(
    ha: HaRest, target: str, *, timeout: float = 12.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = await ha.state("sensor.voip_phonebook")
        raw = last.get("attributes", {}).get("roster_json")
        if raw:
            payload = json.loads(raw)
            contacts = payload.get("contacts") or []
            for item in contacts:
                values = {
                    str(item.get("id") or ""),
                    str(item.get("name") or ""),
                    str(item.get("extension") or ""),
                }
                if target in values:
                    return item
        await asyncio.sleep(0.35)
    raise AssertionError(f"phonebook did not expose {target!r}; last={last}")


async def wait_phonebook_group_member(
    ha: HaRest,
    group: str,
    member: str,
    member_key: str,
    *,
    timeout: float = 12.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = await ha.state("sensor.voip_phonebook")
        raw = last.get("attributes", {}).get("roster_json")
        if raw:
            payload = json.loads(raw)
            for item in payload.get("contacts") or []:
                if str(item.get("id") or item.get("name") or "") != group:
                    continue
                metadata = (
                    item.get("metadata")
                    if isinstance(item.get("metadata"), dict)
                    else {}
                )
                values = [
                    str(value).strip() for value in metadata.get(member_key) or []
                ]
                if member in values:
                    return item
        await asyncio.sleep(0.35)
    raise AssertionError(
        f"phonebook group {group!r} did not expose {member!r} in {member_key}; last={last}"
    )


async def wait_softphone_state(
    ctx: LiveContext, wanted: set[str], *, timeout: float = 10.0
) -> dict[str, Any]:
    wanted_norm = {norm(value) for value in wanted}
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = await ctx.ws.softphone_state()
        if norm(last.get("state")) in wanted_norm:
            return last
        await asyncio.sleep(0.2)
    raise AssertionError(f"HA softphone expected {sorted(wanted)}, last={last}")


async def wait_esp_voip_state(
    ctx: LiveContext, wanted: set[str], *, timeout: float = 10.0
) -> Any:
    try:
        return await ctx.esp.wait("voip_state", wanted, timeout=timeout)
    except AssertionError as err:
        if not ctx.esp.spec.ha_state_entity:
            raise
        wanted_norm = {norm(item) for item in wanted}
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            state = await ctx.ha.state(ctx.esp.spec.ha_state_entity)
            value = state.get("state")
            ctx.esp.values["voip_state"] = value
            if norm(value) in wanted_norm:
                return value
            await asyncio.sleep(0.25)
        raise err


async def wait_runtime(
    ctx: LiveContext,
    predicate: Callable[[dict[str, Any]], bool],
    description: str,
    *,
    timeout: float = 8.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = await ctx.ha.state(ctx.esp.spec.runtime_entity)
        try:
            raw = json.loads(state.get("state") or "{}")
        except json.JSONDecodeError:
            raw = {}
        aliases = {"u": "ui", "d": "duck", "r": "ring"}
        last = {aliases.get(key, key): value for key, value in raw.items()}
        if predicate(last):
            return last
        await asyncio.sleep(0.1)
    raise AssertionError(f"runtime expected {description}; last={last}")


async def runtime_snapshot_available(ctx: LiveContext) -> bool:
    """Return whether this installed ESP firmware publishes its debug snapshot."""
    if not ctx.esp.spec.runtime_entity:
        return False
    try:
        await ctx.ha.state(ctx.esp.spec.runtime_entity)
    except AssertionError as err:
        if "404" in str(err):
            return False
        raise
    return True


async def set_baseline(ctx: LiveContext) -> dict[str, Any]:
    ha_phone = await ctx.ws.softphone_state()
    ha_groups = dict(ha_phone.get("groups") or {})
    original = {
        "extension": str(ctx.esp.values.get("voip_extension") or ""),
        "ring_groups": str(ctx.esp.values.get("voip_ring_groups") or ""),
        "conference_groups": str(ctx.esp.values.get("voip_conference_groups") or ""),
        "ring_on_conference": norm(ctx.esp.values.get("voip_ring_on_conference"))
        == "on",
        "dnd": norm(ctx.esp.values.get("do_not_disturb")) == "on",
        "auto_answer": norm(ctx.esp.values.get("auto_answer")) == "on",
        "ha_extension": str(ha_phone.get("extension") or ""),
        "ha_ring_group": str(ha_groups.get("ring_group") or ""),
        "ha_conference_group": str(ha_groups.get("conference_group") or ""),
        "ha_conference_ring": bool(ha_groups.get("conference_ring", False)),
    }
    try:
        await ctx.esp.text("voip_extension", ctx.args.esp_extension)
        await ctx.esp.text("voip_ring_groups", ctx.args.ring_group)
        await ctx.esp.text("voip_conference_groups", ctx.args.conference_group)
        await ctx.esp.switch("voip_ring_on_conference", False)
        await ctx.esp.switch("do_not_disturb", False)
        await ctx.esp.switch("auto_answer", False)
        await wait_phonebook_contains(ctx.ha, ctx.args.esp_extension)
        await wait_phonebook_contains(ctx.ha, ctx.args.ring_group)
        await wait_phonebook_contains(ctx.ha, ctx.args.conference_group)
        await ctx.ha.service(
            "voip_stack",
            "set_ha_softphone_settings",
            {
                "extension": ctx.args.ha_extension,
                "ring_group": "",
                "conference_group": ctx.args.conference_group,
                "conference_ring": False,
            },
        )
        await wait_phonebook_contains(ctx.ha, ctx.args.ha_extension)
    except BaseException:
        await restore_baseline(ctx, original)
        raise
    return original


async def restore_baseline(ctx: LiveContext, original: dict[str, Any]) -> None:
    await ctx.cleanup()
    await ctx.esp.text("voip_extension", original["extension"])
    await ctx.esp.text("voip_ring_groups", original["ring_groups"])
    await ctx.esp.text("voip_conference_groups", original["conference_groups"])
    await ctx.esp.switch(
        "voip_ring_on_conference", bool(original["ring_on_conference"])
    )
    await ctx.esp.switch("do_not_disturb", bool(original["dnd"]))
    await ctx.esp.switch("auto_answer", bool(original["auto_answer"]))
    await ctx.ha.service(
        "voip_stack",
        "set_ha_softphone_settings",
        {
            "extension": original["ha_extension"],
            "ring_group": original["ha_ring_group"],
            "conference_group": original["ha_conference_group"],
            "conference_ring": bool(original["ha_conference_ring"]),
        },
    )


async def scenario_ha_to_esp_extension_answer_hangup(ctx: LiveContext) -> None:
    await ctx.cleanup()
    await ctx.esp.switch("auto_answer", False)
    await ctx.ha.service("voip_stack", "call", {"destination": ctx.args.esp_extension})
    await wait_esp_voip_state(ctx, {"ringing", "incoming"}, timeout=12)
    await ctx.esp.button("call")
    await wait_esp_voip_state(ctx, {"in_call"}, timeout=12)
    soft = await wait_softphone_state(ctx, {"in_call"}, timeout=8)
    if norm(soft.get("peer_name")) not in {
        norm(ctx.esp.spec.name),
        norm(ctx.args.esp_extension),
    }:
        raise AssertionError(
            f"HA softphone did not resolve ESP extension to ESP peer: {soft}"
        )
    await ctx.ha.service("voip_stack", "hangup", {})
    await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
    ctx.capture("ha_to_esp_extension_answer_hangup")


async def scenario_ha_to_esp_api_answer_hangup(ctx: LiveContext) -> None:
    """Prove the native ESPHome answer and hangup actions execute on-device."""
    await ctx.cleanup()
    await ctx.esp.switch("auto_answer", False)
    await ctx.ha.service("voip_stack", "call", {"destination": ctx.args.esp_extension})
    await wait_esp_voip_state(ctx, {"ringing", "incoming"}, timeout=12)
    prefix = ctx.esp.spec.action_prefix
    await ctx.ha.service("esphome", f"{prefix}_answer_call", {})
    await wait_esp_voip_state(ctx, {"in_call"}, timeout=12)
    await wait_softphone_state(ctx, {"in_call"}, timeout=8)
    await ctx.ha.service("esphome", f"{prefix}_hangup_call", {})
    await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
    await wait_softphone_state(ctx, {"idle"}, timeout=8)
    await ctx.cleanup()
    ctx.capture("ha_to_esp_api_answer_hangup")


async def scenario_ha_to_esp_auto_answer(ctx: LiveContext) -> None:
    """Exercise both real incoming-call contracts and restore the option."""
    await ctx.cleanup()
    has_runtime_snapshot = await runtime_snapshot_available(ctx)
    await ctx.esp.switch("auto_answer", False)
    await ctx.ha.service("voip_stack", "call", {"destination": ctx.args.esp_extension})
    await wait_esp_voip_state(ctx, {"ringing", "incoming"}, timeout=12)
    if has_runtime_snapshot:
        await wait_runtime(
            ctx,
            lambda value: value.get("ui") == 11 and value.get("ring") == 1,
            "ringing UI with active local ringtone",
        )
    else:
        ctx.capture("runtime_snapshot_not_exposed_by_installed_esp_firmware")
    await asyncio.sleep(1.0)
    await wait_esp_voip_state(ctx, {"ringing", "incoming"}, timeout=2)
    await ctx.esp.button("call")
    await wait_esp_voip_state(ctx, {"in_call"}, timeout=12)
    await ctx.ha.service("voip_stack", "hangup", {})
    await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
    await ctx.cleanup()

    await ctx.esp.switch("auto_answer", True)
    await ctx.ha.service("voip_stack", "call", {"destination": ctx.args.esp_extension})
    await wait_esp_voip_state(ctx, {"in_call"}, timeout=12)
    if has_runtime_snapshot:
        await wait_runtime(
            ctx,
            lambda value: (
                value.get("ui") == 13
                and value.get("duck") == 1
                and value.get("ring") == 0
            ),
            "in-call UI, ducking retained and ringtone stopped",
        )
    await ctx.ha.service("voip_stack", "hangup", {})
    await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
    await ctx.esp.switch("auto_answer", False)
    ctx.capture("ha_to_esp_auto_answer")


async def scenario_ha_to_esp_dnd(ctx: LiveContext) -> None:
    await ctx.cleanup()
    await ctx.esp.switch("do_not_disturb", True)
    try:
        await ctx.ha.service(
            "voip_stack", "call", {"destination": ctx.args.esp_extension}
        )
        await wait_esp_voip_state(ctx, {"idle"}, timeout=10)
        soft = await wait_softphone_state(ctx, {"idle", "busy", "declined"}, timeout=10)
        if norm(soft.get("terminal_reason")) not in {
            "busy",
            "dnd",
            "declined",
            "remote_hangup",
        }:
            raise AssertionError(
                f"HA softphone did not surface ESP DND/busy terminal: {soft}"
            )
    finally:
        await ctx.esp.switch("do_not_disturb", False)
    ctx.capture("ha_to_esp_dnd")


async def scenario_ha_to_ring_group_answer(ctx: LiveContext) -> None:
    await _start_ring_group_call(ctx, auto_answer=False)
    await ctx.ha.service("voip_stack", "hangup", {})
    await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
    ctx.capture("ha_to_ring_group_answer")


async def _start_ring_group_call(
    ctx: LiveContext,
    *,
    auto_answer: bool,
) -> dict[str, Any]:
    await ctx.cleanup()
    await ctx.esp.switch("auto_answer", auto_answer)
    await ctx.ha.service("voip_stack", "call", {"destination": ctx.args.ring_group})
    if not auto_answer:
        await wait_esp_voip_state(ctx, {"ringing", "incoming"}, timeout=12)
        await ctx.esp.button("call")
    await wait_esp_voip_state(ctx, {"in_call"}, timeout=12)
    soft = await wait_softphone_state(ctx, {"in_call"}, timeout=8)
    if str(soft.get("peer_name") or "") == ctx.args.ring_group:
        raise AssertionError(
            f"HA softphone still displays ring group instead of winning member: {soft}"
        )
    return soft


async def _assert_ha_terminal_cleanup(ctx: LiveContext) -> dict[str, Any]:
    soft = await wait_softphone_state(
        ctx,
        {"idle", "cancelled", "remote_hangup"},
        timeout=12,
    )
    if soft.get("active_dialogs") or soft.get("pending_call_ids"):
        raise AssertionError(f"HA retained SIP runtime after remote hangup: {soft}")
    return soft


async def scenario_ha_to_ring_group_answer_esp_hangup(ctx: LiveContext) -> None:
    await _start_ring_group_call(ctx, auto_answer=False)
    await ctx.esp.button("call")
    await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
    await _assert_ha_terminal_cleanup(ctx)
    ctx.capture("ha_to_ring_group_answer_esp_hangup")


async def scenario_ha_to_ring_group_auto_answer_ha_hangup(
    ctx: LiveContext,
) -> None:
    await _start_ring_group_call(ctx, auto_answer=True)
    await ctx.ha.service("voip_stack", "hangup", {})
    await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
    await ctx.esp.switch("auto_answer", False)
    ctx.capture("ha_to_ring_group_auto_answer_ha_hangup")


async def scenario_ha_to_ring_group_auto_answer_esp_hangup(
    ctx: LiveContext,
) -> None:
    await _start_ring_group_call(ctx, auto_answer=True)
    await ctx.esp.button("call")
    await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
    await _assert_ha_terminal_cleanup(ctx)
    await ctx.esp.switch("auto_answer", False)
    ctx.capture("ha_to_ring_group_auto_answer_esp_hangup")


async def _start_conference_call(
    ctx: LiveContext,
    *,
    auto_answer: bool,
) -> None:
    await ctx.cleanup()
    await ctx.esp.switch("auto_answer", auto_answer)
    await ctx.esp.switch("voip_ring_on_conference", True)
    await wait_phonebook_group_member(
        ctx.ha,
        ctx.args.conference_group,
        ctx.esp.spec.name,
        "ring_members",
        timeout=15,
    )
    await ctx.ha.service(
        "voip_stack", "call", {"destination": ctx.args.conference_group}
    )
    if not auto_answer:
        await wait_esp_voip_state(ctx, {"ringing", "incoming"}, timeout=12)
        await ctx.esp.button("call")
    await wait_esp_voip_state(ctx, {"in_call"}, timeout=12)
    await wait_softphone_state(ctx, {"in_call"}, timeout=8)


async def scenario_ha_to_conference_group_rings_esp(ctx: LiveContext) -> None:
    await _start_conference_call(ctx, auto_answer=False)
    try:
        await ctx.ha.service("voip_stack", "hangup", {})
        await wait_softphone_state(ctx, {"idle"}, timeout=8)
        await wait_esp_voip_state(ctx, {"in_call"}, timeout=3)
        await ctx.esp.button("call")
        await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
    finally:
        await ctx.esp.switch("voip_ring_on_conference", False)
    ctx.capture("ha_to_conference_group_rings_esp")


async def scenario_ha_to_conference_group_esp_leaves(ctx: LiveContext) -> None:
    await _start_conference_call(ctx, auto_answer=False)
    try:
        await ctx.esp.button("call")
        await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
        await wait_softphone_state(ctx, {"in_call"}, timeout=3)
        await ctx.ha.service("voip_stack", "hangup", {})
        await wait_softphone_state(ctx, {"idle"}, timeout=8)
    finally:
        await ctx.esp.switch("voip_ring_on_conference", False)
    ctx.capture("ha_to_conference_group_esp_leaves")


async def scenario_ha_to_conference_group_auto_answer_ha_leaves(
    ctx: LiveContext,
) -> None:
    await _start_conference_call(ctx, auto_answer=True)
    try:
        await ctx.ha.service("voip_stack", "hangup", {})
        await wait_softphone_state(ctx, {"idle"}, timeout=8)
        await wait_esp_voip_state(ctx, {"in_call"}, timeout=3)
        await ctx.esp.button("call")
        await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
    finally:
        await ctx.esp.switch("auto_answer", False)
        await ctx.esp.switch("voip_ring_on_conference", False)
    ctx.capture("ha_to_conference_group_auto_answer_ha_leaves")


async def scenario_ha_to_conference_group_auto_answer_esp_leaves(
    ctx: LiveContext,
) -> None:
    await _start_conference_call(ctx, auto_answer=True)
    try:
        await ctx.esp.button("call")
        await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
        await wait_softphone_state(ctx, {"in_call"}, timeout=3)
        await ctx.ha.service("voip_stack", "hangup", {})
        await wait_softphone_state(ctx, {"idle"}, timeout=8)
    finally:
        await ctx.esp.switch("auto_answer", False)
        await ctx.esp.switch("voip_ring_on_conference", False)
    ctx.capture("ha_to_conference_group_auto_answer_esp_leaves")


async def scenario_ha_to_conference_group_no_ring(ctx: LiveContext) -> None:
    await ctx.cleanup()
    await ctx.esp.switch("auto_answer", True)
    await ctx.esp.switch("voip_ring_on_conference", False)
    await ctx.ha.service(
        "voip_stack", "call", {"destination": ctx.args.conference_group}
    )
    await wait_softphone_state(ctx, {"in_call"}, timeout=8)
    await asyncio.sleep(1.0)
    await wait_esp_voip_state(ctx, {"idle"}, timeout=2)
    await ctx.ha.service("voip_stack", "hangup", {})
    await wait_softphone_state(ctx, {"idle"}, timeout=8)
    await ctx.esp.switch("auto_answer", False)
    ctx.capture("ha_to_conference_group_no_ring")


async def scenario_esp_to_ha_extension_cancel(ctx: LiveContext) -> None:
    await ctx.cleanup()
    await ctx.esp.service("start_call", {"dest": ctx.args.ha_extension})
    await wait_esp_voip_state(ctx, {"calling", "remote_ringing"}, timeout=12)
    soft = await wait_softphone_state(ctx, {"ringing"}, timeout=8)
    if (
        str(soft.get("dialed_target") or soft.get("callee") or "")
        != ctx.args.ha_extension
    ):
        raise AssertionError(f"HA softphone did not preserve dialed extension: {soft}")
    await ctx.esp.service("decline_call", {"reason": "qualification_cancel"})
    await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
    soft = await wait_softphone_state(ctx, {"idle", "cancelled"}, timeout=10)
    if soft.get("active_dialogs") or soft.get("pending_call_ids"):
        raise AssertionError(f"HA softphone kept SIP runtime after ESP cancel: {soft}")
    ctx.capture("esp_to_ha_extension_cancel")


async def scenario_esp_to_ha_extension_answer_hangup(ctx: LiveContext) -> None:
    """Exercise the public HA answer and hangup actions for an ESP caller."""

    await ctx.cleanup()
    await ctx.esp.service("start_call", {"dest": ctx.args.ha_extension})
    await wait_esp_voip_state(ctx, {"calling", "remote_ringing"}, timeout=12)
    soft = await wait_softphone_state(ctx, {"ringing"}, timeout=8)
    call_id = str(soft.get("call_id") or "").strip()
    device_id = str(soft.get("device_id") or "").strip()
    if not call_id or not device_id:
        raise AssertionError(f"HA ringing state has no phone identity: {soft}")
    await ctx.ha.service(
        "voip_stack",
        "answer",
        {"call_id": call_id, "device_id": device_id},
    )
    await wait_softphone_state(ctx, {"in_call"}, timeout=10)
    await wait_esp_voip_state(ctx, {"in_call"}, timeout=12)
    await ctx.ha.service(
        "voip_stack",
        "hangup",
        {"call_id": call_id, "device_id": device_id},
    )
    await wait_softphone_state(ctx, {"idle"}, timeout=10)
    await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
    ctx.capture("esp_to_ha_extension_answer_hangup")


async def scenario_esp_to_self_extension_busy(ctx: LiveContext) -> None:
    await ctx.cleanup()
    await ctx.esp.service("start_call", {"dest": ctx.args.esp_extension})
    await ctx.esp.wait_predicate(
        lambda: (
            str(ctx.esp.values.get("voip_destination") or "") == ctx.args.esp_extension
            or norm(ctx.esp.values.get("voip_last_reason"))
            in {"busy", "declined", "cancelled", "routing_failed", "local_hangup"}
        ),
        f"ESP self-extension attempt to {ctx.args.esp_extension}",
        timeout=4,
    )
    await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
    reason = norm(ctx.esp.values.get("voip_last_reason"))
    if reason not in {
        "busy",
        "declined",
        "cancelled",
        "routing_failed",
        "local_hangup",
    }:
        raise AssertionError(
            f"ESP self-extension terminal reason was not explicit: {ctx.esp.snapshot()}"
        )
    ctx.capture("esp_to_self_extension_busy")


async def scenario_esp_to_esp_bidirectional(ctx: LiveContext) -> None:
    """Prove direct ESP media in both directions beyond the media watchdog."""

    peer_spec = DEFAULT_ESPS[ctx.args.peer_esp]
    if peer_spec.key == ctx.esp.spec.key:
        raise AssertionError("the ESP peer must be a different device")
    if ctx.args.peer_esp_host or ctx.args.peer_esp_api_port:
        peer_spec = replace(
            peer_spec,
            host=str(ctx.args.peer_esp_host or peer_spec.host).strip(),
            port=int(ctx.args.peer_esp_api_port or peer_spec.port),
        )

    async with EspApi(peer_spec) as peer:
        peer_auto_answer = norm(peer.values.get("auto_answer")) == "on"
        primary_auto_answer = norm(ctx.esp.values.get("auto_answer")) == "on"

        async def call(
            caller: EspApi,
            callee: EspApi,
            *,
            hangup: EspApi,
        ) -> None:
            await callee.switch("auto_answer", True)
            await caller.service("start_call", {"dest": callee.spec.name})
            await caller.wait("voip_state", {"in_call"}, timeout=15)
            await callee.wait("voip_state", {"in_call"}, timeout=15)
            await asyncio.sleep(16.5)
            if any(
                norm(device.values.get("voip_state")) != "in_call"
                for device in (caller, callee)
            ):
                raise AssertionError(
                    "ESP pair did not remain active beyond the 15 second media watchdog"
                )
            await hangup.service("hangup_call")
            await caller.wait("voip_state", {"idle"}, timeout=12)
            await callee.wait("voip_state", {"idle"}, timeout=12)
            ctx.artifacts.append(
                {
                    "label": "esp_to_esp_call",
                    "caller": caller.snapshot(),
                    "callee": callee.snapshot(),
                    "hangup": hangup.spec.key,
                }
            )

        try:
            await call(ctx.esp, peer, hangup=peer)
            await call(peer, ctx.esp, hangup=peer)
        finally:
            for device in (ctx.esp, peer):
                if norm(device.values.get("voip_state")) != "idle":
                    with contextlib.suppress(Exception):
                        await device.service("hangup_call")
            await peer.switch("auto_answer", peer_auto_answer)
            await ctx.esp.switch("auto_answer", primary_auto_answer)


async def scenario_esp_to_trunk_cancel(ctx: LiveContext) -> None:
    if not ctx.args.allow_trunk:
        raise RuntimeError("trunk scenario requires --allow-trunk")
    await ctx.cleanup()
    await ctx.esp.service("start_call", {"dest": ctx.args.trunk_number})
    await wait_esp_voip_state(ctx, {"calling", "remote_ringing"}, timeout=18)
    await ctx.esp.service("decline_call", {"reason": "qualification_cancel"})
    await wait_esp_voip_state(ctx, {"idle"}, timeout=18)
    snap = ctx.esp.snapshot()
    if str(snap.get("destination") or "") not in {ctx.args.trunk_number, ""}:
        raise AssertionError(
            f"ESP trunk terminal target was rewritten unexpectedly: {snap}"
        )
    ctx.capture("esp_to_trunk_cancel")


SCENARIOS: dict[str, Scenario] = {
    "ha_to_esp_auto_answer": Scenario(
        "ha_to_esp_auto_answer",
        "HA calls ESP with auto-answer off and on; ringtone internals are checked when exposed",
        frozenset({"ha", "esp", "auto_answer", "ringtone", "extension"}),
        frozenset(
            {
                "manual_ringing",
                "automatic_in_call",
                "cleanup_idle",
                "optional_runtime_snapshot",
            }
        ),
        scenario_ha_to_esp_auto_answer,
    ),
    "ha_to_esp_extension_answer_hangup": Scenario(
        "ha_to_esp_extension_answer_hangup",
        "HA calls ESP by dynamic extension; ESP answers; HA hangs up",
        frozenset({"ha", "esp", "phonebook", "extension"}),
        frozenset(
            {"esp_ringing", "esp_in_call", "ha_in_call", "remote_bye", "esp_idle"}
        ),
        scenario_ha_to_esp_extension_answer_hangup,
    ),
    "ha_to_esp_api_answer_hangup": Scenario(
        "ha_to_esp_api_answer_hangup",
        "HA calls ESP; native ESPHome actions answer and hang up",
        frozenset({"ha", "esp", "api_actions", "extension"}),
        frozenset({"esp_ringing", "both_in_call", "both_idle"}),
        scenario_ha_to_esp_api_answer_hangup,
    ),
    "ha_to_esp_dnd": Scenario(
        "ha_to_esp_dnd",
        "HA calls ESP by extension while ESP DND is enabled",
        frozenset({"ha", "esp", "dnd", "phonebook"}),
        frozenset({"esp_no_ringing", "ha_terminal_reason", "esp_idle"}),
        scenario_ha_to_esp_dnd,
    ),
    "ha_to_ring_group_answer": Scenario(
        "ha_to_ring_group_answer",
        "HA calls ring group; ESP rings, answers, and becomes visible winner",
        frozenset({"ha", "esp", "ring_group", "phonebook"}),
        frozenset(
            {"esp_ringing", "winner_not_group_label", "esp_in_call", "cleanup_idle"}
        ),
        scenario_ha_to_ring_group_answer,
    ),
    "ha_to_ring_group_answer_esp_hangup": Scenario(
        "ha_to_ring_group_answer_esp_hangup",
        "HA calls ring group; ESP answers manually and hangs up",
        frozenset({"ha", "esp", "ring_group", "phonebook"}),
        frozenset({"esp_in_call", "ha_terminal_reason", "both_idle"}),
        scenario_ha_to_ring_group_answer_esp_hangup,
    ),
    "ha_to_ring_group_auto_answer_ha_hangup": Scenario(
        "ha_to_ring_group_auto_answer_ha_hangup",
        "HA calls ring group; ESP auto-answers and HA hangs up",
        frozenset({"ha", "esp", "ring_group", "phonebook", "auto_answer"}),
        frozenset({"automatic_in_call", "remote_bye", "cleanup_idle"}),
        scenario_ha_to_ring_group_auto_answer_ha_hangup,
    ),
    "ha_to_ring_group_auto_answer_esp_hangup": Scenario(
        "ha_to_ring_group_auto_answer_esp_hangup",
        "HA calls ring group; ESP auto-answers and hangs up",
        frozenset({"ha", "esp", "ring_group", "phonebook", "auto_answer"}),
        frozenset({"automatic_in_call", "ha_terminal_reason", "both_idle"}),
        scenario_ha_to_ring_group_auto_answer_esp_hangup,
    ),
    "ha_to_conference_group_rings_esp": Scenario(
        "ha_to_conference_group_rings_esp",
        "HA joins conference group; ESP answers manually; HA leaves first",
        frozenset({"ha", "esp", "conference_group", "ring_on_conference"}),
        frozenset({"conference_started", "esp_ringing", "esp_joined", "cleanup_idle"}),
        scenario_ha_to_conference_group_rings_esp,
    ),
    "ha_to_conference_group_esp_leaves": Scenario(
        "ha_to_conference_group_esp_leaves",
        "HA joins conference group; ESP answers manually and leaves first",
        frozenset({"ha", "esp", "conference_group", "ring_on_conference"}),
        frozenset({"conference_started", "esp_joined", "both_idle"}),
        scenario_ha_to_conference_group_esp_leaves,
    ),
    "ha_to_conference_group_auto_answer_ha_leaves": Scenario(
        "ha_to_conference_group_auto_answer_ha_leaves",
        "HA joins conference group; ESP auto-answers and HA leaves first",
        frozenset(
            {"ha", "esp", "conference_group", "ring_on_conference", "auto_answer"}
        ),
        frozenset({"conference_started", "automatic_in_call", "cleanup_idle"}),
        scenario_ha_to_conference_group_auto_answer_ha_leaves,
    ),
    "ha_to_conference_group_auto_answer_esp_leaves": Scenario(
        "ha_to_conference_group_auto_answer_esp_leaves",
        "HA joins conference group; ESP auto-answers and leaves first",
        frozenset(
            {"ha", "esp", "conference_group", "ring_on_conference", "auto_answer"}
        ),
        frozenset({"conference_started", "automatic_in_call", "both_idle"}),
        scenario_ha_to_conference_group_auto_answer_esp_leaves,
    ),
    "ha_to_conference_group_no_ring": Scenario(
        "ha_to_conference_group_no_ring",
        "HA joins conference group while ESP conference ringing is disabled",
        frozenset({"ha", "esp", "conference_group", "ring_on_conference"}),
        frozenset({"conference_started", "esp_idle", "cleanup_idle"}),
        scenario_ha_to_conference_group_no_ring,
    ),
    "esp_to_ha_extension_cancel": Scenario(
        "esp_to_ha_extension_cancel",
        "ESP calls HA by extension; HA rings; ESP cancels before answer",
        frozenset({"ha", "esp", "extension", "cancel"}),
        frozenset({"ha_ringing", "dialed_target_preserved", "esp_cancel", "both_idle"}),
        scenario_esp_to_ha_extension_cancel,
    ),
    "esp_to_ha_extension_answer_hangup": Scenario(
        "esp_to_ha_extension_answer_hangup",
        "ESP calls HA by extension; HA answers and hangs up through public actions",
        frozenset({"esp", "ha", "extension", "answer", "hangup"}),
        frozenset({"esp_in_call", "ha_in_call", "ha_bye", "both_idle"}),
        scenario_esp_to_ha_extension_answer_hangup,
    ),
    "esp_to_self_extension_busy": Scenario(
        "esp_to_self_extension_busy",
        "ESP calls its own extension and must not self-ring",
        frozenset({"esp", "extension", "busy"}),
        frozenset({"self_call_rejected", "esp_idle", "terminal_reason"}),
        scenario_esp_to_self_extension_busy,
    ),
    "esp_to_esp_bidirectional": Scenario(
        "esp_to_esp_bidirectional",
        "Two ESP phones call in both directions beyond the media watchdog",
        frozenset({"ha", "esp", "second_esp", "direct_media"}),
        frozenset(
            {
                "both_directions_in_call",
                "media_watchdog_satisfied",
                "caller_and_callee_hangup",
                "both_idle",
            }
        ),
        scenario_esp_to_esp_bidirectional,
    ),
    "esp_to_trunk_cancel": Scenario(
        "esp_to_trunk_cancel",
        "ESP dials an external trunk number and cancels while ringing",
        frozenset({"esp", "ha", "trunk", "cancel"}),
        frozenset({"trunk_route", "cancel_propagated", "esp_idle", "target_preserved"}),
        scenario_esp_to_trunk_cancel,
    ),
}


def selected_scenarios(args: argparse.Namespace) -> list[Scenario]:
    if args.list:
        return []
    names = list(args.scenario)
    if args.all:
        names = list(SCENARIOS)
    if not names:
        names = [
            "ha_to_esp_extension_answer_hangup",
            "esp_to_ha_extension_cancel",
            "ha_to_ring_group_answer",
            "ha_to_conference_group_rings_esp",
            "ha_to_esp_dnd",
            "esp_to_self_extension_busy",
        ]
    return [SCENARIOS[name] for name in names]


def apply_isolated_group_defaults(
    args: argparse.Namespace,
    *,
    stamp: str | None = None,
) -> None:
    """Keep group scenarios isolated from other live household endpoints."""

    suffix = stamp or datetime.now(UTC).strftime("%H%M%S")
    if not args.ring_group:
        args.ring_group = f"q-{args.esp}-ring-{suffix}"
    if not args.conference_group:
        args.conference_group = f"q-{args.esp}-conference-{suffix}"


async def run(args: argparse.Namespace) -> int:
    if args.list:
        for scenario in SCENARIOS.values():
            print(
                f"{scenario.id}: {scenario.title} requires={','.join(sorted(scenario.requires))}"
            )
        return 0
    apply_isolated_group_defaults(args)
    token = qualification_token(args)
    ha = HaRest(args.ha_url, token, insecure=args.insecure)
    esp_spec = DEFAULT_ESPS[args.esp]
    if args.esp_host or args.esp_api_port:
        esp_spec = replace(
            esp_spec,
            host=str(args.esp_host or esp_spec.host).strip(),
            port=int(args.esp_api_port or esp_spec.port),
        )
    output_dir = Path(args.out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    async with HaWs(args.ha_url, token, insecure=args.insecure) as ws:
        async with EspApi(esp_spec) as esp:
            ctx = LiveContext(ha=ha, ws=ws, esp=esp, args=args)
            await ctx.cleanup()
            original = await set_baseline(ctx)
            try:
                for scenario in selected_scenarios(args):
                    if "trunk" in scenario.requires and not args.allow_trunk:
                        results.append(
                            {
                                "scenario": scenario.id,
                                "status": "skipped",
                                "reason": "requires --allow-trunk",
                            }
                        )
                        continue
                    start = time.monotonic()
                    try:
                        await scenario.run(ctx)
                    except Exception as err:  # noqa: BLE001 - write artifact before failing.
                        ctx.capture(f"{scenario.id}_failed")
                        results.append(
                            {
                                "scenario": scenario.id,
                                "status": "failed",
                                "error": str(err),
                                "duration_s": time.monotonic() - start,
                            }
                        )
                        raise
                    results.append(
                        {
                            "scenario": scenario.id,
                            "status": "passed",
                            "duration_s": time.monotonic() - start,
                        }
                    )
                    print(f"PASS {scenario.id}")
                    await ctx.cleanup()
            finally:
                await restore_baseline(ctx, original)
                artifact = {
                    "schema_version": 2,
                    "created_at": datetime.now(UTC).isoformat(),
                    "candidate": candidate_revision(),
                    "esp": esp_spec.key,
                    "results": results,
                    "samples": ctx.artifacts,
                    "events": ws.events,
                }
                path = (
                    output_dir
                    / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{esp_spec.key}_live_matrix.json"
                )
                path.write_text(
                    json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                print(f"artifact={path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-url", default=DEFAULT_HA_URL)
    parser.add_argument(
        "--insecure",
        action="store_true",
        help="Disable HTTPS certificate verification for an explicitly trusted HA endpoint.",
    )
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument(
        "--auth-file",
        type=Path,
        default=DEFAULT_AUTH_FILE,
        help="local HA OAuth JSON used when the private browser helper is absent",
    )
    parser.add_argument("--token")
    parser.add_argument("--esp", choices=sorted(DEFAULT_ESPS), default="ws3")
    parser.add_argument(
        "--esp-host",
        help="override the selected ESP native API host when DHCP changed",
    )
    parser.add_argument(
        "--esp-api-port",
        type=int,
        help="override the selected ESP native API port",
    )
    parser.add_argument("--peer-esp", choices=sorted(DEFAULT_ESPS), default="ws3")
    parser.add_argument(
        "--peer-esp-host",
        help="override the second ESP native API host",
    )
    parser.add_argument(
        "--peer-esp-api-port",
        type=int,
        help="override the second ESP native API port",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=OUT,
        help=f"artifact directory (default: {OUT})",
    )
    parser.add_argument(
        "--scenario", choices=sorted(SCENARIOS), action="append", default=[]
    )
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--allow-trunk", action="store_true")
    parser.add_argument("--esp-extension", default="1000")
    parser.add_argument("--ha-extension", default="666")
    parser.add_argument(
        "--ring-group",
        help="explicit ring group; omitted runs use a unique isolated group",
    )
    parser.add_argument(
        "--conference-group",
        help="explicit conference group; omitted runs use a unique isolated group",
    )
    parser.add_argument("--trunk-number", default="3519968203")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
