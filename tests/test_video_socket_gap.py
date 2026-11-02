"""Production video WS runner, independent UDP sender and FFmpeg oracle."""

import asyncio
from contextlib import asynccontextmanager
import json
import socket
import subprocess
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.ha


async def test_sps_pps_sent_in_websocket_gap_survive_to_next_reader(
    monkeypatch, socket_enabled, tmp_path
):
    from custom_components.voip_stack import video_ws_view as view
    from custom_components.voip_stack.core import rtp, sdp, video_rtp
    from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
    from custom_components.voip_stack.runtime_data import BrowserMediaRuntime
    from custom_components.voip_stack.sip_client import SipCallClient

    encoded = subprocess.check_output(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x120:r=10",
            "-frames:v",
            "2",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-profile:v",
            "baseline",
            "-level:v",
            "3.1",
            "-tune",
            "zerolatency",
            "-f",
            "h264",
            "pipe:1",
        ]
    )
    nals = video_rtp.split_annex_b(encoded)
    parameters = [n for n in nals if n[0] & 31 in {7, 8}]
    pictures = [n for n in nals if n[0] & 31 == 5]
    deltas = [n for n in nals if n[0] & 31 == 1]
    assert deltas
    assert [n[0] & 31 for n in parameters] == [7, 8] and pictures
    sockets = []
    for _ in range(4):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        sockets.append(sock)
    original, control, peer, feedback = sockets
    target = original.getsockname()
    control_target = control.getsockname()
    client = SipCallClient(
        local_ip="127.0.0.1",
        local_name="gap",
        local_sip_port=5060,
        local_rtp_port=0,
        video_rtp_socket=original,
        video_rtcp_socket=control,
    )
    registry = SipEndpointRuntime(allow_dark_sessions=True)
    registry.upsert("gap-call", state="in_call", owner="ha_softphone")
    registry.attach_sip_client("gap-call", "gap-call", client, state="in_call")
    media = BrowserMediaRuntime()
    store = {"call_id": "gap-call"}
    ended = asyncio.Event()
    monkeypatch.setattr(
        view, "require_runtime_data", lambda _: SimpleNamespace(media=media)
    )
    monkeypatch.setattr(view, "call_projection", lambda _: registry)
    monkeypatch.setattr(view, "_ha_softphone_store", lambda *_: store)
    monkeypatch.setattr(view, "_publish_ha_softphone_state", lambda *a, **k: None)
    monkeypatch.setattr(view, "debug_mode", lambda _: False)
    monkeypatch.setattr(
        view, "listen_for_media_call_end", lambda *a: (ended, lambda: None)
    )

    class WebSocket:
        def __init__(self):
            self.ready = asyncio.Event()
            self.finish = asyncio.Event()
            self.frames = asyncio.Queue()

        async def send_json(self, _):
            self.ready.set()

        async def send_bytes(self, data):
            self.frames.put_nowait(data)

        def force_close(self):
            self.finish.set()

        def __aiter__(self):
            return self

        async def __anext__(self):
            await self.finish.wait()
            raise StopAsyncIteration

    def session():
        return view._VideoMediaSession(
            call_id="gap-call",
            local_rtp_port=target[1],
            remote_rtp_host="127.0.0.1",
            remote_rtp_port=peer.getsockname()[1],
            remote_rtcp_host="127.0.0.1",
            remote_rtcp_port=feedback.getsockname()[1],
            remote_rtcp_mux=False,
            video_format=sdp.RtpVideoFormat(
                payload_type=103, profile_level_id="42e01f", packetization_mode=1
            ),
            local_direction="recvonly",
            rtp_socket=client.video_rtp_socket,
            rtcp_socket=client.video_rtcp_socket,
        )

    loop = asyncio.get_running_loop()
    task = None
    second = None
    seq = 10

    async def send(nals, timestamp):
        nonlocal seq
        for i, nal in enumerate(nals):
            await loop.sock_sendto(
                peer,
                rtp.build_packet(
                    rtp.RtpPacket(
                        103, seq, timestamp, 123, nal, marker=i == len(nals) - 1
                    )
                ),
                target,
            )
            seq += 1

    try:
        first = WebSocket()
        snapshot = session()
        view._detach_video_socket(object(), snapshot)
        task = asyncio.create_task(
            view._run_video_session(object(), first, snapshot, endpoint_id="phone")
        )
        await asyncio.wait_for(first.ready.wait(), 1)
        first.finish.set()
        await asyncio.wait_for(task, 1)
        task = None
        assert registry.get_session("gap-call").live and not media.sessions_for("video")
        observed = {
            "original_rtp_fd_after_ws_close": original.fileno(),
            "original_rtcp_fd_after_ws_close": control.fileno(),
            "gap_parameter_nals": [n[0] & 31 for n in parameters],
            "rtp_tuple": target,
            "rtcp_tuple": control_target,
        }
        # No browser reader exists now. Only the authoritative SIP owner may retain these bytes.
        await send(parameters, 9000)
        await asyncio.sleep(0.02)
        second = WebSocket()
        snapshot = session()
        view._detach_video_socket(object(), snapshot)
        task = asyncio.create_task(
            view._run_video_session(object(), second, snapshot, endpoint_id="phone")
        )
        await asyncio.wait_for(second.ready.wait(), 1)
        await send(deltas, 15000)
        await send(pictures, 18000)
        frame = await asyncio.wait_for(second.frames.get(), 1)
        data = frame[view._VIDEO_HEADER.size :]
        (tmp_path / "recovered.h264").write_bytes(data)
        observed["received_nals"] = [n[0] & 31 for n in video_rtp.split_annex_b(data)]
        decoded = subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "h264",
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "pipe:1",
            ],
            input=data,
            capture_output=True,
        )
        observed["ffmpeg_returncode"] = decoded.returncode
        observed["ffmpeg_error"] = decoded.stderr.decode()
        (tmp_path / "gap-observed.json").write_text(json.dumps(observed, indent=2))
        assert decoded.returncode == 0, observed
        assert len(decoded.stdout) == 160 * 120 * 3
        pixel = decoded.stdout[(60 * 160 + 80) * 3 :][:3]
        assert pixel[0] > 150 and pixel[1] < 50 and pixel[2] < 50
        assert (
            client.video_rtp_socket is original and client.video_rtcp_socket is control
        )
        borrowed = (snapshot.rtp_socket, snapshot.rtcp_socket)
        assert all(
            child is not original and child is not control and child.fileno() >= 0
            for child in borrowed
        )
        await client.close()
        assert original.fileno() == control.fileno() == -1
        assert all(child.fileno() >= 0 for child in borrowed)
        ended.set()
        await asyncio.wait_for(task, 2)
        task = None
        assert all(child.fileno() == -1 for child in borrowed)
        assert store["video_drop_wait_keyframe"] == 1
        observed.update(
            sip_closed_originals=True,
            ws_finally_closed_duplicates=True,
            maximum_extra_fds=2,
            video_drop_wait_keyframe=store["video_drop_wait_keyframe"],
        )
        (tmp_path / "gap-observed.json").write_text(json.dumps(observed, indent=2))
    finally:
        if second is not None:
            second.finish.set()
        if task is not None:
            await asyncio.wait_for(task, 2)
        await client.close()
        for sock in sockets:
            sock.close()
    assert (
        not media.sessions_for("video") and original.fileno() == control.fileno() == -1
    )


