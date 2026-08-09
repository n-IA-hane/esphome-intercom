#!/usr/bin/env python3
"""End-to-end HA softphone state/card/routing qualification matrix."""

from __future__ import annotations

import argparse
import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
TEST_CAPTURE_DIR = ROOT / "test_captures"
sys.path.insert(0, str(ROOT / "test_runs"))


HA_BASE = os.environ.get("HA_BASE", "http://127.0.0.1:18123").rstrip("/")
HA_URL = f"{HA_BASE}/lovelace/default_view"
# The stored frontend token is origin-scoped. Keep it on the same local origin
# used by the matrix so an unauthenticated dashboard cannot masquerade as a
# missing card.
os.environ["HA_URL"] = HA_BASE
AUTOMATION = "automation.voip_ha_non_risponde_inoltra_ad_assist"
INBOUND_AUTOMATION = "automation.voip_inbound_trunk_to_rg_casa"
WILDIX_CONFIG = Path(
    os.environ.get(
        "INBOUND_CALLER_CONFIG",
        os.environ.get("WILDIX_CONFIG", "/home/codex/.baresip-wildix-426"),
    )
)
INBOUND_TARGET = os.environ.get("INBOUND_TARGET", "427")
INBOUND_DTMF_TARGET = os.environ.get("INBOUND_DTMF_TARGET", "")
BROWSER_INBOUND_MODE = os.environ.get("BROWSER_INBOUND_MODE", "trunk")
LOCAL_CONFIG = Path(
    os.environ.get("LOCAL_BARESIP_CONFIG", "/home/codex/ha-voip-lab/baresip-source")
)
LOCAL_SIP_TARGET = os.environ.get(
    "LOCAL_SIP_TARGET",
    "sip:Casa@127.0.0.1:15060;transport=tcp",
)
LOCAL_REGISTERED_TARGET = os.environ.get(
    "LOCAL_REGISTERED_TARGET",
    "video_source",
)
FORWARD_SUCCESS_TARGET = os.environ.get("FORWARD_SUCCESS_TARGET", "1666")
FORWARD_SUCCESS_CALLEE = os.environ.get("FORWARD_SUCCESS_CALLEE", "Troiaio")

CARD_STATE = r"""
async () => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    return found;
  };
  const card = deep("voip-stack-card, intercom-card")
    .find((item) => (item.config?.mode || item.config?.card_mode || "") === "ha_softphone");
  if (!card) return null;
  const endpointId = card._getSoftphoneEndpointId?.() || card.config?.endpoint_id || "default";
  const backend = await card._hass.connection.sendMessagePromise({
    type: "voip_stack/ha_softphone_state", endpoint_id: endpointId,
  });
  const snapshot = card._softphoneSnapshot || {};
  const text = [...(card.shadowRoot?.querySelectorAll(
    ".header, .destination-label, .destination-value, .status, .status-reason, " +
    ".error, .offline-title, .hangup-copy, .version"
  ) || [])]
    .filter((item) => !item.hidden)
    .map((item) => (item.innerText || item.textContent || "").trim())
    .filter(Boolean)
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
  return {
    backend: {
      state: backend?.state || "", call_id: backend?.call_id || "", caller: backend?.caller || "",
      callee: backend?.callee || "", terminal_reason: backend?.terminal_reason || "",
      device_id: backend?.device_id || card.config?.device_id || "",
      video_direction: backend?.video_direction || "inactive",
      video_rtp_tx_packets: Number(backend?.video_rtp_tx_packets || 0),
      video_rtp_rx_packets: Number(backend?.video_rtp_rx_packets || 0),
      runtime_resources: backend?.runtime_resources || backend?.media_debug?.runtime_resources || {},
      auto_answer: !!backend?.auto_answer,
      send_video: !!backend?.send_video,
      groups: backend?.groups || {},
    },
    card: {
      state: snapshot.state || "", call_id: snapshot.call_id || "", caller: snapshot.caller || "",
      callee: snapshot.callee || "", terminal_reason: snapshot.terminal_reason || "",
      video_direction: snapshot.video_direction || "inactive",
      auto_answer: !!snapshot.auto_answer,
      send_video: !!snapshot.send_video,
    },
    text,
    auto_answer: !!card._autoAnswer,
    endpoint_id: endpointId,
    softphone_subscribers: window.__voipStackEngine?._softphoneSubscribers?.size ?? -1,
    call_subscribers: window.__voipStackEngine?._callSubscribers?.size ?? -1,
  };
}
"""

