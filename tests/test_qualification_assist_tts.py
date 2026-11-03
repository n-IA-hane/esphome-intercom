"""Host tests of G0 evidence rejection, never a physical Assist qualification."""

from io import BytesIO
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import struct
import wave

import pytest

from qualification.evidence import (
    ASSIST_HIL_SCENARIO, EvidenceError, derive_scenario_evidence,
    hil_artifact_paths, validate_hil_measurement,
)
from qualification.final_session import final_session_plan
from scripts.qualification_plan import build_plan


def bind(root, name, raw):
    (root / name).write_bytes(raw)
    return {"path": name, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}


def write_case(root, *, mutate=None, layer="measured-pcm", identity=None):
    # Native messages follow the actual VoiceAssistant, SpeakerSourceMediaPlayer,
    # Runtime dump and AudioDiag owners. Synthetic success only tests the parser.
    messages = [
        (100, "[I][runtime_controller:800]: SNAPSHOT reason=manual seq=1 mask=0x00000000"),
        (100.1, "[I][runtime_controller:814]: policy va_state=idle source=- output=0"),
        (100.2, "[I][runtime_controller:814]: policy va_response=inactive source=- output=0"),
        (100.3, "[I][runtime_controller:814]: policy display_status=idle source=- output=0"),
        (101, "[D][voice_assistant:615]: State changed from IDLE to START_MICROPHONE"),
        (102, "[I][va:046]: VA: tts_start"),
        (102.1, "micro_decoder.audio_reader: Streaming https://example.invalid/native.flac (FLAC)"),
        (102.2, "micro_decoder.http_client: Connected: status=200 content-type='audio/flac'"),
        (103, "[D][speaker_source:294]: State changed to ANNOUNCING"),
        (105, "[D][voice_assistant:481]: Announcement finished playing"),
        (105.1, "[D][voice_assistant:615]: State changed from RESPONSE_FINISHED to IDLE"),
        (106.2, "[I][runtime_controller:800]: SNAPSHOT reason=manual seq=3 mask=0x00000000"),
        (106.3, "[I][runtime_controller:814]: policy va_state=idle source=- output=0"),
        (106.4, "[I][runtime_controller:814]: policy va_response=inactive source=- output=0"),
        (106.5, "[I][runtime_controller:814]: policy display_status=idle source=- output=0"),
        (106.6, "[I][audio_stack.diagnostics:126]: state=mic hardware=running mic_consumers=YES speaker=NO paused=NO i2s_error=NO"),
        (106.7, "[I][audio_stack.diagnostics:126]: dma: frames=384 descriptors=2 speaker_capacity=48000 speaker_queued_bytes=0 idle_eof_drops=0 pending_tx=0"),
    ]
    if layer == "functional":
        runtime = "[I][runtime_controller:689]: EVENT seq="
        for epoch, marker, name, seq in [(101, "State changed from IDLE", "va_start", 3),
                (103, "State changed to ANNOUNCING", "announcement_started", 5),
                (105, "Announcement finished playing", "media_idle", 7),
                (105.1, "State changed from RESPONSE_FINISHED", "va_idle", 8)]:
            messages = [(time, runtime + str(seq) + " name=" + name + " mask=0x00000000"
                         if time == epoch and marker in text else text) for time, text in messages]
        messages += [(100.8, runtime + "2 name=wake_word mask=0x00000000"),
                     (102.5, runtime + "4 name=va_end mask=0x00000000")]
        messages.sort()
    profile = {"id": "host-fixture-provider", "content_type": "audio/flac", "source_profile_sha256": "ab" * 32}
    states = [{"name": name, "value": value} for name, value in
              (("Wake Word", True), ("Do Not Disturb", False), ("Master Volume", 79))]
    capture = {"logs": [{"epoch": epoch, "message": message} for epoch, message in messages],
               "started_epoch": 99, "finished_epoch": 107, "started_monotonic": 1,
               "finished_monotonic": 9, "run_id": "run", "head": "head", "candidate_id": "candidate",
               "info": {"mac": "AA:BB:CC:DD:EE:FF", "build": "FULL-build"}, "tts_profile": profile,
               "states_before": states, "states_after": [dict(state) for state in states]}
    pcm = BytesIO()
    with wave.open(pcm, "wb") as audio:
        audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        audio.writeframes(struct.pack("<32000h", *([2000, -2000] * 16000)))
    receipt = {"schema_version": 1, "plan_id": "plan", "head": "head", "candidate_id": "candidate",
               "scenario": ASSIST_HIL_SCENARIO, "run_id": "run", "firmware_profile": "waveshare-s3-full",
               "device_mac": "AA:BB:CC:DD:EE:FF", "firmware_build": "FULL-build",
               "proof_layer": layer, "entry_path": "mww", "tts_profile": profile,
               "speaker_pcm": {"stage": "local-speaker-output", "run_id": "run",
                               "device_mac": "AA:BB:CC:DD:EE:FF", "started_epoch": 103, "finished_epoch": 105}}
    receipt["user_feedback"] = {key: receipt[key] for key in
        ("run_id", "head", "candidate_id", "device_mac", "firmware_build")}
    receipt["user_feedback"].update(author_role="user", message_id="synthetic-host-test-not-a-real-user-result",
        outcome="confirmed", epoch=107.2,
        quote="Ho sentito tutta la risposta. Il pannello e tornato idle.",
        review={"reviewer": "host-fixture-trusted-actor", "complete_response_heard": "Ho sentito tutta la risposta.",
                "panel_returned_idle": "Il pannello e tornato idle."})
    for record in (capture, receipt, receipt["user_feedback"]):
        record.update(identity or {})
    if layer == "functional":
        del receipt["speaker_pcm"]
    raw_pcm = pcm.getvalue()
    if mutate:
        raw_pcm = mutate(capture, receipt, raw_pcm) or raw_pcm
    receipt["native_capture"] = bind(root, "native.json", json.dumps(capture).encode())
    receipt["user_feedback"] = bind(root, "user-feedback.json", json.dumps(receipt["user_feedback"]).encode())
    if "speaker_pcm" in receipt:
        receipt["speaker_pcm"]["native_capture_sha256"] = receipt["native_capture"]["sha256"]
        receipt["speaker_pcm"]["capture"] = bind(root, "speaker.wav", raw_pcm)
    result = {"scenario": ASSIST_HIL_SCENARIO, "run_id": "run", "status": "passed",
              "started_monotonic": 0, "finished_monotonic": 10,
              "snapshots": {stage: {"state": "idle", "active_dialogs": 0, "call_scoped_quiescent": True,
                            "resource_counts": {"calls": 0, "runtime_tasks": 1}} for stage in ("pre", "post")},
              "measurement_receipt": bind(root, "measurement.json", json.dumps(receipt).encode())}
    artifact = root / "hil-s3.json"
    artifact.write_text(json.dumps({key: receipt[key] for key in ("plan_id", "head", "candidate_id")} | {
                                   "jobs": {"hil-s3": {"status": "passed", "results": [result]}}}))
    return result, artifact


