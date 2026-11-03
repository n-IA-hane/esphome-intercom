"""Derive bounded scenario claims from qualification artifacts."""

from __future__ import annotations

import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import re
import struct
import wave

from .registry import EXECUTOR_JOBS, SCENARIOS

MEASURED_HIL_SCENARIO = "esp-to-ha-answer-hangup"
ASSIST_HIL_SCENARIO = "full-native-assist-https-tts"
MIN_TRANSPORT_WINDOW_SECONDS = 20


class EvidenceError(RuntimeError):
    """An artifact cannot support the claims assigned to its job."""


CLAIMS: dict[tuple[str, str], dict[str, tuple[str, ...]]] = {
    (ASSIST_HIL_SCENARIO, "hil-s3"): {
        "executors": ("ws3",),
        "oracles": ("native-tts-playback", "https-tts", "runtime-ui-policy", "user-confirmed-output-and-panel"),
        "postconditions": ("native-idle", "no-captured-failure", "preferences-preserved",
                           "cleanup-barrier", "resources-at-baseline"),
    },
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
) -> dict[str, object] | None:
    """Validate observed native or SIP evidence, never acoustic metrology.

    A producer writes a run-bound receipt with call-scoped capture/report pairs.
    Reports retain the existing rtp_pcap_evidence fields and add call_id/run_id;
    tx/rx select the two negotiated streams by their independent wire tuples.
    """
    root = root.resolve()
    if result.get("scenario") not in {MEASURED_HIL_SCENARIO, ASSIST_HIL_SCENARIO}:
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
        if result["scenario"] == ASSIST_HIL_SCENARIO:
            return _validate_assist_measurement(receipt, root, actor_start, actor_end)
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


