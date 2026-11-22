"""Behavioral tests for the canonical outbound SIP leg builder."""

from types import SimpleNamespace
from unittest.mock import Mock

import homeassistant.exceptions as ha_exceptions
import pytest

ha_exceptions.ConfigEntryError = getattr(
    ha_exceptions,
    "ConfigEntryError",
    RuntimeError,
)

from custom_components.voip_stack import endpoint_dialing  # noqa: E402
from custom_components.voip_stack import endpoint_routing  # noqa: E402
from custom_components.voip_stack.peer import Peer  # noqa: E402
from custom_components.voip_stack.core.sdp import RtpPcmFormat  # noqa: E402


pytestmark = pytest.mark.ha


def binding_capabilities(*, maximum=20, known=True):
    from custom_components.voip_stack.core import sdp

    body = ("v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=Contact capabilities\r\n"
        "c=IN IP4 192.0.2.2\r\nt=0 0\r\nm=audio 0 RTP/AVP 96 97 110\r\n"
        "a=rtpmap:96 L16/48000/1\r\na=rtpmap:97 L16/16000/1\r\n"
        "a=rtpmap:110 OPUS/48000/2\r\na=ptime:10\r\n"
        f"a=maxptime:{maximum}\r\na=sendrecv\r\n")
    return SimpleNamespace(metadata={"sip_capabilities_known": known, **sdp.options_capabilities(body)})


def test_standard_rtp_profile_never_reuses_payload_for_different_formats():
    profile = endpoint_routing.sip_target_rtp_audio_profile(binding_capabilities())
    by_payload = {}
    for item in (*profile.send_rtp_formats, *profile.recv_rtp_formats):
        wire = (item.encoding, item.sample_rate, item.channels)
        assert by_payload.setdefault(item.payload_type, wire) == wire
    assert {wire[1] for wire in by_payload.values()} >= {16000, 48000}


def test_contact_receiver_ceiling_does_not_invent_fixed_transmit_quanta():
    profile = endpoint_routing.sip_target_rtp_audio_profile(binding_capabilities())
    assert profile.evidence.send_geometry_complete
    assert profile.evidence.receive_geometry_complete
    assert all(not item.times.fixed for item in profile.evidence.sends)
    assert {fmt.encoding for fmt in profile.send_rtp_formats} == {"L16", "OPUS"}
    assert {fmt.sample_rate for fmt in profile.recv_rtp_formats} == {16000, 48000}
    assert all(fmt.frame_ms <= 20 for fmt in profile.send_rtp_formats)


@pytest.mark.parametrize("known", [False, True])
def test_only_fresh_contact_geometry_can_narrow_the_engine_offer(known):
    profile = endpoint_routing.sip_target_rtp_audio_profile(binding_capabilities(known=known))
    encodings = {fmt.encoding for fmt in profile.send_rtp_formats}
    if known:
        assert encodings == {"L16", "OPUS"}
    else:
        assert {"PCMA", "PCMU", "L16"} <= encodings
    assert any(fmt.sample_rate == 48000 for fmt in profile.send_rtp_formats)
    assert any(fmt.sample_rate == 16000 for fmt in profile.recv_rtp_formats)


def test_invalid_backend_shape_is_not_promoted_from_discovery():
    profile = endpoint_routing.sip_target_rtp_audio_profile(SimpleNamespace(metadata={
        "sip_capabilities_known": True,
        "sip_audio_tx_formats": ["PCMA/48000/2/10"],
        "sip_audio_rx_formats": ["PCMU/48000/2/10"],
    }))
    for formats in (profile.send_rtp_formats, profile.recv_rtp_formats):
        assert all((fmt.sample_rate, fmt.channels) == (8000, 1)
                   for fmt in formats if fmt.encoding in {"PCMA", "PCMU"})


def test_empty_discovery_keeps_engine_preferences_and_truthful_alternatives():
    profile = endpoint_routing.sip_target_rtp_audio_profile(None)
    assert profile.send_rtp_formats[0].sample_rate == 48000
    assert {fmt.sample_rate for fmt in profile.recv_rtp_formats} >= {16000, 48000}


def test_esphome_extension_is_the_authoritative_request_uri_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialer, _created, _reused = _dialer(monkeypatch)

    uri, peer, entry = dialer.sip_uri_for_member(
        "Waveshare P4 Touch",
        [
            Peer(
                name="Waveshare P4 Touch",
                host="192.0.2.57",
                endpoint_kind="esphome",
                sip_port=5060,
                extension="1000",
                sip_uri_user="Waveshare P4 Touch",
            )
        ],
        [],
    )

    assert str(uri) == "sip:1000@192.0.2.57:5060;transport=tcp"
    assert peer is not None
    assert entry is None


