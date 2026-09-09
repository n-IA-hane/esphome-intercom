"""Opt-in VTO offer compatibility without weakening SDP or camera permission."""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest
from .voip_phase1_support import sdp, audio_format

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "profile,capable,trunk,native,camera,expected",
    [
        ("", True, False, False, False, (False, False)),
        ("", True, False, False, True, (True, True)),
        ("dahua_vto", True, False, False, False, (True, False)),
        ("dahua_vto", True, False, False, True, (True, False)),
        ("dahua_vto", False, False, False, False, (False, False)),
        ("dahua_vto", True, True, False, False, (False, False)),
        ("dahua_vto", True, False, True, False, (False, False)),
    ],
)
def test_real_originate_policy_is_explicit(
    profile, capable, trunk, native, camera, expected
):
    tree = ast.parse(
        (ROOT / "custom_components/voip_stack/softphone_originate.py").read_text()
    )
    names = {"dahua_video_compatibility", "camera_send_enabled", "video_enabled"}
    nodes = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id in names for t in n.targets)
    ]
    nodes.sort(key=lambda n: n.lineno)
    env = {
        "video_capable": capable,
        "use_trunk": trunk,
        "native_audio_endpoint": native,
        "entry_metadata": {"sip_video_profile": profile},
        "cfg": {"video_camera_send": True},
        "CONF_VIDEO_CAMERA_SEND": "video_camera_send",
        "call": SimpleNamespace(data={"send_video": camera}),
    }
    exec(
        compile(
            ast.Module(body=nodes, type_ignores=[]),
            "<real outbound video policy>",
            "exec",
        ),
        env,
    )
    assert (env["video_enabled"], env["camera_send_enabled"]) == expected


def test_mode_zero_answer_without_fmtp_and_strict_validation():
    pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
    ordinary = sdp.video_offer_formats_for_target_codec("h264")
    formats = sdp.video_offer_formats_for_target_codec(
        "h264", compatibility_profile="dahua_vto"
    )
    assert ordinary[0].packetization_mode == 1
    assert (
        formats[0].packetization_mode == 0 and formats[0].profile_level_id == "42000a"
    )
    offer = sdp.build_offer_directional(
        "127.0.0.1",
        "127.0.0.1",
        40000,
        [pcm],
        [pcm],
        video_port=40002,
        video_formats=formats,
    )
    answer = (
        "\r\n".join(
            line for line in offer.splitlines() if not line.startswith("a=fmtp:")
        )
        + "\r\n"
    )
    sdp.validate_sdp_answer(offer, answer)
    assert sdp.negotiate_video_answer_directional(answer, formats) is not None
    assert sdp.negotiate_video_answer_directional(answer, ordinary) is None
    audio_only = sdp.build_offer("127.0.0.1", "127.0.0.1", 40000, [pcm])
    with pytest.raises(sdp.SdpError):
        sdp.validate_sdp_answer(audio_only, answer)
    recvonly = sdp.build_offer_directional(
        "127.0.0.1",
        "127.0.0.1",
        40000,
        [pcm],
        [pcm],
        video_port=40002,
        video_formats=formats,
        video_direction="recvonly",
    )
    with pytest.raises(sdp.SdpError):
        sdp.validate_sdp_answer(recvonly, answer)
    assert not sdp.browser_video_send_supported(formats[0])
    assert sdp.browser_video_receive_supported(formats[0])
    assert sdp.video_offer_formats_for_target_codec("h264") == ordinary


