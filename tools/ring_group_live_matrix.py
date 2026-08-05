#!/usr/bin/env python3
"""Real two-browser ring-group qualification through the Wildix trunk."""

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
WILDIX_CONFIG = Path(os.environ.get("WILDIX_CONFIG", "/home/codex/.baresip-wildix-426"))
CASA_URL = f"{HA_BASE}/lovelace/default_view"
TEST_URL = f"{HA_BASE}/lovelace/test"
EXPECT_VIDEO = os.environ.get("EXPECT_VIDEO", "") == "1"
DIRECT_VIDEO_ONLY = os.environ.get("DIRECT_VIDEO_ONLY", "") == "1"
ESP_WINNER_ONLY = os.environ.get("ESP_WINNER_ONLY", "") == "1"
CALLER_CONFIG = (
    Path("/home/codex/.baresip-wildix-426-video")
    if EXPECT_VIDEO and "WILDIX_CONFIG" not in os.environ
    else WILDIX_CONFIG
)
RUN_LOCK = Path("/tmp/voip-stack-ring-group-live-matrix.lock")
ESP_DND_ENTITY = "switch.cucina_waveshare_s3_audio_do_not_disturb"
ESP_AUTO_ANSWER_ENTITY = "switch.cucina_waveshare_s3_audio_auto_answer"
ESP_CALL_STATE_ENTITY = "sensor.cucina_waveshare_s3_audio_voip_state"
CALL_EVENT_ENTITY = "event.voip_stack_call"
INBOUND_AUTOMATION = "automation.voip_inbound_trunk_to_rg_casa"
DIRECT_AUTOMATION_ID = "codex_voip_video_route_matrix"
DIRECT_AUTOMATION = "automation.codex_voip_video_direct_route_matrix"


def _load_runtime_dependencies() -> None:
    """Load browser/lab dependencies only for an actual live matrix run."""

    global BareSip, CLICK, HA_BASE, HomeAssistantApi, SET_AUTO_ANSWER  # noqa: PLW0603
    global SET_SEND_VIDEO, WILDIX_CONFIG, context_kwargs, ha_request  # noqa: PLW0603
    global service, sync_playwright, wait_card  # noqa: PLW0603

    try:
        from playwright.sync_api import sync_playwright as playwright_factory
        from ha_playwright_auth import context_kwargs as browser_context_kwargs
        from inbound_routing_qualification import HomeAssistantApi as ApiClient
        from ha_softphone_matrix import (
            BareSip as BareSipClient,
            CLICK as click_script,
            HA_BASE as matrix_ha_base,
            SET_AUTO_ANSWER as set_auto_answer_script,
            SET_SEND_VIDEO as set_send_video_script,
            WILDIX_CONFIG as matrix_wildix_config,
            ha_request as matrix_ha_request,
            service as matrix_service,
            wait_card as matrix_wait_card,
        )
    except ModuleNotFoundError as err:
        raise RuntimeError(
            "the live ring-group matrix requires Playwright and its laboratory helpers"
        ) from err

    sync_playwright = playwright_factory
    context_kwargs = browser_context_kwargs
    HomeAssistantApi = ApiClient
    BareSip = BareSipClient
    CLICK = click_script
    HA_BASE = matrix_ha_base
    SET_AUTO_ANSWER = set_auto_answer_script
    SET_SEND_VIDEO = set_send_video_script
    WILDIX_CONFIG = matrix_wildix_config
    ha_request = matrix_ha_request
    service = matrix_service
    wait_card = matrix_wait_card


