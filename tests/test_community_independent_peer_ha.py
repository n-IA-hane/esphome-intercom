"""Community packetization witnesses through independent SIPp and native HA."""

import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import shlex
import struct
import subprocess
from unittest.mock import AsyncMock

import pytest
from aiohttp import WSMessage, WSMsgType

from custom_components.voip_stack import audio_ws_view, softphone_originate
from custom_components.voip_stack.audio_ws import decode_audio_frame, encode_audio_frame
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.roster import dump_roster_json
from custom_components.voip_stack.sip_registrar import SipAccount
from tests.test_trunk_calls_ha import lab as lab, trunks as trunks
from tests.test_sip_capabilities_ha import wait_until

pytestmark = [pytest.mark.ha, pytest.mark.skipif(not os.environ.get("VOIP_TEST_SIPP_COMMAND"),
    reason="Independent PCAP playback requires an explicitly configured SIPp with raw socket access")]


class BrowserSink:
    """Collect the production browser wire frames without a fake HA module."""
    def __init__(self):
        self.audio = asyncio.Queue()
        self.messages = asyncio.Queue()
        self.ready = asyncio.Event()

    async def send_json(self, value):
        self.ready.set()

    async def send_bytes(self, value):
        await self.audio.put(bytes(value))

    def force_close(self):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        value = await self.messages.get()
        if value is None:
            raise StopAsyncIteration
        return value


async def feed_browser_tone(browser, audio_format):
    """Supply a separate 450 Hz source through the real browser wire framing."""
    rate = audio_format.sample_rate
    count = audio_format.nominal_frame_bytes // 2
    position = 0
    while True:
        pcm = struct.pack("<%dh" % count, *(round(8000 * math.sin(2 * math.pi * 450 * (position + i) / rate))
            for i in range(count)))
        await browser.messages.put(WSMessage(WSMsgType.BINARY, encode_audio_frame(pcm), ""))
        position += count
        await asyncio.sleep(audio_format.frame_ms / 1000)