def test_registered_account_keeps_its_authenticated_sip_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialer, _created, _reused = _dialer(monkeypatch)

    uri, _peer, _entry = dialer.sip_uri_for_member(
        "Studio",
        [
            Peer(
                name="Studio",
                host="192.0.2.80",
                endpoint_kind="sip_account",
                extension="428",
                sip_uri_user="studio-phone",
            )
        ],
        [],
    )

    assert str(uri) == "sip:studio-phone@192.0.2.80:5060;transport=tcp"


def test_peer_uri_preserves_tls_and_ipv6_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialer, _created, _reused = _dialer(monkeypatch)

    uri, _peer, _entry = dialer.sip_uri_for_member(
        "Secure",
        [
            Peer(
                name="Secure",
                host="2001:db8::80",
                endpoint_kind="sip_account",
                sip_uri_user="secure-phone",
                sip_port=5061,
                device={"sip_transport": "tls"},
            )
        ],
        [],
    )

    assert str(uri) == "sips:secure-phone@[2001:db8::80]:5061;transport=tls"


def _dialer(
    monkeypatch: pytest.MonkeyPatch,
    client_error: BaseException | None = None,
):
    created: dict = {}

    def build_client(**kwargs):
        created.update(kwargs)
        if client_error is not None:
            raise client_error
        return SimpleNamespace(dialog_ids=SimpleNamespace(call_id="outbound-1"))

    monkeypatch.setattr(endpoint_dialing, "SipCallClient", build_client)
    reused = Mock(return_value=True)
    dialer = endpoint_dialing.EndpointDialer(
        hass=SimpleNamespace(),
        local_ip="127.0.0.1",
        config={"sip_port": 5060, "sip_video": False},
        route_resolver=SimpleNamespace(
            is_local_listener_uri=lambda _uri: False,
            logical_endpoint=lambda *_args: None,
        ),
        sip_uri_transport=lambda _uri: "TCP",
        enable_reused_tcp_connection=reused,
    )
    return dialer, created, reused


def test_trunk_policy_uses_external_ports_and_common_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialer, created, reused = _dialer(monkeypatch)
    trunk = SimpleNamespace(config=SimpleNamespace(video_codecs=None))
    bind_trunk = Mock(return_value=True)
    monkeypatch.setattr(endpoint_dialing, "sip_trunk", lambda _hass: trunk)
    monkeypatch.setattr(endpoint_dialing, "reuse_registered_trunk_flow", bind_trunk)
    reservation = SimpleNamespace(ports=(12000, 12002), release=Mock(), bind_audio_sockets=Mock())
    policy = endpoint_dialing.OutboundLegPolicy(
        local_uri_user="426",
        auth_username="auth-426",
        username="426",
        password="secret",
        outbound_proxy="sip:proxy.example.test",
        reuse_registered_flow=False,
        use_trunk_flow=True,
        allow_video=False,
    )

    leg = dialer.prepare_outbound_leg(
        member="100",
        peers=[],
        roster_entries=[],
        local_name="Caller",
        local_rtp_port_index=1,
        uri_override="sip:100@example.test;transport=tcp",
        invite=SimpleNamespace(routing_caller="source", video_format=None,
            send_format=RtpPcmFormat(96, "L16", 16000, 1, 20),
            recv_format=RtpPcmFormat(96, "L16", 16000, 1, 20)),
        port_reservation=reservation,
        policy=policy,
    )

    assert leg is not None
    assert leg.ports is reservation
    reservation.bind_audio_sockets.assert_called_once_with(ports=(12002,))
    bind_trunk.assert_called_once_with(trunk, leg.client)
    assert created["local_rtp_port"] == 12002
    assert created["local_uri_user"] == "426"
    assert created["auth_username"] == "auth-426"
    assert created["username"] == "426"
    assert created["password"] == "secret"
    assert created["outbound_proxy"] == "sip:proxy.example.test"
    assert created["include_common_codecs"] is False
    assert created["generic_video_relay"] is False
    reused.assert_not_called()
    reservation.release.assert_not_called()


