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
from custom_components.voip_stack.peer import Peer  # noqa: E402


pytestmark = pytest.mark.ha


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
    reservation = SimpleNamespace(ports=(12000, 12002), release=Mock())
    policy = endpoint_dialing.OutboundLegPolicy(
        local_uri_user="426",
        auth_username="auth-426",
        username="426",
        password="secret",
        outbound_proxy="sip:proxy.example.test",
        force_common_audio=True,
        reuse_registered_flow=False,
        allow_video=False,
    )

    leg = dialer.prepare_outbound_leg(
        member="100",
        peers=[],
        roster_entries=[],
        local_name="Caller",
        local_rtp_port_index=1,
        uri_override="sip:100@example.test;transport=tcp",
        invite=SimpleNamespace(routing_caller="source", video_format=None),
        port_reservation=reservation,
        policy=policy,
    )

    assert leg is not None
    assert leg.ports is reservation
    assert created["local_rtp_port"] == 12002
    assert created["local_uri_user"] == "426"
    assert created["auth_username"] == "auth-426"
    assert created["username"] == "426"
    assert created["password"] == "secret"
    assert created["outbound_proxy"] == "sip:proxy.example.test"
    assert created["include_common_codecs"] is True
    assert created["generic_video_relay"] is False
    reused.assert_not_called()
    reservation.release.assert_not_called()


def test_failed_external_leg_keeps_caller_owned_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dialer, _created, _reused = _dialer(
        monkeypatch,
        client_error=RuntimeError("client construction failed"),
    )
    reservation = SimpleNamespace(ports=(12000, 12002), release=Mock())

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
