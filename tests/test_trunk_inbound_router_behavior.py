"""Executable source-termination guard for inbound trunk routing."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from custom_components.voip_stack import endpoint_runtime
from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.sip_listener import SipInvite


pytestmark = pytest.mark.ha


@pytest.mark.parametrize("incoming_transport", ["TCP", "UDP"])
@pytest.mark.parametrize("trunk_transport", ["TCP", "UDP"])
async def test_inbound_bridge_constructs_client_with_trunk_transport(
    monkeypatch, incoming_transport, trunk_transport,
) -> None:
    """Issue #108: an ESP's signaling transport cannot select the trunk leg."""
    from custom_components.voip_stack.inbound_routing import bridge
    from custom_components.voip_stack.sip_runtime import uri_transport

    class ClientObserved(Exception):
        pass

    captured = {}

    def client(**kwargs):
        ports.bind_audio_sockets.assert_called_once_with()
        captured.update(kwargs)
        raise ClientObserved

    ports = SimpleNamespace(ports=(44000, 44002), bind_audio_sockets=Mock())
    monkeypatch.setattr(bridge, "take_delayed_offer_ports", lambda *_: ports)
    monkeypatch.setattr(bridge, "SipCallClient", client)
    runtime = SimpleNamespace(
        hass=object(), config={"sip_port": 15060}, local_ip="192.0.2.10",
        sip_uri_transport=uri_transport, ha_peer_name=lambda *_: "HA",
    )
    audio = sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20)
    invite = SipInvite(
        call_id="esp-trunk-108", caller="ESP", target="12345",
        request_uri=sip.parse_sip_uri("sip:12345@192.0.2.10"),
        caller_uri=sip.parse_sip_uri("sip:ESP@192.0.2.20"),
        source_host="192.0.2.20", source_port=5060,
        cseq="1 INVITE", remote_sdp=b"", send_format=audio, recv_format=audio,
        remote_rtp_host="192.0.2.20", remote_rtp_port=40000,
        signaling_transport=incoming_transport,
    )
    decision = SimpleNamespace(target="12345", entry=None, sip_uri="")
    with pytest.raises(ClientObserved):
        await bridge.route_sip_bridge(
            runtime=runtime, invite=invite, decision=decision, peers=[], roster_entries=[],
            trunk_config={"trunk_server": "192.0.2.30", "trunk_port": 5060,
                          "trunk_transport": trunk_transport, "trunk_username": "account"},
            bridge_to_trunk=True, source_endpoint=None, target_endpoint=None,
            resolved_callee="12345", trunk_invite=False,
            registry=SimpleNamespace(own_resource=lambda *_: True),
        )
    assert captured["signaling_transport"].upper() == trunk_transport
    assert captured["local_rtp_port"] == 44002


@dataclass(frozen=True)
class _TestInvite:
    received_via_trunk: bool
    source_host: str
    source_port: int
    signaling_transport: str


def _invite(*, received_via_trunk: bool = False) -> _TestInvite:
    return _TestInvite(
        received_via_trunk=received_via_trunk,
        source_host="198.51.100.20",
        source_port=49152,
        signaling_transport="UDP",
    )


def test_registered_trunk_classifies_trusted_main_listener_invite() -> None:
    trunk = SimpleNamespace(
        registered=True,
        accepts_inbound_source=Mock(return_value=True),
    )

    classified = endpoint_runtime._classify_trunk_invite(
        _invite(), enabled=True, trunk=trunk
    )

    assert classified.received_via_trunk
    trunk.accepts_inbound_source.assert_called_once_with(
        "198.51.100.20", 49152, "UDP"
    )


def test_untrusted_or_unregistered_invite_is_not_classified_as_trunk() -> None:
    untrusted = SimpleNamespace(
        registered=True,
        accepts_inbound_source=Mock(return_value=False),
    )
    unregistered = SimpleNamespace(
        registered=False,
        accepts_inbound_source=Mock(return_value=True),
    )

    assert not endpoint_runtime._classify_trunk_invite(
        _invite(), enabled=True, trunk=untrusted
    ).received_via_trunk
    assert not endpoint_runtime._classify_trunk_invite(
        _invite(received_via_trunk=True), enabled=True, trunk=unregistered
    ).received_via_trunk
    unregistered.accepts_inbound_source.assert_not_called()














def test_static_trunk_classifies_only_explicitly_allowed_sources():
    from custom_components.voip_stack.sip_trunk import SipTrunkClient, SipTrunkConfig

    trunk = SipTrunkClient(
        config=SipTrunkConfig(options_interval=30, enabled=True, transport="udp", server="198.51.100.20",
                             port=5060, domain="", username="", auth_username="",
                             password="", expires=300, register=False,
                             allowed_ips=("198.51.100.20",)),
        local_ip="192.0.2.1", local_sip_port=5060,
    )
    assert not trunk.registered
    assert endpoint_runtime._classify_trunk_invite(_invite(), enabled=True, trunk=trunk).received_via_trunk
    from dataclasses import replace
    stranger = replace(_invite(), source_host="198.51.100.21")
    assert not endpoint_runtime._classify_trunk_invite(stranger, enabled=True, trunk=trunk).received_via_trunk
    assert not endpoint_runtime._classify_trunk_invite(_invite(), enabled=False, trunk=trunk).received_via_trunk