def _compact_error(error: BaseException, limit: int = 1800) -> str:
    """Keep state evidence without embedding the complete Lovelace DOM."""

    value = str(error)
    if len(value) <= limit:
        return value
    tail = min(300, limit // 4)
    return f"{value[: limit - tail]} ... <truncated> ... {value[-tail:]}"


@contextmanager
def _exclusive_run():
    """Prevent two real callers from corrupting one another's evidence."""

    descriptor = os.open(RUN_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as err:
            raise RuntimeError(
                "another ring-group live matrix is already running"
            ) from err
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the real two-browser Wildix ring-group matrix once."
    )
    parser.add_argument(
        "--out",
        default=os.environ.get(
            "RING_GROUP_MATRIX_OUT",
            str(ROOT / "test_captures" / "ring_group_live_matrix.json"),
        ),
    )
    parser.add_argument(
        "--expect-video",
        action=argparse.BooleanOptionalAction,
        default=EXPECT_VIDEO,
    )
    parser.add_argument(
        "--direct-video-only",
        action=argparse.BooleanOptionalAction,
        default=DIRECT_VIDEO_ONLY,
    )
    parser.add_argument(
        "--esp-winner-only",
        action=argparse.BooleanOptionalAction,
        default=ESP_WINNER_ONLY,
    )
    return parser.parse_args()


def _wait_entity(entity_id: str, expected: str, timeout: float = 10) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = ha_request(f"/api/states/{entity_id}")
        if last.get("state") == expected:
            return last
        time.sleep(0.1)
    raise RuntimeError(
        f"timeout waiting for {entity_id}={expected}; got {last.get('state', '-')}"
    )


def _set_esp_dnd(enabled: bool) -> None:
    service(
        "switch",
        "turn_on" if enabled else "turn_off",
        {"entity_id": ESP_DND_ENTITY},
    )
    _wait_entity(ESP_DND_ENTITY, "on" if enabled else "off")


def _set_esp_auto_answer(enabled: bool) -> None:
    service(
        "switch",
        "turn_on" if enabled else "turn_off",
        {"entity_id": ESP_AUTO_ANSWER_ENTITY},
    )
    _wait_entity(ESP_AUTO_ANSWER_ENTITY, "on" if enabled else "off")


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


def _winner(page: Any, call_id: str, label: str) -> dict[str, Any]:
    return wait_card(
        page,
        lambda item: (
            item["backend"]["state"] == "in_call"
            and item["backend"]["call_id"] == call_id
            and (
                not EXPECT_VIDEO
                or (
                    item["backend"]["video_direction"] == "sendrecv"
                    and item["backend"]["video_rtp_tx_packets"] > 0
                    and item["backend"]["video_rtp_rx_packets"] > 0
                )
            )
        ),
        10,
        label,
    )


def _dial() -> BareSip:
    caller = BareSip(CALLER_CONFIG)
    try:
        caller.dial(
            "427",
            wait_for=("183 Session Progress", "Call established"),
        )
    except BaseException:
        caller.close()
        raise
    return caller


def _wait_lab_ready(
    timeout: float = 45,
) -> tuple[HomeAssistantApi, dict[str, Any], dict[str, Any]]:
    """Wait until HA auth, VoIP entities and the ESP mirror are all available."""

    deadline = time.monotonic() + timeout
    last_error = "Home Assistant did not answer"
    while time.monotonic() < deadline:
        try:
            api = HomeAssistantApi()
            esp_dnd = ha_request(f"/api/states/{ESP_DND_ENTITY}")
            inbound_automation = ha_request(f"/api/states/{INBOUND_AUTOMATION}")
            if esp_dnd.get("state") in {"on", "off"} and inbound_automation.get(
                "state"
            ) in {"on", "off"}:
                return api, esp_dnd, inbound_automation
            last_error = (
                f"entities not ready: {ESP_DND_ENTITY}={esp_dnd.get('state')!r}, "
                f"{INBOUND_AUTOMATION}={inbound_automation.get('state')!r}"
            )
        except Exception as err:  # noqa: BLE001 - bounded startup qualification
            last_error = f"{type(err).__name__}: {err}"
        time.sleep(0.5)
    raise RuntimeError(f"HA laboratory not ready after {timeout:.0f}s ({last_error})")


def _set_inbound_automation(enabled: bool, timeout: float = 10) -> None:
    """Set the test dialplan state and wait until HA has applied it."""

    expected = "on" if enabled else "off"
    service_data: dict[str, object] = {"entity_id": INBOUND_AUTOMATION}
    if not enabled:
        service_data["stop_actions"] = True
    service("automation", "turn_on" if enabled else "turn_off", service_data)
    deadline = time.monotonic() + timeout
    last_state = ""
    while time.monotonic() < deadline:
        last_state = str(
            ha_request(f"/api/states/{INBOUND_AUTOMATION}").get("state") or ""
        )
        if last_state == expected:
            return
        time.sleep(0.1)
    raise RuntimeError(
        f"{INBOUND_AUTOMATION} did not become {expected}; got {last_state or 'unknown'}"
    )


def main(*, output: Path | None = None) -> int:
    _load_runtime_dependencies()
    output = output or Path(
        os.environ.get(
            "RING_GROUP_MATRIX_OUT",
            ROOT / "test_captures" / "ring_group_live_matrix.json",
        )
    )
    results: list[dict[str, Any]] = []
    api, esp_dnd_state, inbound_automation_state = _wait_lab_ready()
    original_esp_dnd = esp_dnd_state["state"] == "on"
    original_esp_auto_answer = (
        ha_request(f"/api/states/{ESP_AUTO_ANSWER_ENTITY}")["state"] == "on"
    )
    original_inbound_automation = inbound_automation_state["state"] == "on"
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
            casa = context.new_page()
            test = context.new_page()
            casa.goto(CASA_URL, wait_until="domcontentloaded", timeout=30_000)
            test.goto(TEST_URL, wait_until="domcontentloaded", timeout=30_000)
            for page, name in ((casa, "Casa"), (test, "Test")):
                try:
                    wait_card(
                        page,
                        lambda item: bool(item),
                        30,
                        f"{name} card ready",
                    )
                except RuntimeError:
                    # Immediately after an HA restart the dashboard may load
                    # before the integration-owned Lovelace resource is
                    # registered. One ordinary reload is the same bounded
                    # recovery used by the single-card matrix; a second
                    # failure remains a real qualification failure.
                    page.reload(wait_until="domcontentloaded", timeout=30_000)
                    wait_card(
                        page,
                        lambda item: bool(item),
                        30,
                        f"{name} card ready after reload",
                    )
                page.evaluate(SET_AUTO_ANSWER, False)
                if EXPECT_VIDEO and not page.evaluate(SET_SEND_VIDEO, True):
                    raise RuntimeError(f"failed to enable Send Camera on {name}")

            def run_esp_winner() -> None:
                started = time.monotonic()
                caller: BareSip | None = None
                case_name = (
                    "esp_audio_only_wins_video_offer"
                    if EXPECT_VIDEO
                    else "esp_auto_answer_wins"
                )
                try:
                    _set_esp_dnd(False)
                    _set_esp_auto_answer(True)
                    caller = _dial()
                    # This timer starts at 183, before the configured five-second
                    # inbound DTMF window and the automation route decision.
                    caller.wait_for("Call established", 10)
                    # The upstream dialog may already be established for SIP
                    # INFO DTMF while the configured five-second routing
                    # window is still open. Start the ESP winner deadline from
                    # the full route window, not from that early 200 OK.
                    _wait_entity(ESP_CALL_STATE_ENTITY, "in_call", 10)
                    casa_lost = wait_card(
                        casa,
                        lambda item: (
                            item["backend"]["state"] == "idle"
                            and item["backend"]["terminal_reason"] == "cancelled"
                            and bool(item["backend"]["call_id"])
                        ),
                        5,
                        "ESP winner cancels Casa",
                    )
                    call_id = casa_lost["backend"]["call_id"]
                    wait_card(
                        test,
                        lambda item: (
                            item["backend"]["state"] == "idle"
                            and item["backend"]["terminal_reason"] == "cancelled"
                            and item["backend"]["call_id"] == call_id
                        ),
                        5,
                        "ESP winner cancels Test",
                    )
                    time.sleep(1)
                    _wait_entity(ESP_CALL_STATE_ENTITY, "in_call", 2)
                    aggregate = ha_request(f"/api/states/{CALL_EVENT_ENTITY}").get(
                        "attributes", {}
                    )
                    if aggregate.get("state") != "in_call":
                        raise RuntimeError(
                            f"ESP winner did not retain the established call: {aggregate}"
                        )
                    caller.hangup()
                    _wait_entity(ESP_CALL_STATE_ENTITY, "idle")
                    results.append(
                        {
                            "name": case_name,
                            "status": "pass",
                            "seconds": round(time.monotonic() - started, 3),
                            "call_id": call_id,
                            "winner": "Waveshare S3 Audio",
                            "source_offered_video": EXPECT_VIDEO,
                            "audio_direction": aggregate.get("audio_direction", ""),
                            "video_direction": aggregate.get("video_direction", ""),
                        }
                    )
                except Exception as err:  # noqa: BLE001 - preserve matrix result.
                    results.append(
                        {
                            "name": case_name,
                            "status": "fail",
                            "seconds": round(time.monotonic() - started, 3),
                            "error": _compact_error(err),
                        }
                    )
                finally:
                    if caller is not None:
                        caller.close()
                    for page, endpoint_name in ((casa, "Casa"), (test, "Test")):
                        with suppress(Exception):
                            _state(
                                page,
                                "idle",
                                f"{case_name}: cleanup {endpoint_name}",
                                8,
                            )
                    time.sleep(0.5)

            def run_case(
                name: str,
                *,
                winner_page: Any | None,
                winner_name: str = "",
                decline_page: Any | None = None,
                decline_name: str = "",
                remote_cancel: bool = False,
            ) -> None:
                started = time.monotonic()
                caller: BareSip | None = None
                try:
                    caller = _dial()
                    casa_ringing = _state(casa, "ringing", f"{name}: Casa ringing")
                    test_ringing = _state(test, "ringing", f"{name}: Test ringing")
                    call_id = casa_ringing["backend"]["call_id"]
                    if test_ringing["backend"]["call_id"] != call_id:
                        raise RuntimeError(
                            "ring-group members received different Call-IDs"
                        )
                    if (
                        ha_request(f"/api/states/{ESP_CALL_STATE_ENTITY}")["state"]
                        != "idle"
                    ):
                        raise RuntimeError("DND ESP joined the active ring-group call")
                    if remote_cancel:
                        caller.hangup()
                        _state(casa, "idle", f"{name}: Casa idle")
                        _state(test, "idle", f"{name}: Test idle")
                    else:
                        if decline_page is not None:
                            if not decline_page.evaluate(CLICK, "Decline"):
                                raise RuntimeError(
                                    f"Decline unavailable on {decline_name}"
                                )
                            _state(
                                decline_page,
                                "idle",
                                f"{name}: {decline_name} declined",
                            )
                            other = test if decline_page is casa else casa
                            _state(
                                other, "ringing", f"{name}: remaining member ringing"
                            )
                        if winner_page is None or not winner_page.evaluate(
                            CLICK, "Answer"
                        ):
                            raise RuntimeError(f"Answer unavailable on {winner_name}")
                        answered = _winner(
                            winner_page,
                            call_id,
                            f"{name}: {winner_name} winner media",
                        )
                        caller.wait_for("Call established", 5)
                        loser = test if winner_page is casa else casa
                        _state(loser, "idle", f"{name}: losing member idle")
                        caller.hangup()
                        _state(winner_page, "idle", f"{name}: winner idle")
                        results.append(
                            {
                                "name": name,
                                "status": "pass",
                                "seconds": round(time.monotonic() - started, 3),
                                "call_id": call_id,
                                "winner": winner_name,
                                "esp_dnd": True,
                                "video_direction": answered["backend"][
                                    "video_direction"
                                ],
                                "video_rtp_tx_packets": answered["backend"][
                                    "video_rtp_tx_packets"
                                ],
                                "video_rtp_rx_packets": answered["backend"][
                                    "video_rtp_rx_packets"
                                ],
                            }
                        )
                        return
                    results.append(
                        {
                            "name": name,
                            "status": "pass",
                            "seconds": round(time.monotonic() - started, 3),
                            "call_id": call_id,
                            "esp_dnd": True,
                        }
                    )
                except Exception as err:  # noqa: BLE001 - preserve every matrix result.
                    results.append(
                        {
                            "name": name,
                            "status": "fail",
                            "seconds": round(time.monotonic() - started, 3),
                            "error": _compact_error(err),
                        }
                    )
                finally:
                    if caller is not None:
                        caller.close()
                    for page, endpoint_name in ((casa, "Casa"), (test, "Test")):
                        with suppress(Exception):
                            _state(
                                page,
                                "idle",
                                f"{name}: cleanup {endpoint_name}",
                                8,
                            )
                    time.sleep(0.5)

            def run_direct_video_case(
                name: str,
                target_page: Any,
                target_name: str,
                other_page: Any,
            ) -> None:
                started = time.monotonic()
                caller: BareSip | None = None
                try:
                    api.delete(
                        f"/api/config/automation/config/{DIRECT_AUTOMATION_ID}",
                        allow_missing=True,
                    )
                    api.post(
                        f"/api/config/automation/config/{DIRECT_AUTOMATION_ID}",
                        {
                            "id": DIRECT_AUTOMATION_ID,
                            "alias": "Codex VoIP video direct route matrix",
                            "description": (
                                "Temporary video-preserving route qualification"
                            ),
                            "triggers": [
                                {
                                    "trigger": "event.received",
                                    "target": {"entity_id": CALL_EVENT_ENTITY},
                                    "options": {"event_type": ["route_requested"]},
                                }
                            ],
                            "conditions": [],
                            "actions": [
                                {
                                    "action": "voip_stack.forward",
                                    "data": {
                                        "destination": target_name,
                                        "on_failure": "resume",
                                    },
                                }
                            ],
                            "mode": "parallel",
                            "max": 4,
                        },
                    )
                    api.service("automation", "reload")
                    api.service(
                        "automation",
                        "turn_off",
                        {
                            "entity_id": INBOUND_AUTOMATION,
                            "stop_actions": True,
                        },
                    )
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline:
                        try:
                            if (
                                api.state(DIRECT_AUTOMATION).get("state") == "on"
                                and api.state(INBOUND_AUTOMATION).get("state") == "off"
                            ):
                                break
                        except Exception:
                            pass
                        time.sleep(0.1)
                    else:
                        raise RuntimeError(
                            "temporary direct route automation unavailable"
                        )
                    caller = _dial()
                    ringing = _state(
                        target_page,
                        "ringing",
                        f"{name}: {target_name} ringing",
                    )
                    call_id = ringing["backend"]["call_id"]
                    _state(other_page, "idle", f"{name}: other endpoint idle")
                    if not target_page.evaluate(CLICK, "Answer"):
                        raise RuntimeError(f"Answer unavailable on {target_name}")
                    answered = _winner(
                        target_page,
                        call_id,
                        f"{name}: {target_name} video",
                    )
                    caller.hangup()
                    _state(target_page, "idle", f"{name}: {target_name} idle")
                    results.append(
                        {
                            "name": name,
                            "status": "pass",
                            "seconds": round(time.monotonic() - started, 3),
                            "call_id": call_id,
                            "route_destination": target_name,
                            "winner": target_name,
                            "video_direction": answered["backend"]["video_direction"],
                            "video_rtp_tx_packets": answered["backend"][
                                "video_rtp_tx_packets"
                            ],
                            "video_rtp_rx_packets": answered["backend"][
                                "video_rtp_rx_packets"
                            ],
                        }
                    )
                except Exception as err:  # noqa: BLE001 - retain matrix evidence.
                    results.append(
                        {
                            "name": name,
                            "status": "fail",
                            "seconds": round(time.monotonic() - started, 3),
                            "error": _compact_error(err),
                        }
                    )
                finally:
                    if caller is not None:
                        caller.close()
                    for page, endpoint_name in (
                        (target_page, target_name),
                        (other_page, "other endpoint"),
                    ):
                        try:
                            _state(page, "idle", f"{name}: cleanup {endpoint_name}")
                        except Exception:
                            pass
                    time.sleep(0.5)

            if not DIRECT_VIDEO_ONLY:
                # The operator may deliberately keep this automation disabled.
                # Ring-group qualification owns it only for this bounded phase;
                # the outer finally restores the exact original state.
                _set_inbound_automation(True)
                run_esp_winner()
                if not ESP_WINNER_ONLY:
                    _set_esp_dnd(True)
                    run_case("casa_answers", winner_page=casa, winner_name="Casa")
                    run_case("test_answers", winner_page=test, winner_name="Test")
                    run_case(
                        "casa_declines_test_answers",
                        winner_page=test,
                        winner_name="Test",
                        decline_page=casa,
                        decline_name="Casa",
                    )
                    run_case(
                        "test_declines_casa_answers",
                        winner_page=casa,
                        winner_name="Casa",
                        decline_page=test,
                        decline_name="Test",
                    )
                    run_case("caller_cancels", winner_page=None, remote_cancel=True)
            _set_esp_dnd(True)
            if EXPECT_VIDEO and not ESP_WINNER_ONLY:
                service(
                    "automation",
                    "turn_off",
                    {"entity_id": INBOUND_AUTOMATION, "stop_actions": True},
                )
                try:
                    run_direct_video_case(
                        "wildix_video_to_casa",
                        casa,
                        "Casa",
                        test,
                    )
                    run_direct_video_case(
                        "wildix_video_to_test",
                        test,
                        "Test",
                        casa,
                    )
                finally:
                    if original_inbound_automation:
                        service(
                            "automation",
                            "turn_on",
                            {"entity_id": INBOUND_AUTOMATION},
                        )
            context.close()
            browser.close()
    finally:
        _set_esp_dnd(original_esp_dnd)
        _set_esp_auto_answer(original_esp_auto_answer)
        if EXPECT_VIDEO:
            api.delete(
                f"/api/config/automation/config/{DIRECT_AUTOMATION_ID}",
                allow_missing=True,
            )
            api.service("automation", "reload")
        service(
            "automation",
            "turn_on" if original_inbound_automation else "turn_off",
            {"entity_id": INBOUND_AUTOMATION},
        )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    return 1 if any(item["status"] != "pass" for item in results) else 0


if __name__ == "__main__":
    arguments = _parse_args()
    EXPECT_VIDEO = bool(arguments.expect_video)
    DIRECT_VIDEO_ONLY = bool(arguments.direct_video_only)
    ESP_WINNER_ONLY = bool(arguments.esp_winner_only)
    CALLER_CONFIG = (
        Path("/home/codex/.baresip-wildix-426-video")
        if EXPECT_VIDEO and "WILDIX_CONFIG" not in os.environ
        else WILDIX_CONFIG
    )
    with _exclusive_run():
        raise SystemExit(main(output=Path(arguments.out)))