def test_audio_only_trunk_does_not_reserve_video_for_incoming_video(monkeypatch):
    dialer, created, _reused = _dialer(monkeypatch)
    dialer.config["sip_video"] = True
    trunk = SimpleNamespace(config=SimpleNamespace(video_codecs=()))
    monkeypatch.setattr(endpoint_dialing, "sip_trunk", lambda _hass: trunk)
    monkeypatch.setattr(endpoint_dialing, "reuse_registered_trunk_flow", lambda *_args: True)
    monkeypatch.setattr(endpoint_dialing, "peer_video_codec", lambda *_args: "H264")
    def unexpected_reservation(_hass):
        pytest.fail("Audio-only trunk reserved video media")
    monkeypatch.setattr(endpoint_dialing, "reserve_sip_video_relay_media", unexpected_reservation)
    leg = dialer.prepare_outbound_leg(
        member="100", peers=[], roster_entries=[], local_name="Caller",
        local_rtp_port_index=1, uri_override="sip:100@example.test",
        invite=SimpleNamespace(routing_caller="source", video_format=object(),
            send_format=RtpPcmFormat(96, "L16", 16000, 1, 20),
            recv_format=RtpPcmFormat(96, "L16", 16000, 1, 20)),
        port_reservation=SimpleNamespace(ports=(12000, 12002), release=Mock(), bind_audio_sockets=Mock()),
        policy=endpoint_dialing.OutboundLegPolicy(use_trunk_flow=True),
    )
    assert leg is not None
    assert leg.video_relay is None
    assert created["local_video_rtp_port"] == 0
    assert created["video_formats"] == ()


@pytest.mark.parametrize("endpoint_kind", ["esphome", "sip_account"])
def test_endpoint_kind_uses_the_same_engine_media_plan(monkeypatch, endpoint_kind):
    from custom_components.voip_stack.core import sdp
    from custom_components.voip_stack.sip_client import SipCallClient

    dialer, created, _reused = _dialer(monkeypatch)
    reservation = SimpleNamespace(ports=(12000, 12002), release=Mock(), bind_audio_sockets=Mock())
    peer = Peer(name="Phone", host="192.0.2.20", endpoint_kind=endpoint_kind,
        sip_uri_user="phone", tx_formats=["16000:s16le:1:20"], rx_formats=["48000:s16le:1:10"])
    leg = dialer.prepare_outbound_leg(member="Phone", peers=[peer], roster_entries=[],
        local_name="Caller", local_rtp_port_index=0, port_reservation=reservation)
    assert leg is not None
    expected = endpoint_routing.sip_target_rtp_audio_profile(None)
    assert created["supported_send_rtp_formats"] == expected.send_rtp_formats
    assert created["supported_recv_rtp_formats"] == expected.recv_rtp_formats
    client = SipCallClient(**created)
    client._prepare_initial_offer(delayed_offer=False, audio_direction="sendrecv")
    body = client._local_sdp_body
    assert "x-voip-stack" not in body
    assert {fmt.sample_rate for fmt in sdp.offered_pcm_formats(body)} >= {16000, 48000}


def test_failed_external_leg_keeps_caller_owned_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialer, _created, _reused = _dialer(
        monkeypatch,
        client_error=RuntimeError("client construction failed"),
    )
    reservation = SimpleNamespace(ports=(12000, 12002), release=Mock(), bind_audio_sockets=Mock())

    with pytest.raises(RuntimeError, match="client construction failed"):
        dialer.prepare_outbound_leg(
            member="100",
            peers=[],
            roster_entries=[],
            local_name="Caller",
            local_rtp_port_index=1,
            uri_override="sip:100@example.test;transport=tcp",
            port_reservation=reservation,
        )

    reservation.release.assert_not_called()


def test_unregistered_sip_peer_offers_opus_without_losing_pcm10(monkeypatch):
    """Registration must not decide codec support for an otherwise unknown SIP peer."""
    from custom_components.voip_stack.core import sdp
    from custom_components.voip_stack.core.codec_capabilities import codec_available
    from custom_components.voip_stack.sip_client import SipCallClient

    assert codec_available("OPUS") and codec_available("G722")
    dialer, created, _ = _dialer(monkeypatch)
    peer = Peer(name="Manual SIP", host="192.0.2.60", endpoint_kind="sip_account")
    leg = dialer.prepare_outbound_leg(
        member=peer.name, peers=[peer], roster_entries=[], local_name="Caller",
        local_rtp_port_index=0,
        port_reservation=SimpleNamespace(ports=(12000, 12002), release=Mock(), bind_audio_sockets=Mock()),
    )
    assert leg is not None
    assert created["include_common_codecs"] is False
    client = SipCallClient(**created)
    client._prepare_initial_offer(delayed_offer=False, audio_direction="sendrecv")
    offer = client._local_sdp_body
    formats = sdp.offered_pcm_formats(offer)
    assert any(f.encoding == "OPUS" for f in formats)
    assert any(f.encoding == "L16" and f.sample_rate == 32000 for f in formats)
    assert sdp.parse_sdp(offer)["ptime"] == 20
    assert "x-voip-stack" not in offer