def test_explicit_camera_denial_survives_sendrecv_signaling(monkeypatch):
    import asyncio
    from .voip_phase1_support import _load_video_ws_runtime_module, sip_client

    module = _load_video_ws_runtime_module()
    fmt = sdp.video_offer_formats_for_target_codec("h264")[0]
    pcm = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
    dialog = sip_client.SipDialog(
        target="door",
        remote_host="127.0.0.1",
        remote_sip_port=5060,
        remote_rtp_host="127.0.0.1",
        remote_rtp_port=42000,
        local_rtp_port=41000,
        call_id="test-door",
        local_uri="sip:ha@localhost",
        remote_uri="sip:door@localhost",
        send_format=pcm,
        recv_format=pcm,
        video_format=fmt,
        local_video_rtp_port=41002,
        remote_video_rtp_port=42002,
        remote_video_rtp_host="127.0.0.1",
        local_video_direction="sendrecv",
    )
    client = SimpleNamespace(
        dialog=dialog,
        video_direction="sendrecv",
        camera_send_authorized=False,
        video_rtp_source=None,
        video_rtp_socket=None,
        video_rtcp_socket=None,
        request_video_keyframe=None,
    )
    registry = SimpleNamespace(
        resource_for=lambda *args: None, sip_client_for=lambda cid: client
    )
    monkeypatch.setattr(
        module,
        "active_media_call",
        lambda *args: SimpleNamespace(store={}, call_id="test-door", registry=registry),
    )
    monkeypatch.setattr(module, "transport_config", lambda hass: {})

    async def check():
        session = module._active_video_session(None, "default")
        assert session.can_receive
        assert not session.can_send
        client.camera_send_authorized = True
        assert module._active_video_session(None, "default").can_send

    asyncio.run(check())


def test_two_udp_calls_accept_reported_vto_answer_and_hang_up():
    import asyncio
    from .voip_phase1_support import sip_client, sip

    class Door(asyncio.DatagramProtocol):
        closed = 0

        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            request = sip.parse_message(raw)
            if request.method == "ACK":
                return
            body = b""
            if request.method == "INVITE":
                lines = request.body.decode().splitlines()
                assert any(line.startswith("m=video ") for line in lines)
                body = (
                    "\r\n".join(
                        line for line in lines if not line.startswith("a=fmtp:")
                    )
                    + "\r\n"
                ).encode()
            elif request.method == "BYE":
                self.closed += 1
            else:
                return
            response = sip.build_uas_response(
                request,
                200,
                "OK",
                to_tag="door-test",
                contact_uri=f"sip:door@127.0.0.1:{self.transport.get_extra_info('sockname')[1]}",
                body=body,
            )
            self.transport.sendto(response, address)

    async def scenario():
        loop = asyncio.get_running_loop()
        door = Door()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: door, local_addr=("127.0.0.1", 0)
        )
        port = transport.get_extra_info("sockname")[1]
        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        try:
            for _ in range(2):
                client = sip_client.SipCallClient(
                    local_ip="127.0.0.1",
                    local_name="HA",
                    local_sip_port=0,
                    local_rtp_port=41000,
                    supported_formats=[pcm],
                    local_video_rtp_port=41002,
                    video_formats=sdp.video_offer_formats_for_target_codec(
                        "h264", compatibility_profile="dahua_vto"
                    ),
                    video_direction="sendrecv",
                    camera_send_authorized=False,
                )
                try:
                    assert (
                        await client.invite(
                            target="door",
                            remote_host="127.0.0.1",
                            remote_sip_port=port,
                            timeout=2,
                        )
                        == "in_call"
                    )
                    assert client.dialog.video_format.packetization_mode == 0
                    assert client.dialog.video_format.profile_level_id == "42000a"
                    assert not client.camera_send_authorized
                    await client.terminate(timeout=1)
                    assert client.dialog is None
                finally:
                    await client.close()
            assert door.closed == 2
        finally:
            transport.close()

    asyncio.run(scenario())


def test_contact_service_persists_only_explicit_video_profile(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from .test_phonebook_service_security import _load_phonebook_services
    from .voip_phase1_support import roster

    module = _load_phonebook_services(monkeypatch)
    monkeypatch.setattr(module, "RosterEntry", roster.RosterEntry)
    saved = []
    monkeypatch.setattr(
        module,
        "store_manual_roster_entries",
        lambda hass, entries: saved.extend(entries),
    )
    refresh = AsyncMock()
    call = SimpleNamespace(
        hass=SimpleNamespace(data={}),
        data={
            "name": "Door video",
            "sip_uri": "sip:door@127.0.0.1",
            "sip_video_profile": "dahua_vto",
        },
    )
    asyncio.run(module.build_phonebook_service_handlers(refresh)["add_contact"](call))
    assert saved[0].metadata["sip_video_profile"] == "dahua_vto"
    assert saved[0].sip_uri == "sip:door@127.0.0.1"
    refresh.assert_awaited_once()
