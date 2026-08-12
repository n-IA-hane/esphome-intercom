"""Derive bounded scenario claims from qualification artifacts."""

from __future__ import annotations

import json
from pathlib import Path

from .registry import EXECUTOR_JOBS, SCENARIOS


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
    ("esp-to-ha-answer-hangup", "peer-live"): {
        "executors": ("ha-lab", "sipp"),
        "oracles": ("sip-trace",),
        "postconditions": (),
    },
    ("esp-to-ha-answer-hangup", "browser-real"): {
        "executors": ("playwright",),
        "oracles": ("browser-state",),
        "postconditions": (),
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
        "executors": ("home-ha", "wildix", "sipp"),
        "oracles": (
            "both-peer-dialogs",
            "sip-trace",
            "selected-destination",
        ),
        "postconditions": (
            "digits-consumed-once",
            "cleanup-barrier",
            "resources-at-baseline",
        ),
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
    ("fritzbox-register-contract-replay", "software-full"): {
        "executors": ("software-replay",),
        "oracles": (
            "register-request-uri",
            "register-cseq",
            "register-via-branch",
            "register-refresh",
        ),
        "postconditions": ("timer-f-bounded",),
    },
    ("registered-sip-to-esp-bidirectional-hangup", "peer-live"): {
        "executors": ("ha-lab", "baresip"),
        "oracles": ("both-peer-dialogs", "sip-trace"),
        "postconditions": (),
    },
    ("registered-sip-to-esp-bidirectional-hangup", "hil-s3"): {
        "executors": ("ws3",),
        "oracles": ("esp-state", "rtp-duplex"),
        "postconditions": (
            "single-terminal",
            "cleanup-barrier",
            "resources-at-baseline",
            "immediate-redial",
        ),
    },
    ("p4-audio-to-bidirectional-video-reinvite", "peer-live"): {
        "executors": ("ha-lab",),
        "oracles": ("sip-trace",),
        "postconditions": (),
    },
    ("p4-audio-to-bidirectional-video-reinvite", "browser-real"): {
        "executors": ("playwright",),
        "oracles": (),
        "postconditions": (),
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
    ("p4-full-landscape-jpeg-call-lifecycle", "hil-p4"): {
        "executors": ("p4",),
        "oracles": ("sip-trace", "rtp-duplex", "decoded-video", "esp-runtime"),
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
        "wildix_trunk_dtmf_route_to_esp",
    ),
    ("trunk-dtmf-routing-and-established-dtmf", "browser-real"): (
        "in_call_registered_sip_info_dtmf_event",
        "in_call_rfc4733_dtmf_event",
    ),
    ("esp-to-ha-answer-hangup", "peer-live"): (
        "route_default",
    ),
    ("esp-to-ha-answer-hangup", "browser-real"): (
        "manual_answer_from_card",
    ),
    ("registered-sip-to-esp-bidirectional-hangup", "peer-live"): (
        "registered_sip_peer_auto_answer_on_caller_bye",
        "registered_sip_peer_auto_answer_off_callee_bye",
    ),
    ("p4-audio-to-bidirectional-video-reinvite", "peer-live"): (
        "initial_delayed_offer_caller_bye",
    ),
    ("p4-audio-to-bidirectional-video-reinvite", "browser-real"): (
        "manual_answer_from_card",
    ),
}


def validate_claim_contracts() -> None:
    """Fail when no combination of jobs can prove a declared contract."""

    errors: list[str] = []
    for scenario in SCENARIOS:
        claims = [
            (job, claim)
            for (scenario_id, job), claim in CLAIMS.items()
            if scenario_id == scenario.id
        ]
        for field in ("executors", "oracles", "postconditions"):
            available = {
                value
                for _job, claim in claims
                for value in claim.get(field, ())
            }
            missing = sorted(getattr(scenario, field) - available)
            if missing:
                errors.append(f"{scenario.id} missing {field}: {', '.join(missing)}")
        for job, claim in claims:
            for executor in claim.get("executors", ()):
                owner = EXECUTOR_JOBS.get(executor)
                if owner != job:
                    errors.append(
                        f"{scenario.id} executor {executor} belongs to {owner}, not {job}"
                    )
    if errors:
        raise EvidenceError("; ".join(errors))


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


def _command_passed(payload: object, job: str) -> bool:
    return bool(
        isinstance(payload, dict)
        and payload.get("schema_version") == 1
        and payload.get("job") == job
        and payload.get("status") == "success"
        and payload.get("returncode") == 0
    )


def _contains_passed_scenarios(
    payloads: list[object], required: tuple[str, ...]
) -> bool:
    passed: set[str] = set()
    for payload in payloads:
        results = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(results, list):
            continue
        passed.update(
            str(result.get("name") or result.get("scenario") or "")
            for result in results
            if isinstance(result, dict)
            and result.get("status") in {"pass", "passed"}
        )
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
        supported = any(_command_passed(payload, job) for payload in payloads)
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
        if required is not None and not _contains_passed_scenarios(payloads, required):
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
