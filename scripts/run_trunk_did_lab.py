#!/usr/bin/env python3
"""Qualify configured Fusion or HA-peer trunk called-number routes without a menu.

Profiles describe existing routes. This runner does not configure trunks, change
PBX routes, create automations or deploy a component. Missing paths fail closed.
Automation-contact IVR and established DTMF have independent qualifications.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import json
import hashlib
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "scripts"))
from inbound_routing_qualification import BareSip, EventTrace, HomeAssistantApi, schema_defaults, trace_types, wait_event_state, wait_for
from live_voip_qualification import candidate_revision
from run_answered_sipp_lab import runtime_quiescence

SCENARIO_IDS = (
    "trunk_primary_called_number_routes_directly",
    "trunk_secondary_called_number_routes_directly",
    "fusion_trunk_called_number_routes_to_esp",
)
RETIRED_MENU_FIELDS = frozenset({
    "trunk_inbound_mode", "trunk_dtmf_enabled", "trunk_dtmf_timeout_ms", "trunk_dtmf_terminator",
})


def _read_trunk_policy(api, profile):
    """Inspect the native editor and abort before any final form submission."""
    base = "/api/config/config_entries/subentries/flow"
    result = api.post(base, {"handler": [profile["entry_id"], "trunk"],
        "subentry_id": profile["subentry_id"]})
    flow_id = result["flow_id"]
    try:
        if result.get("step_id") != "user":
            raise RuntimeError("The configured trunk editor is unavailable")
        initial = schema_defaults(result)
        if not initial.get("trunk_enabled"):
            raise RuntimeError("The configured trunk is disabled")
        result = api.post(base + "/" + flow_id, initial)
        if result.get("step_id") != "trunk":
            raise RuntimeError("The configured trunk routing form is unavailable")
        fields = {field["name"] for field in result.get("data_schema", [])}
        if fields.intersection(RETIRED_MENU_FIELDS):
            raise RuntimeError("The deployed trunk editor still exposes retired menu fields")
        policy = schema_defaults(result)
        if policy.get("automation_routing_enabled", False):
            raise RuntimeError("Direct-DID cases require routing automation disabled")
        return {"automation_routing_enabled": False, "legacy_menu_fields": [],
            "trunk_inbound_default_target": str(policy.get("trunk_inbound_default_target") or "")}
    finally:
        api.delete(base + "/" + flow_id)


def _validate_profile(profile):
    if profile.get("route_kind") not in {"fusion", "ha_peer"}:
        raise ValueError("Only explicitly configured Fusion and HA-peer routes are supported")
    for field in ("ha_url", "auth_file", "caller_config", "entry_id", "subentry_id", "trunk_id"):
        if not profile.get(field):
            raise ValueError("Missing configured route field: " + field)
    cases = profile.get("cases") or []
    if not cases:
        raise ValueError("No configured called-number cases are available")
    for case in cases:
        if case.get("name") not in SCENARIO_IDS:
            raise ValueError("Unknown direct called-number scenario")
        if not all(str(case.get(key) or "").strip() for key in ("dial_target", "called_number", "callee")):
            raise ValueError("Each case requires an actual dial target, called number and callee")
        wire = case.get("wire") or {}
        for field in ("caller_trace", "callee_trace", "caller_request_uri", "callee_request_uri"):
            if not wire.get(field):
                raise ValueError("Independent SIP wire evidence is unavailable: " + field)
        if Path(wire["caller_trace"]).resolve() == Path(wire["callee_trace"]).resolve():
            raise ValueError("Caller and callee SIP evidence must come from independent peer logs")
        for field in ("caller_trace", "callee_trace"):
            if not Path(wire[field]).is_file():
                raise ValueError("The configured independent SIP trace is unavailable: " + field)
    if len({case["name"] for case in cases}) != len(cases):
        raise ValueError("Direct called-number scenarios must be unique")


def _caller_config(source: Path) -> tuple[Path, str]:
    """Use the existing authenticated profile without copying or editing it."""
    text = (source / "config").read_text(encoding="utf-8")
    match = re.search(r"(?m)^sip_listen\s+(\S+):(\d+)\s*$", text)
    if match is None:
        raise RuntimeError("bareSIP source config has no sip_listen address")
    return source, match.group(1)


def _require_wire(case, call_id, observed):
    """Require current dialog bytes from both independently supplied peer logs."""
    destination_id = next((str(item.get("dest_call_id") or "") for item in reversed(observed)
        if item.get("dest_call_id")), "")
    if not destination_id:
        return None
    wire = case["wire"]
    proof = {}
    for side, identity in (("caller", call_id), ("callee", destination_id)):
        raw = Path(wire[side + "_trace"]).read_bytes()
        text = raw.decode(errors="replace").replace("\r\n", "\n")
        invite = "INVITE " + wire[side + "_request_uri"] + " SIP/2.0\n"
        messages = text.split(invite)[1:]
        if not any(re.search(r"(?im)^Call-ID:\s*" + re.escape(identity) + r"\s*$",
                message.split("\n\n", 1)[0]) for message in messages):
            return None
        proof[side] = {"call_id": identity, "request_uri": wire[side + "_request_uri"],
            "sha256": hashlib.sha256(raw).hexdigest()}
    return proof


def _run_case(api, *, config, case, trunk_id):
    """Dial the actual configured address, never send post-answer digits."""
    started = time.monotonic()
    caller = BareSip(config, wait_registered=False, working_dir=config.parent)
    try:
        with EventTrace(api) as trace:
            caller.dial(case["dial_target"])
            event = wait_event_state(api, "ringing", 8, expected_remote_party=case["callee"])
            call_id = str(event.get("call_id") or "")
            if not call_id:
                raise RuntimeError("The destination did not expose a call identity")
            observed = trace.for_call(call_id)
            if not any(item.get("called_number") == case["called_number"]
                    and item.get("inbound_trunk_id") == trunk_id for item in observed):
                raise RuntimeError("The actual trunk and normalized called number were not observed")
            wire = wait_for(lambda: _require_wire(case, call_id, trace.for_call(call_id)),
                3, "current caller and callee SIP INVITE bytes")
            events = trace_types(trace, call_id)
            if "route_requested" in events:
                raise RuntimeError("A routing override occurred in this disabled-automation case")
    finally:
        caller.close()
    return {"name": case["name"], "status": "passed", "call_id": call_id,
        "dial_target": case["dial_target"], "called_number": case["called_number"],
        "callee": case["callee"], "inbound_trunk_id": trunk_id,
        "duration_s": round(time.monotonic() - started, 3), "events": events, "wire": wire}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path, required=True,
        help="Existing Fusion or HA-peer trunk route profile; never a substitute provider")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--required-scenario", action="append", choices=SCENARIO_IDS, default=[])
    parser.add_argument("--require-route-kind", choices=("fusion", "ha_peer"))
    args = parser.parse_args()
    results, quiescence = [], None
    try:
        profile = json.loads(args.profile.read_text())
        _validate_profile(profile)
        if args.require_route_kind and profile["route_kind"] != args.require_route_kind:
            raise ValueError("The configured route does not match the required PBX path")
        if not set(args.required_scenario).issubset({case["name"] for case in profile["cases"]}):
            raise ValueError("The configured profile does not provide every required called-number case")
        from live_voip_qualification import _refresh_ha_token
        token = _refresh_ha_token(Path(profile["auth_file"]))
        api = HomeAssistantApi(base_url=profile["ha_url"], token=token)
        policy = _read_trunk_policy(api, profile)
        asyncio.run(runtime_quiescence(profile["ha_url"], token))
        config, _host = _caller_config(Path(profile["caller_config"]))
        for case in profile["cases"]:
            try:
                result = _run_case(api, config=config, case=case, trunk_id=profile["trunk_id"])
                result["policy"] = policy
            except Exception as error:
                result = {"name": case["name"], "status": "failed", "error": str(error)}
            try:
                quiescence = asyncio.run(runtime_quiescence(profile["ha_url"], token))
                result["quiescence"] = quiescence
            except Exception as error:
                result.update(status="failed", cleanup_error=str(error))
            results.append(result)
    except Exception as error:
        results.append({"name": "configured_direct_did_path", "status": "blocked", "error": str(error)})
    artifact = {"schema_version": 1, "created_at": datetime.now(UTC).isoformat(),
        "candidate": candidate_revision(), "results": results, "quiescence": quiescence}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(artifact, indent=2) + "\n")
    return 0 if results and all(row["status"] == "passed" for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