@pytest.mark.parametrize(
    "owner_kind", ["client", "media", "client_with_incomplete_media"]
)
@pytest.mark.parametrize("rtcp_socket", [False, True])
async def test_video_websocket_borrows_call_bound_sockets(
    monkeypatch, socket_enabled, owner_kind, rtcp_socket
):
    from custom_components.voip_stack import video_ws_view as view
    from custom_components.voip_stack.sip_client import SipCallClient

    originals = []
    for _ in range(2 if rtcp_socket else 1):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        originals.append(sock)
    fields = {
        "video_rtp_socket": originals[0],
        "video_rtcp_socket": originals[1] if rtcp_socket else None,
    }
    client = SipCallClient(
        local_ip="127.0.0.1",
        local_name="owned",
        local_sip_port=5060,
        local_rtp_port=0,
        **fields,
    )
    media = (
        fields.copy()
        if owner_kind == "media"
        else {}
        if owner_kind == "client_with_incomplete_media"
        else None
    )
    registry = SimpleNamespace(
        resource_for=lambda *_: media, sip_client_for=lambda *_: client
    )
    monkeypatch.setattr(view, "call_projection", lambda _: registry)
    session = SimpleNamespace(
        call_id="owned-dialog",
        rtp_socket=fields["video_rtp_socket"],
        rtcp_socket=fields["video_rtcp_socket"],
    )
    try:
        view._detach_video_socket(object(), session)
        assert (
            media["video_rtp_socket"]
            if owner_kind == "media"
            else client.video_rtp_socket
        ) is originals[0]
        assert session.rtp_socket is not originals[0]
        assert session.rtp_socket.getsockname() == originals[0].getsockname()
        session.rtp_socket.close()
        if session.rtcp_socket is not None:
            session.rtcp_socket.close()
        # The original descriptors retain their receive queue across a WS gap.
        peer = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for original in originals:
                peer.sendto(b"queued-during-ws-gap", original.getsockname())
                assert (
                    await asyncio.wait_for(
                        asyncio.get_running_loop().sock_recv(original, 64), 1
                    )
                    == b"queued-during-ws-gap"
                )
        finally:
            peer.close()
        await client.close()
        assert all(original.fileno() == -1 for original in originals)
    finally:
        for owned in (session.rtp_socket, session.rtcp_socket, *originals):
            if owned is not None:
                owned.close()