CLICK = r"""
(label) => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    return found;
  };
  const card = deep("voip-stack-card, intercom-card")
    .find((item) => (item.config?.mode || item.config?.card_mode || "") === "ha_softphone");
  if (!card?.shadowRoot) return false;
  const button = [...card.shadowRoot.querySelectorAll("button")].find((item) =>
    (item.innerText || item.textContent || "").trim() === label && !item.hidden && !item.disabled && item.offsetParent !== null
  );
  if (!button) return false;
  button.click();
  return true;
}
"""

SET_AUTO_ANSWER = r"""
async (enabled) => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    return found;
  };
  const card = deep("voip-stack-card, intercom-card")
    .find((item) => (item.config?.mode || item.config?.card_mode || "") === "ha_softphone");
  if (!card?.shadowRoot) return false;
  const input = card.shadowRoot.querySelector("#auto-answer-cb");
  if (!input) return false;
  if (!!input.checked !== !!enabled) input.click();
  for (let attempt = 0; attempt < 100; attempt++) {
    if (
      !!card._autoAnswer === !!enabled &&
      !!card._softphoneSnapshot?.auto_answer === !!enabled &&
      !card._autoAnswerPermissionPending
    ) return true;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  return false;
}
"""

SET_SEND_VIDEO = r"""
async (enabled) => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    return found;
  };
  const card = deep("voip-stack-card, intercom-card")
    .find((item) => (item.config?.mode || item.config?.card_mode || "") === "ha_softphone");
  if (!card?._toggleVideoCamera) return false;
  await card._toggleVideoCamera(!!enabled);
  for (let attempt = 0; attempt < 100; attempt++) {
    if (!!card._softphoneSnapshot?.send_video === !!enabled) return true;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  return false;
}
"""