def validate(result, root):
    return validate_hil_measurement(result, plan={"plan_id": "plan", "head": "head"},
                             candidate_id="candidate", root=root, require_redial=False)


def test_full_plan_cannot_omit_native_assist_baseline():
    plan = build_plan([], base="base", head="head", full=True, event="schedule")
    scenario = next(s for s in plan["scenarios"] if s["id"] == ASSIST_HIL_SCENARIO)
    assert scenario["regressions"] == ("baseline-F003-F088",)
    grouped = final_session_plan("head", "candidate")
    assert ASSIST_HIL_SCENARIO in grouped["scenario_contracts"]
    assert grouped["native_assist_baseline"]["missing_local_PCM"] == "measured-media-not-measured"
    assert grouped["native_assist_baseline"]["volume"] == "preserve-existing-user-value-no-mute-for-native-TTS"
    assert grouped["physical"] == grouped["acoustic"] == "not_run"


@pytest.mark.parametrize("layer", ["functional", "measured-pcm"])
@pytest.mark.parametrize("fault", ["all", "esp-aes", "Announcement source error", "spi:183", "esp-tls-mbedtls"])
def test_real_allocation_failure_beats_claimed_pass_and_ha_idle(tmp_path, fault, layer):
    failure = json.loads((Path(__file__).parent / "fixtures/qualification/assist_tts_allocation_failure.json").read_bytes())

    def actual_failure(capture, receipt, pcm):
        capture.update(failure)
        if fault != "all":
            capture["logs"] = [entry for entry in failure["logs"] if fault in entry["message"]]
            assert capture["logs"]
        capture.update(status="passed", ha_state="idle", allocator_errors=0, playback_complete=True)

    result, _ = write_case(tmp_path, mutate=actual_failure, layer=layer)
    with pytest.raises(EvidenceError, match="playback/allocator/transport error"):
        validate(result, tmp_path)


