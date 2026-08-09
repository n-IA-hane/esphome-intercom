"""Single source of truth for qualification planning and evidence."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import json
from pathlib import Path


class Risk(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class FirmwareProfile:
    id: str
    path: str
    factory_path: str
    areas: frozenset[str]


@dataclass(frozen=True, slots=True)
class QualificationArea:
    id: str
    risk: Risk
    paths: tuple[str, ...]
    jobs: frozenset[str]


@dataclass(frozen=True, slots=True)
class ScenarioContract:
    id: str
    areas: frozenset[str]
    executors: frozenset[str]
    oracles: frozenset[str]
    postconditions: frozenset[str]
    regressions: tuple[str, ...] = ()


REGRESSION_LEDGER_PATH = Path(__file__).with_name("regressions.json")

# An executor is accepted only from the job that actually owns that environment.
EXECUTOR_JOBS = {
    "baresip": "peer-live",
    "ha-lab": "peer-live",
    "home-ha": "peer-live",
    "playwright": "browser-real",
    "p4": "hil-p4",
    "sipp": "peer-live",
    "software-replay": "software-full",
    "wildix": "peer-live",
    "ws3": "hil-s3",
}


FIRMWARE_PROFILES = (
    FirmwareProfile(
        "generic-s3-voip",
        "yamls/voip-only/single-bus/generic-s3-voip.yaml",
        ".esphome/build/generic-s3/build/firmware.factory.bin",
        frozenset({"esp_control", "sip_core", "audio_contract"}),
    ),
    FirmwareProfile(
        "waveshare-s3-full",
        "yamls/full-experience/single-bus/waveshare-s3-full-afe.yaml",
        ".esphome/build/waveshare-s3/build/firmware.factory.bin",
        frozenset({"esp_control", "sip_core", "audio_contract"}),
    ),
    FirmwareProfile(
        "spotpear-ball-v2-full-afe",
        "yamls/full-experience/single-bus/spotpear-ball-v2-full-afe.yaml",
        ".esphome/build/spotpear-ball-v2/build/firmware.factory.bin",
        frozenset({"esp_control", "sip_core", "audio_contract"}),
    ),
    FirmwareProfile(
        "waveshare-p4-jpeg",
        "yamls/voip-only/single-bus/waveshare-p4-touch-videophone-jpeg.yaml",
        ".esphome/.esphome/build/waveshare-p4-touch-videophone-jpeg/build/firmware.factory.bin",
        frozenset({"esp_control", "sip_core", "audio_contract", "video"}),
    ),
    FirmwareProfile(
        "waveshare-p4-h264",
        "yamls/voip-only/single-bus/waveshare-p4-touch-videophone-h264.yaml",
        ".esphome/.esphome/build/waveshare-p4-touch-videophone-h264/build/firmware.factory.bin",
        frozenset({"esp_control", "sip_core", "audio_contract", "video"}),
    ),
    FirmwareProfile(
        "waveshare-p4-full-landscape",
        "yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml",
        ".esphome/.esphome/build/waveshare-p4-touch-full-afe-landscape/build/firmware.factory.bin",
        frozenset({"esp_control", "sip_core", "audio_contract", "video"}),
    ),
)

HIL_FIRMWARE_PROFILES = {
    "hil-s3": "waveshare-s3-full",
    "hil-p4": "waveshare-p4-h264",
}


AREAS = (
    QualificationArea(
        "documentation",
        Risk.LOW,
        ("README.md", "docs/**", "*.md"),
        frozenset({"static"}),
    ),
    QualificationArea(
        "qualification",
        Risk.HIGH,
        (
            "qualification/**",
            "scripts/qualification_*.py",
            "scripts/candidate_lock.py",
            "tests/test_qualification_pipeline.py",
            ".github/workflows/qualification.yml",
        ),
        frozenset({"static", "qualification-selftest"}),
    ),
    QualificationArea(
        "ha_surface",
        Risk.HIGH,
        (
            "custom_components/voip_stack/services.yaml",
            "custom_components/voip_stack/manifest.json",
            "custom_components/voip_stack/strings.json",
            "custom_components/voip_stack/translations/**",
        ),
        frozenset({"static", "software-full", "ha-runtime"}),
    ),
    QualificationArea(
        "ha_lifecycle",
        Risk.CRITICAL,
        (
            "custom_components/voip_stack/*runtime*.py",
            "custom_components/voip_stack/*termination*.py",
            "custom_components/voip_stack/call_registry.py",
            "custom_components/voip_stack/session_cleanup.py",
            "custom_components/voip_stack/inbound_routing/**",
            "custom_components/voip_stack/*orchestrator*.py",
            "custom_components/voip_stack/conference*.py",
            "custom_components/voip_stack/call_forwarder.py",
        ),
        frozenset({"static", "software-full", "ha-runtime", "peer-live", "hil-s3"}),
    ),
    QualificationArea(
        "sip_core",
        Risk.CRITICAL,
        (
            "custom_components/voip_stack/core/sip*.py",
            "custom_components/voip_stack/sip_*.py",
        ),
        frozenset(
            {"static", "software-full", "host-core", "peer-live", "firmware", "hil-s3"}
        ),
    ),
    QualificationArea(
        "browser_media",
        Risk.HIGH,
        (
            "custom_components/voip_stack/frontend/**",
            "custom_components/voip_stack/*ws*.py",
        ),
        frozenset({"static", "software-full", "ha-runtime", "browser-real"}),
    ),
    QualificationArea(
        "video",
        Risk.CRITICAL,
        (
            "custom_components/voip_stack/*video*.py",
            "esphome/components/**/video*",
            "packages/**/video*",
            "yamls/**/*p4*",
        ),
        frozenset({"static", "software-full", "browser-real", "firmware", "hil-p4"}),
    ),
    QualificationArea(
        "esp_control",
        Risk.CRITICAL,
        ("esphome/components/**", "packages/**", "yamls/**"),
        frozenset({"static", "software-full", "firmware", "hil-s3"}),
    ),
    QualificationArea(
        "audio_contract",
        Risk.CRITICAL,
        ("packages/audio/**", "yamls/**/*audio*", "yamls/**/*afe*"),
        frozenset({"static", "software-full", "firmware", "hil-s3"}),
    ),
)


SCENARIOS = (
    ScenarioContract(
        "esp-to-ha-answer-hangup",
        frozenset({"ha_lifecycle", "sip_core", "browser_media", "esp_control"}),
        frozenset({"ha-lab", "sipp", "playwright", "ws3"}),
        frozenset(
            {"sip-trace", "ha-state", "browser-state", "esp-state", "rtp-duplex"}
        ),
        frozenset(
            {
                "single-terminal",
                "cleanup-barrier",
                "resources-at-baseline",
                "immediate-redial",
            }
        ),
        ("issue-93",),
    ),
    ScenarioContract(
        "registered-sip-to-esp-bidirectional-hangup",
        frozenset({"ha_lifecycle", "sip_core", "esp_control"}),
        frozenset({"ha-lab", "baresip", "ws3"}),
        frozenset({"both-peer-dialogs", "sip-trace", "esp-state", "rtp-duplex"}),
        frozenset(
            {
                "single-terminal",
                "cleanup-barrier",
                "resources-at-baseline",
                "immediate-redial",
            }
        ),
        ("registered-sip-auto-hangup-regression",),
    ),
    ScenarioContract(
        "trunk-dtmf-routing-and-established-dtmf",
        frozenset({"ha_lifecycle", "sip_core"}),
        frozenset({"home-ha", "wildix", "sipp"}),
        frozenset(
            {"both-peer-dialogs", "sip-trace", "dtmf-events", "selected-destination"}
        ),
        frozenset(
            {
                "digits-consumed-once",
                "no-in-call-reroute",
                "cleanup-barrier",
                "resources-at-baseline",
            }
        ),
        ("issue-95", "trunk-dtmf-reroute-regression"),
    ),
    ScenarioContract(
        "dahua-interop-contract-replay",
        frozenset({"ha_lifecycle", "sip_core"}),
        frozenset({"software-replay"}),
        frozenset(
            {"digest-challenge", "tcp-flow-state", "negotiated-pcm", "teardown-state"}
        ),
        frozenset({"tcp-flow-reused", "terminal-idempotent"}),
        ("issue-85",),
    ),
    ScenarioContract(
        "esp-to-esp-watchdog-and-bidirectional-hangup",
        frozenset({"ha_lifecycle", "sip_core", "esp_control"}),
        frozenset({"ws3"}),
        frozenset({"both-peer-dialogs", "esp-state", "rtp-duplex"}),
        frozenset(
            {
                "media-survives-watchdog",
                "both-hangup-owners",
                "cleanup-barrier",
                "resources-at-baseline",
            }
        ),
        ("issue-88",),
    ),
    ScenarioContract(
        "fritzbox-pcma-to-assist-frame-reassembly",
        frozenset({"ha_lifecycle", "sip_core", "audio_contract"}),
        frozenset({"software-replay"}),
        frozenset({"rtp-packetization", "assist-pcm-frames"}),
        frozenset({"arbitrary-chunks-reframed"}),
        ("issue-94",),
    ),
    ScenarioContract(
        "p4-audio-to-bidirectional-video-reinvite",
        frozenset(
            {"ha_lifecycle", "sip_core", "browser_media", "esp_control", "video"}
        ),
        frozenset({"ha-lab", "playwright", "p4"}),
        frozenset(
            {
                "sip-trace",
                "rtp-duplex",
                "decoded-video",
                "rendered-video",
                "esp-runtime",
            }
        ),
        frozenset(
            {
                "single-terminal",
                "cleanup-barrier",
                "resources-at-baseline",
                "immediate-redial",
            }
        ),
    ),
)


ALL_JOBS = frozenset(job for area in AREAS for job in area.jobs)


def regression_ledger() -> tuple[dict[str, object], ...]:
    """Load and validate the community regression ledger."""

    payload = json.loads(REGRESSION_LEDGER_PATH.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1 or not isinstance(
        payload.get("regressions"), list
    ):
        raise ValueError("community regression ledger schema is unsupported")
    records = tuple(payload["regressions"])
    scenario_ids = {scenario.id for scenario in SCENARIOS}
    keys: set[str] = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("community regression ledger record is invalid")
        key = str(record.get("id") or "")
        scenarios = record.get("scenarios")
        if not key or key in keys:
            raise ValueError(f"community regression id is invalid: {key}")
        if not isinstance(scenarios, list) or not scenarios:
            raise ValueError(f"community regression has no scenarios: {key}")
        unknown = set(map(str, scenarios)).difference(scenario_ids)
        if unknown:
            raise ValueError(
                f"community regression references unknown scenarios: {key}"
            )
        keys.add(key)
    contract_keys = {
        key
        for scenario in SCENARIOS
        for key in scenario.regressions
        if key.startswith("issue-")
    }
    if keys != contract_keys:
        raise ValueError("community regression ledger and scenario contracts differ")
    return records
