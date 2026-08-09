"""Derive bounded scenario claims from qualification artifacts."""

from __future__ import annotations

import json
from pathlib import Path


class EvidenceError(RuntimeError):
    """An artifact cannot support the claims assigned to its job."""


CLAIMS: dict[tuple[str, str], dict[str, tuple[str, ...]]] = {
    ("ha-phone-policy-and-dnd-routing", "peer-live"): {
        "executors": ("ha-lab", "sipp"),
        "oracles": ("ha-state", "sip-final-status", "selected-destination"),
        "postconditions": ("policy-restored", "resources-at-baseline"),
    },
    ("inbound-route-decision-guards", "peer-live"): {
        "executors": ("ha-lab", "sipp"),
        "oracles": ("route-events", "service-validation", "distinct-call-ids"),
        "postconditions": ("cleanup-barrier", "resources-at-baseline"),
    },
    ("esp-to-ha-answer-hangup", "ha-runtime"): {
        "executors": (),
        "oracles": ("ha-state",),
        "postconditions": ("single-terminal",),
    },
    ("esp-to-ha-answer-hangup", "hil-s3"): {
        "executors": ("ws3",),
        "oracles": ("esp-state", "rtp-duplex"),
        "postconditions": (
            "cleanup-barrier",
            "resources-at-baseline",
            "immediate-redial",
        ),
    },
    ("trunk-dtmf-routing-and-established-dtmf", "peer-live"): {
        "executors": ("sipp",),
        "oracles": ("sip-trace", "selected-destination"),
        "postconditions": ("cleanup-barrier", "resources-at-baseline"),
    },
    ("trunk-dtmf-routing-and-established-dtmf", "browser-real"): {
        "executors": (),
        "oracles": ("dtmf-events",),
        "postconditions": ("no-in-call-reroute",),
    },
    ("dahua-interop-contract-replay", "software-full"): {
        "executors": ("software-replay",),
        "oracles": (
            "digest-challenge",
            "tcp-flow-state",
            "negotiated-pcm",
            "teardown-state",
        ),
        "postconditions": ("tcp-flow-reused", "terminal-idempotent"),
    },
    ("esp-to-esp-watchdog-and-bidirectional-hangup", "hil-s3"): {
        "executors": ("ws3",),
        "oracles": ("both-peer-dialogs", "esp-state", "rtp-duplex"),
        "postconditions": (
            "media-survives-watchdog",
            "both-hangup-owners",
            "cleanup-barrier",
            "resources-at-baseline",
        ),
    },
    ("fritzbox-pcma-to-assist-frame-reassembly", "software-full"): {
        "executors": ("software-replay",),
        "oracles": ("rtp-packetization", "assist-pcm-frames"),
        "postconditions": ("arbitrary-chunks-reframed",),
    },
    ("p4-audio-to-bidirectional-video-reinvite", "hil-p4"): {
        "executors": ("p4",),
        "oracles": (
            "sip-trace",
            "rtp-duplex",
            "decoded-video",
            "rendered-video",
            "esp-runtime",
        ),
        "postconditions": (
            "single-terminal",
            "cleanup-barrier",
            "resources-at-baseline",
            "immediate-redial",
        ),
    },
}


ARTIFACT_SCENARIOS: dict[tuple[str, str], tuple[str, ...]] = {
    ("ha-phone-policy-and-dnd-routing", "peer-live"): (
        "browser_phone_auto_answer_enabled_ha_runtime",
        "browser_phone_auto_answer_disabled_ha_runtime",
        "browser_phone_dnd_enabled",
        "browser_phone_dnd_disabled",
    ),
    ("inbound-route-decision-guards", "peer-live"): (
        "stale_route_sequence_is_rejected",
        "concurrent_route_requests_remain_distinct",
    ),
    ("trunk-dtmf-routing-and-established-dtmf", "peer-live"): (
        "dtmf_primary_extension_bypasses_automation",
        "dtmf_secondary_extension_bypasses_automation",
    ),
    ("trunk-dtmf-routing-and-established-dtmf", "browser-real"): (
        "in_call_registered_sip_info_dtmf_event",
        "in_call_rfc4733_dtmf_event",
    ),
}


def _load(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceError(f"scenario artifact is not valid JSON: {path}") from error


def _all_passed(payload: object) -> bool:
    results = payload.get("results") if isinstance(payload, dict) else payload
    return bool(results) and isinstance(results, list) and all(
        isinstance(result, dict) and result.get("status") in {"pass", "passed"}
        for result in results
    )


def _contains_passed_scenarios(payload: object, required: tuple[str, ...]) -> bool:
    results = payload.get("results") if isinstance(payload, dict) else payload
    if not isinstance(results, list):
        return False
    passed = {
        str(result.get("name") or result.get("scenario") or "")
        for result in results
        if isinstance(result, dict) and result.get("status") in {"pass", "passed"}
    }
    return set(required).issubset(passed)


def _hil_passed(payload: object, job: str, scenario_id: str) -> bool:
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    record = jobs.get(job) if isinstance(jobs, dict) else None
    if not isinstance(record, dict) or record.get("status") != "passed":
        return False
    for result in record.get("results", []):
        if not isinstance(result, dict) or result.get("scenario") != scenario_id:
            continue
        snapshots = result.get("snapshots")
        post = snapshots.get("post") if isinstance(snapshots, dict) else None
        return (
            result.get("status") == "passed"
            and isinstance(post, dict)
            and post.get("call_scoped_quiescent") is True
        )
    return False


def derive_scenario_evidence(
    job: str,
    plan: dict[str, object],
    artifacts: list[Path],
) -> list[dict[str, object]]:
    """Return only claims supported by a successful, job-specific artifact."""

    planned_ids = {
        str(scenario.get("id") or "")
        for scenario in plan.get("scenarios", [])
        if isinstance(scenario, dict)
    }
    relevant = {key for key in CLAIMS if key[1] == job and key[0] in planned_ids}
    if not relevant:
        return []
    payloads = [_load(path) for path in artifacts if path.suffix == ".json"]
    if job in {"software-full", "ha-runtime"}:
        supported = any(path.suffix == ".log" and path.stat().st_size for path in artifacts)
    elif job in {"peer-live", "browser-real"}:
        supported = any(_all_passed(payload) for payload in payloads)
    elif job in {"hil-s3", "hil-p4"}:
        supported = True
    else:
        return []
    if not supported:
        raise EvidenceError(f"{job} produced no supported scenario artifact")

    claims: list[dict[str, object]] = []
    for scenario in plan.get("scenarios", []):
        if not isinstance(scenario, dict):
            continue
        scenario_id = str(scenario.get("id") or "")
        claim = CLAIMS.get((scenario_id, job))
        if claim is None:
            continue
        required = ARTIFACT_SCENARIOS.get((scenario_id, job))
        if required is not None and not any(
            _contains_passed_scenarios(payload, required) for payload in payloads
        ):
            raise EvidenceError(
                f"{job} did not prove exact scenarios for {scenario_id}: "
                f"{', '.join(required)}"
            )
        if job.startswith("hil-") and not any(
            _hil_passed(payload, job, scenario_id) for payload in payloads
        ):
            raise EvidenceError(
                f"{job} did not prove planned scenario {scenario_id} and quiescence"
            )
        claims.append({"scenario_id": scenario_id, "status": "passed", **claim})
    return claims