@pytest.mark.parametrize("fault", ["unfinished", "ui_replying", "runtime_responding", "owner_live",
    "missing_pcm", "silent_pcm", "truncated_pcm", "short_pcm", "http_pcm", "wake_off", "volume_changed",
    "stale_run", "stale_actor", "lite", "wrong_device", "no_runtime", "speaker_not_drained", "early_idle"])
def test_missing_or_contradictory_native_observation_never_qualifies(tmp_path, fault):
    def mutate(capture, receipt, pcm):
        if fault in {"unfinished", "no_runtime", "speaker_not_drained"}:
            marker = {"unfinished": "Announcement finished", "no_runtime": "SNAPSHOT", "speaker_not_drained": "speaker_queued_bytes"}[fault]
            capture["logs"] = [entry for entry in capture["logs"] if marker not in entry["message"]]
        elif fault in {"ui_replying", "runtime_responding"}:
            old, new = ("display_status=idle", "display_status=responding") if fault == "ui_replying" else ("va_state=idle", "va_state=responding")
            entry = next(entry for entry in reversed(capture["logs"]) if old in entry["message"])
            entry["message"] = entry["message"].replace(old, new)
            capture["ha_state"] = "idle"
        elif fault == "owner_live":
            capture["logs"][-1]["message"] += "\nactivity va_responding priority=3 group=voice"
        elif fault == "wake_off":
            capture["states_after"][0]["value"] = False
        elif fault == "volume_changed":
            capture["states_after"][-1]["value"] = 0
        elif fault == "stale_run":
            capture["run_id"] = "older-run"
        elif fault == "stale_actor":
            capture["finished_monotonic"] = 11
        elif fault == "early_idle":
            for entry in capture["logs"]:
                if entry["epoch"] >= 106:
                    entry["epoch"] -= 1
        elif fault == "lite":
            receipt["firmware_profile"] = "generic-s3-voip"
        elif fault == "wrong_device":
            capture["info"]["mac"] = "other"
        elif fault == "http_pcm":
            receipt["speaker_pcm"]["stage"] = "http-decoder-output"
        elif fault == "missing_pcm":
            return b"not a recording"
        elif fault == "silent_pcm":
            return pcm[:44] + bytes(len(pcm) - 44)
        elif fault == "truncated_pcm":
            return pcm[:-2]
        elif fault == "short_pcm":
            receipt["speaker_pcm"]["finished_epoch"] = 104

    result, _ = write_case(tmp_path, mutate=mutate)
    with pytest.raises(EvidenceError):
        validate(result, tmp_path)


def test_bound_observations_attest_only_owned_native_claims_and_hash_every_input(tmp_path):
    result, artifact = write_case(tmp_path)
    validate(result, tmp_path)
    claims = derive_scenario_evidence("hil-s3", {"head": "head", "plan_id": "plan",
        "scenarios": [{"id": ASSIST_HIL_SCENARIO}]}, [artifact], candidate_id="candidate")
    assert claims[0]["executors"] == ("ws3",)
    assert "local-speaker-pcm" not in claims[0]["oracles"]
    assert claims[0]["proof_layers"]["measured_pcm"] == "passed"
    assert not {"physical-panel", "acoustic", "rtp-duplex"}.intersection(claims[0]["oracles"])
    assert {p.name for p in hil_artifact_paths([artifact], tmp_path)} == {"measurement.json", "native.json", "speaker.wav", "user-feedback.json"}
    (tmp_path / "speaker.wav").write_bytes(b"different recording")
    with pytest.raises(EvidenceError, match="hash or size mismatch"):
        validate(result, tmp_path)


