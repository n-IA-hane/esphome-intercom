"""Digest-gated UDP calls exercise the actual qualification peer transactions."""

import asyncio
import json
from pathlib import Path
import re
import socket
from types import SimpleNamespace

import pytest

from .test_sip_video_peer import _load_tool


def _credentials(tmp_path):
    directory = tmp_path / ".secrets"
    directory.mkdir(mode=0o700)
    path = directory / "peer.json"
    path.write_text(json.dumps({"username": "caller", "auth_username": "digest-user",
                                "password": "fixture-digest-password"}))
    path.chmod(0o600)
    return path


def _args(server, tmp_path, credentials, *, cancel=False):
    return SimpleNamespace(
        host="127.0.0.1", port=server.getsockname()[1], target="destination", user="caller",
        display_name="Digest caller", expect_connected_name="", user_agent="", local_ip="127.0.0.1",
        credentials_file=credentials, audio_codec="opus", codec="audio", direction="sendrecv",
        video_profile="RTP/AVP", answer_timeout=0.15 if cancel else 1.0, allow_audio_only=False,
        add_video_after=-1, activate_video_after=-1, expect_reinvite_status=0, remove_video_after=-1,
        readd_video_after=-1, audio_hold_after=-1 if cancel else 0, audio_hold_seconds=0.04,
        duration=0.8, audio_file="", audio_gain=1.0, video_file="", video_rx_file="", video_fps=5,
        answer_local_reinvite=False, local_reinvite_status=200, out=str(tmp_path / "peer-result.json"),
    )


async def _exercise(tmp_path, *, status, authenticated=True, cancel=False, video=False, reject=False, duplicate=False):
    peer = _load_tool()
    loop = asyncio.get_running_loop()
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    video_media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for sock in (server, media, video_media):
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
    args = _args(server, tmp_path, _credentials(tmp_path) if authenticated else None, cancel=cancel)
    if video:
        args.codec, args.direction, args.add_video_after, args.audio_hold_after = "jpeg", "recvonly", 0, -1
        args.user = None
    requests = []
    verified = []
    header = "Authorization" if status == 401 else "Proxy-Authorization"
    challenge_header = "WWW-Authenticate" if status == 401 else "Proxy-Authenticate"
    challenge = 'Digest realm="peer-test", nonce="fixed-test-nonce", algorithm=SHA-256, qop="auth-int"'

    async def echo_audio():
        while True:
            raw, address = await loop.sock_recvfrom(media, 4096)
            await loop.sock_sendto(media, raw, address)

    async def serve():
        while True:
            raw, address = await loop.sock_recvfrom(server, 65535)
            request = peer.sip.parse_message(raw)
            requests.append(request)
            headers = [(name, value + ";tag=server" if name == "To" and ";tag=" not in value else value)
                       for name, value in peer._response_headers(request)]
            if request.method == "ACK":
                if (not authenticated or (cancel and request.header("CSeq").startswith("2 "))
                        or (reject and request.header("CSeq") == "2 ACK")):
                    return
                continue
            if request.method == "CANCEL":
                await loop.sock_sendto(server, peer.sip.build_response(200, "OK", headers), address)
                final = [(name, value.replace(" CANCEL", " INVITE") if name == "CSeq" else value)
                         for name, value in headers]
                await loop.sock_sendto(server, peer.sip.build_response(487, "Request Terminated", final), address)
                continue
            assert request.method in {"INVITE", "BYE"}
            if not request.header(header):
                await loop.sock_sendto(server, peer.sip.build_response(
                    status, "Unauthorized" if status == 401 else "Proxy Authentication Required",
                    [*headers, (challenge_header, challenge)]), address)
                continue
            auth = peer._load("sip_auth")
            verified.append(auth.verify_digest_authorization(
                authorization_header=request.header(header), username="digest-user",
                password="fixture-digest-password", realm="peer-test", nonce="fixed-test-nonce",
                method=request.method, uri=request.uri, body=request.body,
                offered_algorithms=frozenset({"SHA-256"}), offered_qops=frozenset({"auth-int"}),
            ))
            if reject:
                await loop.sock_sendto(server, peer.sip.build_response(
                    status, "Unauthorized", [*headers, (challenge_header, challenge)]), address)
            elif cancel:
                await loop.sock_sendto(server, peer.sip.build_response(180, "Ringing", headers), address)
            elif request.method == "BYE":
                await loop.sock_sendto(server, peer.sip.build_response(200, "OK", headers), address)
                return
            else:
                if duplicate and request.header("CSeq") == "2 INVITE":
                    previous = [(name, value + ";tag=server" if name == "To" else value)
                                for name, value in peer._response_headers(requests[0])]
                    await loop.sock_sendto(server, peer.sip.build_response(
                        status, "Unauthorized", [*previous, (challenge_header, challenge)]), address)
                answer = re.sub(rb"m=audio \d+", f"m=audio {media.getsockname()[1]}".encode(), request.body)
                parsed_video = peer.sdp.parse_video_sdp(request.body) if b"m=video" in request.body else None
                if parsed_video is not None:
                    answer = re.sub(rb"m=video \d+", f"m=video {video_media.getsockname()[1]}".encode(), answer)
                    answer = answer.replace(b"a=recvonly", b"a=sendonly")
                await loop.sock_sendto(server, peer.sip.build_response(
                    200, "OK", [*headers, ("Content-Type", "application/sdp")], answer), address)
                if parsed_video is not None:
                    # This is a transaction/RTP transport witness, without a decoder claim.
                    packet = peer.rtp.build_packet(peer.rtp.RtpPacket(26, 1, 9000, 7, bytes(16), marker=True))
                    await loop.sock_sendto(video_media, packet, ("127.0.0.1", int(parsed_video["media_port"])))

    echo = asyncio.create_task(echo_audio())
    transaction = asyncio.create_task(serve())
    try:
        async with asyncio.timeout(4):
            if cancel:
                with pytest.raises(TimeoutError, match="not answered"):
                    await peer.async_main(args)
            elif not authenticated:
                with pytest.raises(RuntimeError, match=f"{status}"):
                    await peer.async_main(args)
            elif reject:
                with pytest.raises(ValueError, match="credentials were rejected"):
                    await peer.async_main(args)
            else:
                await peer.async_main(args)
            await transaction
        return requests, verified, json.loads(Path(args.out).read_text())
    finally:
        for task in (echo, transaction):
            task.cancel()
        await asyncio.gather(echo, transaction, return_exceptions=True)
        server.close()
        media.close()
        video_media.close()