def captured_peer_tone(path, encoding):
    """Decode the peer-bound wire payload independently with FFmpeg."""
    trace = subprocess.run(["tshark", "-r", str(path), "-Y", "udp.dstport == 25000", "-T", "fields",
        "-e", "udp.payload"], capture_output=True, text=True, check=True).stdout
    packets = [bytes.fromhex(line.replace(":", "")) for line in trace.splitlines() if line]
    wire = b"".join(raw[12:] for raw in packets if len(raw) > 12 and raw[0] >> 6 == 2)
    rate = 16000 if encoding == "PCM" else 8000
    decoded = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f",
        "s16le" if encoding == "PCM" else "mulaw", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
        "-f", "s16le", "pipe:1"], input=wire, capture_output=True, check=True).stdout
    samples = struct.unpack("<%dh" % (len(decoded) // 2), decoded)
    assert len(samples) >= rate // 2
    powers = {frequency: abs(sum(value * complex(math.cos(2 * math.pi * frequency * index / rate),
        math.sin(2 * math.pi * frequency * index / rate)) for index, value in enumerate(samples)))
        for frequency in (440, 450, 460, 500)}
    assert max(powers, key=powers.get) == 450 and powers[450] > 10000
    path.with_suffix(".peer-received.s16le").write_bytes(decoded)
    return {"decoded_samples": len(samples), "measured_tone_hz": 450, "rtp_packets": len(packets)}


def media_pcap(path, encoding):
    """Write independent Ethernet/IPv4/UDP/RTP packets, 40 ms per packet."""
    rate, pt = (16000, 97) if encoding == "PCM" else (8000, 0)
    expected = bytearray()
    data = bytearray(struct.pack("<IHHIIII", 0xa1b2c3d4, 2, 4, 0, 0, 65535, 1))
    for index in range(20):
        samples = [round(8000 * math.sin(2 * math.pi * 500 * (index * rate // 25 + i) / rate))
                   for i in range(rate // 25)]
        if encoding == "PCM":
            payload = struct.pack("<%dh" % len(samples), *samples)
            expected.extend(payload)
        else:
            # ffmpeg's independent G.711 encoder supplies the wire bytes.
            pcm = struct.pack("<%dh" % len(samples), *samples)
            payload = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
                "-f", "mulaw", "pipe:1"], input=pcm, capture_output=True, check=True).stdout
            expected.extend(subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "mulaw", "-ar", str(rate), "-ac", "1", "-i", "pipe:0",
                "-f", "s16le", "pipe:1"], input=payload, capture_output=True, check=True).stdout)
        rtp = struct.pack("!BBHII", 0x80, pt, index + 1, index * rate // 25, 0x12345678) + payload
        udp = struct.pack("!HHHH", 25000, 40000, len(rtp) + 8, 0) + rtp
        header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, len(udp) + 20, index, 0, 64, 17, 0,
                             socket.inet_aton("127.0.0.1"), socket.inet_aton("127.0.0.1"))
        words = struct.unpack("!10H", header)
        total = sum(words)
        while total >> 16:
            total = (total & 0xffff) + (total >> 16)
        header = header[:10] + struct.pack("!H", ~total & 0xffff) + header[12:]
        packet = bytes.fromhex("00112233445566778899aabb0800") + header + udp
        data.extend(struct.pack("<IIII", 1, index * 40000, len(packet), len(packet)) + packet)
    path.write_bytes(data)
    return bytes(expected)


def uas_scenario(path, encoding, media, user_agent):
    pt, rate = (97, 16000) if encoding == "PCM" else (0, 8000)
    path.write_text(f'''<?xml version="1.0" encoding="ISO-8859-1" ?>
<!DOCTYPE scenario SYSTEM "sipp.dtd">
<scenario name="Community packetization independent UAS">
<recv request="INVITE" optional="true" next="10"/>
<recv request="OPTIONS"/>
<send next="99"><![CDATA[
SIP/2.0 405 Method Not Allowed
[last_Via:]
[last_From:]
[last_To:];tag=community-options
[last_Call-ID:]
[last_CSeq:]
Content-Length: 0

]]></send>
<label id="10"/>
<send><![CDATA[
SIP/2.0 200 OK
[last_Via:]
[last_From:]
[last_To:];tag=community
[last_Call-ID:]
[last_CSeq:]
Contact: <sip:door@[local_ip]:[local_port]>
User-Agent: {user_agent}
Content-Type: application/sdp
Content-Length: [len]

v=0
o=- 1 1 IN IP4 [local_ip]
s=Community station witness
c=IN IP4 [local_ip]
t=0 0
m=audio [media_port] RTP/AVP {pt}
a=rtpmap:{pt} {encoding}/{rate}/1
a=ptime:20
a=sendrecv
]]></send>
<recv request="ACK"/>
<pause milliseconds="350"/>
<nop><action><exec play_pcap_audio="{media}"/></action></nop>
<recv request="BYE" timeout="8000"/>
<send><![CDATA[
SIP/2.0 200 OK
[last_Via:]
[last_From:]
[last_To:]
[last_Call-ID:]
[last_CSeq:]
Content-Length: 0

]]></send>
<pause milliseconds="100"/>
<label id="99"/>
<nop/>
</scenario>
''')


def registration_scenario(path, user_agent):
    # Finish each independent OPTIONS transaction rather than inheriting SIPp's
    # default four-second out-of-call timer after the REGISTER scenario ends.
    (path.parent / "options.xml").write_text('''<?xml version="1.0"?>
<scenario name="Independent OPTIONS">
<recv request="OPTIONS"/>
<send><![CDATA[
SIP/2.0 405 Method Not Allowed
[last_Via:]
[last_From:]
[last_To:];tag=independent-options
[last_Call-ID:]
[last_CSeq:]
Content-Length: 0

]]></send>
</scenario>''')
    request = """REGISTER sip:[remote_ip]:[remote_port] SIP/2.0
Via: SIP/2.0/[transport] [local_ip]:[local_port];branch=[branch];rport
From: <sip:door@[remote_ip]>;tag=community-register
To: <sip:door@[remote_ip]>
Call-ID: [call_id]
CSeq: {sequence} REGISTER
Contact: <sip:door@[local_ip]:[local_port];transport=udp>
Expires: 60
User-Agent: {user_agent}
{authorization}Content-Length: 0

"""
    first = request.format(sequence=1, user_agent=user_agent, authorization="")
    second = request.format(sequence=2, user_agent=user_agent,
        authorization='[authentication username=door password=isolated-test-only]\n')
    path.write_text('<?xml version="1.0"?><scenario name="Independent REGISTER">'
        + '<send><![CDATA[' + first + ']]></send><recv response="401" auth="true"/>'
        + '<send><![CDATA[' + second + ']]></send><recv response="200"/>'
        + '<pause milliseconds="1000"/></scenario>')


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("encoding,route", [("PCM", "registered"), ("PCM", "static"), ("PCMU", "static")])
async def test_sipp_40ms_station_audio_reaches_native_browser_owner(trunks, tmp_path, monkeypatch, encoding, route):
    """Exercise real peer RTP, negotiated receive capacity and complete teardown."""
    lab = trunks
    from custom_components.voip_stack.trunk_runtime import async_stop_sip_trunk
    from custom_components.voip_stack.roster import RosterEntry
    await async_stop_sip_trunk(lab.hass)
    lab.runtime.trunk_configs = {}
    lab.runtime.trunk_config = {}
    lab.runtime.default_trunk_id = ""
    evidence = Path(os.environ.get("VOIP_COMMUNITY_EVIDENCE_DIR", str(tmp_path / "evidence"))) / (encoding + "-" + route)
    evidence.mkdir(parents=True, exist_ok=True)
    source = PhoneEndpoint(endpoint_id="browser:community", name="Community browser",
        device_id="community-browser", kind=EndpointKind.BROWSER, availability="available")
    lab.runtime.endpoints.register(source)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        peer_port = probe.getsockname()[1]
    ua = "Dahua UAC/3.0 VTO2111D-P-S2 V4.700.0.3" if encoding == "PCM" else "Hikvision-community-witness"
    metadata = {"sip_profile": "dahua", "dahua_audio": "pcm"} if encoding == "PCM" else {}
    registrar = lab.calls.component("registrar")
    if route == "registered":
        registrar.enabled = True
        registrar.update_accounts([SipAccount("door", "Community door", "isolated-test-only")])
        lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="door", name="Community door",
            username="door", kind=EndpointKind.SIP_ACCOUNT))
        register = evidence / "register.xml"
        registration_scenario(register, ua)
        registered = await asyncio.to_thread(subprocess.run,
            [*shlex.split(os.environ.get("VOIP_TEST_SIPP_COMMAND", "sipp")),
             f"127.0.0.1:{lab.runtime.transport_config['sip_port']}", "-sf", str(register),
             "-i", "127.0.0.1", "-p", str(peer_port), "-m", "1", "-timeout", "4s", "-nostdin",
             "-oocsf", str(evidence / "options.xml"),
             "-trace_msg", "-message_file", str(evidence / "registration.log")],
            cwd=evidence, capture_output=True, text=True)
        (evidence / "registration-console.log").write_text(registered.stdout + registered.stderr)
        assert registered.returncode == 0
        binding, = registrar.registered_contacts("door")
        assert binding.user_agent == ua
        rows = registrar.registered_roster_entries()
        assert rows and rows[0].metadata["sip_profile"] == "dahua"
        lab.roster.extend(rows)
    else:
        lab.roster.append(RosterEntry("Community door", sip_uri=f"sip:door@127.0.0.1:{peer_port}", metadata=metadata))
    lab.runtime.directory.replace(lab.roster)
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    expected = await asyncio.to_thread(media_pcap, evidence / "source.pcap", encoding)
    scenario = evidence / "station.xml"
    uas_scenario(scenario, encoding, evidence / "source.pcap", ua)
    capture = subprocess.Popen([*shlex.split(os.environ.get("VOIP_TEST_CAPTURE_COMMAND", "sudo -n tcpdump")),
        "-i", "lo", "-U", "--immediate-mode", "-w", str(evidence / "wire.pcap"), "udp or tcp"],
        stdout=subprocess.DEVNULL, stderr=(evidence / "tcpdump.log").open("w"))
    result_capture_pid = capture.pid
    process = subprocess.Popen([*shlex.split(os.environ.get("VOIP_TEST_SIPP_COMMAND", "sipp")), "-sf", str(scenario), "-i", "127.0.0.1", "-p", str(peer_port),
        "-mi", "127.0.0.1", "-mp", "25000", "-m", "2" if route == "registered" else "1", "-timeout", "12s", "-nostdin",
        "-trace_msg", "-message_file", str(evidence / "sipp-messages.log"), "-trace_err",
        "-error_file", str(evidence / "sipp-errors.log")], cwd=evidence,
        stdout=(evidence / "sipp.log").open("w"), stderr=subprocess.STDOUT)
    browser = BrowserSink()
    media_task = None
    feed_task = None
    result = {"candidate": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "candidate_diff_sha256": hashlib.sha256(subprocess.check_output(["git", "diff"])).hexdigest(), "encoding": encoding, "route": route,
        "scope": "native HA public outgoing browser service to independent SIPp peer",
        "hardware_tested": False, "peer_pid": process.pid, "capture_pid": result_capture_pid, "passed": False}
    try:
        await asyncio.sleep(.15)
        assert process.poll() is None, (evidence / "sipp.log").read_text()
        await lab.hass.services.async_call("voip_stack", "call", {"device_id": source.device_id,
            "destination": "Community door"}, blocking=True)
        def client():
            call_id = lab.runtime.endpoints.require(source.endpoint_id).active_call_id
            return lab.calls.sip_client_for(call_id) if call_id else None
        await wait_until(lambda: client() is not None and client().dialog is not None)
        active = client()
        session = audio_ws_view._active_softphone_media_session(lab.hass, source.endpoint_id)
        assert session is not None
        assert session.recv_format.encoding == encoding
        assert session.recv_format.frame_ms == (20 if encoding == "PCM" else 10)
        assert session.recv_format.max_frame_ms >= 40
        result["receive_capacity_ms"] = session.recv_format.max_frame_ms
        media_task = asyncio.create_task(audio_ws_view._run_audio_session(lab.hass, browser, session,
            endpoint_id=source.endpoint_id))
        await asyncio.wait_for(browser.ready.wait(), 2)
        feed_task = asyncio.create_task(feed_browser_tone(browser, session.send_format.audio_format))
        frame_count = 800 // session.recv_format.frame_ms
        chunks = [decode_audio_frame(await asyncio.wait_for(browser.audio.get(), 2)) for _ in range(frame_count)]
        actual = b"".join(chunks)
        frame_bytes = session.recv_format.audio_format.nominal_frame_bytes
        assert all(len(chunk) == frame_bytes for chunk in chunks)
        assert actual == expected
        (evidence / "browser-received.s16le").write_bytes(actual)
        samples = struct.unpack("<%dh" % (len(actual) // 2), actual)
        crossings = [index for index in range(1, len(samples)) if samples[index - 1] <= 0 < samples[index]]
        measured_tone = session.recv_format.audio_format.sample_rate * (len(crossings) - 1) / (crossings[-1] - crossings[0])
        assert abs(measured_tone - 500) < 1
        result.update(received_frames=len(chunks), received_samples=len(actual)//2,
            measured_tone_hz=measured_tone, sample_sha256=hashlib.sha256(actual).hexdigest())
        feed_task.cancel()
        await asyncio.gather(feed_task, return_exceptions=True)
        feed_task = None
        await browser.messages.put(None)
        await asyncio.wait_for(media_task, 2)
        media_task = None
        await lab.hass.services.async_call("voip_stack", "hangup", {"device_id": source.device_id}, blocking=True)
        await wait_until(lambda: not lab.calls.sessions)
        assert await asyncio.to_thread(process.wait, 4) == 0, (evidence / "sipp.log").read_text()
        assert not lab.calls.endpoint_claims_snapshot()
        assert not lab.calls.sip_clients_snapshot()
        assert not lab.calls.client_watchers_snapshot()
        assert not lab.calls.relays_snapshot()
        assert not lab.runtime.rtp_port_pool.get("used")
        assert active.dialog is None and active.audio_rtp_socket is None
        result["resources_zero"] = result["passed"] = True
    finally:
        if feed_task is not None:
            feed_task.cancel()
            await asyncio.gather(feed_task, return_exceptions=True)
        if media_task is not None:
            await browser.messages.put(None)
            await asyncio.wait_for(media_task, 2)
        if lab.runtime.endpoints.require(source.endpoint_id).active_call_id:
            await lab.hass.services.async_call("voip_stack", "hangup", {"device_id": source.device_id}, blocking=True)
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, 2)
        if capture.poll() is None:
            subprocess.run(["sudo", "-n", "kill", "-INT", str(capture.pid)], check=True)
            await asyncio.to_thread(capture.wait, 3)
        result["capture_exit"] = capture.returncode
        if result["passed"]:
            result["passed"] = False
            result["independent_peer_receive"] = await asyncio.to_thread(captured_peer_tone, evidence / "wire.pcap", encoding)
            result["passed"] = True
        (evidence / "RESULT.json").write_text(json.dumps(result, indent=2) + "\n")
