#!/usr/bin/env python3
"""Exercise the isolated HA lab with real SIP peers and native automations."""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Callable

import aiohttp


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "scripts"))

from inbound_routing_qualification import (  # noqa: E402
    EventTrace,
    FlowSnapshot,
    HomeAssistantApi,
    wait_for,
)
from live_voip_qualification import candidate_revision  # noqa: E402
from run_answered_sipp_lab import (  # noqa: E402
    lab_token,
    run_answered_case,
    runtime_quiescence,
)


PACKAGE = ROOT / "qualification/home_assistant/voip_qualification.yaml"
HELPER_PREFIX = "voip_qualification_"
ROUTE_ACTIONS = (
    "no_action",
    "default",
    "answer_ha",
    "decline",
    "busy",
    "cancel",
    "forward",
    "bridge",
)
ANSWER_CASES = (
    "registered_sip_peer_auto_answer_on_caller_bye",
    "registered_sip_peer_auto_answer_off_callee_bye",
    "initial_delayed_offer_caller_bye",
)
POLICY_CASES = (
    "browser_phone_auto_answer_enabled_ha_runtime",
    "browser_phone_auto_answer_disabled_ha_runtime",
    "browser_phone_dnd_enabled",
    "browser_phone_dnd_disabled",
)
CONCURRENCY_CASES = (
    "stale_route_sequence_is_rejected",
    "concurrent_route_requests_remain_distinct",
)
EXTERNAL_EXECUTABLE_CONTRACTS = {
    "dtmf_extension_bypasses_automation": (
        "scripts/run_dtmf_precedence_lab.py",
        "dtmf_primary_extension_bypasses_automation",
    ),
    "ingress_extension_is_not_overridden": (
        "scripts/run_dtmf_precedence_lab.py",
        "dtmf_secondary_extension_bypasses_automation",
    ),
}


@dataclass(frozen=True, slots=True)
class MatrixCase:
    name: str
    execute: Callable[[], dict[str, object]]