def test_info_only_native_events_and_actual_quoted_review_do_not_claim_pcm(tmp_path):
    result, artifact = write_case(tmp_path, layer="functional")
    proof = validate(result, tmp_path)
    assert proof["functional"] == "passed" and proof["user_output_and_panel"] == "confirmed"
    assert proof["measured_pcm"] == proof["acoustic_metrology"] == "not_measured"
    assert proof["allocator_callback_installation"] == "not_observed"
    assert not (tmp_path / "speaker.wav").exists()
    assert {p.name for p in hil_artifact_paths([artifact], tmp_path)} == {"measurement.json", "native.json", "user-feedback.json"}
    claims = derive_scenario_evidence("hil-s3", {"head": "head", "plan_id": "plan",
        "scenarios": [{"id": ASSIST_HIL_SCENARIO}]}, [artifact], candidate_id="candidate")
    assert claims[0]["proof_layers"] == proof
    assert "local-speaker-pcm" not in claims[0]["oracles"]


@pytest.mark.parametrize("fault", ["pending", "boolean_confirmation", "missing_quote", "foreign_quote", "stale_feedback",
    "wrong_build", "wrong_user", "ui_replying", "missing_media_idle", "queued_only", "manual_stop", "va_error",
    "probe_failure", "probe_overflow", "bad_install", "wrong_profile", "wrong_mime", "late_va_end",
    "default_reviewer", "missing_message_id", "user_replying", "first_word_only", "supplied_bad_pcm"])
def test_functional_needs_native_join_and_bound_actual_user_review(tmp_path, fault):
    def mutate(capture, receipt, pcm):
        feedback = receipt["user_feedback"]
        if fault == "pending":
            feedback["outcome"] = "pending"
        elif fault == "boolean_confirmation":
            feedback["review"]["complete_response_heard"] = True
        elif fault == "missing_quote":
            del feedback["quote"]
        elif fault == "foreign_quote":
            feedback["review"]["panel_returned_idle"] = "A statement absent from the real message."
        elif fault == "stale_feedback":
            feedback["epoch"] = 98
        elif fault == "wrong_build":
            feedback["firmware_build"] = "old-image"
        elif fault == "wrong_user":
            feedback["author_role"] = "assistant"
        elif fault == "default_reviewer":
            feedback["review"]["reviewer"] = True
        elif fault == "missing_message_id":
            feedback["message_id"] = ""
        elif fault in {"user_replying", "first_word_only"}:
            feedback.update(outcome="failed", quote="Il pannello e ancora replying." if fault == "user_replying"
                            else "Ho sentito soltanto la prima parola.")
        elif fault == "supplied_bad_pcm":
            receipt["speaker_pcm"] = {"stage": "http-decoder-output"}
        elif fault == "ui_replying":
            entry = next(e for e in reversed(capture["logs"]) if "display_status=idle" in e["message"])
            entry["message"] = entry["message"].replace("display_status=idle", "display_status=responding")
        elif fault in {"missing_media_idle", "queued_only"}:
            for entry in capture["logs"]:
                if "name=media_idle" in entry["message"]:
                    entry["message"] = "unobserved" if fault == "missing_media_idle" else entry["message"].replace("EVENT seq=", "EVENT_QUEUE seq=")
        elif fault in {"manual_stop", "va_error", "probe_failure", "probe_overflow", "bad_install"}:
            extra = {"manual_stop": "EVENT seq=9 name=manual_voice_toggle", "va_error": "EVENT seq=9 name=va_error",
                     "probe_failure": "[I][alloc.probe:116]: failure size=4092 count=1",
                     "probe_overflow": "[I][alloc.probe:116]: snapshot events=0 unique=0 overflow=1 reentrant=0",
                     "bad_install": "[I][alloc.probe:106]: installed result=-1 fixed_internal_bytes=480"}[fault]
            capture["logs"][-1]["message"] += "\n" + extra
        elif fault == "wrong_profile":
            capture["tts_profile"] = {**capture["tts_profile"], "source_profile_sha256": "cd" * 32}
        elif fault == "wrong_mime":
            # Non-FLAC providers are profile-specific, not globally declared as FLAC.
            receipt["tts_profile"] = capture["tts_profile"] = {**capture["tts_profile"], "content_type": "audio/mpeg"}
        elif fault == "late_va_end":
            for entry in capture["logs"]:
                if "name=va_end" in entry["message"]:
                    entry["message"] = "EVENT seq=9 name=other_event"
            capture["logs"][-1]["message"] += "\nEVENT seq=9 name=va_end"
    result, _ = write_case(tmp_path, layer="functional", mutate=mutate)
    with pytest.raises(EvidenceError):
        validate(result, tmp_path)


