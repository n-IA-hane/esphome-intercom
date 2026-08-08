#!/usr/bin/env python3
"""Exercise the isolated HA lab with real SIP peers and native automations."""

from __future__ import annotations

import argparse
import asyncio
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


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "scripts"))

from inbound_routing_qualification import (  # noqa: E402
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
ROUTE_ACTIONS = ("default", "decline", "busy", "cancel", "forward", "bridge")
ANSWER_CASES = (
    "registered_sip_auto_answer_on_caller_bye",
    "registered_sip_auto_answer_off_callee_bye",
)


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
            "input_select.voip_qualification_route_action",
            "input_text.voip_qualification_expected_origin",
            "input_text.voip_qualification_expected_caller",
            "input_text.voip_qualification_expected_target",
            "input_text.voip_qualification_destination",
        )
        self.original = {entity: self.api.state(entity)["state"] for entity in entities}
        automation = self.api.state("automation.voip_qualification_route_decision")
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

    def select(self, action: str, *, destination: str = "") -> None:
        if action not in ROUTE_ACTIONS:
            raise ValueError(f"unsupported qualification route action: {action}")
        self.api.service(
            "input_select",
            "select_option",
            {
                "entity_id": "input_select.voip_qualification_route_action",
                "option": action,
            },
        )
        values = {
            "input_text.voip_qualification_expected_origin": "trunk",
            "input_text.voip_qualification_expected_caller": "SIPp caller",
            "input_text.voip_qualification_expected_target": "",
            "input_text.voip_qualification_destination": destination,
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
        str(scenario),
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


@contextmanager
def _registered_local_trunk(out_dir: Path, port: int):
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

    baseline_ports = hass_udp_ports()
    scenario = out_dir / "local-trunk-register.xml"
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
            "127.0.0.1",
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
        new_ports = hass_udp_ports() - baseline_ports
        new_ports.discard(int(contacts[0][1]))
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
    token = lab_token(args.ha_url, args.credentials)
    api = HomeAssistantApi(base_url=args.ha_url, token=token)

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

    def execute(case: MatrixCase) -> None:
        if selected and case.name not in selected:
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
                with suppress(Exception):
                    api.service(
                        "homeassistant",
                        "reload_config_entry",
                        {"entry_id": snapshot.entry_id},
                    )
                    _cleanup(args.ha_url, token)
                    recovered = True
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

                def answered(action: str = action) -> dict[str, object]:
                    package.select(
                        action,
                        destination="" if action == "default" else "video_sink",
                    )
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
                    result["automation_decision"] = package.decision(action)
                    return result

                execute(MatrixCase(f"route_{action}", answered))

            def auto_answer_on() -> dict[str, object]:
                package.select("forward", destination="video_sink")
                return run_answered_case(
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

            execute(MatrixCase(ANSWER_CASES[0], auto_answer_on))

            with tempfile.TemporaryDirectory(prefix="voip-manual-answer-") as temp:
                manual = _manual_baresip_config(
                    args.callee_config, Path(temp) / "baresip"
                )

                def auto_answer_off() -> dict[str, object]:
                    package.select("forward", destination="video_sink")
                    return run_answered_case(
                        "callee_bye",
                        ROOT / "tests/sipp/answered-remote-bye.xml",
                        target_host=target_host,
                        target_port=target_port,
                        extension="9999",
                        local_port=trunk_source_port,
                        callee_config=manual,
                        capture_dir=run_dir,
                        ha_url=args.ha_url,
                        token=token,
                    )

                execute(MatrixCase(ANSWER_CASES[1], auto_answer_off))
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