class BareSip:
    def __init__(
        self,
        config: Path,
        *,
        headless_audio: bool = False,
        dtmf_mode: str = "",
        video_codec: str = "",
    ) -> None:
        TEST_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        if dtmf_mode and dtmf_mode not in {"auto", "info", "rtpevent"}:
            raise ValueError(f"unsupported bareSIP DTMF mode: {dtmf_mode}")
        self._temporary_config: tempfile.TemporaryDirectory[str] | None = None
        runtime_config = config
        if headless_audio or dtmf_mode or video_codec:
            self._temporary_config = tempfile.TemporaryDirectory(
                prefix="voip-baresip-headless-"
            )
            runtime_config = Path(self._temporary_config.name)
            shutil.copytree(config, runtime_config, dirs_exist_ok=True)
            config_path = runtime_config / "config"
            content = config_path.read_text(encoding="utf-8")
            if headless_audio:
                replacements = {
                    "audio_player": "audio_player\t\taubridge,nil",
                    "audio_source": "audio_source\t\tausine,10",
                    "audio_alert": "audio_alert\t\taubridge,nil",
                }
                for key, value in replacements.items():
                    content = re.sub(
                        rf"(?m)^{key}\s+.*$",
                        value,
                        content,
                        count=1,
                    )
                for module in ("aubridge.so", "ausine.so"):
                    content = re.sub(
                        rf"(?m)^\s*#?module\s+{re.escape(module)}\s*$",
                        f"module\t\t\t{module}",
                        content,
                        count=1,
                    )
            if video_codec:
                codec = video_codec.strip().upper()
                if codec not in {"H264", "VP8"}:
                    raise ValueError(f"unsupported bareSIP video codec: {video_codec}")
                content = re.sub(
                    r"(?m)^\s*#?video_source\s+.*$",
                    "video_source\t\tfakevideo,nil",
                    content,
                    count=1,
                )
                content = re.sub(
                    r"(?m)^\s*#?video_display\s+.*$",
                    "video_display\t\tfakevideo,nil",
                    content,
                    count=1,
                )
                for module in ("avcodec.so", "vp8.so", "fakevideo.so"):
                    content = re.sub(
                        rf"(?m)^\s*#?module\s+{re.escape(module)}\s*$",
                        f"module\t\t\t{module}",
                        content,
                        count=1,
                    )
            config_path.write_text(content, encoding="utf-8")
            if dtmf_mode or video_codec:
                accounts_path = runtime_config / "accounts"
                accounts = accounts_path.read_text(encoding="utf-8")
                if dtmf_mode:
                    accounts = re.sub(
                        r"(?<=;)dtmfmode=[^;\r\n]+",
                        f"dtmfmode={dtmf_mode}",
                        accounts,
                    )
                if video_codec:
                    accounts = re.sub(r";video_codecs=[^;>]+", "", accounts)
                    accounts = re.sub(
                        r">",
                        f";video_codecs={video_codec.strip().upper()}>",
                        accounts,
                    )
                accounts_path.write_text(accounts, encoding="utf-8")
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen(
            ["baresip", "-f", str(runtime_config)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
            cwd=TEST_CAPTURE_DIR,
            start_new_session=True,
        )
        os.close(slave)
        os.set_blocking(self.master, False)
        self.output = ""
        self.call_established = False
        try:
            self.wait_for("registered successfully", 8)
        except BaseException:
            # Construction failures occur before callers can append this
            # instance to their cleanup list. Own the child from the moment it
            # is spawned so auth/config errors cannot orphan a busy process.
            self.close()
            raise

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
            time.sleep(0.05)
        raise RuntimeError(
            f"bareSIP timeout waiting for {needle}: {self.output[-2000:]}"
        )

    def wait_for_any(self, needles: tuple[str, ...], timeout: float) -> str:
        deadline = time.monotonic() + timeout
        wanted = tuple(needle.lower() for needle in needles)
        while time.monotonic() < deadline:
            output = self.read().lower()
            if any(needle in output for needle in wanted):
                return self.output
            time.sleep(0.05)
        raise RuntimeError(
            f"bareSIP timeout waiting for {' or '.join(needles)}: {self.output[-2000:]}"
        )

    def command(self, command: str) -> None:
        os.write(self.master, f"{command}\n".encode())

    def dial(
        self,
        target: str,
        *,
        wait_for: str | tuple[str, ...] = "Call established",
    ) -> str:
        self.command(f"/dial {target}")
        if isinstance(wait_for, tuple):
            output = self.wait_for_any(wait_for, 10)
        else:
            output = self.wait_for(wait_for, 10)
        self.call_established = "call established" in output.lower()
        return output

    def digits(self, digits: str, interval: float = 0.45) -> None:
        for digit in digits:
            if digit not in "0123456789*#ABCD":
                raise ValueError(f"unsupported DTMF digit: {digit}")
            os.write(self.master, digit.encode())
            time.sleep(interval)

    def hangup(self) -> None:
        self.command("/hangup")

    def close(self) -> None:
        if self.proc.poll() is None:
            with suppress(Exception):
                self.command("/hangup")
            with suppress(Exception):
                self.command("/quit")
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(self.proc.pid, signal.SIGTERM)
                try:
                    self.proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        os.killpg(self.proc.pid, signal.SIGKILL)
                    with suppress(subprocess.TimeoutExpired):
                        self.proc.wait(timeout=2)
        with suppress(OSError):
            os.close(self.master)
        if self._temporary_config is not None:
            self._temporary_config.cleanup()
            self._temporary_config = None


def ha_request(path: str, data: dict[str, Any] | None = None) -> Any:
    from ha_playwright_auth import ha_token

    body = None if data is None else json.dumps(data).encode()
    request = urllib.request.Request(
        f"{HA_BASE}{path}",
        data=body,
        headers={
            "Authorization": f"Bearer {ha_token()}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = response.read()
    return json.loads(payload) if payload else None


def optional_entity_state(entity_id: str) -> str | None:
    """Return an entity state when present in the selected HA instance."""

    try:
        return str(ha_request(f"/api/states/{entity_id}")["state"])
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def service(domain: str, name: str, data: dict[str, Any] | None = None) -> Any:
    return ha_request(f"/api/services/{domain}/{name}", data or {})


def event_state() -> dict[str, Any]:
    return ha_request("/api/states/event.voip_stack_call")["attributes"]


def wait_card(
    page, predicate: Callable[[dict[str, Any]], bool], timeout: float, label: str
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        try:
            last = page.evaluate(CARD_STATE)
        except Exception as error:  # noqa: BLE001 - HA may reload the dashboard.
            if "Execution context was destroyed" not in str(error):
                raise
            page.wait_for_load_state("domcontentloaded", timeout=5_000)
            continue
        if last and predicate(last):
            return last
        page.wait_for_timeout(100)
    raise RuntimeError(
        f"timeout waiting for {label}: {json.dumps(last, ensure_ascii=False)}"
    )


def matching(page, state: str) -> dict[str, Any]:
    return wait_card(
        page,
        lambda item: (
            item["backend"]["state"] == state
            and item["card"]["state"] == state
            and item["backend"]["call_id"] == item["card"]["call_id"]
        ),
        12,
        f"backend/card {state}",
    )


def dial_trunk() -> BareSip:
    caller = BareSip(
        WILDIX_CONFIG,
        video_codec="VP8" if os.environ.get("EXPECT_VIDEO", "") == "1" else "",
    )
    try:
        # RTP telephone-event can remain in an early dialog, while SIP INFO
        # collection requires an established dialog.  Both are valid trunk
        # entry paths and must be observed instead of assumed by the runner.
        caller.dial(
            INBOUND_TARGET,
            wait_for=("180 Ringing", "183 Session Progress", "Call established"),
        )
        if INBOUND_DTMF_TARGET:
            caller.wait_for("Call established", 10)
            caller.digits(INBOUND_DTMF_TARGET)
    except BaseException:
        caller.close()
        raise
    return caller


def dial_browser_inbound() -> BareSip:
    """Call the observed browser phone from the selected real peer."""
    if BROWSER_INBOUND_MODE == "trunk":
        return dial_trunk()
    if BROWSER_INBOUND_MODE == "registered":
        caller = BareSip(LOCAL_CONFIG)
        try:
            caller.dial(LOCAL_SIP_TARGET, wait_for="180 Ringing")
        except BaseException:
            caller.close()
            raise
        return caller
    raise ValueError(f"unsupported browser inbound mode: {BROWSER_INBOUND_MODE}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out", default=str(ROOT / "test_runs" / "ha_softphone_matrix.json")
    )
    parser.add_argument("--only", action="append", default=[])
    args = parser.parse_args()
    from ha_playwright_auth import context_kwargs
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    results: list[dict[str, Any]] = []
    active: list[BareSip] = []
    page: Any | None = None
    automation_states = {
        entity_id: state == "on"
        for entity_id in (AUTOMATION, INBOUND_AUTOMATION)
        if (state := optional_entity_state(entity_id)) is not None
    }

    def case(name: str, run: Callable[[], dict[str, Any]]) -> None:
        if args.only and name not in args.only:
            return
        started = time.monotonic()
        detail: dict[str, Any] = {}
        failure: Exception | None = None
        try:
            detail = run()
        except Exception as error:  # noqa: BLE001 - matrix must continue and report every row.
            failure = error
        finally:
            while active:
                active.pop().close()
            cleanup_error: Exception | None = None
            if page is not None and not page.is_closed():
                try:
                    page.evaluate(SET_AUTO_ANSWER, False)
                    wait_card(
                        page,
                        lambda item: (
                            item["backend"]["state"] == "idle"
                            and item["card"]["state"] == "idle"
                            and item["backend"]["runtime_resources"].get(
                                "call_scoped_quiescent"
                            )
                            is True
                        ),
                        8,
                        "post-scenario quiescence",
                    )
                except Exception as error:  # noqa: BLE001 - preserve cleanup evidence.
                    cleanup_error = error
            record: dict[str, Any] = {
                "name": name,
                "status": "pass"
                if failure is None and cleanup_error is None
                else "fail",
                "seconds": round(time.monotonic() - started, 3),
            }
            if failure is not None:
                record["error"] = str(failure)
            if cleanup_error is not None:
                record["cleanup_error"] = str(cleanup_error)
            if failure is None:
                record.update(detail)
            results.append(record)

    disabled_automations = [
        entity_id
        for entity_id in automation_states
        if entity_id != INBOUND_AUTOMATION
        or os.environ.get("KEEP_INBOUND_AUTOMATION", "") != "1"
    ]
    if disabled_automations:
        service(
            "automation",
            "turn_off",
            {
                "entity_id": disabled_automations,
                "stop_actions": True,
            },
        )
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                executable_path="/usr/bin/chromium",
                args=[
                    "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream",
                    "--autoplay-policy=no-user-gesture-required",
                    f"--unsafely-treat-insecure-origin-as-secure={HA_BASE}",
                ],
            )
            context = browser.new_context(**context_kwargs())
            page = context.new_page()
            page.goto(HA_URL, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(5_000)
            card_ready = """() => {
                  const all = (root = document) => {
                    let found = [...root.querySelectorAll('voip-stack-card, intercom-card')];
                    for (const node of root.querySelectorAll('*')) if (node.shadowRoot) found = found.concat(all(node.shadowRoot));
                    return found;
                  };
                  return all().some((card) => (card.config?.mode || card.config?.card_mode || '') === 'ha_softphone');
                }"""
            try:
                page.wait_for_function(card_ready, timeout=30_000)
            except PlaywrightTimeoutError:
                # Directly after an HA restart the dashboard can finish before
                # the integration-owned Lovelace resource is registered. One
                # ordinary reload is the same recovery HA asks of a browser;
                # a second failure remains a real test failure.
                page.reload(wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_function(card_ready, timeout=30_000)
            initial = wait_card(
                page,
                lambda item: item["backend"]["state"] == "idle",
                15,
                "initial idle",
            )
            phone_device_id = str(initial["backend"].get("device_id") or "")
            if not phone_device_id:
                raise RuntimeError("HA softphone device_id is unavailable")
            page.evaluate(SET_AUTO_ANSWER, False)
            if os.environ.get("EXPECT_VIDEO", "") == "1":
                if not page.evaluate(SET_SEND_VIDEO, True):
                    raise RuntimeError("failed to enable Send Camera")

            def remote_hangup() -> dict[str, Any]:
                caller = dial_browser_inbound()
                active.append(caller)
                ringing = matching(page, "ringing")
                caller.hangup()
                idle = matching(page, "idle")
                expected_terminal = (
                    "remote_hangup" if caller.call_established else "cancelled"
                )
                if idle["card"]["terminal_reason"] != expected_terminal:
                    raise RuntimeError(f"wrong terminal reason: {idle}")
                return {
                    "call_id": ringing["card"]["call_id"],
                    "terminal": idle["card"]["terminal_reason"],
                }

            case("trunk_live_ringing_remote_hangup", remote_hangup)

            def refresh_ringing() -> dict[str, Any]:
                caller = dial_browser_inbound()
                active.append(caller)
                ringing = matching(page, "ringing")
                page.reload(wait_until="domcontentloaded")
                page.wait_for_timeout(3_000)
                restored = matching(page, "ringing")
                if restored["card"]["call_id"] != ringing["card"]["call_id"]:
                    raise RuntimeError("refresh changed call_id")
                caller.hangup()
                matching(page, "idle")
                return {"call_id": ringing["card"]["call_id"]}

            case("refresh_during_ringing", refresh_ringing)

            def answer_from_card() -> dict[str, Any]:
                caller = dial_browser_inbound()
                active.append(caller)
                ringing = matching(page, "ringing")
                if not page.evaluate(CLICK, "Answer"):
                    raise RuntimeError("Answer button unavailable")
                answered = matching(page, "in_call")
                caller.wait_for("Call established", 5)
                if os.environ.get("EXPECT_VIDEO", "") == "1":
                    answered = wait_card(
                        page,
                        lambda item: (
                            item["backend"]["state"] == "in_call"
                            and item["backend"]["video_direction"] == "sendrecv"
                            and item["backend"]["video_rtp_tx_packets"] > 0
                            and item["backend"]["video_rtp_rx_packets"] > 0
                        ),
                        8,
                        "bidirectional video RTP",
                    )
                caller.hangup()
                matching(page, "idle")
                return {
                    "call_id": ringing["card"]["call_id"],
                    "answered": answered["card"],
                }

            case("manual_answer_from_card", answer_from_card)

            def wait_dtmf_event(
                call_id: str,
                digit: str,
                transport: str,
                *,
                source_leg: str = "caller",
            ) -> dict[str, Any]:
                deadline = time.monotonic() + 5
                observed = event_state()
                while time.monotonic() < deadline and not (
                    observed.get("event_type") == "dtmf"
                    and observed.get("call_id") == call_id
                    and observed.get("digit") == digit
                ):
                    time.sleep(0.05)
                    observed = event_state()
                if observed.get("event_type") != "dtmf":
                    raise RuntimeError(
                        f"in-dialog DTMF event was not published: {observed}"
                    )
                if observed.get("source_leg") != source_leg:
                    raise RuntimeError(f"DTMF source leg is not canonical: {observed}")
                if observed.get("transport") != transport:
                    raise RuntimeError(
                        f"DTMF did not use expected {transport}: {observed}"
                    )
                return observed

            def in_call_sip_info_dtmf_event() -> dict[str, Any]:
                caller = dial_trunk()
                active.append(caller)
                ringing = matching(page, "ringing")
                if not page.evaluate(CLICK, "Answer"):
                    raise RuntimeError("Answer button unavailable")
                matching(page, "in_call")
                observed_digits: list[str] = []
                observed: dict[str, Any] = {}
                for digit in "0123456789*#":
                    caller.digits(digit)
                    observed = wait_dtmf_event(
                        ringing["backend"]["call_id"], digit, "sip_info"
                    )
                    observed_digits.append(str(observed.get("digit") or ""))
                caller.hangup()
                matching(page, "idle")
                return {
                    "call_id": ringing["backend"]["call_id"],
                    "digits": "".join(observed_digits),
                    "transport": observed.get("transport"),
                    "source_leg": observed.get("source_leg"),
                    "ingress": observed.get("ingress"),
                }

            case("in_call_sip_info_dtmf_event", in_call_sip_info_dtmf_event)

            def in_call_registered_sip_info_dtmf_event() -> dict[str, Any]:
                caller = BareSip(
                    LOCAL_CONFIG,
                    headless_audio=True,
                    dtmf_mode="info",
                )
                active.append(caller)
                caller.dial(LOCAL_SIP_TARGET, wait_for="180 Ringing")
                ringing = matching(page, "ringing")
                if not page.evaluate(CLICK, "Answer"):
                    raise RuntimeError("Answer button unavailable")
                matching(page, "in_call")
                caller.wait_for("Call established", 5)
                caller.digits("5")
                observed = wait_dtmf_event(
                    ringing["backend"]["call_id"], "5", "sip_info"
                )
                caller.hangup()
                matching(page, "idle")
                return {
                    "call_id": ringing["backend"]["call_id"],
                    "digit": observed.get("digit"),
                    "transport": observed.get("transport"),
                    "source_leg": observed.get("source_leg"),
                    "ingress": observed.get("ingress"),
                }

            case(
                "in_call_registered_sip_info_dtmf_event",
                in_call_registered_sip_info_dtmf_event,
            )

            def in_call_rfc4733_dtmf_event() -> dict[str, Any]:
                caller = BareSip(
                    LOCAL_CONFIG,
                    headless_audio=True,
                    dtmf_mode="rtpevent",
                )
                active.append(caller)
                caller.dial(
                    LOCAL_SIP_TARGET,
                    wait_for="180 Ringing",
                )
                ringing = matching(page, "ringing")
                if not page.evaluate(CLICK, "Answer"):
                    raise RuntimeError("Answer button unavailable")
                matching(page, "in_call")
                caller.wait_for("Call established", 5)
                caller.digits("6")
                observed = wait_dtmf_event(
                    ringing["backend"]["call_id"], "6", "rtp_event"
                )
                caller.hangup()
                matching(page, "idle")
                return {
                    "call_id": ringing["backend"]["call_id"],
                    "digit": observed.get("digit"),
                    "transport": observed.get("transport"),
                    "source_leg": observed.get("source_leg"),
                    "ingress": observed.get("ingress"),
                }

            case("in_call_rfc4733_dtmf_event", in_call_rfc4733_dtmf_event)

            def outbound_dtmf_event(
                *,
                mode: str,
                digits: str,
                transport: str,
            ) -> dict[str, Any]:
                callee = BareSip(
                    LOCAL_CONFIG,
                    headless_audio=True,
                    dtmf_mode=mode,
                )
                active.append(callee)
                service(
                    "voip_stack",
                    "call",
                    {
                        "destination": LOCAL_REGISTERED_TARGET,
                        "device_id": phone_device_id,
                    },
                )
                calling = wait_card(
                    page,
                    lambda item: (
                        item["backend"]["state"] in {"calling", "remote_ringing"}
                        and item["card"]["state"] == item["backend"]["state"]
                        and item["backend"]["call_id"] == item["card"]["call_id"]
                    ),
                    12,
                    "outbound registered SIP ringing",
                )
                callee.wait_for("Incoming call", 8)
                callee.command("/accept")
                callee.wait_for("Call established", 8)
                matching(page, "in_call")
                observed_digits: list[str] = []
                observed: dict[str, Any] = {}
                for digit in digits:
                    callee.digits(digit)
                    observed = wait_dtmf_event(
                        calling["backend"]["call_id"],
                        digit,
                        transport,
                        source_leg="callee",
                    )
                    observed_digits.append(str(observed.get("digit") or ""))
                callee.hangup()
                matching(page, "idle")
                return {
                    "call_id": calling["backend"]["call_id"],
                    "digits": "".join(observed_digits),
                    "transport": observed.get("transport"),
                    "source_leg": observed.get("source_leg"),
                    "direction": observed.get("direction"),
                }

            case(
                "outbound_sip_info_dtmf_event",
                lambda: outbound_dtmf_event(
                    mode="info", digits="8", transport="sip_info"
                ),
            )
            case(
                "outbound_rfc4733_dtmf_event",
                lambda: outbound_dtmf_event(
                    mode="rtpevent", digits="9", transport="rtp_event"
                ),
            )
            case(
                "outbound_rfc4733_dtmf_keypad",
                lambda: outbound_dtmf_event(
                    mode="rtpevent", digits="0123456789*#", transport="rtp_event"
                ),
            )

            def decline_from_card() -> dict[str, Any]:
                caller = dial_browser_inbound()
                active.append(caller)
                ringing = matching(page, "ringing")
                if not page.evaluate(CLICK, "Decline"):
                    raise RuntimeError("Decline button unavailable")
                idle = matching(page, "idle")
                return {
                    "call_id": ringing["card"]["call_id"],
                    "terminal": idle["card"]["terminal_reason"],
                }

            case("decline_from_card", decline_from_card)

            def auto_answer() -> dict[str, Any]:
                if not page.evaluate(SET_AUTO_ANSWER, True):
                    raise RuntimeError("failed to enable Auto Answer")
                page.wait_for_timeout(500)
                caller = dial_browser_inbound()
                active.append(caller)
                answered = matching(page, "in_call")
                caller.hangup()
                matching(page, "idle")
                page.evaluate(SET_AUTO_ANSWER, False)
                return {"call_id": answered["card"]["call_id"]}

            case("auto_answer", auto_answer)

            def forward_assist() -> dict[str, Any]:
                caller = dial_browser_inbound()
                active.append(caller)
                ringing = matching(page, "ringing")
                service(
                    "voip_stack",
                    "forward",
                    {
                        "destination": FORWARD_SUCCESS_TARGET,
                        "on_failure": "resume",
                    },
                )
                released = matching(page, "idle")
                if released["card"]["terminal_reason"] != "forwarded":
                    raise RuntimeError(
                        f"forward was not exposed as forwarded: {released}"
                    )
                deadline = time.monotonic() + 10
                aggregate = event_state()
                while time.monotonic() < deadline and not (
                    aggregate.get("state") == "in_call"
                    and aggregate.get("callee") == FORWARD_SUCCESS_CALLEE
                ):
                    time.sleep(0.1)
                    aggregate = event_state()
                if (
                    aggregate.get("state") != "in_call"
                    or aggregate.get("callee") != FORWARD_SUCCESS_CALLEE
                ):
                    raise RuntimeError(f"Assist did not answer: {aggregate}")
                if aggregate.get("call_id") != ringing["card"]["call_id"]:
                    raise RuntimeError("logical call_id changed during forward")
                caller.hangup()
                matching(page, "idle")
                return {
                    "call_id": aggregate["call_id"],
                    "released": released["card"],
                    "aggregate": aggregate,
                }

            case("forward_releases_ha_and_keeps_call_alive", forward_assist)

            def failed_forward_resume() -> dict[str, Any]:
                caller = dial_browser_inbound()
                active.append(caller)
                ringing = matching(page, "ringing")
                attrs = event_state()
                service(
                    "voip_stack",
                    "forward",
                    {
                        "call_id": ringing["backend"]["call_id"],
                        "destination": "sip:nobody@127.0.0.1:9",
                        "expected_state": attrs["state"],
                        "expected_sequence": attrs["sequence"],
                        "on_failure": "resume",
                    },
                )
                resumed = matching(page, "ringing")
                if resumed["card"]["call_id"] != ringing["card"]["call_id"]:
                    raise RuntimeError("resume changed call_id")
                caller.hangup()
                matching(page, "idle")
                return {"call_id": ringing["card"]["call_id"]}

            case("failed_forward_resumes_ha", failed_forward_resume)

            def two_browsers() -> dict[str, Any]:
                second = context.new_page()
                second.goto(HA_URL, wait_until="domcontentloaded")
                second.wait_for_timeout(4_000)
                caller = dial_browser_inbound()
                active.append(caller)
                first_state = matching(page, "ringing")
                second_state = matching(second, "ringing")
                caller.hangup()
                matching(page, "idle")
                matching(second, "idle")
                second.close()
                if first_state["card"]["call_id"] != second_state["card"]["call_id"]:
                    raise RuntimeError("browser cards observed different calls")
                return {"call_id": first_state["card"]["call_id"]}

            case("two_browser_subscribers", two_browsers)

            def local_registered_sip() -> dict[str, Any]:
                caller = BareSip(LOCAL_CONFIG)
                active.append(caller)
                caller.dial(
                    LOCAL_SIP_TARGET,
                    wait_for="180 Ringing",
                )
                ringing = matching(page, "ringing")
                caller.hangup()
                matching(page, "idle")
                return {
                    "call_id": ringing["card"]["call_id"],
                    "caller": ringing["card"]["caller"],
                }

            case("registered_sip_live_ringing", local_registered_sip)

            def local_registered_sip_answer() -> dict[str, Any]:
                caller = BareSip(
                    LOCAL_CONFIG,
                    headless_audio=True,
                    video_codec=os.environ.get("LOCAL_VIDEO_CODEC", "VP8"),
                )
                active.append(caller)
                caller.dial(LOCAL_SIP_TARGET, wait_for="180 Ringing")
                ringing = matching(page, "ringing")
                if not page.evaluate(CLICK, "Answer"):
                    raise RuntimeError("Answer button unavailable")
                caller.wait_for("Call established", 5)
                answered = matching(page, "in_call")
                if os.environ.get("EXPECT_VIDEO", "") == "1":
                    answered = wait_card(
                        page,
                        lambda item: (
                            item["backend"]["state"] == "in_call"
                            and item["backend"]["video_direction"] == "sendrecv"
                            and item["backend"]["video_rtp_tx_packets"] > 0
                            and item["backend"]["video_rtp_rx_packets"] > 0
                        ),
                        8,
                        "registered SIP bidirectional video RTP",
                    )
                caller.hangup()
                matching(page, "idle")
                return {
                    "call_id": ringing["card"]["call_id"],
                    "answered": answered["card"],
                }

            case("registered_sip_answer_from_card", local_registered_sip_answer)

            service("automation", "turn_on", {"entity_id": AUTOMATION})

            def automation_fallback() -> dict[str, Any]:
                caller = dial_browser_inbound()
                active.append(caller)
                ringing = matching(page, "ringing")
                released = wait_card(
                    page,
                    lambda item: (
                        item["backend"]["state"] == "idle"
                        and item["card"]["state"] == "idle"
                    ),
                    45,
                    "automation forward releasing HA",
                )
                deadline = time.monotonic() + 8
                aggregate = event_state()
                while time.monotonic() < deadline and not (
                    aggregate.get("state") == "in_call"
                    and aggregate.get("callee") == "Troiaio"
                ):
                    time.sleep(0.1)
                    aggregate = event_state()
                if (
                    aggregate.get("state") != "in_call"
                    or aggregate.get("callee") != "Troiaio"
                ):
                    raise RuntimeError(f"automation fallback failed: {aggregate}")
                if released["card"]["terminal_reason"] not in {"", "forwarded"}:
                    raise RuntimeError(
                        f"forward exposed an unexpected terminal reason: {released}"
                    )
                caller.hangup()
                matching(page, "idle")
                return {"call_id": ringing["card"]["call_id"], "aggregate": aggregate}

            case("single_automation_30s_fallback", automation_fallback)
            context.close()
            browser.close()
    finally:
        for entity_id, was_on in automation_states.items():
            restore_data: dict[str, Any] = {"entity_id": entity_id}
            if not was_on:
                restore_data["stop_actions"] = True
            service("automation", "turn_on" if was_on else "turn_off", restore_data)
        for caller in active:
            caller.close()
        for path in TEST_CAPTURE_DIR.glob("dump-sip:*.wav"):
            path.unlink(missing_ok=True)

    Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 1 if any(item["status"] != "pass" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
