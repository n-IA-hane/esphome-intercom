"""Explicit privileged NAT case, invoked by sip_nat_lab, never by normal CI.

SIPp is the independent ordinary registered phone. The real HA integration
owns routing/relays, and maintained FFmpeg/JPEG peer helpers own media.
"""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest
import pytest_socket

from tests.test_automation_originate_ha import lab as lab
from custom_components.voip_stack import endpoint_runtime, peer_snapshot
from custom_components.voip_stack.const import CONF_REGISTRAR_ENABLED, CONF_SIP_VIDEO, CONF_VIDEO_CAMERA_SEND
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.runtime_diagnostics import runtime_resource_snapshot
from custom_components.voip_stack.sip_registrar import SipAccount
from tools.ha_voip_lab.sip_nat_lab import CLIENT_IP, NAT_IP, SERVER_IP, private_json


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.ha


@pytest.mark.parametrize("transport", ["udp", "tcp"])
async def test_ordinary_registered_phone_keeps_duplex_pcm_and_jpeg_across_real_nat(lab, monkeypatch, transport):
    namespace = os.environ["SIP_NAT_CLIENT_NS"]
    if re.fullmatch(r"sipq-[a-z0-9]+-client", namespace) is None:
        raise ValueError("An owned isolated client namespace is required")
    output = Path(os.environ["SIP_NAT_OUTPUT"]) / transport
    output.mkdir(parents=True, mode=0o700)
    options_body = os.environ.get("SIP_NAT_OPTIONS_BODY", "native")
    rebind = os.environ.get("SIP_NAT_REBIND") == "1"
    if options_body not in {"native", "empty"}:
        raise ValueError("Unknown capability description case")
    fixture = ROOT / "tests/fixtures/sdp/native-options-mixed-quanta.sdp"
    # This is an independent SIPp response on the wire, never HA metadata.
    # Keep the captured absence of ptime and every native payload/attribute.
    capabilities = fixture.read_text().replace("192.0.2.30", CLIENT_IP).replace("\n", "\r\n") if options_body == "native" else ""
    capability_file = output / "options-body.sdp"
    fd = os.open(capability_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", newline="") as stream:
        stream.write(capabilities)
    username, password = "ordinary_nat_" + transport, secrets.token_hex(24)
    for name in ("nat-register.xml", "nat-media-uas.xml"):
        body = (ROOT / "tools/qualification/sipp" / name).read_text()
        body = body.replace("__USERNAME__", username).replace("__PASSWORD__", password)
        body = body.replace("__CAPABILITY_FILE__", str(capability_file))
        fd = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(body)
    media_log = output / "media-fields.log"
    fd = os.open(media_log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    pytest_socket.socket_allow_hosts(["127.0.0.1", CLIENT_IP, NAT_IP, SERVER_IP])
    lab.runtime.sip = lab.calls = SipEndpointRuntime(allow_dark_sessions=True)
    lab.runtime.transport_config.update({"sip_port": 15060, "rtp_port": 18000, "advertise_host": SERVER_IP,
        CONF_REGISTRAR_ENABLED: True, CONF_SIP_VIDEO: True, CONF_VIDEO_CAMERA_SEND: True})
    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value=SERVER_IP))
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", AsyncMock(return_value=[]))
    endpoint = PhoneEndpoint(endpoint_id="sip:" + username, username=username, name="Ordinary NAT phone",
        kind=EndpointKind.SIP_ACCOUNT)
    lab.runtime.endpoints.register(endpoint)
    assert not any(item.kind is EndpointKind.ESPHOME for item in lab.runtime.endpoints.endpoints)
    assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
    registrar = lab.calls.component("registrar")
    registrar.update_accounts([SipAccount(username, endpoint.name, password)])
    processes, logs = [], []
    result = {"passed": False, "transport": transport, "scope": "software SIP/PCM/JPEG across actual isolated SNAT",
        "physical_esp_qualified": False, "options_body": options_body,
        "native_fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest() if capabilities else None}

    def spawn(command, label, client=False):
        fd = os.open(output / (label + ".log"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        log = os.fdopen(fd, "wb")
        logs.append(log)
        prefix = ["ip", "netns", "exec", namespace] if client else []
        process = subprocess.Popen([*prefix, *map(str, command)], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            cwd=output)
        processes.append(process)
        return process

    async def bounded_wait(predicate, *, timeout=8):
        async with asyncio.timeout(timeout):
            while not predicate():
                if any(process.poll() not in (None, 0) for process in processes):
                    raise RuntimeError("A maintained NAT qualification actor failed")
                await asyncio.sleep(.05)

    def resources():
        return runtime_resource_snapshot(lab.calls, detailed=True, rtp_port_pool=lab.runtime.rtp_port_pool,
            browser_media=lab.runtime.media)

    try:
        phone = spawn(["sipp", f"{SERVER_IP}:15060", "-sf", output / "nat-register.xml",
            "-rxsf", output / "nat-media-uas.xml",
            "-i", CLIENT_IP, "-p", "20600", "-bind_local", "-mi", CLIENT_IP, "-mp", "40000",
            "-t", "u1" if transport == "udp" else "t1", "-m", "1", "-r", "1", "-l", "2",
            "-timeout", "25s", "-timeout_error", "-nostdin", "-trace_logs", "-log_file", media_log], "sipp", client=True)
        await bounded_wait(lambda: bool(registrar.registered_contacts(username)
            and username.casefold() in lab.runtime.capability_probes
            and lab.runtime.capability_probes[username.casefold()].done()))
        initial, = registrar.registered_contacts(username)
        result["initial_registration_flow"] = initial.snapshot()
        await bounded_wait(lambda: bool(registrar.registered_contacts(username)
            and registrar.registered_contacts(username)[0].cseq >= 3
            and lab.runtime.capability_probes[username.casefold()].done()), timeout=10)
        binding, = registrar.registered_contacts(username)
        assert binding.source_host == NAT_IP
        assert CLIENT_IP in binding.advertised_contact_uri
        assert NAT_IP in binding.contact_uri and CLIENT_IP not in binding.contact_uri
        assert binding.call_id == initial.call_id
        assert binding.advertised_contact_uri == initial.advertised_contact_uri
        if rebind and transport == "udp":
            assert binding.source_port != initial.source_port
            assert 42000 <= binding.source_port <= 42009
        else:
            assert binding.flow_key == initial.flow_key
        if options_body == "native":
            assert binding.capabilities["sdp_features"] == ["directional_audio_v1"]
            assert not binding.capabilities["sip_audio_tx_formats"]
            assert not binding.capabilities["sip_audio_rx_formats"]
        else:
            assert binding.capabilities is None
        result["registered_flow"] = binding.snapshot()
        assert resources()["call_scoped_quiescent"]
        media = spawn([sys.executable, ROOT / "tools/ha_voip_lab/sip_nat_media.py", "--remote-fields-file", media_log,
            "--duration", "10", "--output", output / "client-media"], "media", client=True)
        await bounded_wait(lambda: (output / "client-media/ready.json").exists())
        caller = spawn([sys.executable, ROOT / "tools/sip_video_peer.py", "--host", SERVER_IP, "--port", "15060",
            "--target", username, "--user", "nat-caller", "--local-ip", SERVER_IP, "--codec", "jpeg",
            "--audio-codec", "l16-16k-10", "--direction", "sendrecv", "--video-size", "160x120",
            "--video-fps", "10", "--duration", "4" if rebind else "7", "--video-rx-file", output / "caller-video.mjpeg",
            "--out", output / "caller.json"], "caller")
        await bounded_wait(lambda: "SIP_NAT_MEDIA" in media_log.read_text())
        match = re.search(r"SIP_NAT_MEDIA ra=(\d+) rv=(\d+) tx=(\d+) rx=(\d+)", media_log.read_text())
        assert match
        audio_port, video_port, send_pt, receive_pt = map(int, match.groups())
        result["negotiated_media"] = {"ha_audio_port": audio_port, "ha_video_port": video_port,
            "client_tx_payload": send_pt, "client_rx_payload": receive_pt,
            "receiver_prepared_before_call": True, "stop_owner": "caller BYE/completion"}
        await bounded_wait(lambda: caller.poll() is not None, timeout=12)
        media.send_signal(signal.SIGTERM)
        await bounded_wait(lambda: media.poll() is not None)
        assert media.returncode == caller.returncode == 0
        result["client_media"] = json.loads((output / "client-media/result.json").read_text())
        result["caller_media"] = json.loads((output / "caller.json").read_text())
        assert result["client_media"]["passed"]
        assert result["caller_media"]["video_rx_frames"] >= 3
        decoded = await asyncio.create_subprocess_exec("ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_frames,width,height", "-of", "json", str(output / "caller-video.mjpeg"),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        data, errors = await asyncio.wait_for(decoded.communicate(), 10)
        assert decoded.returncode == 0 and not errors
        result["caller_independent_jpeg_decode"] = json.loads(data)["streams"][0]
        assert int(result["caller_independent_jpeg_decode"]["nb_read_frames"]) >= 3
        await bounded_wait(lambda: resources()["call_scoped_quiescent"])
        result["after_call"] = resources()
        await bounded_wait(lambda: phone.poll() is not None, timeout=15)
        assert phone.returncode == 0
        assert not registrar.registered_contacts(username)
        result["passed"] = True
    finally:
        for process in reversed(processes):
            if process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait, timeout=5)
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(session.call_id, TerminationIntent("test_cleanup"))
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)
        for log in logs:
            log.close()
        result["actor_processes"] = [{"pid": process.pid, "return_code": process.returncode}
            for process in processes]
        for path in output.glob("*.log"):
            path.write_text(path.read_text(errors="replace").replace(password, "[redacted]"))
            path.chmod(0o600)
        (output / "nat-register.xml").unlink()
        private_json(output / "result.json", result)
        pytest_socket.socket_allow_hosts(["127.0.0.1"])
