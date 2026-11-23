"""Actual UDP local applications admit and learn only their negotiated NAT peer."""

import asyncio
import socket
import struct

import pytest
from homeassistant.config_entries import ConfigSubentry

from custom_components.voip_stack.conference import ConferenceRoom
from custom_components.voip_stack.core import rtp, sdp, sip
from custom_components.voip_stack.core.codec_policy import CodecPolicy
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.paging import PagingCall, async_setup_paging
from custom_components.voip_stack.paging_config import paging_group_entries
from custom_components.voip_stack.trunk_runtime import async_stop_sip_trunk
from custom_components.voip_stack.sip_listener import SipInvite
from custom_components.voip_stack.store import config_entry
from .test_queue_call_ha import queue_lab as queue_lab
from .test_trunk_calls_ha import lab as lab, trunks as trunks

pytestmark = [pytest.mark.ha, pytest.mark.usefixtures("socket_enabled")]


def udp(host):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((host, 0))
    sock.setblocking(False)
    return sock


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


def packet(fmt, sequence, *, ssrc=0x445566, payload=None, pt=None):
    count = fmt.audio_format.nominal_frame_samples * fmt.channels
    payload = struct.pack(f"!{count}h", *([2300] * count)) if payload is None else payload
    return rtp.build_packet(rtp.RtpPacket(fmt.payload_type if pt is None else pt,
        sequence, sequence * fmt.rtp_timestamp_step, ssrc, payload))


async def nonzero(sock, diagnostics):
    """Observe actual PCM RTP, including the normal initial silence packets."""
    try:
        async with asyncio.timeout(2):
            while True:
                if isinstance(sock, asyncio.Queue):
                    queued = await sock.get()
                    data = queued[0] if isinstance(queued, tuple) else queued
                else:
                    data = await asyncio.get_running_loop().sock_recv(sock, 8192)
                try:
                    received = rtp.parse_packet(data)
                except ValueError:
                    continue
                if len(received.payload) % 2 == 0 and received.payload:
                    values = struct.unpack(f"!{len(received.payload) // 2}h", received.payload)
                    if max(abs(value) for value in values) > 1000:
                        return received
    except TimeoutError:
        pytest.fail(f"No paging/conference PCM reached the actual peer: {diagnostics()}")


async def negative_packets(fmt, nat, wrong, server_port, delivered, endpoint):
    """Untrusted, unoffered, malformed, duplicate and probation packets cannot rebind."""
    loop = asyncio.get_running_loop()
    before = endpoint()
    candidate = udp("127.0.0.2")
    try:
        cases = [
            (wrong, packet(fmt, 7)),
            (candidate, packet(fmt, 7, pt=112)),
            (candidate, packet(fmt, 7, payload=b"\x01")),
            (candidate, packet(fmt, 6)),
            (candidate, packet(fmt, 30, ssrc=0x778899)),
        ]
        for source, raw in cases:
            await loop.sock_sendto(source, raw, ("127.0.0.1", server_port))
            async with asyncio.timeout(2):
                while await delivered.get() != raw:
                    pass
            assert endpoint() == before
        assert before == ("127.0.0.2", nat.getsockname()[1])
    finally:
        candidate.close()