@pytest.mark.parametrize("include_feedback", [True, False])
def test_final_verifier_uses_derived_layers_and_requires_feedback_hash(tmp_path, include_feedback):
    from qualification.registry import SCENARIOS
    from scripts.candidate_lock import candidate_id
    from scripts.qualification_plan import plan_id
    from scripts.record_qualification_result import build_result
    from scripts.verify_qualification import EXPECTED_REPOSITORIES, verify

    candidate = {"schema_version": 1, "toolchain": {}, "repositories": {
        repo: {"commit": "head", "dirty": False} for repo in EXPECTED_REPOSITORIES}}
    candidate["candidate_id"] = candidate_id(candidate)
    plan = build_plan(["docs/TESTING_AND_DEBUG.md"], base="base", head="head", full=False, event="manual")
    scenario = next(s for s in SCENARIOS if s.id == ASSIST_HIL_SCENARIO)
    plan.update(required_jobs=["hil-s3"], scenarios=json.loads(json.dumps([asdict(scenario)], default=sorted)), regressions=[])
    plan["plan_id"] = plan_id(plan)
    _, artifact = write_case(tmp_path, layer="functional", identity={
        "candidate_id": candidate["candidate_id"], "plan_id": plan["plan_id"]})
    claims = derive_scenario_evidence("hil-s3", plan, [artifact], candidate_id=candidate["candidate_id"])
    # An external pass flag cannot upgrade a functional receipt into measured PCM.
    claims[0]["proof_layers"]["measured_pcm"] = "passed"
    claims = json.loads(json.dumps(claims))  # Same boundary as the maintained CLI.
    inputs = hil_artifact_paths([artifact], tmp_path)
    if not include_feedback:
        inputs = [p for p in inputs if p.name != "user-feedback.json"]
    results = build_result("hil-s3", "success", [artifact, *inputs], tmp_path,
        plan_id=plan["plan_id"], candidate_id=candidate["candidate_id"], head="head", scenario_evidence=claims)
    errors, manifest = verify(plan, candidate, results, artifact_root=tmp_path)
    if include_feedback:
        assert errors == []
        assert manifest["scenario_evidence"][0]["proof_layers"]["measured_pcm"] == "not_measured"
    else:
        assert "measured HIL inputs are missing from job artifact hashes: hil-s3" in errors


def test_actual_provider_mime_is_profile_bound_instead_of_globally_pinned_to_flac(tmp_path):
    def provider(capture, receipt, pcm):
        profile = {**capture["tts_profile"], "content_type": "audio/mpeg"}
        receipt["tts_profile"] = capture["tts_profile"] = profile
        for entry in capture["logs"]:
            entry["message"] = entry["message"].replace("audio/flac", "audio/mpeg").replace("native.flac", "native.mp3")
    result, _ = write_case(tmp_path, layer="functional", mutate=provider)
    assert validate(result, tmp_path)["tts_profile"]["content_type"] == "audio/mpeg"


def test_trusted_review_can_quote_an_actual_short_confirmation_in_context(tmp_path):
    def short(capture, receipt, pcm):
        feedback = receipt["user_feedback"]
        feedback.update(quote="Si.", context="La risposta era completa e il pannello e tornato idle?")
        feedback["review"].update(complete_response_heard="Si.", panel_returned_idle="Si.")
    result, _ = write_case(tmp_path, layer="functional", mutate=short)
    assert validate(result, tmp_path)["user_output_and_panel"] == "confirmed"
