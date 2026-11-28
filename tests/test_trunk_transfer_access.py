"""External-call access is enforced before sending or relaying SIP REFER."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import json

import pytest

from custom_components.voip_stack import call_transfer, sip_rtp_bridge
from custom_components.voip_stack.core import audio_format, sdp
from custom_components.voip_stack.endpoint_registry import EndpointRegistry
from custom_components.voip_stack.local_transfer import try_local_audio_transfer
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.runtime_data import VoipStackRuntime
from custom_components.voip_stack.contact_config import materialize_contact_timeout
from custom_components.voip_stack.phone_directory import PhoneDirectory
from custom_components.voip_stack.roster import parse_roster_json
from custom_components.voip_stack.core.sip_transfer import SipReferTarget
from custom_components.voip_stack.sip_client import SipCallClient, SipTransferResult

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("kind", ["queue", "paging"])
@pytest.mark.parametrize("explicit_uri", [False, True])
async def test_restricted_source_cannot_delegate_local_application_without_its_origin(kind, explicit_uri):
    from custom_components.voip_stack.core.sip import SipUri

    identity = f"{kind}:local"
    runtime, client = _runtime(entries=[{"id": identity, "name": "Application",
        "metadata": {"virtual_endpoint": kind}}])
    runtime.transport_config.update(advertise_host="192.0.2.1", sip_port=5060)
    destination = str(SipUri(identity, "192.0.2.1", 5060)) if explicit_uri else "Application"
    result = await call_transfer.async_transfer_call(runtime, call_transfer.CallTransferRequest("call", destination))
    assert not result.accepted and result.status == 403 and result.state == "external_calls_disabled"
    client.refer.assert_not_awaited()


@pytest.mark.parametrize("kind", ["queue", "paging", "ring"])
async def test_disabled_local_transfer_target_is_rejected_before_refer(kind):
    from custom_components.voip_stack.core import sip

    metadata = {"group_type": "ring"} if kind == "ring" else {"virtual_endpoint": kind}
    runtime, client = _runtime(blocked=False, entries=[{"id": "disabled", "name": "Disabled",
        "enabled": False, "metadata": metadata}])
    runtime.transport_config.update(advertise_host="192.0.2.1", sip_port=5060)
    with pytest.raises(sip.SipError, match="unavailable or disabled"):
        await call_transfer.async_transfer_call(runtime, call_transfer.CallTransferRequest("call", "Disabled"))
    client.refer.assert_not_awaited()


def _runtime(*, blocked=True, remote="sip:desk@192.0.2.20", entries=None):
    client = Mock(spec=SipCallClient)
    client.dialog = SimpleNamespace(
        local_uri="sip:HA@192.0.2.1:5060", remote_uri=remote
    )
    client.refer = AsyncMock(return_value=SipTransferResult(True, 200, "completed"))
    session = SimpleNamespace(
        metadata={"source_endpoint_id": "sip:restricted"}, legs={}
    )
    runtime = SimpleNamespace(
        transport_config={
            "external_call_blocked_endpoints": ["sip:restricted"] if blocked else [],
        },
        trunk_config={"trunk_server": "provider.example", "trunk_port": 5060},
        trunk_configs={}, default_trunk_id="",
        sip=SimpleNamespace(
            get_session=lambda _call_id: session,
            component=lambda _name: None,
            sip_client_for=lambda call_id: client if call_id == "call" else None,
        ),
        endpoints=SimpleNamespace(resolve=lambda _value: None),
        directory=PhoneDirectory(parse_roster_json(json.dumps([
            materialize_contact_timeout(entry, 30) for entry in entries or []
        ]))),
    )
    return runtime, client


@pytest.mark.parametrize("incoming", [False, True])
@pytest.mark.parametrize(
    "target,blocked,denied",
    [
        ("sip:441234567890@provider.example", True, True),
        ("sip:441234567890@provider.example", False, False),
        ("sip:desk@192.0.2.20", True, False),
        ("sip:441234567890@192.0.2.1", True, True),
    ],
)
async def test_refer_enforces_access_before_sending(incoming, target, blocked, denied):
    runtime, client = _runtime(blocked=blocked)
    if incoming:
        result = await call_transfer.async_transfer_target(runtime, "call", SipReferTarget(target))
    else:
        result = await call_transfer.async_transfer_call(
            runtime, call_transfer.CallTransferRequest("call", target)
        )
    if denied:
        assert not result.accepted
        assert result.status == 403
        assert result.state == "external_calls_disabled"
        client.refer.assert_not_awaited()
    else:
        assert result.accepted
        client.refer.assert_awaited_once_with(SipReferTarget(target))


@pytest.mark.parametrize("target,denied", [
    ("sip:unknown@other-provider.example", True),
    ("sip:desk@192.0.2.20", False),
    ("sip:101@192.0.2.1", False),
])
async def test_trunk_refer_requires_known_internal_destination(target, denied):
    runtime, client = _runtime(
        remote="sip:caller@provider.example",
        entries=[{"id": "desk", "extension": "101", "sip_uri": "sip:desk@192.0.2.20"}],
    )
    result = await call_transfer.async_transfer_target(runtime, "call", SipReferTarget(target))
    assert result.accepted is not denied
    assert client.refer.await_count == (0 if denied else 1)


async def test_permission_is_read_again_for_later_transfer():
    runtime, client = _runtime()
    target = SipReferTarget("sip:441234567890@provider.example")
    denied = await call_transfer.async_transfer_target(runtime, "call", target)
    assert denied.status == 403
    client.refer.assert_not_awaited()
    runtime.transport_config["external_call_blocked_endpoints"] = []
    accepted = await call_transfer.async_transfer_target(runtime, "call", target)
    assert accepted.accepted
    client.refer.assert_awaited_once_with(target)


async def test_attended_transfer_to_trunk_keeps_both_dialogs_when_denied():
    runtime, source = _runtime()
    consultation = Mock(spec=SipCallClient)
    consultation.dialog = SimpleNamespace(remote_uri="sip:441234567890@provider.example", remote_target_uri="sip:441234567890@provider.example")
    consultation.dialog_ids = SimpleNamespace(
        call_id="consultation", remote_tag="remote", local_tag="local"
    )
    consultation.refer = AsyncMock()
    runtime.sip.sip_client_for = {"call": source, "consultation": consultation}.get
    result = await call_transfer.async_transfer_call(
        runtime, call_transfer.CallTransferRequest("call", "", "consultation")
    )
    assert result.status == 403
    source.refer.assert_not_awaited()
    consultation.refer.assert_not_awaited()
    assert source.dialog is not None
    assert consultation.dialog is not None


async def test_inbound_listener_does_not_send_forbidden_refer():
    runtime, _client = _runtime()
    runtime.sip.sip_client_for = lambda _call_id: None
    server = SimpleNamespace(
        remote_uri_for_call=lambda _call_id: "sip:desk@192.0.2.20",
        local_ip="192.0.2.1", local_sip_port=5060,
        async_refer=AsyncMock(),
    )
    runtime.sip.component = lambda name: server if name == "udp_listener" else None
    result = await call_transfer.async_transfer_call(
        runtime, call_transfer.CallTransferRequest("call", "sip:441234567890@provider.example")
    )
    assert result.status == 403
    server.async_refer.assert_not_awaited()


async def test_resolved_trunk_address_is_not_a_refer_bypass():
    runtime, client = _runtime()
    runtime.sip.component = lambda _name: SimpleNamespace(active_registrar_target=("198.51.100.7", 5060))
    result = await call_transfer.async_transfer_target(
        runtime, "call", SipReferTarget("sip:441234567890@198.51.100.7")
    )
    assert result.status == 403
    client.refer.assert_not_awaited()


async def test_local_ha_contact_alias_cannot_hide_trunk_uri():
    runtime, client = _runtime(entries=[{
        "id": "outside", "sip_uri": "sip:441234567890@provider.example",
    }])
    result = await call_transfer.async_transfer_target(
        runtime, "call", SipReferTarget("sip:outside@192.0.2.1")
    )
    assert result.status == 403
    client.refer.assert_not_awaited()


@pytest.mark.parametrize("metadata", [
    {"group_type": "ring", "ring_members": ["External"]},
    {"group_type": "conference", "ring_members": ["External"]},
    {"virtual_endpoint": "automation"},
])
@pytest.mark.parametrize("blocked", [True, False])
async def test_delegated_refer_cannot_launder_origin_through_ha_dialplan(metadata, blocked):
    runtime, client = _runtime(blocked=blocked, entries=[
        {"id": "Indirect", "metadata": metadata},
        {"id": "External", "number": "441234567890"},
    ])
    target = SipReferTarget("sip:Indirect@192.0.2.1")
    result = await call_transfer.async_transfer_target(runtime, "call", target)
    if blocked:
        assert result.status == 403
        assert result.state == "external_calls_disabled"
        client.refer.assert_not_awaited()
    else:
        assert result.accepted
        client.refer.assert_awaited_once_with(target)


class _Transport:
    def __init__(self, protocol):
        self.protocol = protocol
        self.sent = []
        self.closed = 0

    def get_protocol(self):
        return self.protocol

    def set_protocol(self, protocol):
        self.protocol = protocol

    def is_closing(self):
        return bool(self.closed)

    def close(self):
        self.closed += 1

    def sendto(self, data, address):
        assert not self.closed
        self.sent.append((data, address))


def _relay(index, releases):
    fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
    wire = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
    result = sip_rtp_bridge.SipRtpRelay(
        left=sip_rtp_bridge.RtpPeer("127.0.0.1", 10000 + index * 10, 96, fmt, rtp_format=wire),
        right=sip_rtp_bridge.RtpPeer("127.0.0.1", 10002 + index * 10, 96, fmt, rtp_format=wire),
        left_port=20000 + index * 10, right_port=20002 + index * 10,
        on_release=lambda ports: releases.append((index, ports)),
    )
    for side in ("left", "right"):
        protocol = sip_rtp_bridge._RelayProtocol(result, side)
        transport = _Transport(protocol)
        protocol.connection_made(transport)
        setattr(result, f"{side}_transport", transport)
    return result


async def test_local_transfer_records_both_trunk_identities():
    calls = SipEndpointRuntime(allow_dark_sessions=True)
    original = calls.upsert("original", owner="bridge", state="in_call")
    consultation = calls.upsert("consult", owner="bridge", state="in_call")
    releases = []
    first, second = _relay(0, releases), _relay(1, releases)
    for session, wire, relay, trunk_id in (
        (original, "a", first, "source-trunk"),
        (consultation, "c", second, "dest-trunk"),
    ):
        calls.add_leg(session.call_id, wire, role="callee", state="in_call")
        session.legs[wire].dialog = SimpleNamespace(
            dialog=object(), close=AsyncMock(), terminate=AsyncMock(),
            dialog_ids=SimpleNamespace(call_id=wire), on_refer=None,
            on_media_update=None, on_connected_identity=None,
            on_info_dtmf=None, on_info_video=None,
        )
        session.legs[wire].trunk_id = trunk_id
        calls.set_bridge_link(session.call_id, wire)
        calls.attach_relay(session.call_id, relay)
    runtime = VoipStackRuntime(
        transport_config={}, assist_config={}, trunk_config={},
        endpoints=EndpointRegistry(), phones=None, sip=calls,
    )
    runtime.rtp_port_pool = {"used": {20000, 20002, 20010, 20012}}
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(
            async_entries=lambda _domain: [SimpleNamespace(runtime_data=runtime)]
        )
    )
    grant = SimpleNamespace(
        original=original, consultation=consultation,
        pending_call_id="", endpoint_id="", current=lambda: True,
    )
    captured = {}

    def capture_registration(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("captured")

    left = first.right_transport
    try:
        with patch.object(calls, "register_bridge", side_effect=capture_registration):
            with pytest.raises(RuntimeError, match="captured"):
                await try_local_audio_transfer(hass, runtime, grant, None)
        assert captured["inbound_trunk_id"] == "source-trunk"
        assert captured["outbound_trunk_id"] == "dest-trunk"
    finally:
        escaped = left.get_protocol().relay
        if escaped not in (first, second):
            await escaped.stop()
        await calls.shutdown()
