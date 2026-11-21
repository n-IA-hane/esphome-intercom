"""Mirror test across repositories: the new engine offers, the firmware answers.

The reduced offer for a phone that declared its formats is written by the new engine and given
to the host build of the firmware negotiator. The firmware must select the formats the real
device uses, and the new engine must read the size of the two descriptions as far smaller than
the offers Home Assistant sends today.
"""

import json
from pathlib import Path
import subprocess
import sys

import pytest

from custom_components.voip_stack.core.media import leg
from tests.media_corpus_support import CORPUS
from tests.test_media_offer_plan_ha import evidence

pytestmark = pytest.mark.ha

FIRMWARE = Path(__file__).resolve().parents[2] / "esphome-voip-stack"
if not (FIRMWARE / "tests" / "test_media_corpus.py").is_file():
    FIRMWARE = Path("/home/codex/esphome-voip-stack")
EXPECTED = {
    "esp-p4": {"tx_rate": 16000, "tx_ms": 20, "tx_pt": 97, "rx_rate": 48000, "rx_ms": 10, "rx_pt": 96},
    "esp-box-opus": {"tx_ms": 20, "tx_pt": 98, "rx_ms": 20, "rx_pt": 98},
}


@pytest.fixture(scope="module")
def firmware_negotiator(tmp_path_factory):
    if not (FIRMWARE / "tests" / "test_media_corpus.py").is_file():
        pytest.skip("firmware repository not available")
    work = tmp_path_factory.mktemp("mirror")
    source = work / "mirror.cpp"
    # The firmware fixture builder imports its sibling test modules while it runs.
    sys.path.insert(0, str(FIRMWARE / "tests"))
    try:
        import test_media_corpus as firmware_test
        from test_sdp_receive_payloads import ROOT, sdp_fixture_source

        source.write_text(sdp_fixture_source() + firmware_test.HARNESS)
    finally:
        sys.path.remove(str(FIRMWARE / "tests"))
    binary = work / "mirror"
    built = subprocess.run(["g++", "-std=c++17", "-I", str(ROOT), str(source), "-o", str(binary)],
                           capture_output=True, text=True)
    assert built.returncode == 0, built.stderr[-3000:]
    return binary


@pytest.mark.parametrize("device", sorted(EXPECTED))
def test_firmware_accepts_the_reduced_offer_with_the_formats_it_uses(firmware_negotiator, device, tmp_path):
    plan = leg.create_offer_for_declared_peer(leg.PHONE_POLICY, evidence(device))
    offer = leg.write_offer(plan, "192.0.2.1", 40000, directional=True, maxptime=32)
    path = tmp_path / "offer.sdp"
    path.write_bytes(offer.encode())
    ran = subprocess.run([str(firmware_negotiator), str(path), device], capture_output=True, text=True, timeout=30)
    selected = json.loads(ran.stdout.strip().splitlines()[-1])
    assert selected["outcome"] == "accept", selected
    assert {key: selected[key] for key in EXPECTED[device]} == EXPECTED[device], selected


@pytest.mark.parametrize("device", sorted(EXPECTED))
def test_the_reduced_offer_is_much_smaller_than_the_one_sent_today(device):
    """Measured on a real call: 1760 bytes for an audio only INVITE, above the 1300 byte limit."""
    plan = leg.create_offer_for_declared_peer(leg.PHONE_POLICY, evidence(device))
    reduced = len(leg.write_offer(plan, "192.0.2.1", 40000, directional=True, maxptime=32).encode())
    captured = min(len((case / "01_local_offer.sdp").read_bytes().split(b"m=video")[0])
                   for case in (CORPUS / "cases").glob(f"{device}-answers-*"))
    assert reduced < 420 and captured - reduced > 350, (reduced, captured)
