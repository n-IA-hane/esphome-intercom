"""Independent registered H.264 peers exercise a receive-only video panel route."""

import asyncio
from collections import defaultdict
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay
from tests.test_community_independent_peer_ha import start_capture
from tests.test_sip_phone_transfers_ha import (
    assert_h264_color_bars, assert_peer_audio_tones, registered_phones as registered_phones,
)
from tests.test_sip_capabilities_ha import wait_until
from tests.test_trunk_calls_ha import lab as lab, trunks as trunks

pytestmark = [pytest.mark.ha, pytest.mark.live, pytest.mark.skipif(not os.environ.get("VOIP_TEST_SIPP_COMMAND"),
    reason="Independent video wire capture requires explicit laboratory raw socket access")]


@pytest.fixture
def isolated_peer_output(tmp_path, monkeypatch):
    from tools import ha_softphone_matrix
    monkeypatch.setattr(ha_softphone_matrix, "TEST_CAPTURE_DIR", tmp_path / "peer-runtime")


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"video": ("transfer_a", "transfer_b"),
    "manual": ("transfer_b",), "h264_profile_level_id": "42001f"}], indirect=True)
async def test_real_h264_peers_keep_audio_during_receive_only_panel_direction_changes(
    isolated_peer_output, registered_phones, trunks, tmp_path, monkeypatch,
):
    lab = trunks
    caller, panel, _unused = registered_phones
    evidence = Path(os.environ.get("VOIP_COMMUNITY_EVIDENCE_DIR", str(tmp_path / "evidence"))) / "H264-panel-directions"
    evidence.mkdir(parents=True, exist_ok=True)
    capture = start_capture(evidence)
    result = {"candidate": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "scope": "two independent registered baresip H264 peers; panel receives video only",
        "hardware_tested": False, "source": "https://github.com/n-IA-hane/esphome-intercom/issues/131",
        "capture_pid": capture.pid, "peer_pids": [peer.proc.pid for peer in registered_phones], "passed": False}
    forwarded = defaultdict(list)
    start_video = SipVideoRtpRelay.start

    class ObservedTransport:
        def __init__(self, transport, side):
            self.transport, self.side = transport, side

        def sendto(self, raw, address):
            forwarded[self.side].append(bytes(raw))
            self.transport.sendto(raw, address)

        def __getattr__(self, name):
            return getattr(self.transport, name)

    async def observed_start(relay):
        await start_video(relay)
        for (side, rtcp), transport in tuple(relay._transports.items()):
            if not rtcp:
                relay._transports[(side, rtcp)] = ObservedTransport(transport, side)

    monkeypatch.setattr(SipVideoRtpRelay, "start", observed_start)
    try:
        await asyncio.to_thread(caller.command, "/dial transfer_b")
        await asyncio.to_thread(panel.wait_for, "Incoming call", 3)
        await asyncio.to_thread(panel.command, "/acceptdir audio=sendrecv video=recvonly")
        await asyncio.to_thread(caller.wait_for, "Call established", 3)
        await asyncio.to_thread(panel.wait_for, "Call established", 3)
        session, = lab.calls.sessions.values()
        await wait_until(lambda: lab.calls.resource_for(session.call_id, "relay") is not None)
        relay = lab.calls.resource_for(session.call_id, "relay")
        assert relay.video_relay is not None
        async with asyncio.timeout(6):
            while max((len(items) for items in forwarded.values()), default=0) < 20:
                await asyncio.sleep(.01)
        side = max(forwarded, key=lambda key: len(forwarded[key]))
        before = list(forwarded[side])
        await asyncio.to_thread(assert_h264_color_bars, before, evidence / "before.h264")
        result["initial_decoded_color_bar_frames_min"] = 2
        left, right = relay.left_tx_packets, relay.right_tx_packets
        await asyncio.to_thread(caller.command, "/videodir recvonly")
        await wait_until(lambda: relay.left_tx_packets >= left + 30 and relay.right_tx_packets >= right + 30)
        assert session.live and session.answer_committed
        result["audio_preserved_during_direction_change"] = True
        baseline = len(forwarded[side])
        await asyncio.to_thread(caller.command, "/videodir sendrecv")
        async with asyncio.timeout(6):
            while len(forwarded[side]) < baseline + 25:
                await asyncio.sleep(.01)
        await asyncio.to_thread(assert_h264_color_bars, forwarded[side][baseline:], evidence / "after.h264")
        result["restored_decoded_color_bar_frames_min"] = 2
        await asyncio.to_thread(panel.hangup)
        await wait_until(lambda: not lab.calls.sessions)
        for peer in registered_phones:
            await asyncio.to_thread(peer.close)
        assert_peer_audio_tones(tmp_path, (("transfer_a", 550), ("transfer_b", 450)))
        assert not lab.calls.endpoint_claims_snapshot()
        assert not lab.calls.sip_clients_snapshot()
        assert not lab.calls.client_watchers_snapshot()
        assert not lab.calls.relays_snapshot()
        assert not lab.runtime.rtp_port_pool.get("used")
        result["independently_decoded_audio_tones_hz"] = [550, 450]
        result["forwarded_video_packets"] = {key: len(items) for key, items in forwarded.items()}
        result["resources_zero"] = result["passed"] = True
    finally:
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.terminate_call_wait(session.call_id, reason="test_cleanup")
        for index, peer in enumerate(registered_phones):
            if peer.proc.poll() is None:
                await asyncio.to_thread(peer.close)
            (evidence / f"baresip-{index}.log").write_text(peer.read())
        for wav in tmp_path.glob("transfer_*/*-dec.wav"):
            shutil.copyfile(wav, evidence / f"{wav.parent.name}-{wav.name}")
        if capture.poll() is None:
            subprocess.run(["sudo", "-n", "kill", "-INT", str(capture.pid)], check=True)
            await asyncio.to_thread(capture.wait, 3)
        result["capture_exit"] = capture.returncode
        (evidence / "RESULT.json").write_text(json.dumps(result, indent=2) + "\n")
