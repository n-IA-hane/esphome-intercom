"""A trusted trunk keeps the same SIP dialog when phone policy selects voicemail."""

import asyncio
import re
import socket
import struct
import wave
from contextlib import closing

import numpy as np
import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import callback

from custom_components.voip_stack.config import normalized_trunk_config
from custom_components.voip_stack.core import rtp, sip
from custom_components.voip_stack.mailbox import EVENT_VOICEMAIL_RECEIVED, get_archive
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.phone_policy import PhonePolicy
from custom_components.voip_stack.sip_trunk import SipTrunkClient
from custom_components.voip_stack.store import config_entry
from custom_components.voip_stack.trunk_runtime import trunk_client_config
from custom_components.voip_stack.websocket_api import CALL_EVENT

from .test_automation_originate_ha import lab as lab
from .test_browser_logical_availability_ha import resources
from .test_sip_phone_transfers_ha import registered_phones as registered_phones
from .test_trunk_calls_ha import trunks as trunks
from .test_voicemail_ha import configured_tts_provider as configured_tts_provider

pytestmark = pytest.mark.ha
PROVIDER_HOST = "127.0.0.42"


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
@pytest.mark.parametrize(("policy", "rtp_digits", "select_digits"), [
    ("always", True, True), ("unavailable", False, True),
    ("unavailable", True, False), ("always", False, False),
    ("no_answer", True, True), ("no_answer", False, False),
    ("busy_response", True, False), ("busy_response", False, False),
    ("unavailable_response", True, False), ("unavailable_response", False, False),
])
async def test_trunk_account_policy_deposits_caller_audio_twice(
    trunks, registered_phones, tmp_path, monkeypatch, caplog, policy, rtp_digits, select_digits,
):
    runtime, hass = trunks, trunks.hass
    listener = ("127.0.0.1", runtime.runtime.transport_config["sip_port"])
    hass.config.media_dirs = {"local": str(tmp_path / "media")}
    entry = config_entry(hass)
    hass.config_entries.async_update_entry(entry, data={**entry.data, "voicemail": {
        "enabled": True, "tts_entity_id": "tts.test", "language": "en",
    }})
    peer_failure = policy in {"busy_response", "unavailable_response"}
    peer = runtime.phones[0]
    if peer_failure:
        from .test_trunk_direct_pending_ha import _register_target
        mailbox_id = "reactive-target"
        runtime.runtime.endpoints.register(PhoneEndpoint(mailbox_id, "Kitchen", EndpointKind.SIP_ACCOUNT, username=mailbox_id))
        await _register_target(runtime, peer, monkeypatch, username=mailbox_id)
        peer.final_status = 486 if policy == "busy_response" else 503
    else:
        mailbox_id = "transfer_b" if policy in {"always", "no_answer"} else "kitchen"
    existing = next((row for row in entry.subentries.values()
                     if row.subentry_type == "phone" and row.data.get("username") == mailbox_id), None)
    settings = {"name": "Kitchen", "endpoint_id": mailbox_id, "username": mailbox_id,
                "kind": "sip_account", "voicemail": {"enabled": True, "max_seconds": 5}}
    if existing is not None:
        hass.config_entries.async_update_subentry(entry, existing, data={**existing.data, **settings})
    else:
        hass.config_entries.async_add_subentry(entry, ConfigSubentry(
            subentry_type="phone", title="Kitchen", unique_id=f"phone:{mailbox_id}", data=settings))
    endpoints = runtime.runtime.endpoints
    if policy in {"always", "no_answer"} or peer_failure:
        assert runtime.calls.component("registrar").registered_contacts(mailbox_id)
    else:
        endpoints.register(PhoneEndpoint(mailbox_id, "Kitchen", EndpointKind.SIP_ACCOUNT, username=mailbox_id))
    effective_policy = "unavailable" if policy == "unavailable_response" else "busy" if policy == "busy_response" else policy
    endpoints.update(mailbox_id, name="Kitchen", extension="4",
                     policy=PhonePolicy.from_data({effective_policy: {"kind": "voicemail", "target": mailbox_id},
                                                   **({"ringing_timeout": 1} if policy == "no_answer" else {})}))
    if policy == "always":
        endpoints.update(mailbox_id, dnd=True)
    from custom_components.voip_stack.directory_runtime import compose_directory_entries
    automation = [row for row in runtime.roster if row.metadata.get("virtual_endpoint") == "automation"]
    runtime.roster[:] = [*compose_directory_entries(hass, [], runtime.calls.component("registrar").registered_roster_entries()), *automation]
    runtime.runtime.directory.replace(runtime.roster)
    config = normalized_trunk_config({
        "trunk_enabled": True, "trunk_register": False, "trunk_server": PROVIDER_HOST,
        "trunk_allowed_ips": [f"{PROVIDER_HOST}/32"], "trunk_inbound_mode": "dtmf", "trunk_dtmf_enabled": True,
        "trunk_dtmf_timeout_ms": 200, "trunk_inbound_default_target": "Kitchen",
    })
    provider = SipTrunkClient(config=trunk_client_config(config), local_ip=listener[0],
                              local_sip_port=listener[1], trunk_id="test-provider")
    runtime.runtime.trunk_configs["test-provider"] = config
    runtime.calls.adopt_component("trunk:test-provider", provider, closer=provider.stop)
    recording = asyncio.Event()
    saved = asyncio.Queue()

    @callback
    def state(event):
        if event.data.get("event_type") == "voicemail_recording":
            recording.set()

    @callback
    def received(event):
        saved.put_nowait(event.data)

    hass.bus.async_listen(CALL_EVENT, state)
    hass.bus.async_listen(EVENT_VOICEMAIL_RECEIVED, received)
    archive = await get_archive(hass)
    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as source, closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as media:
        for sock in (source, media):
            sock.bind((PROVIDER_HOST, 0))
            sock.setblocking(False)
        contact = f"sip:provider@{PROVIDER_HOST}:{source.getsockname()[1]}"
        destination = f"sip:450@{listener[0]}:{listener[1]}"
        formats = "96 101" if rtp_digits else "96"
        sdp = (f"v=0\r\no=- 1 1 IN IP4 {PROVIDER_HOST}\r\ns=Voicemail test\r\nc=IN IP4 {PROVIDER_HOST}\r\nt=0 0\r\n"
               f"m=audio {media.getsockname()[1]} RTP/AVP {formats}\r\na=rtpmap:96 L16/16000/1\r\n"
               + ("a=rtpmap:101 telephone-event/16000\r\na=fmtp:101 0-15\r\n" if rtp_digits else "")
               + "a=ptime:20\r\na=sendrecv\r\n").encode()

        for attempt in range(2):
            len(registered_phones[1].read())
            log_offset = len(caplog.records)
            recording.clear()
            call_id = f"mailbox-trunk-{attempt}"
            to = f"<{destination}>"
            async def send(method, sequence, *, body=b"", content_type="application/sdp"):
                headers = [("Via", f"SIP/2.0/UDP {PROVIDER_HOST}:{source.getsockname()[1]};branch=z9hG4bK-{method}-{attempt}"),
                           ("From", f"<{contact}>;tag=caller-{attempt}"), ("To", to), ("Call-ID", call_id),
                           ("CSeq", f"{sequence} {method}"), ("Contact", f"<{contact}>"), ("Max-Forwards", "70")]
                if body:
                    headers.append(("Content-Type", content_type))
                await hass.loop.sock_sendto(source, sip.build_request(method, destination, headers, body), listener)

            async def response(method, status):
                async with asyncio.timeout(4):
                    while True:
                        msg = sip.parse_message(await hass.loop.sock_recv(source, 16384))
                        if msg.header("Call-ID") == call_id and msg.header("CSeq").endswith(" " + method) and msg.status_code == status:
                            return msg

            await send("INVITE", 1, body=sdp)
            offer = await response("INVITE", 200)
            to = offer.header("To")
            target = (listener[0], int(re.search(rb"m=audio (\d+)", offer.body)[1]))
            await send("ACK", 1)
            await asyncio.wait_for(recording.wait(), 4)
            assert runtime.calls.get_session(call_id).owner == "voicemail"
            if peer_failure:
                assert any(f"SIP RX {peer.final_status} " in record.getMessage() for record in caplog.records[log_offset:])
            tone = np.rint(8000 * np.sin(2 * np.pi * 450 * np.arange(320) / 16000)).astype(">i2")
            for n in range(24):
                packet = rtp.RtpPacket(96, 10 + n, 5000 + 320 * n, 701 + attempt, tone.tobytes())
                await hass.loop.sock_sendto(media, rtp.build_packet(packet), target)
                await asyncio.sleep(.02)
            await send("BYE", 3)
            await response("BYE", 200)
            deposited = await asyncio.wait_for(saved.get(), 4)
            assert deposited["mailbox_id"] == mailbox_id
            messages = archive.list(mailbox_id)
            assert len(messages) == attempt + 1
            message = next(m for m in messages if m["id"] == deposited["message_id"])
            with wave.open(str(archive.segment_path(message["id"], mailbox_id)), "rb") as audio:
                assert audio.getframerate() == 16000 and audio.getsampwidth() == 2
                assert tone.astype("<i2").tobytes() * 8 in audio.readframes(audio.getnframes())
            async with asyncio.timeout(3):
                while runtime.calls.sessions:
                    await asyncio.sleep(.01)
            assert resources(runtime)["call_scoped_quiescent"]
            if peer_failure:
                assert len(peer.invites) == attempt + 1 and not peer.answered.is_set()
            elif policy == "no_answer":
                assert "Incoming call" in registered_phones[1].read()
                assert "Call established" not in registered_phones[1].read()
            else:
                assert "Incoming call" not in registered_phones[1].read()
