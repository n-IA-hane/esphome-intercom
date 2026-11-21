"""Issue 165's registered incoming PCMU witness through a real SIPp process."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import shlex
import socket
import struct
import subprocess

import pytest

from custom_components.voip_stack import audio_ws_view
from custom_components.voip_stack.audio_ws import decode_audio_frame
from custom_components.voip_stack.endpoint_routing import roster_from_peers
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.roster import dump_roster_json
from custom_components.voip_stack.sip_registrar import SipAccount
from tests.test_community_independent_peer_ha import (
    BrowserSink, captured_peer_tone, feed_browser_tone, media_pcap, registration_scenario,
)
from tests.test_sip_capabilities_ha import wait_until
from tests.test_trunk_calls_ha import lab as lab, trunks as trunks

pytestmark = [pytest.mark.ha, pytest.mark.live, pytest.mark.skipif(not os.environ.get("VOIP_TEST_SIPP_COMMAND"),
    reason="Independent PCAP playback requires an explicitly configured SIPp with raw socket access")]


def incoming_scenario(path, media):
    headers = """Via: SIP/2.0/[transport] [local_ip]:[local_port];branch=[branch];rport
From: <sip:door@[remote_ip]>;tag=community-incoming
To: <sip:600@[remote_ip]>
Call-ID: [call_id]
Contact: <sip:door@[local_ip]:[local_port];transport=udp>
Max-Forwards: 70
User-Agent: Independent-issue165-witness
"""
    sdp = """v=0
o=- 1 1 IN IP4 [local_ip]
s=Issue 165 independent station witness
c=IN IP4 [local_ip]
t=0 0
m=audio [media_port] RTP/AVP 0
a=rtpmap:0 PCMU/8000
a=ptime:20
a=sendrecv
"""
    invite = "INVITE sip:600@[remote_ip]:[remote_port] SIP/2.0\n" + headers
    first = invite + "CSeq: 1 INVITE\nContent-Type: application/sdp\nContent-Length: [len]\n\n" + sdp
    retry = invite + ('CSeq: 2 INVITE\n[authentication username=door password=isolated-test-only]\n'
                      'Content-Type: application/sdp\nContent-Length: [len]\n\n') + sdp
    ack = """ACK sip:600@[remote_ip]:[remote_port] SIP/2.0
Via: SIP/2.0/[transport] [local_ip]:[local_port];branch=[branch];rport
From: <sip:door@[remote_ip]>;tag=community-incoming
[last_To:]
Call-ID: [call_id]
CSeq: 1 ACK
Content-Length: 0