@pytest.mark.parametrize("kind", ["esphome", "sip_account", "browser", "assist"])
@pytest.mark.parametrize("tx", [None, ["16000:s16le:1:10"]])
def test_device_diagnostics_do_not_narrow_a_sip_contact_offer(kind, tx):
    plain = endpoint_routing.sip_target_rtp_audio_profile(None)
    diagnostics = SimpleNamespace(metadata={"endpoint_kind": kind, "tx_formats": tx,
        "sip_audio_tx_formats": ["OPUS/48000/2/20"]})
    assert endpoint_routing.sip_target_rtp_audio_profile(diagnostics) == plain


@pytest.mark.parametrize("conference", [False, True])
@pytest.mark.parametrize("use_trunk,uri", [
    (True, "sip:441234567890@provider.example"),
    (False, "sip:441234567890@provider.example"),
    (False, "sip:441234567890@198.51.100.8"),
])
def test_restricted_group_member_is_rejected_before_allocating_media(monkeypatch, use_trunk, uri, conference):
    from custom_components.voip_stack import runtime_data
    dialer, created, reused = _dialer(monkeypatch)
    dialer.config["external_call_blocked_endpoints"] = ["sip:restricted"]
    session = SimpleNamespace(metadata={"source_endpoint_id": "sip:restricted"})
    access_runtime = SimpleNamespace(
        transport_config=dialer.config,
        trunk_config={"trunk_server": "provider.example"},
        sip=SimpleNamespace(get_session=lambda _: session,
                            component=lambda _: SimpleNamespace(active_registrar_target=("198.51.100.8", 5060))),
    )
    monkeypatch.setattr(runtime_data, "require_runtime_data", lambda _: access_runtime)
    monkeypatch.setattr(endpoint_dialing, "sip_trunk", lambda _: SimpleNamespace(active_registrar_target=("198.51.100.8", 5060)))
    allocate = Mock(side_effect=AssertionError("must not allocate forbidden leg"))
    monkeypatch.setattr(endpoint_dialing.RtpPortReservation, "allocate", allocate)
    leg = dialer.prepare_outbound_leg(
        member="External", peers=[], roster_entries=[], local_name="HA",
        local_rtp_port_index=0, uri_override=uri,
        invite=None if conference else SimpleNamespace(call_id="call"),
        source_call_id="call" if conference else "",
        policy=endpoint_dialing.OutboundLegPolicy(use_trunk_flow=use_trunk),
    )
    assert leg is None
    assert not created
    allocate.assert_not_called()
    reused.assert_not_called()


async def test_conference_retains_initiator_access_for_outbound_members(monkeypatch):
    from custom_components.voip_stack import conference_ringing, runtime_data
    from custom_components.voip_stack.roster import RosterEntry

    dialer, created, _reused = _dialer(monkeypatch)
    dialer.config["external_call_blocked_endpoints"] = ["sip:restricted"]
    session = SimpleNamespace(live=True, metadata={"source_endpoint_id": "sip:restricted"})
    registry = SimpleNamespace(
        sessions={"owner": session}, resolve_session_id=lambda value: value,
        get_session=lambda call_id: session if call_id == "owner" else None,
        component=lambda _: None,
    )
    access_runtime = SimpleNamespace(
        transport_config=dialer.config, trunk_config={"trunk_server": "provider.example"}, sip=registry,
    )
    monkeypatch.setattr(runtime_data, "require_runtime_data", lambda _: access_runtime)
    monkeypatch.setattr(endpoint_dialing, "sip_trunk", lambda _: None)
    monkeypatch.setattr(conference_ringing, "call_registry", lambda _: registry)
    monkeypatch.setattr(conference_ringing, "conference_manager", lambda *a, **kw: SimpleNamespace(rooms={"Room": SimpleNamespace(
        name="Room", _closed=False, legs={"owner": object()})}))
    monkeypatch.setattr(conference_ringing, "endpoint_directory", lambda _: SimpleNamespace())
    allocate = Mock(side_effect=AssertionError("conference must not allocate forbidden member"))
    monkeypatch.setattr(endpoint_dialing.RtpPortReservation, "allocate", allocate)
    runtime = conference_ringing.ConferenceRingRuntime(
        hass=dialer.hass, config=dialer.config, local_ip=dialer.local_ip,
        browser_leg_for_member=lambda *_: None,
        prepare_outbound_leg=dialer.prepare_outbound_leg,
    )
    await conference_ringing.async_ring_conference_members(
        runtime, room_name="Room", caller="Restricted", source_host="192.0.2.30",
        entry=RosterEntry(id="Room", metadata={"ring_members": ["External"]}), peers=[],
        roster_entries=[RosterEntry(id="External", sip_uri="sip:441234567890@provider.example")],
        owner_call_id="owner",
    )
    assert not created
    allocate.assert_not_called()