@pytest.mark.parametrize("status", [401, 407])
def test_digest_udp_invite_hold_resume_and_bye_keep_identity_and_advance_cseq(tmp_path, capsys, status):
    requests, verified, result = asyncio.run(_exercise(tmp_path, status=status))
    assert result["ok"] is True and result["bye_response_status"] == 200
    assert result["hold_status"] == result["resume_status"] == 200
    attempts = [request for request in requests if request.method in {"INVITE", "BYE"}]
    assert [request.header("CSeq") for request in attempts] == [
        "1 INVITE", "2 INVITE", "3 INVITE", "4 INVITE", "5 INVITE", "6 INVITE", "7 BYE", "8 BYE"]
    assert len({request.header("Call-ID") for request in requests}) == 1
    assert len({request.header("From") for request in requests}) == 1
    assert len({peer_branch(request) for request in attempts}) == len(attempts)
    for request in attempts:
        if request.method == "INVITE":
            ack = next(item for item in requests if item.method == "ACK" and
                       item.header("CSeq").split()[0] == request.header("CSeq").split()[0])
            assert (peer_branch(ack) == peer_branch(request)) is (int(request.header("CSeq").split()[0]) % 2 == 1)
    assert len(verified) == 4
    assert [entry[2] for entry in verified] == [1, 2, 3, 4]
    assert result["audio_tx_packets"] > 0 and result["audio_rx_packets"] > 0
    output = capsys.readouterr()
    assert "fixture-digest-password" not in output.out + output.err + json.dumps(result)
    assert result["user_agent"] == "VoIP-Stack-Video-Lab"


def peer_branch(request):
    return next(value.split("=", 1)[1] for value in request.header("Via").split(";")
                if value.startswith("branch="))


@pytest.mark.parametrize("status", [401, 407])
def test_without_credentials_challenge_remains_unauthorized_and_is_acked(tmp_path, status):
    requests, verified, result = asyncio.run(_exercise(tmp_path, status=status, authenticated=False))
    assert [request.method for request in requests] == ["INVITE", "ACK"]
    assert not verified and result["ok"] is False
    assert result["sip_statuses"] == [status]


def test_cancel_after_authenticated_retry_uses_pending_invite_cseq_and_branch(tmp_path):
    requests, verified, result = asyncio.run(_exercise(tmp_path, status=401, cancel=True))
    assert len(verified) == 1 and result["cancel_transaction_complete"] is True
    cancel = next(request for request in requests if request.method == "CANCEL")
    retry = next(request for request in requests if request.header("CSeq") == "2 INVITE")
    assert cancel.header("CSeq") == "2 CANCEL"
    assert peer_branch(cancel) == peer_branch(retry)
    assert requests[-1].header("CSeq") == "2 ACK"


@pytest.mark.parametrize("status", [401, 407])
def test_video_reinvite_uses_authenticated_sequence_before_bye(tmp_path, status):
    requests, verified, result = asyncio.run(_exercise(tmp_path, status=status, video=True))
    assert result["ok"] is True and result["reinvite_status"] == 200
    assert result["reinvite_statuses"] == [status, 200]
    assert result["video_rx_packets"] > 0
    attempts = [request.header("CSeq") for request in requests if request.method in {"INVITE", "BYE"}]
    assert attempts == ["1 INVITE", "2 INVITE", "3 INVITE", "4 INVITE", "5 BYE", "6 BYE"]
    assert len(verified) == 3
    assert "sip:caller@" in requests[0].header("From")


def test_credentials_are_private_and_password_is_not_represented(tmp_path):
    peer = _load_tool()
    path = _credentials(tmp_path)
    credentials = peer._load_credentials(path)
    assert credentials.username == "caller" and credentials.auth_username == "digest-user"
    assert "fixture-digest-password" not in repr(credentials)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        peer._load_credentials(path)


def test_rejected_credentials_are_bounded_and_each_final_challenge_is_acked(tmp_path):
    requests, verified, result = asyncio.run(_exercise(tmp_path, status=401, reject=True))
    assert [request.method for request in requests] == ["INVITE", "ACK", "INVITE", "ACK"]
    assert result["sip_statuses"] == [401, 401] and result["ok"] is False
    assert len(verified) == 1


def test_duplicate_final_challenge_is_acked_without_another_retry(tmp_path):
    requests, verified, result = asyncio.run(_exercise(tmp_path, status=401, duplicate=True))
    assert len([request for request in requests if request.header("CSeq") == "1 ACK"]) == 2
    assert result["authentication_retries"] == 4 and len(verified) == 4
