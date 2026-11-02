"""Derive bounded scenario claims from qualification artifacts."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from .registry import EXECUTOR_JOBS, SCENARIOS

MEASURED_HIL_SCENARIO = "esp-to-ha-answer-hangup"
MIN_TRANSPORT_WINDOW_SECONDS = 20


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


def _bound_artifact(reference: object, root: Path) -> bytes:
    """Read the exact producer artifact, confined to the evidence directory."""
    if not isinstance(reference, dict):
        raise EvidenceError("missing measured artifact")
    relative = Path(str(reference.get("path") or ""))
    path = (root / relative).resolve()
    if relative.is_absolute() or ".." in relative.parts or root not in path.parents:
        raise EvidenceError("measured artifact escapes evidence root")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise EvidenceError("measured artifact is unavailable") from error
    if (not raw or reference.get("bytes") != len(raw)
            or reference.get("sha256") != hashlib.sha256(raw).hexdigest()):
        raise EvidenceError("measured artifact hash or size mismatch")
    return raw


def validate_hil_measurement(
    result: dict[str, object], *, plan: dict[str, object],
    candidate_id: str, root: Path, require_redial: bool,
) -> None:
    """Validate transport observations, never acoustic or decoded-audio proof.

    A producer writes a run-bound receipt with call-scoped capture/report pairs.
    Reports retain the existing rtp_pcap_evidence fields and add call_id/run_id;
    tx/rx select the two negotiated streams by their independent wire tuples.
    """
    root = root.resolve()
    if result.get("scenario") != MEASURED_HIL_SCENARIO:
        raise EvidenceError(f"no measured validator for HIL scenario: {result.get('scenario')}")
    try:
        actor_start, actor_end = result["started_monotonic"], result["finished_monotonic"]
        if (any(type(value) not in (int, float) or not math.isfinite(value)
                for value in (actor_start, actor_end)) or actor_end <= actor_start):
            raise EvidenceError("missing observed actor monotonic interval")
        snapshots = result["snapshots"]
        for snapshot in (snapshots["pre"], snapshots["post"]):
            counts = snapshot["resource_counts"]
            if (snapshot.get("state") != "idle" or snapshot.get("active_dialogs") != 0
                    or snapshot.get("call_scoped_quiescent") is not True
                    or not isinstance(counts, dict) or not counts
                    # runtime_diagnostics keeps this integration-lifetime task
                    # count outside its call_scoped_keys. Compare its baseline.
                    or any(type(value) is not int or value < 0
                           or (key != "runtime_tasks" and value != 0)
                           for key, value in counts.items())):
                raise EvidenceError("call resources did not return to the measured baseline")
        if snapshots["pre"]["resource_counts"] != snapshots["post"]["resource_counts"]:
            raise EvidenceError("call resource inventory changed")
        receipt = json.loads(_bound_artifact(result.get("measurement_receipt"), root))
        expected = {
            "schema_version": 1, "plan_id": plan.get("plan_id"),
            "head": plan.get("head"), "candidate_id": candidate_id,
            "scenario": result.get("scenario"), "run_id": result.get("run_id"),
        }
        if (not candidate_id or not expected["run_id"] or not isinstance(receipt, dict)
                or any(receipt.get(key) != value for key, value in expected.items())):
            raise EvidenceError("measured receipt identity mismatch")
        sessions = receipt.get("sessions")
        if not isinstance(sessions, list) or len(sessions) < (2 if require_redial else 1):
            raise EvidenceError("missing measured call or redial session")
        call_ids, captures = set(), set()
        previous_cleanup = None
        for number, session in enumerate(sessions):
            call_id = session["call_id"]
            if not isinstance(call_id, str) or not call_id or call_id in call_ids:
                raise EvidenceError("redial must have a distinct Call-ID")
            call_ids.add(call_id)
            times = [session[key] for key in ("started_at", "connected_at", "ended_at", "quiescent_at")]
            if (any(type(value) not in (int, float) or not math.isfinite(value) for value in times)
                    or not times[0] <= times[1] < times[2] <= times[3]):
                raise EvidenceError("invalid measured session timeline")
            # These are host CLOCK_MONOTONIC observations from the local actor
            # and producer, never device clocks or timestamps from another host.
            if times[0] < actor_start or times[3] > actor_end:
                raise EvidenceError("measured session is outside the actor monotonic interval")
            if previous_cleanup is not None and not 0 <= times[0] - previous_cleanup <= 1.0:
                raise EvidenceError("redial did not start immediately after cleanup")
            previous_cleanup = times[3]
            if session.get("states") != ["in_call", "idle"]:
                raise EvidenceError("missing measured ESP state transitions")
            capture = _bound_artifact(session.get("capture"), root)
            capture_hash = hashlib.sha256(capture).hexdigest()
            if capture_hash in captures:
                raise EvidenceError("capture was reused across call sessions")
            captures.add(capture_hash)
            report = json.loads(_bound_artifact(session.get("rtp_report"), root))
            if (report.get("status") != "passed" or report.get("failures") != []
                    or report.get("pcap_sha256") != capture_hash
                    or report.get("call_id") != call_id
                    or report.get("run_id") != expected["run_id"]):
                raise EvidenceError("RTP report is not bound to this call capture")
            selected = []
            for direction in ("tx", "rx"):
                selection = session[direction]
                streams = [stream for stream in report["streams"] if all(
                    stream.get(key) == selection.get(key)
                    for key in ("source", "destination", "ssrc", "payload_type")
                )]
                if len(streams) != 1:
                    raise EvidenceError("missing directional RTP stream")
                stream = streams[0]
                if (not all(isinstance(selection.get(key), str) and selection[key]
                            for key in ("source", "destination", "ssrc"))
                        or type(stream.get("packets")) is not int or stream["packets"] < 2
                        or type(stream.get("payload_type")) is not int
                        or not 0 <= stream["payload_type"] <= 127
                        or stream.get("media_type") != "audio"
                        or type(stream.get("clock_rate")) is not int or stream["clock_rate"] <= 0
                        or type(selection.get("clock_rate")) is not int
                        or stream["clock_rate"] != selection["clock_rate"]
                        or stream.get("expected_delta_ms") != selection["ptime_ms"]
                        or type(selection["ptime_ms"]) not in (int, float)
                        or not math.isfinite(selection["ptime_ms"]) or selection["ptime_ms"] <= 0
                        or type(stream.get("duration_seconds")) not in (int, float)
                        or not math.isfinite(stream["duration_seconds"])
                        or stream["duration_seconds"] <= 0):
                    raise EvidenceError("missing measured RTP packets or negotiated format")
                duration = stream["duration_seconds"]
                if (duration > times[2] - times[1]
                        or (number == 0 and duration < MIN_TRANSPORT_WINDOW_SECONDS)
                        or type(stream.get("lost_packets")) is not int or stream["lost_packets"] != 0
                        or type(stream.get("timestamp_step")) is not int
                        or stream.get("timestamp_step") != stream["clock_rate"] * selection["ptime_ms"] / 1000
                        or duration * 1000 / (stream["packets"] - 1) / selection["ptime_ms"] > 1.25):
                    raise EvidenceError("RTP observation window, loss or cadence is invalid")
                selected.append(stream)
            if (selected[0]["source"] != selected[1]["destination"]
                    or selected[0]["destination"] != selected[1]["source"]
                    or selected[0]["source"] == selected[0]["destination"]):
                raise EvidenceError("RTP streams do not prove both directions")
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise EvidenceError("invalid measured HIL receipt") from error


def hil_artifact_paths(artifacts: list[Path], root: Path) -> list[Path]:
    """Include every measured input in the existing final job hash manifest."""
    root = root.resolve()
    measured = []
    for artifact in artifacts:
        payload = _load(artifact) if artifact.suffix == ".json" else None
        jobs = payload.get("jobs", {}) if isinstance(payload, dict) else {}
        for job in ("hil-s3", "hil-p4"):
            for result in jobs.get(job, {}).get("results", []):
                reference = result.get("measurement_receipt")
                receipt = json.loads(_bound_artifact(reference, root))
                references = [reference, *[session[key] for session in receipt["sessions"]
                    for key in ("capture", "rtp_report")]]
                for reference in references:
                    _bound_artifact(reference, root)
                    path = root / reference["path"]
                    if path not in measured:
                        measured.append(path)
    return measured


def _hil_passed(
    payload: object, job: str, scenario_id: str, *, plan: dict[str, object],
    candidate_id: str, root: Path,
) -> bool:
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    record = jobs.get(job) if isinstance(jobs, dict) else None
    if (not isinstance(record, dict) or record.get("status") != "passed"
            or payload.get("plan_id") != plan.get("plan_id")
            or payload.get("head") != plan.get("head")
            or payload.get("candidate_id") != candidate_id):
        return False
    for result in record.get("results", []):
        if not isinstance(result, dict) or result.get("scenario") != scenario_id:
            continue
        snapshots = result.get("snapshots")
        post = snapshots.get("post") if isinstance(snapshots, dict) else None
        quiescent = (
            result.get("status") == "passed"
            and isinstance(post, dict)
            and post.get("call_scoped_quiescent") is True
        )
        if quiescent:
            try:
                validate_hil_measurement(result, plan=plan, candidate_id=candidate_id,
                    root=root, require_redial="immediate-redial" in CLAIMS[(scenario_id, job)]["postconditions"])
            except EvidenceError:
                return False
        return quiescent
    return False


def derive_scenario_evidence(
    job: str,
    plan: dict[str, object],
    artifacts: list[Path],
    *, candidate_id: str = "", artifact_root: Path | None = None,
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
        if job.startswith("hil-") and scenario_id != MEASURED_HIL_SCENARIO:
            raise EvidenceError(f"{job} has no measured validator for planned scenario {scenario_id}")
        if job.startswith("hil-") and not any(
            _hil_passed(payload, job, scenario_id, plan=plan,
                candidate_id=candidate_id, root=artifact_root or path.parent)
            for path in artifacts if path.suffix == ".json"
            for payload in [_load(path)]
        ):
            raise EvidenceError(
                f"{job} did not prove planned scenario {scenario_id} and quiescence"
            )
        claims.append({"scenario_id": scenario_id, "status": "passed", **claim})
    return claims
