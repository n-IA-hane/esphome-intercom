#!/usr/bin/env python3
"""Real two-browser HA softphone audio/video qualification matrix."""

from __future__ import annotations

import argparse
from contextlib import contextmanager, suppress
import fcntl
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "test_runs"))

HA_BASE = os.environ.get("HA_BASE", "http://127.0.0.1:18123").rstrip("/")
EXPECT_VIDEO = os.environ.get("EXPECT_VIDEO", "") == "1"
RUN_LOCK = Path("/tmp/voip-stack-local-softphone-live-matrix.lock")
CASA_URL = f"{HA_BASE}/lovelace/default_view"
TEST_URL = f"{HA_BASE}/lovelace/test"

SET_TARGET = r"""
(name) => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    return found;
  };
  const card = deep("voip-stack-card, intercom-card")
    .find((item) => (item.config?.mode || item.config?.card_mode || "") === "ha_softphone");
  const target = (card?._softphoneTargets?.() || []).find((item) => String(item.name || "") === name);
  if (!card || !target?.device_id) return false;
  card._setSoftphoneTarget(target.device_id);
  return card._getSoftphoneTargetDevice?.()?.device_id === target.device_id;
}
"""

ENGINE_STATS = """() => ({
  state: String(globalThis.__voipStackEngine?.state || ""),
  call_id: String(globalThis.__voipStackEngine?.callId || ""),
  video_active: Boolean(globalThis.__voipStackEngine?.videoActive),
  stats: globalThis.__voipStackEngine?.stats || {},
})"""

HANGUP = r"""
async () => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    return found;
  };
  const card = deep("voip-stack-card, intercom-card")
    .find((item) => (item.config?.mode || item.config?.card_mode || "") === "ha_softphone");
  if (!card?._hangup) return false;
  await card._hangup();
  return true;
}
"""

BROWSER_MEDIA_BUSY = r"""
() => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    return found;
  };
  const card = deep("voip-stack-card, intercom-card")
    .find((item) => (item.config?.mode || item.config?.card_mode || "") === "ha_softphone");
  if (!card?.shadowRoot) return null;
  return {
    busy: Boolean(card._otherPhoneOwnsBrowserMedia?.()),
    call_disabled: Boolean(card.shadowRoot.querySelector(".voip-button.call")?.disabled),
    state: String(card._softphoneSnapshot?.state || ""),
    error: String(card._errorMsg || ""),
  };
}
"""


def _load_runtime_dependencies() -> None:
    """Load Playwright only after command-line help has been handled."""

    global CLICK, HA_BASE, SET_AUTO_ANSWER, SET_SEND_VIDEO  # noqa: PLW0603
    global context_kwargs, sync_playwright, wait_card  # noqa: PLW0603

    try:
        from playwright.sync_api import sync_playwright as playwright_factory
        from ha_playwright_auth import context_kwargs as browser_context_kwargs
        from ha_softphone_matrix import (
            CLICK as click_script,
            HA_BASE as matrix_ha_base,
            SET_AUTO_ANSWER as set_auto_answer_script,
            SET_SEND_VIDEO as set_send_video_script,
            wait_card as matrix_wait_card,
        )
    except ModuleNotFoundError as err:
        raise RuntimeError(
            "the local softphone matrix requires Playwright and its laboratory helpers"
        ) from err

    sync_playwright = playwright_factory
    context_kwargs = browser_context_kwargs
    CLICK = click_script
    HA_BASE = matrix_ha_base
    SET_AUTO_ANSWER = set_auto_answer_script
    SET_SEND_VIDEO = set_send_video_script
    wait_card = matrix_wait_card


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the real two-browser local softphone matrix once."
    )
    parser.add_argument(
        "--out",
        default=os.environ.get(
            "LOCAL_SOFTPHONE_MATRIX_OUT",
            str(ROOT / "test_captures" / "local_softphone_live_matrix.json"),
        ),
    )
    parser.add_argument(
        "--expect-video",
        action=argparse.BooleanOptionalAction,
        default=EXPECT_VIDEO,
    )
    parser.add_argument(
        "--ring-group",
        default=os.environ.get("LOCAL_SOFTPHONE_RING_GROUP", ""),
        help="optional ring group exercised through the local HA bridge",
    )
    return parser.parse_args()