def _validate_assist_measurement(receipt: dict, root: Path, actor_start: float, actor_end: float) -> dict:
    """Read existing observer logs and a local sink recording, not pass booleans.

    Functional acceptance needs actual, run-bound user testimony as well as
    native observations. Measured PCM remains a separate, stricter proof layer.
    This validator never starts an interaction or capture.
    """
    capture = json.loads(_bound_artifact(receipt.get("native_capture"), root))
    logs = capture["logs"]
    if not isinstance(logs, list) or not logs:
        raise EvidenceError("missing native Assist log capture")
    # Preserve the primary failure even if a producer claims idle/passed, or the
    # decoder's error path changes its media-player state back to IDLE.
    errors = ("failed to allocate", "out of memory", "announcement source error",
              "media source error", "tls read error", "transmit failed - err 101",
              "decode drained after reader error", "name=va_error", "name=va_stop_timeout",
              "name=voice_stop", "name=va_barging")
    for entry in logs:
        message = entry["message"]
        if ("[E][" in message or re.search(r"\bE \(\d+\) ", message)
                or any(error in message.lower() for error in errors)
                or re.search(r"\[alloc\.probe:\d+\].*\bfailure size=", message)
                or re.search(r"\[alloc\.probe:\d+\].*installed result=(?!0\b)", message)
                or re.search(r"\[alloc\.probe:\d+\].*\b(?:events|overflow|reentrant)=[1-9]\d*", message)):
            raise EvidenceError("native Assist capture contains a playback/allocator/transport error")
    times = [entry["epoch"] for entry in logs]
    start, end = capture["started_epoch"], capture["finished_epoch"]
    if (any(type(value) not in (int, float) or not math.isfinite(value)
            for value in [start, end, *times]) or not start <= times[0] <= times[-1] <= end
            or times != sorted(times)):
        raise EvidenceError("invalid native capture timeline")
    observed = [capture["started_monotonic"], capture["finished_monotonic"]]
    if (any(type(value) not in (int, float) or not math.isfinite(value) for value in observed)
            or not actor_start <= observed[0] < observed[1] <= actor_end
            or abs((end - start) - (observed[1] - observed[0])) > 0.1
            or any(capture.get(key) != receipt[key] for key in ("run_id", "head", "candidate_id"))):
        raise EvidenceError("native capture is outside this candidate/actor run")
    if (receipt.get("firmware_profile") != "waveshare-s3-full"
            or any(not receipt.get(key) for key in ("device_mac", "firmware_build"))
            or capture["info"].get("mac") != receipt["device_mac"]
            or capture["info"].get("build") != receipt["firmware_build"]):
        raise EvidenceError("native FULL capture device/build mismatch")

    def one(marker: str) -> int:
        found = [i for i, entry in enumerate(logs) if marker in entry["message"]]
        if len(found) != 1:
            raise EvidenceError("missing or ambiguous native Assist playback transition")
        return found[0]

    layer = receipt.get("proof_layer", "measured-pcm")
    if layer not in {"functional", "measured-pcm"}:
        raise EvidenceError("unknown native Assist proof layer")
    profile = receipt["tts_profile"]
    if (profile != capture.get("tts_profile") or not profile.get("id")
            or not re.fullmatch(r"[0-9a-f]{64}", profile.get("source_profile_sha256", ""))
            or not re.fullmatch(r"audio/[\w.+-]+", profile.get("content_type", ""))):
        raise EvidenceError("missing candidate-bound native TTS provider profile")
    tts = one("VA: tts_start")
    https = one("Streaming https://")
    connected = one("Connected: status=200 content-type='" + profile["content_type"] + "'")
    if layer == "functional":
        def event(name: str) -> int:
            found = [i for i, entry in enumerate(logs)
                     if re.search(r"\bEVENT seq=\d+ name=" + re.escape(name) + r"\b", entry["message"])]
            if len(found) != 1:
                raise EvidenceError("missing or ambiguous dispatched native Assist event")
            return found[0]
        entry = receipt["entry_path"]
        if entry not in {"mww", "manual_voice_toggle"}:
            raise EvidenceError("unrecognized existing native Assist entry path")
        trigger = event("wake_word" if entry == "mww" else entry)
        begin, playing, finished, idle = (event(name) for name in
            ("va_start", "announcement_started", "media_idle", "va_idle"))
        if not trigger < begin < tts or not tts <= event("va_end") <= idle:
            raise EvidenceError("native Assist event join did not finish naturally")
        # An additional manual toggle/cancel after the start is not natural EOF.
        if any("name=manual_voice_toggle" in row["message"] for row in logs[begin + 1:]):
            raise EvidenceError("native playback was interrupted by a manual stop")
    else:
        begin = one("State changed from IDLE to START_MICROPHONE")
        playing = one("State changed to ANNOUNCING")
        finished = one("Announcement finished playing")
        idle = one("State changed from RESPONSE_FINISHED to IDLE")
    if not begin < tts <= https < playing < finished < idle or not tts < connected < playing:
        raise EvidenceError("native HTTPS TTS did not finish in pipeline order")

    def idle_runtime(entries: list[dict]) -> int:
        snapshots = [i for i, entry in enumerate(entries) if "SNAPSHOT reason=" in entry["message"]]
        if not snapshots:
            raise EvidenceError("missing native Runtime/UI policy snapshot")
        block = "\n".join(entry["message"] for entry in entries[snapshots[-1]:])
        policies = dict(re.findall(r"\bpolicy (\w+)=(\w+) source=", block))
        if any(policies.get(key) != value for key, value in
               {"va_state": "idle", "va_response": "inactive", "display_status": "idle"}.items()):
            raise EvidenceError("native Runtime/UI is still active despite HA idle")
        if re.search(r"\bactivity (?:va_\w*|media_\w*|announcement) priority=", block):
            raise EvidenceError("native Assist/media owner remains active")
        return snapshots[-1]

    idle_runtime(logs[:begin])
    post_logs = logs[idle + 1:]
    snapshot = idle_runtime(post_logs)
    if post_logs[snapshot]["epoch"] - times[idle] < 1:
        raise EvidenceError("native post-playback idle was not observed after the quiet window")
    post = "\n".join(entry["message"] for entry in post_logs[snapshot:])
    if (not re.search(r"state=mic .*speaker=NO .*i2s_error=NO", post)
            or not re.search(r"speaker_queued_bytes=0 .*pending_tx=0", post)):
        raise EvidenceError("missing observed local speaker drain after playback")
    # These are existing API state observations, not producer 'restored=True'.
    before, after = ({entry["name"]: entry["value"] for entry in capture[key]}
                     for key in ("states_before", "states_after"))
    if (before.get("Wake Word") is not True or after.get("Wake Word") is not True
            or any(key not in before or key not in after or before[key] != after[key]
                   for key in ("Wake Word", "Do Not Disturb", "Master Volume"))):
        raise EvidenceError("native Wake/DND/volume observations missing or changed")
    feedback = json.loads(_bound_artifact(receipt.get("user_feedback"), root))
    quote = feedback.get("quote")
    review = feedback["review"]
    feedback_epoch = feedback.get("epoch")
    actor_end_epoch = end + actor_end - observed[1]
    if (any(feedback.get(key) != receipt[key] for key in
            ("run_id", "head", "candidate_id", "device_mac", "firmware_build"))
            or feedback.get("author_role") != "user" or not isinstance(feedback.get("message_id"), str)
            or not feedback["message_id"].strip() or feedback.get("outcome") != "confirmed"
            or not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip()
            or type(feedback_epoch) not in (int, float) or not math.isfinite(feedback_epoch)
            or not times[idle] <= feedback_epoch <= actor_end_epoch
            or not isinstance(quote, str) or not quote.strip()
            or any(not isinstance(review.get(key), str) or not review[key].strip()
                   or review[key] not in quote for key in ("complete_response_heard", "panel_returned_idle"))):
        raise EvidenceError("missing actual user confirmation of complete output and panel idle")
    proof = {"functional": "passed", "user_output_and_panel": "confirmed",
             "measured_pcm": "not_measured", "acoustic_metrology": "not_measured",
             "tts_profile": profile,
             "allocator_callback_installation": "observed-zero-result" if any(
                 re.search(r"\[alloc\.probe:\d+\].*installed result=0\b", row["message"]) for row in logs)
                 else "not_observed"}
    if layer == "functional" and "speaker_pcm" not in receipt:
        return proof
    pcm = receipt["speaker_pcm"]
    if (pcm.get("stage") != "local-speaker-output" or pcm.get("run_id") != receipt["run_id"]
            or pcm.get("device_mac") != receipt["device_mac"]
            or pcm.get("native_capture_sha256") != receipt["native_capture"]["sha256"]
            or pcm.get("started_epoch") != times[playing] or pcm.get("finished_epoch") != times[finished]):
        raise EvidenceError("PCM is not bound to the local speaker playback window")
    try:
        with wave.open(BytesIO(_bound_artifact(pcm.get("capture"), root)), "rb") as audio:
            count, rate = audio.getnframes(), audio.getframerate()
            if (audio.getsampwidth() != 2 or audio.getnchannels() not in (1, 2)
                    or rate not in (16000, 24000, 32000, 48000) or audio.getcomptype() != "NONE"):
                raise EvidenceError("unsupported local speaker PCM format")
            data = audio.readframes(count)
            if (not data or len(data) != count * audio.getnchannels() * 2
                    or abs(count / rate - (times[finished] - times[playing])) > 0.05
                    or not any(value[0] for value in struct.iter_unpack("<h", data))):
                raise EvidenceError("local speaker PCM is silent, truncated or incomplete")
    except (wave.Error, EOFError) as error:
        raise EvidenceError("local speaker PCM cannot be decoded") from error
    return {**proof, "measured_pcm": "passed"}


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
                inputs = ([receipt["native_capture"], receipt["user_feedback"],
                           *([receipt["speaker_pcm"]["capture"]] if "speaker_pcm" in receipt else [])]
                    if result.get("scenario") == ASSIST_HIL_SCENARIO else
                    [session[key] for session in receipt["sessions"] for key in ("capture", "rtp_report")])
                references = [reference, *inputs]
                for reference in references:
                    _bound_artifact(reference, root)
                    path = root / reference["path"]
                    if path not in measured:
                        measured.append(path)
    return measured


