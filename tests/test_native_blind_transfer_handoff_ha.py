"""Standard REFER keeps the opposite native SIP/RTP leg in its existing owner."""

import asyncio
import socket
import struct
import re
from io import BytesIO
from types import SimpleNamespace

from PIL import Image, ImageDraw

import pytest

from custom_components.voip_stack.core import rtp, sip, sdp, video_rtp
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.sip_transfer import SipReferTarget
from custom_components.voip_stack.sip_client import SipCallClient, SipDatagramQueueProtocol
from custom_components.voip_stack.sip_registrar import SipAccount
from .test_trunk_calls_ha import trunks as trunks, lab as lab
from .test_udp_outbound_flow_ha import register_phone, PASSWORD

pytestmark = pytest.mark.ha


async def until(predicate, seconds=7):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.01)


def attach_video_peer(monkeypatch, phone, video_socket, encoding, direction, refused_refers, refer_status):
    """An independent SIP peer authors video answers and rejects delegated REFER."""
    respond = phone.respond
    receive = phone.datagram_received

    def answer(request, address, status, body=b""):
        if request.method == "INVITE" and status == 200 and body and b"m=video " in request.body:
            # RTP payload numbers are scoped to one media section. Audio may
            # legally use the same number for telephone-event.
            video_section = next(section for section in re.split(rb"(?m)^m=", request.body)
                                 if section.startswith(b"video "))
            mapping = re.search(rb"a=rtpmap:(\d+) " + encoding.encode() + rb"/90000", video_section)
            assert mapping is not None, request.body
            payload = int(mapping[1])
            video = (f"m=video {video_socket.getsockname()[1]} RTP/AVP {payload}\r\n"
                     f"a=rtpmap:{payload} {encoding}/90000\r\na={direction}\r\n")
            if encoding == "H264":
                offered = re.search(rb"a=fmtp:" + str(payload).encode() + rb" ([^\r\n]+)", video_section)
                video += f"a=fmtp:{payload} " + (offered[1].decode() if offered else "packetization-mode=1;profile-level-id=42e01f") + "\r\n"
            body += video.encode()
        respond(request, address, status, body)

    def dispatch(raw, address):
        message = sip.parse_message(raw)
        if message.method != "REFER":
            receive(raw, address)
            return
        refused_refers.append(message)
        headers = [(name, message.header(name)) for name in ("Via", "From", "To", "Call-ID", "CSeq")]
        phone.transport.sendto(sip.build_response(refer_status,
            "Method Not Allowed" if refer_status == 405 else "Not Implemented", headers), address)

    monkeypatch.setattr(phone, "respond", answer)
    monkeypatch.setattr(phone, "datagram_received", dispatch)


def color_bars_jpeg():
    picture = Image.new("RGB", (160, 120), "red")
    draw = ImageDraw.Draw(picture)
    draw.rectangle((53, 0, 106, 119), fill="lime")
    draw.rectangle((107, 0, 159, 119), fill="blue")
    output = BytesIO()
    picture.save(output, format="JPEG", quality=85, subsampling=2, optimize=False)
    return output.getvalue()


async def send_jpeg(loop, sock, port, picture, *, frames=1, ssrc=0x3434):
    sequence, timestamp = 10, 90000
    for _ in range(frames):
        packets = list(video_rtp.packetize_jpeg(picture, payload_type=26,
            sequence=sequence, timestamp=timestamp, ssrc=ssrc))
        for packet in packets:
            await loop.sock_sendto(sock, rtp.build_packet(packet), ("127.0.0.1", port))
        sequence = rtp.next_sequence(packets[-1].sequence)
        timestamp = rtp.next_timestamp(timestamp, 18000)
        if frames > 1:
            await asyncio.sleep(.2)