async def test_video_socket_second_dup_failure_preserves_originals(
    monkeypatch, socket_enabled
):
    from custom_components.voip_stack import video_ws_view as view

    originals = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(2)]
    client = SimpleNamespace(
        video_rtp_socket=originals[0], video_rtcp_socket=originals[1]
    )
    registry = SimpleNamespace(
        resource_for=lambda *_: None, sip_client_for=lambda *_: client
    )
    monkeypatch.setattr(view, "call_projection", lambda _: registry)
    session = SimpleNamespace(
        call_id="owned", rtp_socket=originals[0], rtcp_socket=originals[1]
    )
    borrowed = []
    duplicate = socket.socket.dup

    def fail_second(original):
        if original is originals[1]:
            raise OSError("forced second descriptor failure")
        child = duplicate(original)
        borrowed.append(child)
        return child

    monkeypatch.setattr(socket.socket, "dup", fail_second)
    try:
        with pytest.raises(OSError, match="second descriptor"):
            view._detach_video_socket(object(), session)
        assert len(borrowed) == 1 and borrowed[0].fileno() == -1
        assert session.rtp_socket is client.video_rtp_socket is originals[0]
        assert session.rtcp_socket is client.video_rtcp_socket is originals[1]
        assert all(original.fileno() >= 0 for original in originals)
    finally:
        for original in originals:
            original.close()


async def test_video_transport_start_failure_closes_only_borrowed_descriptors(
    monkeypatch, socket_enabled
):
    from custom_components.voip_stack import video_ws_view as view
    from custom_components.voip_stack.core import sdp

    originals = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(2)]
    for original in originals:
        original.bind(("127.0.0.1", 0))
        original.setblocking(False)
    client = SimpleNamespace(
        video_rtp_socket=originals[0], video_rtcp_socket=originals[1]
    )
    registry = SimpleNamespace(
        resource_for=lambda *_: None, sip_client_for=lambda *_: client
    )
    monkeypatch.setattr(view, "call_projection", lambda _: registry)
    session = view._VideoMediaSession(
        call_id="owned",
        local_rtp_port=originals[0].getsockname()[1],
        remote_rtp_host="127.0.0.1",
        remote_rtp_port=40000,
        remote_rtcp_host="127.0.0.1",
        remote_rtcp_port=40001,
        remote_rtcp_mux=False,
        video_format=sdp.RtpVideoFormat(103),
        local_direction="recvonly",
        rtp_socket=originals[0],
        rtcp_socket=originals[1],
    )
    view._detach_video_socket(object(), session)
    borrowed = (session.rtp_socket, session.rtcp_socket)
    closed = []

    async def fail_start(*args, **kwargs):
        raise OSError("forced transport startup failure")

    async def ws_close(**kwargs):
        closed.append(kwargs)

    monkeypatch.setattr(
        asyncio.get_running_loop(), "create_datagram_endpoint", fail_start
    )
    try:
        await view._run_video_session(
            object(), SimpleNamespace(close=ws_close), session, endpoint_id="phone"
        )
        assert closed[0]["code"] == 1013
        assert all(child.fileno() == -1 for child in borrowed)
        assert all(original.fileno() >= 0 for original in originals)
    finally:
        for original in originals:
            original.close()