def _hil_passed(
    payload: object, job: str, scenario_id: str, *, plan: dict[str, object],
    candidate_id: str, root: Path,
) -> dict[str, object] | None:
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    record = jobs.get(job) if isinstance(jobs, dict) else None
    if (not isinstance(record, dict) or record.get("status") != "passed"
            or payload.get("plan_id") != plan.get("plan_id")
            or payload.get("head") != plan.get("head")
            or payload.get("candidate_id") != candidate_id):
        return None
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
        if not quiescent:
            return None
        if quiescent:
            try:
                proof = validate_hil_measurement(result, plan=plan, candidate_id=candidate_id,
                    root=root, require_redial="immediate-redial" in CLAIMS[(scenario_id, job)]["postconditions"])
            except EvidenceError:
                return None
            return {"status": "passed", **({"proof_layers": proof} if proof else {})}
    return None


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
        if job.startswith("hil-") and scenario_id not in {MEASURED_HIL_SCENARIO, ASSIST_HIL_SCENARIO}:
            raise EvidenceError(f"{job} has no measured validator for planned scenario {scenario_id}")
        observed = next((proof for path in artifacts if path.suffix == ".json"
            for payload in [_load(path)]
            if (proof := _hil_passed(payload, job, scenario_id, plan=plan,
                candidate_id=candidate_id, root=artifact_root or path.parent)
            )), None) if job.startswith("hil-") else None
        if job.startswith("hil-") and observed is None:
            raise EvidenceError(
                f"{job} did not prove planned scenario {scenario_id} and quiescence"
            )
        claims.append({"scenario_id": scenario_id, "status": "passed", **claim,
                       **({"proof_layers": observed["proof_layers"]}
                          if observed and "proof_layers" in observed else {})})
    return claims