"""
    connected_ack = ack.replace("CSeq: 1 ACK", "CSeq: 2 ACK")
    path.write_text('<?xml version="1.0"?><scenario name="Issue 165 registered incoming">'
        '<send><![CDATA[' + first + ']]></send><recv response="401" auth="true"/>'
        '<send><![CDATA[' + ack + ']]></send><send><![CDATA[' + retry + ']]></send>'
        '<recv response="100" optional="true"/><recv response="180" optional="true"/>'
        '<recv response="200" timeout="5000" rrs="true"/>'
        '<send><![CDATA[' + connected_ack + ']]></send><pause milliseconds="350"/>'
        '<nop><action><exec play_pcap_audio="' + str(media) + '"/></action></nop>'
        '<recv request="BYE" timeout="6000"/><send><![CDATA[\n'
        'SIP/2.0 200 OK\n[last_Via:]\n[last_From:]\n[last_To:]\n[last_Call-ID:]\n[last_CSeq:]\n'
        'Content-Length: 0\n\n]]></send><pause milliseconds="100"/></scenario>')


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_hikvision_reported_registered_incoming_40ms_after_20ms_sdp(trunks, tmp_path):
    lab = trunks
    from custom_components.voip_stack.trunk_runtime import async_stop_sip_trunk
    await async_stop_sip_trunk(lab.hass)
    lab.runtime.trunk_configs = {}
    lab.runtime.trunk_config = {}
    lab.runtime.default_trunk_id = ""
    evidence = Path(os.environ.get("VOIP_COMMUNITY_EVIDENCE_DIR", str(tmp_path / "evidence"))) / "Hikvision-incoming"
    evidence.mkdir(parents=True, exist_ok=True)
    browser_endpoint = PhoneEndpoint(endpoint_id="browser:community", name="Community browser",
        device_id="community-browser", extension="600", kind=EndpointKind.BROWSER,
        availability="available")
    lab.runtime.endpoints.register(browser_endpoint)
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="door", name="Community door",
        username="door", kind=EndpointKind.SIP_ACCOUNT))
    registrar = lab.calls.component("registrar")
    registrar.enabled = True
    registrar.update_accounts([SipAccount("door", "Community door", "isolated-test-only")])
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        peer_port = probe.getsockname()[1]
    register = evidence / "register.xml"
    # Issue 165 does not supply the station's User-Agent. Preserve that gap.
    registration_scenario(register, "Independent-issue165-witness")
    command = shlex.split(os.environ["VOIP_TEST_SIPP_COMMAND"])
    remote = f"127.0.0.1:{lab.runtime.transport_config['sip_port']}"
    result = {"candidate": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "candidate_diff_sha256": hashlib.sha256(subprocess.check_output(["git", "diff"])).hexdigest(),
        "source": "https://github.com/n-IA-hane/esphome-intercom/issues/165",
        "scope": "registered UDP station to native HA browser audio owner; no physical station or Android browser",
        "hardware_tested": False, "passed": False}
    capture = subprocess.Popen(["sudo", "-n", "tcpdump", "-i", "lo", "-U", "--immediate-mode",
        "-w", str(evidence / "wire.pcap"), "udp or tcp"], stdout=subprocess.DEVNULL,
        stderr=(evidence / "tcpdump.log").open("w"))
    result["capture_pid"] = capture.pid
    peer = None
    media_task = None
    feed_task = None
    browser = BrowserSink()
    try:
        registered = await asyncio.to_thread(subprocess.run, [*command, remote, "-sf", str(register),
            "-i", "127.0.0.1", "-p", str(peer_port), "-m", "1", "-timeout", "4s", "-nostdin",
            "-oocsf", str(evidence / "options.xml"), "-trace_msg", "-message_file", str(evidence / "registration.log")],
            cwd=evidence, capture_output=True, text=True)
        (evidence / "registration-console.log").write_text(registered.stdout + registered.stderr)
        assert registered.returncode == 0
        binding, = registrar.registered_contacts("door")
        lab.roster[:] = roster_from_peers(lab.hass, [], registrar.registered_roster_entries())
        lab.runtime.directory.replace(lab.roster)
        lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
        expected = await asyncio.to_thread(media_pcap, evidence / "source.pcap", "PCMU")
        scenario = evidence / "station.xml"
        incoming_scenario(scenario, evidence / "source.pcap")
        peer = subprocess.Popen([*command, remote, "-sf", str(scenario), "-i", "127.0.0.1",
            "-p", str(peer_port), "-s", "600", "-auth_uri", "600@" + remote, "-mi", "127.0.0.1", "-mp", "25000", "-m", "1", "-timeout", "10s",
            "-nostdin", "-oocsf", str(evidence / "options.xml"), "-trace_msg",
            "-message_file", str(evidence / "sipp-messages.log"), "-trace_err",
            "-error_file", str(evidence / "sipp-errors.log")], cwd=evidence,
            stdout=(evidence / "sipp.log").open("w"), stderr=subprocess.STDOUT)
        result["peer_pid"] = peer.pid
        await wait_until(lambda: lab.runtime.endpoints.require(browser_endpoint.endpoint_id).active_call_id)
        call_id = lab.runtime.endpoints.require(browser_endpoint.endpoint_id).active_call_id
        await lab.hass.services.async_call("voip_stack", "answer", {"device_id": browser_endpoint.device_id,
            "call_id": call_id, "media_client_id": "community-media-owner"}, blocking=True)
        session = audio_ws_view._active_softphone_media_session(lab.hass, browser_endpoint.endpoint_id)
        assert session is not None
        assert (session.recv_format.encoding, session.recv_format.payload_type, session.recv_format.frame_ms) == ("PCMU", 0, 20)
        assert session.recv_format.max_frame_ms >= 40
        media_task = asyncio.create_task(audio_ws_view._run_audio_session(lab.hass, browser, session,
            endpoint_id=browser_endpoint.endpoint_id))
        await asyncio.wait_for(browser.ready.wait(), 2)
        feed_task = asyncio.create_task(feed_browser_tone(browser, session.send_format.audio_format))
        frames = [decode_audio_frame(await asyncio.wait_for(browser.audio.get(), 2)) for _ in range(40)]
        received = b"".join(frames)
        assert all(len(frame) == 320 for frame in frames)
        assert received == expected
        samples = struct.unpack("<%dh" % (len(received) // 2), received)
        crossings = [index for index in range(1, len(samples)) if samples[index - 1] <= 0 < samples[index]]
        tone = 8000 * (len(crossings) - 1) / (crossings[-1] - crossings[0])
        assert abs(tone - 500) < 1
        (evidence / "browser-received.s16le").write_bytes(received)
        result.update(received_frames=len(frames), received_samples=len(samples), measured_tone_hz=tone)
        feed_task.cancel()
        await asyncio.gather(feed_task, return_exceptions=True)
        feed_task = None
        await browser.messages.put(None)
        await asyncio.wait_for(media_task, 2)
        media_task = None
        await lab.hass.services.async_call("voip_stack", "hangup", {"device_id": browser_endpoint.device_id,
            "call_id": call_id}, blocking=True)
        await wait_until(lambda: not lab.calls.sessions)
        assert await asyncio.to_thread(peer.wait, 3) == 0
        assert registrar.registered_contacts("door")[0] is binding
        assert not lab.calls.endpoint_claims_snapshot()
        assert not lab.calls.relays_snapshot()
        assert not lab.calls.sip_clients_snapshot()
        assert not lab.calls.client_watchers_snapshot()
        assert not lab.runtime.rtp_port_pool.get("used")
        result["resources_zero"] = result["passed"] = True
    finally:
        if feed_task is not None:
            feed_task.cancel()
            await asyncio.gather(feed_task, return_exceptions=True)
        if media_task is not None:
            await browser.messages.put(None)
            await asyncio.wait_for(media_task, 2)
        active = lab.runtime.endpoints.require(browser_endpoint.endpoint_id).active_call_id
        if active:
            await lab.hass.services.async_call("voip_stack", "hangup", {"device_id": browser_endpoint.device_id,
                "call_id": active}, blocking=True)
        if peer is not None and peer.poll() is None:
            peer.terminate()
            await asyncio.to_thread(peer.wait, 3)
        if capture.poll() is None:
            subprocess.run(["sudo", "-n", "kill", "-INT", str(capture.pid)], check=True)
            await asyncio.to_thread(capture.wait, 3)
        result["capture_exit"] = capture.returncode
        if result["passed"]:
            result["passed"] = False
            result["independent_peer_receive"] = await asyncio.to_thread(captured_peer_tone, evidence / "wire.pcap", "PCMU")
            result["passed"] = True
        (evidence / "RESULT.json").write_text(json.dumps(result, indent=2) + "\n")