class QualificationPackage:
    """Drive the checked-in HA package through its public entity services."""

    def __init__(self, api: HomeAssistantApi) -> None:
        self.api = api
        self.original: dict[str, str] = {}

    def __enter__(self) -> QualificationPackage:
        entities = (
            "input_boolean.voip_qualification_enabled",
            "input_boolean.voip_qualification_condition",
            "input_boolean.voip_qualification_forward_enabled",
            "input_select.voip_qualification_route_action",
            "input_select.voip_qualification_false_route_action",
            "input_select.voip_qualification_forward_failure",
            "input_text.voip_qualification_expected_origin",
            "input_text.voip_qualification_expected_caller",
            "input_text.voip_qualification_expected_target",
            "input_text.voip_qualification_destination",
            "input_text.voip_qualification_false_destination",
            "input_text.voip_qualification_forward_destination",
            "input_text.voip_qualification_last_forward",
        )
        self.original = {entity: self.api.state(entity)["state"] for entity in entities}
        for entity_id in (
            "automation.voip_qualification_route_decision",
            "automation.voip_qualification_ringing_forward",
        ):
            automation = self.api.state(entity_id)
            if automation["state"] != "on":
                self.api.service(
                    "automation",
                    "turn_on",
                    {"entity_id": automation["entity_id"]},
                )
        self.api.service(
            "input_boolean",
            "turn_on",
            {"entity_id": "input_boolean.voip_qualification_enabled"},
        )
        return self

    def select(
        self,
        action: str,
        *,
        destination: str = "",
        condition: bool = True,
        false_action: str = "no_action",
        false_destination: str = "",
        expected_origin: str = "trunk",
        expected_caller: str = "SIPp caller",
        expected_target: str = "",
    ) -> None:
        if action not in ROUTE_ACTIONS:
            raise ValueError(f"unsupported qualification route action: {action}")
        if false_action not in (*ROUTE_ACTIONS, "no_action"):
            raise ValueError(
                f"unsupported qualification false route action: {false_action}"
            )
        self.api.service(
            "input_select",
            "select_option",
            {
                "entity_id": "input_select.voip_qualification_route_action",
                "option": action,
            },
        )
        self.api.service(
            "input_select",
            "select_option",
            {
                "entity_id": "input_select.voip_qualification_false_route_action",
                "option": false_action,
            },
        )
        self.api.service(
            "input_boolean",
            "turn_on" if condition else "turn_off",
            {"entity_id": "input_boolean.voip_qualification_condition"},
        )
        values = {
            "input_text.voip_qualification_expected_origin": expected_origin,
            "input_text.voip_qualification_expected_caller": expected_caller,
            "input_text.voip_qualification_expected_target": expected_target,
            "input_text.voip_qualification_destination": destination,
            "input_text.voip_qualification_false_destination": false_destination,
            "input_text.voip_qualification_last_decision": "",
        }
        for entity_id, value in values.items():
            self.api.service(
                "input_text",
                "set_value",
                {"entity_id": entity_id, "value": value},
            )

    def decision(self, action: str, timeout: float = 8) -> str:
        state = wait_for(
            lambda: (
                value
                if f"|{action}|"
                in (
                    value := self.api.state(
                        "input_text.voip_qualification_last_decision"
                    )["state"]
                )
                else ""
            ),
            timeout,
            f"qualification automation decision {action}",
        )
        return str(state)

    def assert_no_decision(self) -> str:
        value = self.api.state(
            "input_text.voip_qualification_last_decision"
        )["state"]
        if value:
            raise RuntimeError(f"unexpected qualification decision: {value}")
        return str(value)

    def forward(
        self,
        destination: str,
        *,
        on_failure: str = "resume",
    ) -> None:
        if on_failure not in {"resume", "terminate", "busy"}:
            raise ValueError(f"unsupported forward failure policy: {on_failure}")
        self.api.service(
            "input_text",
            "set_value",
            {
                "entity_id": "input_text.voip_qualification_last_forward",
                "value": "",
            },
        )
        self.api.service(
            "input_text",
            "set_value",
            {
                "entity_id": "input_text.voip_qualification_forward_destination",
                "value": destination,
            },
        )
        self.api.service(
            "input_select",
            "select_option",
            {
                "entity_id": "input_select.voip_qualification_forward_failure",
                "option": on_failure,
            },
        )
        self.api.service(
            "input_boolean",
            "turn_on",
            {"entity_id": "input_boolean.voip_qualification_forward_enabled"},
        )

    def forward_decision(self, destination: str, timeout: float = 8) -> str:
        value = wait_for(
            lambda: (
                state
                if state.endswith(f"|{destination}")
                else ""
            )
            if (
                state := self.api.state(
                    "input_text.voip_qualification_last_forward"
                )["state"]
            )
            else "",
            timeout,
            f"ringing forward decision to {destination}",
        )
        return str(value)

    def disable_forward(self) -> None:
        self.api.service(
            "input_boolean",
            "turn_off",
            {"entity_id": "input_boolean.voip_qualification_forward_enabled"},
        )

    def __exit__(self, *_args: object) -> None:
        for entity_id, value in self.original.items():
            domain = entity_id.partition(".")[0]
            if domain == "input_boolean":
                self.api.service(
                    domain,
                    "turn_on" if value == "on" else "turn_off",
                    {"entity_id": entity_id},
                )
            elif domain == "input_select":
                self.api.service(
                    domain,
                    "select_option",
                    {"entity_id": entity_id, "option": value},
                )
            else:
                self.api.service(
                    domain,
                    "set_value",
                    {"entity_id": entity_id, "value": value},
                )


def _final_response_scenario(status: int) -> str:
    return f"""<?xml version=\"1.0\" encoding=\"UTF-8\" ?>
<!DOCTYPE scenario SYSTEM \"sipp.dtd\">
<scenario name=\"VoIP qualification route {status}\">
  <send retrans=\"500\"><![CDATA[
    INVITE sip:[service]@[remote_ip]:[remote_port] SIP/2.0
    Via: SIP/2.0/[transport] [local_ip]:[local_port];branch=z9hG4bK-[pid]-[call_number]
    From: \"SIPp caller\" <sip:sipp@[local_ip]:[local_port]>;tag=[pid]route
    To: <sip:[service]@[remote_ip]:[remote_port]>
    Call-ID: [call_id]
    CSeq: 1 INVITE
    Contact: <sip:sipp@[local_ip]:[local_port]>
    Max-Forwards: 70
    Content-Type: application/sdp
    Content-Length: [len]

    v=0
    o=sipp 1 1 IN IP4 [local_ip]
    s=VoIP qualification route
    c=IN IP4 [local_ip]
    t=0 0
    m=audio 6000 RTP/AVP 8 0
    a=rtpmap:8 PCMA/8000
    a=rtpmap:0 PCMU/8000
    a=sendrecv

  ]]></send>
  <recv response=\"100\" optional=\"true\" />
  <recv response=\"{status}\" timeout=\"8000\" />
  <send><![CDATA[
    ACK sip:[service]@[remote_ip]:[remote_port] SIP/2.0
    Via: SIP/2.0/[transport] [local_ip]:[local_port];branch=z9hG4bK-[pid]-[call_number]
    From: \"SIPp caller\" <sip:sipp@[local_ip]:[local_port]>;tag=[pid]route
    To: <sip:[service]@[remote_ip]:[remote_port]>[peer_tag_param]
    Call-ID: [call_id]
    CSeq: 1 ACK
    Max-Forwards: 70
    Content-Length: 0

  ]]></send>
</scenario>
"""