async def receive_jpeg(loop, sock):
    decoder = video_rtp.JpegDepacketizer()
    async with asyncio.timeout(3):
        while True:
            raw = await loop.sock_recv(sock, 65536)
            if len(raw) < 12 or raw[0] >> 6 != 2 or raw[1] & 127 != 26:
                continue
            frame = decoder.push(rtp.parse_packet(raw))
            if frame is not None:
                image = Image.open(BytesIO(frame.data)).convert("RGB")
                assert image.size == (160, 120)
                for x, channel in ((26, 0), (80, 1), (133, 2)):
                    pixel = image.getpixel((x, 60))
                    assert pixel[channel] > 120 and pixel[channel] > max(pixel[i] for i in range(3) if i != channel) + 80
                return


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("transferor,video_case,refer_status", [
    ("source", None, 501), ("destination", None, 405),
    ("source", "jpeg", 501), ("destination", "jpeg", 405),
    ("source", "transcoded", 501),
])
async def test_refer_then_immediate_bye_retains_opposite_wire_and_duplex_media(
    trunks, monkeypatch, transferor, video_case, refer_status, tmp_path,
):
    lab = trunks
    registry = lab.calls
    registrar = registry.component("registrar")
    registrar.enabled = True
    account = SipAccount("native-source", "Native source", PASSWORD)
    from custom_components.voip_stack.phone_config import _add_phone_subentry, sync_registry_from_entry
    from custom_components.voip_stack.store import config_entry, sip_accounts
    entry = config_entry(lab.hass)
    _add_phone_subentry(lab.hass, entry, data={
        "kind": "sip_account", "name": account.display_name,
        "username": account.username, "password": account.password,
        "username_source": "user", "video_enabled": bool(video_case),
    }, title=account.display_name)
    sync_registry_from_entry(lab.hass, entry)
    source = lab.runtime.endpoints.by_username(account.username)
    assert source is not None and source.endpoint_id == account.username
    registrar.update_accounts(sip_accounts(lab.hass))
    from custom_components.voip_stack.config_entry_runtime import async_config_entry_updated
    await async_config_entry_updated(lab.hass, entry)
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert registrar.accounts[account.username.casefold()].password == account.password
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.1", 0))
    audio.setblocking(False)
    caller = SipCallClient(local_ip="127.0.0.1", local_name=account.username,
        local_uri_user=account.username, local_sip_port=0, local_rtp_port=audio.getsockname()[1],
        supported_formats=[AudioFormat(16000, frame_ms=20)],
        username=account.username, password=account.password)
    caller.transport, caller.protocol = await lab.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(caller.queue), local_addr=("127.0.0.1", 0))
    caller.local_sip_port = caller.transport.get_extra_info("sockname")[1]
    target, transfer = lab.phones[1], None
    peer_lifetime = None
    target.auto_answer = False
    accepted = asyncio.Event()
    transfer_finished = asyncio.Event()
    final_notify_statuses = []
    refused_refers, video_sockets, senders = [], [], []
    if video_case:
        from custom_components.voip_stack.const import CONF_SIP_VIDEO, CONF_VIDEO_TRANSCODING
        from .test_video_originate_unknown_ha import set_configured_video_policy
        lab.hass.config_entries.async_update_entry(entry, data={**entry.data,
            CONF_SIP_VIDEO: True, CONF_VIDEO_TRANSCODING: video_case == "transcoded"})
        for username in (account.username, "bedroom0", "bedroom1"):
            set_configured_video_policy(lab, username, True)
        lab.runtime.transport_config[CONF_SIP_VIDEO] = True
        lab.runtime.transport_config[CONF_VIDEO_TRANSCODING] = video_case == "transcoded"
        for endpoint in lab.calls.component("udp_listener")._dialog_endpoints():
            endpoint.enable_video = True
            endpoint.enable_video_transcoding = video_case == "transcoded"
        for host in ("127.0.0.1", "127.0.0.1", "127.0.0.1"):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind((host, 0))
            sock.setblocking(False)
            video_sockets.append(sock)
        for index, phone in enumerate(lab.phones):
            encoding = "H264" if video_case == "transcoded" and index == 0 else "JPEG"
            direction = ("recvonly" if index == 0 else "sendonly") if video_case == "transcoded" else "sendrecv"
            attach_video_peer(monkeypatch, phone, video_sockets[index + 1], encoding, direction,
                refused_refers, refer_status)
            next(entry for entry in lab.roster if entry.id == f"bedroom{index}").metadata["sip_video_codec"] = encoding.lower()
        lab.runtime.directory.replace(lab.roster)
        caller.local_video_rtp_port = video_sockets[0].getsockname()[1]
        caller.video_formats = (sdp.RtpVideoFormat(payload_type=26, encoding="JPEG"),)
        caller.video_format = caller.video_formats[0]
        caller.video_direction = "sendonly" if video_case == "transcoded" else "sendrecv"
    try:
        async def receive_registration(_method=None, *, status=None):
            async with asyncio.timeout(2):
                while True:
                    raw, address = await caller.queue.get()
                    message = sip.parse_message(raw)
                    if message.method == "OPTIONS":
                        caller._send_response_to_request(message, *address, 200, "OK")
                    elif message.is_response and (status is None or message.status_code == status):
                        return message, address
        registering = SimpleNamespace(address=("127.0.0.1", caller.local_sip_port),
            transport=caller.transport, receive=receive_registration)
        binding = await register_phone(SimpleNamespace(a=registering, registrar=registrar, lab=lab,
            listener=("127.0.0.1", lab.runtime.transport_config["sip_port"])),
            username=account.username, outbound=False, discover=False)
        assert binding.source_host == "127.0.0.1" and binding.source_port == caller.local_sip_port
        assert registrar.registered_contacts(account.username)
        state = await caller.invite(target="bedroom0", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        if state == "ringing":
            state = await caller.wait_for_final(timeout=3)
        assert state == "in_call"
        peer_lifetime = asyncio.create_task(caller.wait_for_dialog_termination(),
            name="independent-native-caller-dialog")
        if video_case:
            assert caller.signaling_transport == "TCP"
            assert caller.writer.get_extra_info("sockname")[0] == caller.local_ip
        await until(lambda: bool(registry.sessions))
        original = registry.get_session(caller.dialog_ids.call_id)
        original_relay = registry.resource_for(original.call_id, "relay")
        original_video = original_relay.video_relay
        if video_case:
            assert original_video is not None
            original_video_transports = {id(item) for item in original_video._transports.values()}
            original_workers = {id(item) for item in original_video._transcode.transcoders.values()}
            assert bool(original_workers) is (video_case == "transcoded")
        old_destination = registry.bridge_link_for(original.call_id)
        retained_client = registry.sip_client_for(old_destination)
        uri = f'sip:bedroom1@127.0.0.1:{lab.runtime.transport_config["sip_port"]}'
        if transferor == "source" and caller.signaling_transport == "TCP":
            from custom_components.voip_stack.core.sip_transfer import SipTargetDialog
            from custom_components.voip_stack.sip_tcp_io import read_sip_stream_message

            # Knowing the real dialog tags on the same host is insufficient:
            # the alternate connection never authenticated that INVITE.
            source_host = caller._tcp_writer.writer.get_extra_info("sockname")[0]
            reader, foreign = await asyncio.open_connection("127.0.0.1",
                lab.runtime.transport_config["sip_port"], local_addr=(source_host, 0))
            try:
                foreign_port = foreign.get_extra_info("sockname")[1]
                assert foreign_port != caller.local_sip_port
                dialog_target = SipTargetDialog(caller.dialog_ids.call_id,
                    caller.dialog_ids.remote_tag, caller.dialog_ids.local_tag)
                request = sip.build_request("REFER", caller.dialog.remote_target_uri, [
                    ("Via", f"SIP/2.0/TCP {source_host}:{foreign_port};branch=z9hG4bK-foreign-refer"),
                    ("From", f"<{caller.dialog.local_uri}>;tag=foreign"),
                    ("To", f"<{caller.dialog.remote_target_uri}>"),
                    ("Call-ID", sip.make_call_id("foreign-refer")), ("CSeq", "1 REFER"),
                    ("Contact", f"<sip:{account.username}@{source_host}:{foreign_port};transport=tcp>"),
                    ("Refer-To", f"<{uri}>"), ("Target-Dialog", str(dialog_target)),
                ])
                foreign.write(request)
                await foreign.drain()
                raw = await asyncio.wait_for(read_sip_stream_message(reader), 2)
                assert raw is not None and sip.parse_message(raw).status_code == 403
                assert registry.get_session(original.call_id) is original and original.live
                assert registry.resource_for(original.call_id, "relay") is original_relay
                assert not target.invited.is_set()
            finally:
                foreign.close()
                await foreign.wait_closed()
        if transferor == "source":
            native_send = caller._send_in_dialog_request
            async def send(method, **kwargs):
                response = await native_send(method, **kwargs)
                if method == "REFER" and response is not None and response.status_code == 200:
                    accepted.set()
                return response
            monkeypatch.setattr(caller, "_send_in_dialog_request", send)
            transfer = asyncio.create_task(caller.refer(SipReferTarget(uri)))
            await asyncio.wait_for(accepted.wait(), 3)
            await caller.terminate(timeout=2)
        else:
            phone = lab.phones[0]
            receive = phone.datagram_received
            def dispatch(raw, address):
                message = sip.parse_message(raw)
                receive(raw, address)
                if message.method == "NOTIFY":
                    phone.respond(message, address, 200)
                    from custom_components.voip_stack.core.sip_transfer import parse_sipfrag_status
                    status = parse_sipfrag_status(message.body)
                    if status >= 200:
                        final_notify_statuses.append(status)
                        transfer_finished.set()
                if (message.status_code == 200 and message.header("CSeq") == "42 REFER"):
                    accepted.set()
                    phone.remote_hangup(cseq=43)
            monkeypatch.setattr(phone, "datagram_received", dispatch)
            invite = phone.last_request
            port = phone.transport.get_extra_info("sockname")[1]
            phone.transport.sendto(sip.build_request("REFER", str(sip.parse_sip_uri(invite.header("Contact"))), [
                ("Via", f"SIP/2.0/UDP 127.0.0.1:{port};branch=z9hG4bK-native-transfer"),
                ("From", invite.header("To") + ";tag=bedroom"), ("To", invite.header("From")),
                ("Call-ID", invite.header("Call-ID")), ("CSeq", "42 REFER"),
                ("Refer-To", f"<{uri}>"), ("Contact", f"<sip:bedroom0@127.0.0.1:{port}>")]), phone.remote)
            await asyncio.wait_for(accepted.wait(), 3)
        await asyncio.wait_for(target.invited.wait(), 4)
        assert not target.answered.is_set()
        assert any(session.live for session in registry.sessions.values())
        target.answer()
        if transferor == "destination":
            # The retained session can publish in_call before its media commit.
            # Final REFER success is the independent peer-visible commit barrier.
            await asyncio.wait_for(transfer_finished.wait(), 4)
            assert final_notify_statuses[-1] == 200
        await until(lambda: any(session.live and session.state == "in_call" and registry.bridge_link_for(session.call_id)
            not in {"", old_destination} for session in registry.sessions.values()))
        current = next(session for session in registry.sessions.values()
            if session.live and session.state == "in_call" and registry.bridge_link_for(session.call_id) not in {"", old_destination})
        relay = registry.resource_for(current.call_id, "relay")
        assert relay is original_relay
        if video_case:
            assert relay.video_relay is original_video
            assert {id(item) for item in original_video._transports.values()} == original_video_transports
            assert {id(item) for item in original_video._transcode.transcoders.values()} == original_workers
            assert not refused_refers, "Local forwarding must preserve the call without delegating REFER to the provider"
        if transferor == "source":
            assert current.call_id != original.call_id
            assert registry.source_sip_client_for(current.call_id) is retained_client
            assert (await asyncio.wait_for(transfer, 4)).accepted
        else:
            assert current is original
            assert caller.dialog is not None
        assert "bedroom1" in current.endpoint_claims
        # Independent native endpoints author PCM RTP in both directions.
        samples = struct.pack(">320h", *([1234, -2345] * 160))
        source_socket = lab.phones[0].rtp_socket if transferor == "source" else audio
        for side, sock in (("left", source_socket), ("right", target.rtp_socket)):
            peer = getattr(relay, side)
            packet = rtp.build_packet(rtp.RtpPacket(payload_type=peer.payload_type,
                sequence=100, timestamp=32000, ssrc=0x10203040 + (side == "right"), payload=samples))
            await lab.hass.loop.sock_sendto(sock, packet, ("127.0.0.1", getattr(relay, f"{side}_port")))
        await until(lambda: relay.left_tx_packets > 0 and relay.right_tx_packets > 0)
        if video_case:
            video = relay.video_relay
            picture = color_bars_jpeg()
            if video_case == "jpeg":
                source_video = video_sockets[1] if transferor == "source" else video_sockets[0]
                for side, sender_socket, receiver_socket, ssrc in (
                    ("left", source_video, video_sockets[2], 0x3434),
                    ("right", video_sockets[2], source_video, 0x4545),
                ):
                    await send_jpeg(lab.hass.loop, sender_socket, getattr(video, f"{side}_port"), picture, ssrc=ssrc)
                    await receive_jpeg(lab.hass.loop, receiver_socket)
                assert not video.transcoding
            else:
                from .test_sip_phone_transfers_ha import assert_h264_color_bars
                assert video.transcodes_from("right") and not video.transcodes_from("left")
                sender = asyncio.create_task(send_jpeg(lab.hass.loop, video_sockets[2], video.right_port,
                    picture, frames=25))
                senders.append(sender)
                captured = []
                async with asyncio.timeout(8):
                    while len(captured) < 20:
                        raw = await lab.hass.loop.sock_recv(video_sockets[1], 65536)
                        if len(raw) >= 12 and raw[0] >> 6 == 2 and raw[1] & 127 == video.left.send_format.payload_type:
                            captured.append(raw)
                await asyncio.to_thread(assert_h264_color_bars, captured, tmp_path / "retained-native.h264")
            assert not video._staged_peers
        target.remote_hangup(cseq=7)
        await until(lambda: not registry.sessions)
        assert not registry.endpoint_claims_snapshot()
        assert not lab.runtime.rtp_port_pool.get("used")
    finally:
        for sender in senders:
            sender.cancel()
        await asyncio.gather(*senders, return_exceptions=True)
        if transfer is not None and not transfer.done():
            transfer.cancel()
            await asyncio.gather(transfer, return_exceptions=True)
        await caller.close()
        if peer_lifetime is not None:
            await asyncio.gather(peer_lifetime, return_exceptions=True)
        audio.close()
        for sock in video_sockets:
            sock.close()
