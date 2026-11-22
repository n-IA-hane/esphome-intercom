"""The production HA offer and ESP answer agree on standard asymmetric audio."""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from custom_components.voip_stack.core.sdp_composer import _write_offer_plan
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_invite_exchange import InviteExchange
from tests.media_corpus_support import CORPUS
from tests.test_media_offer_plan_ha import declared_offer

pytestmark = pytest.mark.ha

FIRMWARE = Path(os.environ.get(
    "VOIP_STACK_REPO", str(Path(__file__).resolve().parents[2] / "esphome-voip-stack")))
EXPECTED = {
    "esp-p4": {"tx_rate": 16000, "tx_ms": 20, "tx_pt": 97, "rx_rate": 48000, "rx_ms": 10, "rx_pt": 96},
    "esp-box-opus": {"tx_ms": 20, "tx_pt": 98, "rx_ms": 20, "rx_pt": 98},
}


def written_offer(device):
    return _write_offer_plan(
        declared_offer(device), "192.0.2.1", "192.0.2.1", 40000,
        audio_direction="sendrecv", audio_rtcp=False, video_port=0, video_formats=(),
        video_direction="inactive", codec_policy=None)


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
    offer = written_offer(device)
    path = tmp_path / "offer.sdp"
    path.write_bytes(offer.encode())
    ran = subprocess.run([str(firmware_negotiator), str(path), device], capture_output=True, text=True, timeout=30)
    selected = json.loads(ran.stdout.strip().splitlines()[-1])
    assert selected["outcome"] == "accept", selected
    assert {key: selected[key] for key in EXPECTED[device]} == EXPECTED[device], selected


@pytest.mark.parametrize("device", sorted(EXPECTED))
def test_bounded_standard_offer_fits_the_sip_transaction_budget(device):
    """Compare the actual transaction with the preserved large-offer witness."""
    body = written_offer(device)
    reduced = len(body.encode())
    captured = min(len((case / "01_local_offer.sdp").read_bytes().split(b"m=video")[0])
                   for case in (CORPUS / "cases").glob(f"{device}-answers-*"))
    client = SipCallClient(local_ip="192.0.2.1", local_name="Casa", local_uri_user="Casa",
                           local_sip_port=5060, local_rtp_port=40000)
    exchange = InviteExchange.for_target(device, "192.0.2.2", 5060, "", "UDP")
    client._begin_initial_invite(exchange, device)
    client._pending_invite_body = body.encode()
    # Check the actual transaction, including headers, against the SIP boundary.
    assert len(client._build_pending_invite()) <= 1300
    assert captured - reduced > 350, (reduced, captured)