def _run_final_response(
    status: int,
    *,
    host: str,
    port: int,
    local_port: int,
    out_dir: Path,
) -> dict[str, object]:
    scenario = out_dir / f"route-{status}-{local_port}.xml"
    scenario.write_text(_final_response_scenario(status), encoding="utf-8")
    command = [
        "sipp",
        f"{host}:{port}",
        "-sf",
        str(scenario.resolve()),
        "-s",
        "9999",
        "-i",
        "127.0.0.1",
        "-p",
        str(local_port),
        "-m",
        "1",
        "-timeout",
        "12s",
        "-trace_msg",
        "-trace_err",
        "-nostdin",
    ]
    completed = subprocess.run(
        command,
        cwd=out_dir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=15,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"SIPp expected {status}, exit {completed.returncode}: "
            f"{completed.stdout[-2000:]}"
        )
    return {"sip_status": status}


def _run_sipp_scenario(
    scenario: Path,
    *,
    host: str,
    port: int,
    extension: str,
    local_port: int,
    out_dir: Path,
) -> dict[str, object]:
    command = [
        "sipp",
        f"{host}:{port}",
        "-sf",
        str(scenario.resolve()),
        "-s",
        extension,
        "-i",
        "127.0.0.1",
        "-p",
        str(local_port),
        "-m",
        "1",
        "-timeout",
        "15s",
        "-trace_msg",
        "-trace_err",
        "-nostdin",
    ]
    completed = subprocess.run(
        command,
        cwd=out_dir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=18,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError(
            f"SIPp {scenario.name} exited {completed.returncode}: "
            f"{completed.stdout[-2000:]}"
        )
    return {"scenario": scenario.stem, "sip_status": "completed"}


def _manual_baresip_config(source: Path, destination: Path) -> Path:
    shutil.copytree(source, destination)
    accounts = destination / "accounts"
    text = accounts.read_text(encoding="utf-8")
    if "answermode=auto" not in text:
        raise RuntimeError("lab bareSIP account does not declare answermode=auto")
    accounts.write_text(
        text.replace("answermode=auto", "answermode=manual"), encoding="utf-8"
    )
    return destination


def _cleanup(base_url: str, token: str) -> dict[str, object]:
    return asyncio.run(runtime_quiescence(base_url, token))


async def _ha_ws_command(
    api: HomeAssistantApi, command: dict[str, object]
) -> dict[str, object]:
    """Execute one authenticated HA WebSocket registry command."""

    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(
            f"{api.base_url.replace('http', 'ws', 1)}/api/websocket"
        ) as websocket:
            await websocket.receive_json(timeout=5)
            await websocket.send_json(
                {"type": "auth", "access_token": api._token}
            )
            authenticated = await websocket.receive_json(timeout=5)
            if authenticated.get("type") != "auth_ok":
                raise RuntimeError("Home Assistant WebSocket authentication failed")
            await websocket.send_json({"id": 1, **command})
            return dict(await websocket.receive_json(timeout=5))


async def _ha_ws_result(
    api: HomeAssistantApi, command: dict[str, object]
) -> object:
    response = await _ha_ws_command(api, command)
    if not response.get("success"):
        raise RuntimeError(f"Home Assistant WebSocket command failed: {response}")
    return response.get("result")


def _phone_policy_target(
    api: HomeAssistantApi,
    *,
    endpoint_id: str,
    policy: str,
) -> tuple[str, str, str]:
    """Resolve a phone Device and policy entity without lab-specific names."""

    call_state = next(
        (
            item
            for item in api.get("/api/states")
            if str(item.get("entity_id") or "").startswith("sensor.")
            and str((item.get("attributes") or {}).get("endpoint_id") or "")
            == endpoint_id
        ),
        None,
    )
    if call_state is None:
        raise RuntimeError(f"phone endpoint is unavailable: {endpoint_id}")
    entities = asyncio.run(
        _ha_ws_result(api, {"type": "config/entity_registry/list"})
    )
    call_entry = next(
        (
            item
            for item in entities
            if item.get("entity_id") == call_state["entity_id"]
        ),
        None,
    )
    device_id = str((call_entry or {}).get("device_id") or "")
    if not device_id:
        raise RuntimeError(f"phone Device is unavailable: {endpoint_id}")
    devices = asyncio.run(
        _ha_ws_result(api, {"type": "config/device_registry/list"})
    )
    device = next(
        (item for item in devices if item.get("id") == device_id), None
    )
    device_name = str(
        (device or {}).get("name_by_user") or (device or {}).get("name") or ""
    ).strip()
    if not device_name:
        raise RuntimeError(f"phone Device name is unavailable: {endpoint_id}")
    suffix = f"_{policy}"
    policy_entry = next(
        (
            item
            for item in entities
            if item.get("device_id") == device_id
            and item.get("platform") == "voip_stack"
            and str(item.get("unique_id") or "").endswith(suffix)
        ),
        None,
    )
    entity_id = str((policy_entry or {}).get("entity_id") or "")
    if not entity_id:
        raise RuntimeError(
            f"phone policy entity is unavailable: {endpoint_id}/{policy}"
        )
    return device_id, entity_id, device_name


def _wait_switch(api: HomeAssistantApi, entity_id: str, enabled: bool) -> str:
    expected = "on" if enabled else "off"
    return str(
        wait_for(
            lambda: (
                state
                if (state := api.state(entity_id)["state"]) == expected
                else ""
            ),
            5,
            f"{entity_id}={expected}",
        )
    )


def _set_phone_policy(
    api: HomeAssistantApi,
    *,
    service: str,
    field: str,
    entity_id: str,
    device_id: str,
    enabled: bool,
    verify: Callable[[], dict[str, object]] | None = None,
) -> dict[str, object]:
    """Exercise one public phone-policy service and restore its real entity."""

    original = api.state(entity_id)["state"] == "on"
    try:
        api.service(
            "voip_stack", service, {"device_id": device_id, field: enabled}
        )
        observed = _wait_switch(api, entity_id, enabled)
        result: dict[str, object] = {
            "service": f"voip_stack.{service}",
            "entity_id": entity_id,
            "device_id": device_id,
            "requested": enabled,
            "observed": observed,
        }
        if verify is not None:
            result["behavior"] = verify()
        return result
    finally:
        api.service(
            "voip_stack", service, {"device_id": device_id, field: original}
        )
        _wait_switch(api, entity_id, original)


def _route_requests(
    trace: EventTrace,
    *,
    minimum: int,
    timeout: float = 8,
) -> list[dict[str, object]]:
    return list(
        wait_for(
            lambda: (
                requests
                if len(
                    requests := [
                        item
                        for item in trace.items
                        if item.get("event_type") == "route_requested"
                        or item.get("route_request")
                        or item.get("state") == "route_requested"
                    ]
                )
                >= minimum
                else []
            ),
            timeout,
            f"{minimum} route_requested events",
        )
    )


def _parallel_sipp(
    invocations: tuple[Callable[[], dict[str, object]], ...],
) -> list[dict[str, object]]:
    with ThreadPoolExecutor(max_workers=len(invocations)) as executor:
        futures = [executor.submit(invocation) for invocation in invocations]
        return [future.result() for future in futures]


@contextmanager
def _registered_local_trunk(
    out_dir: Path,
    port: int,
    *,
    host: str = "127.0.0.1",
):
    def hass_udp_ports() -> set[int]:
        output = subprocess.run(
            ["ss", "-H", "-u", "-a", "-n", "-p"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=True,
        ).stdout
        return {
            int(match.group(1))
            for line in output.splitlines()
            if '(("hass",' in line
            and (match := re.search(r"\s(?:0\.0\.0\.0|127\.0\.0\.1):(\d+)\s", line))
        }

    ephemeral_floor = int(
        Path("/proc/sys/net/ipv4/ip_local_port_range")
        .read_text(encoding="utf-8")
        .split()[0]
    )
    baseline_ports = hass_udp_ports()
    scenario = (out_dir / "local-trunk-register.xml").resolve()
    scenario.write_text(
        """<?xml version="1.0" encoding="UTF-8" ?>
<!DOCTYPE scenario SYSTEM "sipp.dtd">
<scenario name="VoIP qualification local trunk registrar">
  <recv request="REGISTER" />
  <send><![CDATA[
    SIP/2.0 200 OK
    [last_Via:]
    [last_From:]
    [last_To:];tag=qualification
    [last_Call-ID:]
    [last_CSeq:]
    [last_Contact:]
    X-Qualification-Target: [remote_ip]:[remote_port]
    Content-Length: 0

  ]]></send>
</scenario>
""",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [
            "sipp",
            "-sf",
            str(scenario),
            "-i",
            host,
            "-p",
            str(port),
            "-m",
            "1",
            "-timeout",
            "10s",
            "-trace_msg",
            "-trace_err",
            "-nostdin",
        ],
        cwd=out_dir,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    registered_target: tuple[str, int] | None = None

    def contact_target() -> tuple[str, int]:
        nonlocal registered_target
        if registered_target is not None:
            return registered_target
        output, _ = process.communicate(timeout=5)
        if process.returncode:
            raise RuntimeError(f"local SIPp trunk failed: {output[-2000:]}")
        logs = sorted(out_dir.glob("local-trunk-register_*_messages.log"))
        if not logs:
            raise RuntimeError("local SIPp trunk did not write its message trace")
        contacts = re.findall(
            r"Contact:\s*<sip:[^@>]+@([^:;>]+):(\d+)",
            logs[-1].read_text(encoding="utf-8", errors="replace"),
            flags=re.IGNORECASE,
        )
        if not contacts:
            raise RuntimeError("HA trunk REGISTER did not contain a local Contact port")
        new_ports = {
            candidate
            for candidate in hass_udp_ports() - baseline_ports
            if candidate >= ephemeral_floor
        }
        if len(new_ports) != 1:
            raise RuntimeError(
                f"expected one HA trunk UDP socket, found {sorted(new_ports)}"
            )
        registered_target = (contacts[0][0], new_ports.pop())
        return registered_target

    try:
        time.sleep(0.15)
        if process.poll() is not None:
            output, _ = process.communicate()
            raise RuntimeError(f"local SIPp trunk did not start: {output[-2000:]}")
        yield contact_target
        contact_target()
    finally:
        if process.poll() is None:
            process.terminate()
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=2)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-url", default="http://127.0.0.1:18123")
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path("/home/codex/ha-voip-lab/.credentials"),
    )
    parser.add_argument(
        "--callee-config",
        type=Path,
        default=Path("/home/codex/ha-voip-lab/baresip-sink"),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "test_captures/ha-real-matrix",
    )
    parser.add_argument(
        "--installed-package",
        type=Path,
        default=Path(
            "/home/codex/ha-voip-lab/config/packages/voip_qualification.yaml"
        ),
    )
    parser.add_argument("--only", action="append", default=[])
    parser.add_argument(
        "--policy-endpoint-id",
        help="Explicit browser endpoint used by phone-policy cases",
    )
    parser.add_argument("--summary-output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.installed_package.is_file():
        raise RuntimeError(
            f"Home Assistant qualification package is missing: {args.installed_package}"
        )
    package_bytes = PACKAGE.read_bytes()
    if args.installed_package.read_bytes() != package_bytes:
        raise RuntimeError(
            "Home Assistant is not running the checked-in qualification package"
        )
    run_dir = args.out_dir / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=False)
    delayed_cancel = run_dir / "inbound-cancel-after-forward-delay.xml"
    delayed_cancel.write_text(
        (ROOT / "tests/sipp/inbound-cancel.xml")
        .read_text(encoding="utf-8")
        .replace('<pause milliseconds="100" />', '<pause milliseconds="1500" />'),
        encoding="utf-8",
    )
    token = lab_token(args.ha_url, args.credentials)
    api = HomeAssistantApi(base_url=args.ha_url, token=token)

    wait_for(
        lambda: api.request("GET", "/api/config").get("state") == "RUNNING",
        30,
        "Home Assistant runtime readiness",
    )

    def qualification_automation_ready() -> bool:
        try:
            return (
                api.state("automation.voip_qualification_route_decision")["state"]
                == "on"
            )
        except RuntimeError:
            return False

    wait_for(
        qualification_automation_ready,
        15,
        "qualification automation readiness",
    )
    snapshot = FlowSnapshot.capture(api)
    package_hash = hashlib.sha256(package_bytes).hexdigest()
    results: list[dict[str, object]] = []
    selected = set(args.only)
    trunk_source_port = 19999
    matrix_tainted = False

    def execute(case: MatrixCase) -> None:
        nonlocal matrix_tainted
        if selected and case.name not in selected:
            return
        if matrix_tainted:
            results.append(
                {
                    "name": case.name,
                    "status": "skipped",
                    "reason": "previous failure left the shared SIP lab tainted",
                }
            )
            return
        started = time.monotonic()
        before: dict[str, object] = {}
        try:
            before = _cleanup(args.ha_url, token)
            detail = case.execute()
            after = _cleanup(args.ha_url, token)
            results.append(
                {
                    "name": case.name,
                    "status": "passed",
                    "duration_s": round(time.monotonic() - started, 3),
                    **({"before": before} if before else {}),
                    "after": after,
                    **detail,
                }
            )
        except Exception as error:  # noqa: BLE001 - preserve complete matrix evidence.
            recovered = False
            with suppress(Exception):
                api.service("voip_stack", "hangup")
                _cleanup(args.ha_url, token)
                recovered = True
            if not recovered:
                matrix_tainted = True
            results.append(
                {
                    "name": case.name,
                    "status": "failed",
                    "duration_s": round(time.monotonic() - started, 3),
                    **({"before": before} if before else {}),
                    "error": str(error),
                    "recovered_for_next_case": recovered,
                }
            )

    fake_trunk = {
        "trunk_transport": "udp",
        "trunk_server": "127.0.0.1",
        "trunk_port": 19999,
        "trunk_domain": "127.0.0.1",
        "trunk_username": "sipp",
        "trunk_auth_username": "sipp",
        "trunk_password": "qualification-only",
        "trunk_expires": 60,
        "trunk_outbound_proxy": "",
    }
    try:
        with _registered_local_trunk(run_dir, 19999) as trunk_contact_target:
            snapshot.apply(
                api,
                mode="direct",
                automation=True,
                default_target="video_sink",
                trunk_override=fake_trunk,
            )
            target_host, target_port = trunk_contact_target()
        with QualificationPackage(api) as package:
            def answered_route(
                action: str,
                *,
                destination: str = "",
                scenario: Path = ROOT / "tests/sipp/answered-local-bye.xml",
                mode: str = "caller_bye",
                callee_config: Path = args.callee_config,
                expected_decision: str | None = None,
                **selection: object,
            ) -> dict[str, object]:
                package.select(action, destination=destination, **selection)
                result = run_answered_case(
                    mode,
                    scenario,
                    target_host=target_host,
                    target_port=target_port,
                    extension="9999",
                    local_port=trunk_source_port,
                    callee_config=callee_config,
                    capture_dir=run_dir,
                    ha_url=args.ha_url,
                    token=token,
                )
                result["automation_decision"] = (
                    package.decision(expected_decision)
                    if expected_decision
                    else package.assert_no_decision()
                )
                return result

            for action, status in (("decline", 603), ("busy", 486), ("cancel", 487)):

                def rejected(
                    action: str = action, status: int = status
                ) -> dict[str, object]:
                    package.select(action)
                    detail = _run_final_response(
                        status,
                        host=target_host,
                        port=target_port,
                        local_port=trunk_source_port,
                        out_dir=run_dir,
                    )
                    detail["automation_decision"] = package.decision(action)
                    return detail

                execute(MatrixCase(f"route_{action}", rejected))

            for action in ("default", "forward", "bridge"):
                execute(
                    MatrixCase(
                        f"route_{action}",
                        lambda action=action: answered_route(
                            action,
                            destination=("" if action == "default" else "video_sink"),
                            expected_decision=action,
                        ),
                    )
                )
            execute(
                MatrixCase(
                    "route_no_action_uses_fallback",
                    lambda: answered_route("no_action"),
                )
            )
            for name, selection in (
                ("caller_filter_miss_uses_fallback", {"expected_caller": "Different caller"}),
                ("callee_filter_miss_uses_fallback", {"expected_target": "Different target"}),
                ("ingress_filter_miss_uses_fallback", {"expected_origin": "extension"}),
            ):
                execute(
                    MatrixCase(
                        name,
                        lambda selection=selection: answered_route(
                            "decline", **selection
                        ),
                    )
                )

            policy_requested = not selected or bool(selected.intersection(POLICY_CASES))
            if policy_requested and not args.policy_endpoint_id:
                raise RuntimeError(
                    "phone-policy cases require --policy-endpoint-id"
                )
            policy_endpoint_id = str(args.policy_endpoint_id or "")
            auto_answer_target = (
                _phone_policy_target(
                    api,
                    endpoint_id=policy_endpoint_id,
                    policy="auto_answer",
                )
                if policy_requested
                else ("", "", "")
            )
            dnd_target = (
                _phone_policy_target(
                    api, endpoint_id=policy_endpoint_id, policy="dnd"
                )
                if policy_requested
                else ("", "", "")
            )

            def dnd_enabled_behavior() -> dict[str, object]:
                package.select("forward", destination=dnd_target[2])
                result = _run_final_response(
                    486,
                    host=target_host,
                    port=target_port,
                    local_port=trunk_source_port,
                    out_dir=run_dir,
                )
                result["automation_decision"] = package.decision("forward")
                return result

            def dnd_disabled_behavior() -> dict[str, object]:
                package.select("forward", destination=dnd_target[2])
                result = _run_sipp_scenario(
                    delayed_cancel,
                    host=target_host,
                    port=target_port,
                    extension="9999",
                    local_port=trunk_source_port,
                    out_dir=run_dir,
                )
                result["automation_decision"] = package.decision("forward")
                return result

            for name, service, field, target, enabled, verify in (
                (
                    POLICY_CASES[0],
                    "set_auto_answer",
                    "auto_answer",
                    auto_answer_target,
                    True,
                    None,
                ),
                (
                    POLICY_CASES[1],
                    "set_auto_answer",
                    "auto_answer",
                    auto_answer_target,
                    False,
                    None,
                ),
                (
                    POLICY_CASES[2],
                    "set_dnd",
                    "dnd",
                    dnd_target,
                    True,
                    dnd_enabled_behavior,
                ),
                (
                    POLICY_CASES[3],
                    "set_dnd",
                    "dnd",
                    dnd_target,
                    False,
                    dnd_disabled_behavior,
                ),
            ):
                execute(
                    MatrixCase(
                        name,
                        lambda service=service, field=field, target=target,
                        enabled=enabled, verify=verify: _set_phone_policy(
                            api,
                            service=service,
                            field=field,
                            device_id=target[0],
                            entity_id=target[1],
                            enabled=enabled,
                            verify=verify,
                        ),
                    )
                )

            def stale_route_sequence() -> dict[str, object]:
                package.select("no_action")
                with EventTrace(api) as trace:
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(
                            _run_final_response,
                            404,
                            host=target_host,
                            port=target_port,
                            local_port=trunk_source_port,
                            out_dir=run_dir,
                        )
                        request = _route_requests(trace, minimum=1)[0]
                        call_id = str(request.get("call_id") or "")
                        sequence = int(request.get("sequence") or 0)
                        state = str(request.get("state") or "")
                        if not call_id or not request.get("route_request"):
                            raise RuntimeError(
                                f"incomplete route request event: {request}"
                            )
                        rejection = asyncio.run(
                            _ha_ws_command(
                                api,
                                {
                                    "type": "call_service",
                                    "domain": "voip_stack",
                                    "service": "select_inbound_destination",
                                    "service_data": {
                                        "destination": "video_sink",
                                        "call_id": call_id,
                                        "expected_state": state,
                                        "expected_sequence": sequence + 1,
                                    },
                                },
                            )
                        )
                        if rejection.get("success"):
                            raise RuntimeError("stale route sequence was accepted")
                        sip_result = future.result()
                return {
                    **sip_result,
                    "call_id": call_id,
                    "observed_sequence": sequence,
                    "rejected_sequence": sequence + 1,
                    "rejection": rejection.get("error"),
                }

            execute(MatrixCase(CONCURRENCY_CASES[0], stale_route_sequence))

            def concurrent_route_requests() -> dict[str, object]:
                package.select("no_action")
                with EventTrace(api) as trace:
                    sip_results = _parallel_sipp(
                        (
                            lambda: _run_final_response(
                                404,
                                host=target_host,
                                port=target_port,
                                local_port=trunk_source_port,
                                out_dir=run_dir,
                            ),
                            lambda: _run_final_response(
                                404,
                                host=target_host,
                                port=target_port,
                                local_port=trunk_source_port + 1,
                                out_dir=run_dir,
                            ),
                        )
                    )
                    requests = _route_requests(trace, minimum=2)
                call_ids = {
                    str(request.get("call_id") or "") for request in requests
                }
                call_ids.discard("")
                if len(call_ids) != 2:
                    raise RuntimeError(
                        f"concurrent route requests were not distinct: {requests}"
                    )
                return {
                    "call_ids": sorted(call_ids),
                    "route_request_count": len(requests),
                    "sip_results": sip_results,
                }

            execute(MatrixCase(CONCURRENCY_CASES[1], concurrent_route_requests))
            execute(
                MatrixCase(
                    "conditional_presence_home",
                    lambda: answered_route(
                        "forward",
                        destination="video_sink",
                        condition=True,
                        false_action="default",
                        expected_decision="forward",
                    ),
                )
            )
            execute(
                MatrixCase(
                    "conditional_presence_away",
                    lambda: answered_route(
                        "forward",
                        destination="video_sink",
                        condition=False,
                        false_action="default",
                        expected_decision="default",
                    ),
                )
            )

            def ringing_forward_success() -> dict[str, object]:
                package.select("forward", destination="Casa")
                package.forward("video_sink", on_failure="resume")
                try:
                    result = run_answered_case(
                        "caller_bye",
                        ROOT / "tests/sipp/answered-local-bye.xml",
                        target_host=target_host,
                        target_port=target_port,
                        extension="9999",
                        local_port=trunk_source_port,
                        callee_config=args.callee_config,
                        capture_dir=run_dir,
                        ha_url=args.ha_url,
                        token=token,
                    )
                    result["initial_decision"] = package.decision("forward")
                    result["forward_decision"] = package.forward_decision("video_sink")
                    return result
                finally:
                    package.disable_forward()

            execute(MatrixCase("ringing_forward_to_available_phone", ringing_forward_success))

            def ringing_forward_failure_resume() -> dict[str, object]:
                package.select("forward", destination="Casa")
                package.forward("missing qualification target", on_failure="resume")
                try:
                    result = _run_sipp_scenario(
                        delayed_cancel,
                        host=target_host,
                        port=target_port,
                        extension="9999",
                        local_port=trunk_source_port,
                        out_dir=run_dir,
                    )
                    result["initial_decision"] = package.decision("forward")
                    result["forward_decision"] = package.forward_decision(
                        "missing qualification target"
                    )
                    return result
                finally:
                    package.disable_forward()

            execute(MatrixCase("ringing_forward_failure_resumes_source", ringing_forward_failure_resume))

            execute(
                MatrixCase(
                    ANSWER_CASES[0],
                    lambda: answered_route(
                        "forward",
                        destination="video_sink",
                        expected_decision="forward",
                    ),
                )
            )
            execute(
                MatrixCase(
                    ANSWER_CASES[2],
                    lambda: answered_route(
                        "forward",
                        destination="video_sink",
                        scenario=ROOT / "tests/sipp/initial-delayed-offer-local-bye.xml",
                        expected_decision="forward",
                    ),
                )
            )

            with tempfile.TemporaryDirectory(prefix="voip-manual-answer-") as temp:
                manual = _manual_baresip_config(
                    args.callee_config, Path(temp) / "baresip"
                )

                execute(
                    MatrixCase(
                        ANSWER_CASES[1],
                        lambda: answered_route(
                            "forward",
                            destination="video_sink",
                            scenario=ROOT / "tests/sipp/answered-remote-bye.xml",
                            mode="callee_bye",
                            callee_config=manual,
                            expected_decision="forward",
                        ),
                    )
                )
    finally:
        with suppress(Exception):
            snapshot.apply(api)
        with suppress(Exception):
            _cleanup(args.ha_url, token)

    artifact = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "candidate": candidate_revision(),
        "qualification_package_sha256": package_hash,
        "home_assistant": api.get("/api/config").get("version"),
        "external_executable_contracts": EXTERNAL_EXECUTABLE_CONTRACTS,
        "results": results,
    }
    summary = run_dir / "summary.json"
    summary_text = json.dumps(artifact, indent=2)
    summary.write_text(summary_text, encoding="utf-8")
    if args.summary_output is not None:
        args.summary_output.parent.mkdir(parents=True, exist_ok=True)
        args.summary_output.write_text(summary_text + "\n", encoding="utf-8")
    print(
        json.dumps(
            {"artifact": str(summary), "results": results}, indent=2
        )
    )
    return 1 if any(item["status"] != "passed" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
