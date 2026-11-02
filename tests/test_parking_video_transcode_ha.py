"""Real owned H264/JPEG media crosses the retained parking video owner."""

import asyncio
from collections import defaultdict
from io import BytesIO
import socket
import subprocess
from types import SimpleNamespace

import pytest
from PIL import Image

from .test_parking_lot_ha import registered_phones, trunks, lab, custom_integrations
from .test_sip_phone_transfers_ha import assert_h264_color_bars
from .test_udp_outbound_flow_ha import register_phone, PASSWORD

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"video": ("transfer_a", "transfer_b")}], indirect=True)
@pytest.mark.parametrize("jpeg_fps,enabled", [(10, True), (25, True), (10, False)])
async def test_parked_h264_source_jpeg_pickup_uses_two_owned_converters(
    registered_phones, trunks, monkeypatch, tmp_path, jpeg_fps, enabled, socket_enabled,
):
    from custom_components.voip_stack.const import CONF_VIDEO_TRANSCODING
    from custom_components.voip_stack.core import rtp, sdp, video_rtp
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.sip_client import SipCallClient
    from custom_components.voip_stack.sip_registrar import SipAccount
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.endpoint_routing import assemble_directory_entries
    from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay
    from custom_components.voip_stack import video_transcoder, media_ports
    from custom_components.voip_stack.store import config_entry

    runtime = trunks
    runtime.runtime.transport_config[CONF_VIDEO_TRANSCODING] = enabled
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {"enabled": True}})
    captured = defaultdict(list)
    spawned = defaultdict(list)
    native_spawn = video_transcoder.asyncio.create_subprocess_exec

    async def observe_spawn(*arguments, **kwargs):
        process = await native_spawn(*arguments, **kwargs)
        if "-c:v" in arguments:
            codec = arguments[arguments.index("-c:v") + 1]
            spawned[codec].append(process)
            assert sum(item.returncode is None for item in spawned[codec]) <= 1
        return process

    monkeypatch.setattr(video_transcoder.asyncio, "create_subprocess_exec", observe_spawn)
    original_output = SipVideoRtpRelay.handle_transcoded_rtp

    def observe_output(owner, side, raw):
        before = owner.transcoded_output_packets
        original_output(owner, side, raw)
        if owner.transcoded_output_packets > before and len(captured[side]) < 512:
            captured[side].append(bytes(raw))

    monkeypatch.setattr(SipVideoRtpRelay, "handle_transcoded_rtp", observe_output)
    caller, operator, _unused = registered_phones

    async def wait_for(predicate, timeout=8):
        async with asyncio.timeout(timeout):
            while not predicate():
                for phone in registered_phones:
                    phone.read()
                await asyncio.sleep(.01)

    await asyncio.to_thread(caller.dial, "transfer_b")
    await wait_for(lambda: any(s.state == "in_call" for s in runtime.calls.sessions.values()))
    session = next(iter(runtime.calls.sessions.values()))
    original = runtime.calls.resource_for(session.call_id, "relay")
    video, audio = original.video_relay, original.left_transport
    assert video is not None
    await wait_for(lambda: video.left_tx_packets > 3 and video.right_tx_packets > 3)
    ports = tuple(video._transports[key].get_extra_info("sockname") for key in sorted(video._transports))
    runtime.runtime.endpoints.update("transfer_b", device_id="owned-video-operator")
    await runtime.hass.services.async_call("voip_stack", "park", {"device_id": "owned-video-operator"}, blocking=True, return_response=True)
    lot = get_parking_lot(runtime.hass)
    await wait_for(lambda: "701" in lot.calls and not runtime.runtime.endpoints.get("transfer_b").active_call_id)
    media = runtime.calls.resource_for(session.call_id, "relay")
    assert media.transport is audio and media.video_relay is video
    assert video.left.video_format.direction == video.right.video_format.direction == "inactive"
    assert not video._transcode.transcoders

    # The existing H264 fixture produces 5fps. These are JPEG input rates and
    # receiver capabilities, never a claim of 10/25fps panel presentation.
    picture = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
        "color=red:size=160x120,drawbox=x=53:y=0:w=54:h=120:color=lime:t=fill,drawbox=x=107:y=0:w=53:h=120:color=blue:t=fill",
        "-frames:v", "1", "-c:v", "mjpeg", "-huffman", "default", "-force_duplicated_matrix", "1",
        "-q:v", "5", "-pix_fmt", "yuvj420p", "-threads", "1", "-f", "image2pipe", "pipe:1"], capture_output=True, check=True, timeout=5).stdout
    assert 0 < len(picture) < 1024 * 1024
    (tmp_path / "owned-rgb.jpg").write_bytes(picture)
    audio_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio_socket.bind(("127.0.0.1", 0))
    audio_socket.setblocking(False)
    for _ in range(32):
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        try:
            jpeg_socket, rtcp_socket = media_ports.bind_sip_video_sockets(port)
            break
        except OSError:
            continue
    else:
        pytest.fail("Could not bind an owned RTP/RTCP pair")
    sockets = [audio_socket, jpeg_socket, rtcp_socket]
    jpeg = sdp.RtpVideoFormat(payload_type=26, encoding="JPEG", max_framerate=jpeg_fps)
    username = "owned-jpeg-picker"
    registrar = runtime.calls.component("registrar")
    original_accounts = list(registrar.accounts.values())
    registrar.update_accounts([*original_accounts, SipAccount(username, username, PASSWORD)])
    runtime.runtime.endpoints.register(PhoneEndpoint(endpoint_id=username, name=username, username=username, kind=EndpointKind.SIP_ACCOUNT))
    picker = SipCallClient(local_ip="127.0.0.1", local_name=username, local_uri_user=username,
        local_sip_port=0, local_rtp_port=audio_socket.getsockname()[1], username=username,
        auth_username=username, password=PASSWORD, supported_formats=[AudioFormat(8000, "s16le", 1, 20)],
        local_video_rtp_port=jpeg_socket.getsockname()[1], video_formats=(jpeg,), video_direction="sendrecv")
    picker.audio_rtp_socket = audio_socket
    send_task = None
    watcher = None
    processes = []
    stopped = asyncio.Event()
    try:
        from custom_components.voip_stack.core import sip
        await picker.start()

        async def receive(_method=None, *, status=None):
            async with asyncio.timeout(2):
                while True:
                    raw, address = await picker.queue.get()
                    message = sip.parse_message(raw)
                    if message.method == "OPTIONS":
                        picker._send_response_to_request(message, *address, 200, "OK")
                    elif status is None or message.status_code == status:
                        return message, address

        phone = SimpleNamespace(address=("127.0.0.1", picker.local_sip_port), transport=picker.transport, receive=receive)
        binding = await register_phone(SimpleNamespace(a=phone, registrar=registrar, lab=runtime,
            listener=("127.0.0.1", runtime.runtime.transport_config["sip_port"])),
            username=username, outbound=False, discover=False)
        assert binding.source_port == picker.local_sip_port
        runtime.roster[:] = assemble_directory_entries(runtime.hass, [], registrar.registered_roster_entries())
        result = await picker.invite(target="701", remote_host="127.0.0.1", remote_sip_port=runtime.runtime.transport_config["sip_port"], timeout=3)
        if not enabled:
            assert result != "in_call"
            await wait_for(lambda: lot.calls["701"].claim is None)
            assert lot.calls["701"].session is session and session.live
            assert media.video_relay is video and media.transport is audio
            assert video.left.video_format.direction == video.right.video_format.direction == "inactive"
            assert not video._transcode.transcoders and not video._staged_peers
            return
        assert result == "in_call"
        watcher = asyncio.create_task(picker.wait_for_dialog_termination())
        await wait_for(lambda: not lot.calls)
        resumed = runtime.calls.resource_for(session.call_id, "relay")
        assert resumed.left_transport is audio and resumed.video_relay is video
        assert tuple(video._transports[key].get_extra_info("sockname") for key in sorted(video._transports)) == ports
        assert set(video._transcode.transcoders) == {"left", "right"}
        workers = tuple(video._transcode.transcoders.values())
        assert len({id(worker) for worker in workers}) == 2
        processes = [worker.process for worker in workers]
        assert all(process is not None and process.returncode is None for process in processes)

        async def send_jpeg():
            sequence, timestamp = 12000, 90000
            target = (picker.dialog.remote_video_rtp_host, picker.dialog.remote_video_rtp_port)
            send_fps = min(jpeg_fps, picker.dialog.send_video_format.max_framerate or jpeg_fps)
            while not stopped.is_set():
                if picker.dialog.local_video_direction not in {"sendonly", "sendrecv"}:
                    await asyncio.sleep(1 / send_fps)
                    continue
                packets = video_rtp.packetize_jpeg(picture, payload_type=26, sequence=sequence, timestamp=timestamp, ssrc=123456)
                for packet in packets:
                    await asyncio.get_running_loop().sock_sendto(jpeg_socket, rtp.build_packet(packet), target)
                sequence = rtp.next_sequence(packets[-1].sequence)
                timestamp = rtp.next_timestamp(timestamp, int(90000 / send_fps))
                await asyncio.sleep(1 / send_fps)

        send_task = asyncio.create_task(send_jpeg())
        depacketizer, images = video_rtp.JpegDepacketizer(), []
        async with asyncio.timeout(8):
            while len(images) < 2 or len(captured["right"]) < 15:
                raw = await asyncio.get_running_loop().sock_recv(jpeg_socket, 65536)
                if len(raw) < 12 or raw[0] >> 6 != 2 or raw[1] & 127 != 26:
                    continue
                complete = depacketizer.push(rtp.parse_packet(raw))
                if complete is not None:
                    images.append(complete.data)
        image = Image.open(BytesIO(images[-1])).convert("RGB").resize((160, 120))
        for x, channel in ((26, 0), (80, 1), (133, 2)):
            pixel = image.getpixel((x, 60))
            assert pixel[channel] > 120 and pixel[channel] > max(pixel[i] for i in range(3) if i != channel) + 80
        await asyncio.to_thread(assert_h264_color_bars, captured["right"], tmp_path / "jpeg-to-source.h264")
        assert video.left_tx_packets > 10 and video.right_tx_packets > 10
        left_worker = video._transcode.transcoders["left"]
        left_pid = left_worker.process.pid
        server_dialog = next(endpoint.active_dialogs[picker.dialog_ids.call_id]
            for endpoint in runtime.calls.component("udp_listener")._dialog_endpoints()
            if picker.dialog_ids.call_id in endpoint.active_dialogs)
        resume_start = 0
        for direction, active in (("recvonly", {"left"}), ("sendrecv", {"left", "right"})):
            retired = video._transcode.transcoders.get("right")
            retired_process = retired.process if retired is not None else None
            if direction == "sendrecv":
                resume_start = len(captured["right"])
            previous = picker.dialog
            candidate = await picker.async_prepare_video_reinvite(
                local_video_rtp_port=jpeg_socket.getsockname()[1], video_formats=(jpeg,),
                video_direction=direction, expected_dialog=previous, timeout=3)
            assert candidate is not None, (direction, picker.last_sip_status_code, picker.last_sip_reason)
            assert picker.commit_prepared_reinvite(previous, candidate)
            await wait_for(lambda: set(video._transcode.transcoders) == active
                and not video._staged_peers and not server_dialog.media_lock.locked()
                and (direction != "recvonly" or retired_process.returncode is not None))
            assert video._transcode.transcoders["left"] is left_worker
            assert left_worker.process.pid == left_pid
            assert media.video_relay is None and resumed.video_relay is video
            assert runtime.calls.resource_for(session.call_id, "relay") is resumed
        await wait_for(lambda: len(captured["right"]) >= resume_start + 8)
        await asyncio.to_thread(assert_h264_color_bars, captured["right"][resume_start:], tmp_path / "resumed-jpeg-to-source.h264")
        assert caller.read().count("Call established") == 1
    finally:
        stopped.set()
        if send_task is not None:
            send_task.cancel()
            await asyncio.gather(send_task, return_exceptions=True)
        if picker.dialog is not None:
            await picker.terminate(timeout=1)
        await picker.close()
        if watcher is not None:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        registrar.update_accounts(original_accounts)
        for sock in sockets:
            sock.close()
        await asyncio.to_thread(caller.hangup)
        await wait_for(lambda: not runtime.calls.sessions, timeout=5)
        assert not runtime.runtime.rtp_port_pool["used"]
        assert not runtime.calls.endpoint_claims_snapshot()
        assert not video._transcode.transcoders
        assert all(process.returncode is not None for process in processes)
        assert all(process.returncode is not None for group in spawned.values() for process in group)
