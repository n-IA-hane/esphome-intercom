#!/usr/bin/env python3
"""Run repeatable local checks for VoIP Stack / ESPHome intercom work."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv/bin/python"
SIP_PROFILE_TESTS = [
    "tests/test_sip_uri.py",
    "tests/test_sip_profile.py",
    "tests/test_sip_client_socket.py",
    "tests/test_sdp_pcm_profile.py",
    "tests/test_rtp_profile.py",
    "tests/test_roster_resolver.py",
    "tests/test_router_contract.py",
    "tests/test_sip_protocol.py",
    "tests/test_sip_registrar.py",
    "tests/test_sip_bridge.py",
    "tests/test_sip_tcp_profile.py",
]
COMPILE_PROFILES = [
    "yamls/voip-only/single-bus/generic-s3-voip.yaml",
    "yamls/full-experience/single-bus/waveshare-s3-full-afe.yaml",
    "yamls/full-experience/single-bus/spotpear-ball-v2-full-afe.yaml",
    "yamls/voip-only/single-bus/waveshare-p4-touch-videophone-jpeg.yaml",
    "yamls/voip-only/single-bus/waveshare-p4-touch-videophone-h264.yaml",
    "yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml",
]
TEST_SECRETS = "wifi_ssid: qualification\nwifi_password: qualification\n"


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


@contextmanager
def temporary_test_secrets(enabled: bool):
    """Provide non-secret Wi-Fi values only for isolated compile checks."""
    created: list[Path] = []
    if enabled:
        for directory in {Path(profile).parent for profile in COMPILE_PROFILES}:
            path = ROOT / directory / "secrets.yaml"
            if not path.exists():
                path.write_text(TEST_SECRETS, encoding="utf-8")
                created.append(path)
    try:
        yield
    finally:
        for path in created:
            path.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-profiles", action="store_true", help="Compile maintained SIP ESPHome profiles.")
    parser.add_argument(
        "--test-secrets",
        action="store_true",
        help="Provide disposable non-secret Wi-Fi values while compiling.",
    )
    args = parser.parse_args()

    py = str(PYTHON if PYTHON.exists() else Path(sys.executable))
    run([py, "-m", "py_compile",
         "custom_components/voip_stack/__init__.py",
         "custom_components/voip_stack/sip_client.py",
         "custom_components/voip_stack/sip_listener.py",
         "custom_components/voip_stack/core/video_rtp.py",
         "custom_components/voip_stack/video_ws_view.py",
         "custom_components/voip_stack/websocket_api.py",
         "tools/sip_video_browser_probe.py",
         "tests/support/qualification_matrix.py"])
    run([py, "-m", "ruff", "check", "custom_components", "scripts", "tests", "tools"])
    run([py, "-m", "pytest", "-q", *SIP_PROFILE_TESTS])
    run([py, "tests/test_device_resolver_sip.py"])
    run([py, "tests/test_frontend_card_contract.py"])
    run([py, "-m", "pytest", "-q", "tests/test_ha_softphone_backend_contract.py"])
    run([py, "tests/test_qualification_matrix.py"])
    run([py, "tests/test_runtime_controller_target_model.py"])
    run([py, "tests/support/qualification_matrix.py", "--validate", "--summary"])
    for module in sorted(
        (ROOT / "custom_components/voip_stack/frontend").glob("*.js")
    ):
        run(["node", "--check", str(module.relative_to(ROOT))])
    run(["./scripts/yaml_paths.sh", "check"])
    run(["git", "diff", "--check"])

    if args.compile_profiles:
        esphome = str(ROOT / ".venv/bin/esphome")
        with temporary_test_secrets(args.test_secrets):
            for profile in COMPILE_PROFILES:
                run([esphome, "compile", profile])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
