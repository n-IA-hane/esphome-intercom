"""Contracts for the deterministic SIP media qualification peer."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "sip_video_peer.py"
SOFTPHONE_MATRIX = ROOT / "tools" / "ha_softphone_matrix.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("sip_video_peer", TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load SIP media qualification peer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_audio_offer_can_express_hold_and_resume_directions() -> None:
    peer = _load_tool()
    common = {
        "local_ip": "127.0.0.1",
        "audio_port": 40000,
        "video_port": 0,
        "codec": "audio",
        "direction": "sendrecv",
        "video_profile": "RTP/AVP",
    }

    held = peer._offer(**common, audio_direction="sendonly")
    resumed = peer._offer(**common, audio_direction="sendrecv")

    assert b"a=sendonly\r\n" in held
    assert b"a=sendrecv\r\n" not in held
    assert b"a=sendrecv\r\n" in resumed


def test_video_removal_offer_keeps_rejected_media_without_rtcp_port() -> None:
    peer = _load_tool()

    offer = peer._offer(
        local_ip="127.0.0.1",
        audio_port=40000,
        video_port=0,
        codec="vp8",
        direction="inactive",
        video_profile="RTP/AVP",
    )

    assert b"m=video 0 RTP/AVP 103\r\n" in offer
    assert b"a=inactive\r\n" in offer
    assert b"a=rtcp:1\r\n" not in offer


def test_dahua_profile_offer_uses_vendor_pcm_and_keeps_video() -> None:
    peer = _load_tool()

    offer = peer._offer(
        local_ip="127.0.0.1",
        audio_port=40000,
        video_port=40002,
        codec="h264",
        direction="sendrecv",
        video_profile="RTP/AVP",
        audio_codec="dahua-pcm",
    )

    assert b"m=audio 40000 RTP/AVP 97 101\r\n" in offer
    assert b"a=rtpmap:97 PCM/16000\r\n" in offer
    assert b"a=ptime:20\r\n" in offer
    assert b"PCMA/8000" not in offer
    assert b"L16/" not in offer
    assert b"m=video 40002 RTP/AVP 102\r\n" in offer
    assert b"a=rtpmap:102 H264/90000\r\n" in offer


def test_dahua_profile_request_identity_is_explicit() -> None:
    peer = _load_tool()

    headers = peer._request_headers(
        method="INVITE",
        local_ip="127.0.0.1",
        local_port=5062,
        local_user="100",
        local_display_name="Dio Cane",
        remote_uri="sip:9901@127.0.0.1:5060",
        call_id="dahua-sim",
        local_tag="from-tag",
        cseq=1,
        branch="z9hG4bKdahua",
        user_agent="Dahua UAC/3.0",
    )

    assert ("User-Agent", "Dahua UAC/3.0") in headers
    assert (
        "From",
        '"Dio Cane" <sip:100@127.0.0.1:5062>;tag=from-tag',
    ) in headers
    assert ("Supported", "from-change") in headers
    assert any(name == "Allow" and "UPDATE" in value for name, value in headers)


def test_remote_uri_percent_encodes_endpoint_names_with_spaces() -> None:
    peer = _load_tool()

    assert peer._remote_uri("Waveshare P4 Touch", "192.0.2.1", 5060) == (
        "sip:Waveshare%20P4%20Touch@192.0.2.1:5060;transport=udp"
    )


def test_p4_l16_profile_matches_firmware_packet_cadence() -> None:
    peer = _load_tool()

    offer = peer._offer(
        local_ip="127.0.0.1",
        audio_port=40000,
        video_port=40002,
        codec="h264",
        direction="sendrecv",
        video_profile="RTP/AVP",
        audio_codec="l16-16k",
    )

    assert b"a=rtpmap:96 L16/16000/1\r\n" in offer
    assert b"a=ptime:16\r\n" in offer
    assert peer.AUDIO_PROFILES["l16-16k"]["frame_samples"] == 256
    assert peer.AUDIO_PROFILES["l16-16k"]["frame_bytes"] == 512


def test_h264_peer_repeats_parameter_sets_at_random_access_points() -> None:
    peer = _load_tool()

    encoder_args = peer.VIDEO_PROFILES["h264"]["args"]

    assert "-x264-params" in encoder_args
    assert any("repeat-headers=1" in value for value in encoder_args)


def test_audio_hold_mode_rejects_video_qualification() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(TOOL),
            "--codec",
            "vp8",
            "--audio-hold-after",
            "1",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )

    assert completed.returncode == 2
    assert "audio hold qualification requires --codec audio" in completed.stderr


def test_softphone_matrix_uses_the_public_device_selector() -> None:
    source = SOFTPHONE_MATRIX.read_text()

    assert '"device_id": phone_device_id' in source
    assert '{"destination": "Codex", "endpoint_id": "default"}' not in source


def test_media_peer_requires_bye_acknowledgement() -> None:
    source = TOOL.read_text()

    assert '"bye_response_status": None' in source
    assert 'raise TimeoutError("SIP BYE was not acknowledged")' in source


def test_media_peer_acks_non_successful_invite_finals() -> None:
    source = TOOL.read_text()
    failure = source[
        source.index("if message.status_code >= 300:") :
        source.index('raise RuntimeError(', source.index("if message.status_code >= 300:"))
    ]

    assert 'sip.build_request(' in failure
    assert '"ACK"' in failure
    assert "branch=invite_branch" in failure
    assert 'result["failure_ack_sent"] = True' in failure


def test_media_peer_cleans_up_after_rejected_initial_invite(
    tmp_path: Path,
) -> None:
    peer = _load_tool()

    async def run_rejection() -> tuple[str, dict]:
        loop = asyncio.get_running_loop()
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server.setblocking(False)
        server.bind(("127.0.0.1", 0))
        output = tmp_path / "rejected.json"
        args = types.SimpleNamespace(
            host="127.0.0.1",
            port=server.getsockname()[1],
            target="rejected",
            user="caller",
            display_name="Rejected caller",
            expect_connected_name="",
            user_agent="VoIP-Stack-Test",
            local_ip="127.0.0.1",
            audio_codec="pcma",
            codec="audio",
            direction="sendrecv",
            video_profile="RTP/AVP",
            answer_timeout=1.0,
            allow_audio_only=False,
            add_video_after=-1,
            activate_video_after=-1,
            expect_reinvite_status=0,
            remove_video_after=-1,
            readd_video_after=-1,
            audio_hold_after=-1,
            audio_hold_seconds=2,
            duration=1.0,
            audio_file="",
            video_file="",
            video_rx_file="",
            out=str(output),
        )

        async def reject() -> str:
            raw, address = await loop.sock_recvfrom(server, 65535)
            request = peer.sip.parse_message(raw)
            await loop.sock_sendto(
                server,
                peer.sip.build_response(
                    403,
                    "Forbidden",
                    peer._response_headers(request),
                ),
                address,
            )
            ack, _address = await loop.sock_recvfrom(server, 65535)
            return peer.sip.parse_message(ack).method or ""

        try:
            server_task = asyncio.create_task(reject())
            try:
                await peer.async_main(args)
            except RuntimeError as err:
                assert "403 Forbidden" in str(err)
            else:
                raise AssertionError("rejected INVITE unexpectedly succeeded")
            return await server_task, json.loads(output.read_text())
        finally:
            server.close()

    ack_method, result = asyncio.run(run_rejection())

    assert ack_method == "ACK"
    assert result["failure_ack_sent"] is True
    assert result["error"].startswith("RuntimeError: SIP call failed")


def test_media_peer_completes_cancel_transaction_and_acks_invite_final() -> None:
    peer = _load_tool()

    async def run_transaction() -> tuple[dict, str]:
        loop = asyncio.get_running_loop()
        client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for sock in (client, server):
            sock.setblocking(False)
            sock.bind(("127.0.0.1", 0))
        call_id = "cancel-transaction@127.0.0.1"
        result: dict = {}

        async def answer_cancel() -> str:
            raw, address = await loop.sock_recvfrom(server, 65535)
            request = peer.sip.parse_message(raw)
            assert request.method == "CANCEL"
            headers = peer._response_headers(request)
            await loop.sock_sendto(
                server,
                peer.sip.build_response(200, "OK", headers),
                address,
            )
            invite_headers = [
                (name, "1 INVITE" if name == "CSeq" else value)
                for name, value in headers
            ]
            await loop.sock_sendto(
                server,
                peer.sip.build_response(
                    487,
                    "Request Terminated",
                    invite_headers,
                ),
                address,
            )
            ack, _address = await loop.sock_recvfrom(server, 65535)
            return peer.sip.parse_message(ack).method or ""

        try:
            server_task = asyncio.create_task(answer_cancel())
            await peer._cancel_pending_invite(
                client,
                server.getsockname(),
                local_ip="127.0.0.1",
                local_port=client.getsockname()[1],
                local_user="caller",
                local_display_name="Caller Name",
                remote_uri=f"sip:2600@127.0.0.1:{server.getsockname()[1]}",
                call_id=call_id,
                local_tag="from-tag",
                invite_branch="z9hG4bKcancel",
                user_agent="VoIP-Stack-Test",
                result=result,
            )
            return result, await server_task
        finally:
            client.close()
            server.close()

    result, ack_method = asyncio.run(run_transaction())

    assert result["cancel_response_status"] == 200
    assert result["cancel_invite_status"] == 487
    assert result["cancel_ack_sent"] is True
    assert result["cancel_transaction_complete"] is True
    assert ack_method == "ACK"


def test_jpeg_capture_records_complete_rfc2435_frames_without_ffmpeg(
    tmp_path: Path,
) -> None:
    peer = _load_tool()
    quantizers = bytes(range(1, 129))
    frame = (
        peer.video_rtp._jpeg_interchange_header(
            jpeg_type=1,
            width_blocks=40,
            height_blocks=30,
            quantizers=quantizers,
            dri=0,
        )
        + (b"\x11\x22\x33\x44" * 400)
        + b"\xff\xd9"
    )
    packets = peer.video_rtp.packetize_jpeg(
        frame,
        payload_type=26,
        sequence=65534,
        timestamp=9000,
        ssrc=7,
        max_payload=220,
    )
    output = tmp_path / "capture.mjpeg"
    counters = {
        "video_rx_frames": 0,
        "video_rx_frame_bytes": 0,
        "video_rx_marker_packets": 0,
        "video_rx_dropped_access_units": 0,
        "video_rx_capture_invalid_payloads": 0,
    }

    recorder = peer._JpegRtpRecorder(str(output))
    for packet in packets:
        recorder.push(packet, counters)
    recorder.close(counters)

    assert output.read_bytes() == frame
    assert counters["video_rx_frames"] == 1
    assert counters["video_rx_frame_bytes"] == len(frame)
    assert counters["video_rx_marker_packets"] == 1
    assert counters["video_rx_dropped_access_units"] == 0
    assert counters["video_rx_capture_invalid_payloads"] == 0


def test_jpeg_capture_rejects_wrong_payload_type(tmp_path: Path) -> None:
    peer = _load_tool()
    output = tmp_path / "capture.mjpeg"
    counters = {
        "video_rx_frames": 0,
        "video_rx_frame_bytes": 0,
        "video_rx_marker_packets": 0,
        "video_rx_dropped_access_units": 0,
        "video_rx_capture_invalid_payloads": 0,
    }
    recorder = peer._JpegRtpRecorder(str(output))

    recorder.push(
        peer.rtp.RtpPacket(103, 1, 9000, 7, b"not-jpeg", marker=True),
        counters,
    )
    recorder.close(counters)

    assert output.read_bytes() == b""
    assert counters["video_rx_frames"] == 0
    assert counters["video_rx_marker_packets"] == 0
    assert counters["video_rx_capture_invalid_payloads"] == 1


def test_jpeg_capture_path_does_not_start_ffmpeg_receiver() -> None:
    source = TOOL.read_text()

    assert 'if args.codec == "jpeg":' in source
    assert 'result["video_rx_capture_backend"] = "rfc2435"' in source