def incoming(fmt, declared, identity, *, signal="127.0.0.2"):
    body = (f"v=0\r\nc=IN IP4 {declared.getsockname()[0]}\r\n"
        f"m=audio {declared.getsockname()[1]} RTP/AVP {fmt.payload_type}\r\n"
        f"a=rtpmap:{fmt.payload_type} L16/{fmt.sample_rate}/1\r\n"
        "a=ptime:20\r\na=maxptime:20\r\na=sendrecv\r\n").encode()
    return SipInvite(source_host=signal, source_port=5060,
        request_uri=sip.SipUri("VoIP QA NAT conference", "127.0.0.1", 5060),
        caller_uri=sip.SipUri(identity, signal, 5060), target="VoIP QA NAT conference",
        caller=identity, call_id=identity, cseq="1 INVITE", remote_sdp=body,
        remote_rtp_host=declared.getsockname()[0], remote_rtp_port=declared.getsockname()[1],
        send_format=fmt, recv_format=fmt, send_formats=(fmt,), recv_formats=(fmt,))


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_paging_actual_fanout_keeps_nat_source_guards(queue_lab, monkeypatch):
    runtime = queue_lab
    entry = config_entry(runtime.hass)
    await async_stop_sip_trunk(runtime.hass)
    runtime.runtime.trunk_configs = {}
    runtime.runtime.trunk_config = {}
    runtime.runtime.default_trunk_id = ""
    group = ConfigSubentry(subentry_type="paging", title="VoIP QA NAT paging",
        unique_id="paging:nat-page", data={"paging_group_id": "nat-page",
        "name": "VoIP QA NAT paging", "extension": "6989", "members": ["bedroom0"],
        "enabled": True, "answer_timeout": 2, "timeout": 8})
    runtime.hass.config_entries.async_add_subentry(entry, group)
    await runtime.hass.async_block_till_done()
    await async_setup_paging(runtime.hass, entry)
    rows = [row for row in paging_group_entries(entry)
            if row.metadata.get("paging_group_id") == "nat-page"]
    assert len(rows) == 1 and rows[0].extension == "6989"
    runtime.roster.extend(rows)
    runtime.runtime.directory.replace(runtime.roster)
    runtime.phones[0].direction = "recvonly"
    caller, nat = await runtime.caller()
    declared, wrong = udp("127.0.0.3"), udp("127.0.0.4")
    # The real SIP socket stays on .2. Only its advertised media address/port
    # differ, like the authenticated WireGuard caller in the retained witness.
    caller.media_ip = "127.0.0.3"
    caller.local_rtp_port = declared.getsockname()[1]
    caller.codec_policy = CodecPolicy(audio=("L16",))
    watcher = None
    call_id = ""
    try:
        answer = await caller.invite(target="6989", remote_host="127.0.0.1",
            remote_sip_port=runtime.runtime.transport_config["sip_port"], timeout=3)
        if answer == "ringing":
            answer = await caller.wait_for_final(timeout=3)
        assert answer == "in_call"
        call_id = caller.dialog_ids.call_id
        watcher = asyncio.create_task(caller.wait_for_dialog_termination())
        await until(lambda: runtime.calls.resource_for(call_id, "paging") is not None)
        application = runtime.calls.resource_for(call_id, "paging")
        assert isinstance(application, PagingCall)
        await until(lambda: application.media is not None)
        media = application.media
        assert media.invite.remote_rtp_host == "127.0.0.3"
        assert media.invite.source_host == "127.0.0.2"
        processed = asyncio.Queue()
        original = media.handle_rtp

        def observed(data, address):
            original(data, address)
            processed.put_nowait(data)

        monkeypatch.setattr(media, "handle_rtp", observed)
        fmt = caller.dialog.send_format
        loop = asyncio.get_running_loop()
        for sequence in range(1, 7):
            await loop.sock_sendto(nat, packet(fmt, sequence), ("127.0.0.1", media.local_rtp_port))
        # First oracle is the actual paging fanout, not a Source35-only field.
        await nonzero(runtime.phones[0].audio, lambda: media.counters)
        assert media.counters["rtp_rx"] >= 1
        def endpoint():
            return media.remote_rtp_host, media.remote_rtp_port
        assert endpoint() == nat.getsockname()
        await negative_packets(fmt, nat, wrong, media.local_rtp_port, processed, endpoint)
        # Local application TX must also use the learned host and port.
        await media.tx_queue.put(struct.pack("<320h", *([2500] * 320)))
        await nonzero(nat, lambda: media.counters)
        updated = media.invite
        media.prepare_media_update(updated)()
        assert endpoint() == nat.getsockname()
    finally:
        await caller.terminate()
        if call_id:
            barrier = runtime.calls.request_termination(call_id, TerminationIntent("nat_test_cleanup"))
            if barrier is not None:
                await barrier
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        declared.close()
        wrong.close()


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_conference_actual_mixer_keeps_nat_source_guards(queue_lab, monkeypatch):
    runtime = queue_lab
    fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20, max_frame_ms=20)
    nat, declared, wrong, recipient = (udp(host) for host in
        ("127.0.0.2", "127.0.0.3", "127.0.0.4", "127.0.0.5"))
    room = ConferenceRoom(runtime.hass, name="VoIP QA NAT conference", local_ip="127.0.0.1")
    processed = asyncio.Queue()
    original = room.handle_rtp

    def observed(identity, data, address):
        original(identity, data, address)
        if identity == "nat-leg":
            processed.put_nowait(data)

    monkeypatch.setattr(room, "handle_rtp", observed)
    try:
        assert (await room.join(incoming(fmt, declared, "nat-leg"))).status == 200
        assert (await room.join(incoming(fmt, recipient, "other-leg", signal="127.0.0.5"))).status == 200
        leg, other = room.legs["nat-leg"], room.legs["other-leg"]
        loop = asyncio.get_running_loop()
        for sequence in range(1, 7):
            await loop.sock_sendto(nat, packet(fmt, sequence), ("127.0.0.1", leg.local_ports[0]))
        # The other participant hears actual mixed PCM; Source34 drops all NAT input.
        received = await nonzero(recipient, lambda: {"rx": leg.rx_packets, "host": leg.remote_host})
        assert received.payload == struct.pack("!320h", *([2300] * 320))
        def endpoint():
            return leg.remote_host, leg.remote_port
        assert endpoint() == nat.getsockname()
        await negative_packets(fmt, nat, wrong, leg.local_ports[0], processed, endpoint)
        for sequence in range(1, 7):
            await loop.sock_sendto(recipient, packet(fmt, sequence, ssrc=0x112233),
                ("127.0.0.1", other.local_ports[0]))
        # The mixer return path must reach .2, not merely accept input from it.
        await nonzero(nat, lambda: {"rx": other.rx_packets, "return_host": leg.remote_host})
    finally:
        await room.close("nat_test_cleanup")
        for sock in (nat, declared, wrong, recipient):
            sock.close()
