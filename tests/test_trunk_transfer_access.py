"""External-call access is enforced before sending or relaying SIP REFER."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import json

import pytest

from custom_components.voip_stack import call_transfer
from custom_components.voip_stack.core.sip_transfer import SipReferTarget
from custom_components.voip_stack.sip_client import SipCallClient, SipTransferResult

pytestmark = pytest.mark.ha


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
        sip=SimpleNamespace(
            get_session=lambda _call_id: session,
            component=lambda _name: None,
            sip_clients_snapshot=lambda: {"call": client},
        ),
        endpoints=SimpleNamespace(resolve=lambda _value: None),
        phonebook_sensor=SimpleNamespace(
            extra_state_attributes={"roster_json": json.dumps(entries or [])}
        ),
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
    consultation.dialog = SimpleNamespace(remote_uri="sip:441234567890@provider.example")
    consultation.dialog_ids = SimpleNamespace(
        call_id="consultation", remote_tag="remote", local_tag="local"
    )
    consultation.refer = AsyncMock()
    runtime.sip.sip_clients_snapshot = lambda: {"call": source, "consultation": consultation}
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
    runtime.sip.sip_clients_snapshot = lambda: {}
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