@contextmanager
def _exclusive_run():
    descriptor = os.open(RUN_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as err:
            raise RuntimeError(
                "another local softphone live matrix is already running"
            ) from err
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _state(page: Any, expected: str, label: str, timeout: float = 12) -> dict[str, Any]:
    return wait_card(
        page,
        lambda item: (
            item["backend"]["state"] == expected
            and item["card"]["state"] == expected
            and item["backend"]["call_id"] == item["card"]["call_id"]
        ),
        timeout,
        label,
    )


def _wait_video(page: Any, label: str) -> dict[str, Any]:
    deadline = time.monotonic() + 12
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = page.evaluate(ENGINE_STATS) or {}
        video = (last.get("stats") or {}).get("video") or {}
        if (
            last.get("video_active")
            and int(video.get("sent") or 0) > 0
            and int(video.get("received") or 0) > 0
        ):
            return last
        page.wait_for_timeout(100)
    raise RuntimeError(f"timeout waiting for {label}: {last}")


def _wait_card_ready(page: Any, name: str) -> dict[str, Any]:
    """Wait through HA's post-restart Lovelace resource registration window."""
    try:
        return wait_card(page, lambda item: bool(item), 15, f"{name} card ready")
    except RuntimeError:
        page.reload(wait_until="domcontentloaded", timeout=30_000)
        return wait_card(
            page,
            lambda item: bool(item),
            30,
            f"{name} card ready after reload",
        )


def main() -> int:
    arguments = _parse_args()
    _load_runtime_dependencies()
    output = Path(arguments.out)
    results: list[dict[str, Any]] = []
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
        casa = context.new_page()
        test = context.new_page()
        casa.goto(CASA_URL, wait_until="domcontentloaded", timeout=30_000)
        test.goto(TEST_URL, wait_until="domcontentloaded", timeout=30_000)
        for page, name in ((casa, "Casa"), (test, "Test")):
            current = _wait_card_ready(page, name)
            if current["backend"]["state"] != "idle":
                page.evaluate(HANGUP)
                _state(page, "idle", f"{name} initial cleanup")
            page.evaluate(SET_AUTO_ANSWER, False)
            if arguments.expect_video and not page.evaluate(SET_SEND_VIDEO, True):
                raise RuntimeError(f"failed to enable Send Camera on {name}")

        def run_case(
            name: str,
            caller: Any,
            caller_name: str,
            callee: Any,
            callee_name: str,
            *,
            hangup: Any,
            auto_answer: bool = False,
            target_name: str = "",
        ) -> None:
            started = time.monotonic()
            try:
                if auto_answer and not callee.evaluate(SET_AUTO_ANSWER, True):
                    raise RuntimeError(f"failed to enable Auto Answer on {callee_name}")
                selected_target = target_name or callee_name
                if not caller.evaluate(SET_TARGET, selected_target):
                    raise RuntimeError(
                        f"{selected_target} is not selectable from {caller_name}"
                    )
                if not caller.evaluate(CLICK, "Call"):
                    raise RuntimeError(f"Call unavailable on {caller_name}")
                outgoing = wait_card(
                    caller,
                    lambda item: (
                        item["backend"]["state"]
                        in {"calling", "remote_ringing", "in_call"}
                        and item["card"]["state"]
                        in {"calling", "remote_ringing", "in_call"}
                        and item["backend"]["call_id"] == item["card"]["call_id"]
                    ),
                    12,
                    f"{name}: caller started",
                )
                incoming = wait_card(
                    callee,
                    lambda item: (
                        item["backend"]["state"] in {"ringing", "in_call"}
                        and item["card"]["state"] in {"ringing", "in_call"}
                        and item["backend"]["call_id"] == item["card"]["call_id"]
                    ),
                    12,
                    f"{name}: callee incoming",
                )
                call_id = outgoing["backend"]["call_id"]
                if incoming["backend"]["call_id"] != call_id:
                    raise RuntimeError("local endpoints received different Call-IDs")
                if not auto_answer:
                    if not callee.evaluate(CLICK, "Answer"):
                        raise RuntimeError(f"Answer unavailable on {callee_name}")
                _state(caller, "in_call", f"{name}: caller in call")
                _state(callee, "in_call", f"{name}: callee in call")
                media: dict[str, Any] = {}
                if arguments.expect_video:
                    media[caller_name] = _wait_video(caller, f"{caller_name} video")
                    media[callee_name] = _wait_video(callee, f"{callee_name} video")
                if not hangup.evaluate(HANGUP):
                    raise RuntimeError("Hangup unavailable")
                _state(caller, "idle", f"{name}: caller idle")
                _state(callee, "idle", f"{name}: callee idle")
                if auto_answer:
                    callee.evaluate(SET_AUTO_ANSWER, False)
                results.append(
                    {
                        "name": name,
                        "status": "pass",
                        "seconds": round(time.monotonic() - started, 3),
                        "call_id": call_id,
                        "media": media,
                    }
                )
            except Exception as err:  # noqa: BLE001
                results.append(
                    {
                        "name": name,
                        "status": "fail",
                        "seconds": round(time.monotonic() - started, 3),
                        "error": str(err),
                    }
                )
                for page in (caller, callee):
                    with suppress(Exception):
                        page.evaluate(HANGUP)
                if auto_answer:
                    with suppress(Exception):
                        callee.evaluate(SET_AUTO_ANSWER, False)
                for page, endpoint_name in (
                    (caller, caller_name),
                    (callee, callee_name),
                ):
                    with suppress(Exception):
                        _state(page, "idle", f"{name}: cleanup {endpoint_name}")
                time.sleep(0.5)

        run_case("casa_to_test_caller_hangup", casa, "Casa", test, "Test", hangup=casa)
        run_case("test_to_casa_callee_hangup", test, "Test", casa, "Casa", hangup=casa)
        run_case(
            "test_to_casa_auto_answer",
            test,
            "Test",
            casa,
            "Casa",
            hangup=test,
            auto_answer=True,
        )
        if arguments.ring_group:
            run_case(
                "casa_to_ring_group_test_answers",
                casa,
                "Casa",
                test,
                "Test",
                hangup=test,
                target_name=arguments.ring_group,
            )

        started = time.monotonic()
        try:
            if not casa.evaluate(SET_AUTO_ANSWER, True):
                raise RuntimeError("failed to enable Auto Answer on Casa")
            if not test.evaluate(SET_TARGET, "Casa"):
                raise RuntimeError("Casa is not selectable from Test")
            if not test.evaluate(CLICK, "Call"):
                raise RuntimeError("Call unavailable on Test")
            _state(test, "in_call", "navigation cleanup: Test in call")
            _state(casa, "in_call", "navigation cleanup: Casa in call")
            if arguments.expect_video:
                _wait_video(test, "navigation cleanup: Test video")
                _wait_video(casa, "navigation cleanup: Casa video")

            # A full Lovelace navigation destroys the Test card and document,
            # but sessionStorage intentionally survives so a real active call
            # can be handed off after reload.
            test.goto(CASA_URL, wait_until="domcontentloaded", timeout=30_000)
            _wait_card_ready(test, "Casa after Test navigation")
            active_busy = test.evaluate(BROWSER_MEDIA_BUSY)
            if not active_busy or not active_busy["busy"]:
                raise RuntimeError(
                    f"active Test call did not reserve the browser: {active_busy}"
                )
            if active_busy["error"]:
                raise RuntimeError(
                    f"spectator card exposed expected media ownership as an error: {active_busy}"
                )

            if not casa.evaluate(HANGUP):
                raise RuntimeError("Hangup unavailable on Casa")
            _state(casa, "idle", "navigation cleanup: original Casa idle")
            deadline = time.monotonic() + 12
            released = None
            while time.monotonic() < deadline:
                released = test.evaluate(BROWSER_MEDIA_BUSY)
                if (
                    released
                    and not released["busy"]
                    and not released["call_disabled"]
                    and not released["error"]
                ):
                    break
                test.wait_for_timeout(100)
            else:
                raise RuntimeError(
                    f"browser claim/error survived terminal call: {released}"
                )
            original_released = casa.evaluate(BROWSER_MEDIA_BUSY)
            if not original_released or original_released["error"]:
                raise RuntimeError(
                    f"original card retained a media error after hangup: {original_released}"
                )
            results.append(
                {
                    "name": "test_navigation_releases_terminal_browser_claim",
                    "status": "pass",
                    "seconds": round(time.monotonic() - started, 3),
                    "active_busy": active_busy,
                    "released": released,
                    "original_released": original_released,
                }
            )
        except Exception as err:  # noqa: BLE001
            results.append(
                {
                    "name": "test_navigation_releases_terminal_browser_claim",
                    "status": "fail",
                    "seconds": round(time.monotonic() - started, 3),
                    "error": str(err),
                }
            )
            for page in (casa, test):
                with suppress(Exception):
                    page.evaluate(HANGUP)
        finally:
            with suppress(Exception):
                casa.evaluate(SET_AUTO_ANSWER, False)
        context.close()
        browser.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    return 1 if any(item["status"] != "pass" for item in results) else 0


if __name__ == "__main__":
    with _exclusive_run():
        raise SystemExit(main())