@pytest.mark.parametrize(
    "change",
    [
        "terminated",
        "generation",
        "call_replaced",
        "runtime_replaced",
        "owner_replaced",
        "owner_revoked",
        "shutdown",
        "media_removed",
        "media_updated",
    ],
)
async def test_handshake_rechecks_call_claim_and_current_media(monkeypatch, change):
    from aiohttp import web
    from custom_components.voip_stack import video_ws_view as view
    from custom_components.voip_stack.endpoint_session import SessionPhase
    from custom_components.voip_stack.media_ws_session import (
        ClaimedMediaWebSocket,
        MediaWebSocketSession,
    )
    from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
    from custom_components.voip_stack.runtime_data import BrowserMediaRuntime
    from custom_components.voip_stack.websocket_owner import MediaWebSocketOwner

    runtime = SipEndpointRuntime(allow_dark_sessions=True)
    call = runtime.create_session("owned")
    current_runtime = [runtime]
    media = BrowserMediaRuntime()
    owners = media.owners_for("video")
    owner = MediaWebSocketOwner(user_id="user", client_id="client")
    key = "phone|owned"
    owners[key] = owner
    lease = MediaWebSocketSession(
        media, owners, media.owner_lock_for("video"), key, owner, lambda: None
    )
    snapshots = [SimpleNamespace(call_id="owned", revision=1)]
    closed = []
    attached = []

    async def handshake(_):
        if change == "terminated":
            call.phase = SessionPhase.TERMINATED
        elif change == "generation":
            call.generation += 1
        elif change == "call_replaced":
            runtime.calls["owned"] = SimpleNamespace(
                live=True, generation=call.generation
            )
        elif change == "runtime_replaced":
            current_runtime[0] = SipEndpointRuntime(allow_dark_sessions=True)
        elif change == "owner_replaced":
            owners[key] = MediaWebSocketOwner()
        elif change == "owner_revoked":
            owner.revoke()
        elif change == "shutdown":
            media.shutdown.set()
        elif change == "media_removed":
            snapshots.clear()
        elif change == "media_updated":
            snapshots[:] = [SimpleNamespace(call_id="owned", revision=2)]

    async def close(**kwargs):
        closed.append(kwargs)

    ws = SimpleNamespace(prepare=handshake, close=close)

    def resolve():
        if not snapshots:
            raise web.HTTPConflict()
        return None, snapshots[0]

    prepared = SimpleNamespace(
        context=SimpleNamespace(hass=object(), endpoint_id="phone", local_bridge=None),
        registry=runtime,
        local_call=None,
        resolve_current=resolve,
    )

    async def prepare_request(*args, **kwargs):
        return prepared

    @asynccontextmanager
    async def claim(*args, **kwargs):
        yield ClaimedMediaWebSocket(ws, owner, lease)

    async def run(*args, **kwargs):
        attached.append(args[2])

    monkeypatch.setattr(view, "async_prepare_media_websocket_request", prepare_request)
    monkeypatch.setattr(view, "async_claimed_media_websocket", claim)
    monkeypatch.setattr(view, "call_projection", lambda _: current_runtime[0])
    monkeypatch.setattr(view, "_detach_video_socket", lambda _, s: attached.append(s))
    monkeypatch.setattr(view, "_run_video_session", run)
    assert (
        await view.VoipVideoWebSocketView().get(SimpleNamespace(transport=None)) is ws
    )
    if change == "media_updated":
        assert (
            not closed and len(attached) == 2 and all(s.revision == 2 for s in attached)
        )
    else:
        assert not attached and closed == [
            {
                "code": 1008,
                "message": b"video media changed before attachment"
                if change == "media_removed"
                else b"video call changed before attachment",
            }
        ]
