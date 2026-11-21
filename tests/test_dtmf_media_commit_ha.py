"""The actual media commit owner waits for a digit before replacing its leg."""

import asyncio
import struct
from types import SimpleNamespace

import pytest

from custom_components.voip_stack import bridge_media_updates
from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.sip_bridge import dialog_rtp_peer
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_rtp_bridge import SipRtpRelay
from .test_dialog_payload_lifetime_ha import dialog

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("interruption", ["none", "deadline", "cancel", "close"])
async def test_same_dialog_reinvite_commits_after_three_terminal_reports(monkeypatch, socket_enabled, interruption):
    import pytest_socket
    pytest_socket.socket_allow_hosts(["127.0.0.1"])
    packets = asyncio.Queue()

    class Inbox(asyncio.DatagramProtocol):
        def datagram_received(self, raw, _address):
            pt, sequence, timestamp, ssrc = struct.unpack("!xBHII", raw[:12])
            digit, flags, duration = struct.unpack("!BBH", raw[12:])
            packets.put_nowait((pt & 127, sequence, timestamp, ssrc, digit, bool(flags & 128), duration))

    loop = asyncio.get_running_loop()
    inbox, _ = await loop.create_datagram_endpoint(Inbox, local_addr=("127.0.0.1", 0))
    output, _ = await loop.create_datagram_endpoint(asyncio.DatagramProtocol, local_addr=("127.0.0.1", 0))
    remote = inbox.get_extra_info("sockname")
    pcmu = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
    previous = dialog(pcmu, pcmu, dtmf_payload_type=101, dtmf_clock_rate=8000,
                      send_dtmf_payload_type=101, send_dtmf_clock_rate=8000)
    previous.remote_rtp_host, previous.remote_rtp_port = remote
    client = SipCallClient(local_ip="127.0.0.1", local_name="HA", local_sip_port=5060, local_rtp_port=40002,
        supported_formats=[AudioFormat(8000, frame_ms=20), AudioFormat(48000, frame_ms=10)])
    client.dialog = previous
    body = (f"v=0\r\nc=IN IP4 127.0.0.1\r\nm=audio {remote[1]} RTP/AVP 110 126\r\n"
            "a=rtpmap:110 L16/48000/1\r\na=rtpmap:126 telephone-event/48000\r\na=fmtp:126 6\r\n"
            "a=ptime:10\r\na=sendrecv\r\n")
    request = sip.parse_message(sip.build_request("INVITE", "sip:HA@127.0.0.1", [
        ("Content-Type", "application/sdp"), ("Contact", "<sip:Door@127.0.0.1>")], body.encode()))
    candidate, _ = client._answer_remote_offer(request)
    relay = SipRtpRelay(left=dialog_rtp_peer(previous), right=dialog_rtp_peer(previous), left_port=0, right_port=0)
    relay.right_transport = output
    session = SimpleNamespace(call_id=previous.call_id, live=True, generation=1)
    registry = SimpleNamespace(get_session=lambda _: session, is_generation_current=lambda *_: True,
                               resource_for=lambda _call, kind: relay if kind == "relay" else None)
    monkeypatch.setattr(bridge_media_updates, "call_registry", lambda _: registry)
    bridge_media_updates.BridgeMediaUpdateBinder(object()).attach_audio_side(client, relay,
        source_call_id=session.call_id, side="right")
    prepared = await client.on_media_update(previous, candidate, "INVITE")
    assert prepared is not None
    original_peer = relay.right
    digit = asyncio.create_task(relay._send_dtmf_event("right", original_peer, "5", duration_ms=40))
    commit = None
    try:
        observed = [await asyncio.wait_for(packets.get(), 1)]
        async def apply_before_deadline():
            async with asyncio.timeout(.02 if interruption == "deadline" else 1):
                await prepared.commit()
        commit = asyncio.create_task(apply_before_deadline())
        await asyncio.sleep(0)
        assert not commit.done() and relay.right is original_peer
        if interruption == "cancel":
            commit.cancel()
            with pytest.raises(asyncio.CancelledError):
                await commit
        elif interruption == "deadline":
            with pytest.raises(TimeoutError):
                await commit
        elif interruption == "close":
            await relay.stop()
            with pytest.raises(RuntimeError, match="closed"):
                await commit
            await asyncio.wait_for(digit, 1)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(packets.get(), .08)
            assert relay.right is original_peer
            assert all(not lock.locked() for lock in relay._dtmf_locks.values())
            return
        await asyncio.wait_for(digit, 1)
        async with asyncio.timeout(1):
            while sum(packet[5] for packet in observed) < 3:
                observed.append(await packets.get())
        if interruption == "none":
            await asyncio.wait_for(commit, 1)
        assert {packet[0] for packet in observed} == {101}
        assert len({packet[2] for packet in observed}) == len({packet[3] for packet in observed}) == 1
        assert [packet[6] for packet in observed if packet[5]] == [320] * 3
        assert all(not lock.locked() for lock in relay._dtmf_locks.values())
        assert not any(task.get_name() in {"voip-media-dtmf-barrier", "voip-media-relay-stop"}
                       for task in asyncio.all_tasks() if not task.done())
        if interruption != "none":
            assert relay.right is original_peer
            return
        assert relay.right is not original_peer
        digit = asyncio.create_task(relay._send_dtmf_event("right", relay.right, "6", duration_ms=40))
        following = [await asyncio.wait_for(packets.get(), 1)]
        await asyncio.wait_for(digit, 1)
        async with asyncio.timeout(1):
            while sum(packet[5] for packet in following) < 3:
                following.append(await packets.get())
        assert {packet[0] for packet in following} == {126}
        assert {packet[3] for packet in following}.isdisjoint({packet[3] for packet in observed})
        assert [packet[6] for packet in following if packet[5]] == [1920] * 3
    finally:
        for task in (commit, digit):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(task for task in (commit, digit) if task is not None), return_exceptions=True)
        output.close()
        inbox.close()


async def test_two_owner_barrier_uses_one_order_and_releases_on_cancellation():
    from custom_components.voip_stack.sip_rtp_bridge import audio_reconfiguration_guard, RtpPeer
    pcmu = sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20)
    def relay():
        return SipRtpRelay(left=RtpPeer("127.0.0.1", 10000, 0, pcmu.audio_format, rtp_format=pcmu, send_rtp_format=pcmu),
                           right=RtpPeer("127.0.0.1", 10002, 0, pcmu.audio_format, rtp_format=pcmu, send_rtp_format=pcmu), left_port=0, right_port=0)
    first, second = relay(), relay()
    entered = asyncio.Queue()
    release = asyncio.Event()
    async def commit(*owners):
        async with audio_reconfiguration_guard(*owners):
            entered.put_nowait(owners)
            await release.wait()
    one = asyncio.create_task(commit(first, second))
    two = asyncio.create_task(commit(second, first))
    try:
        await asyncio.wait_for(entered.get(), 1)
        assert entered.empty()
        one.cancel()
        two.cancel()
        await asyncio.gather(one, two, return_exceptions=True)
        assert all(not lock.locked() for owner in (first, second) for lock in owner._dtmf_locks.values())
        async with audio_reconfiguration_guard(second, first, deadline=asyncio.get_running_loop().time()+.1):
            assert all(lock.locked() for owner in (first, second) for lock in owner._dtmf_locks.values())
    finally:
        for task in (one, two):
            task.cancel()
        await asyncio.gather(one, two, return_exceptions=True)
