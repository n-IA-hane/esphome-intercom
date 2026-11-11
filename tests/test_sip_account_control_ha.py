"""PBX controls real registered SIP legs without claiming hardware control."""

import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import ServiceValidationError, Unauthorized

from custom_components.voip_stack.call_queues import QueueDefinition, QueueManager, QueuePolicy
from custom_components.voip_stack.phone_control import CallControlRequest, PhoneOperation
from custom_components.voip_stack.roster import RosterEntry
from .test_sip_phone_transfers_ha import registered_phones as registered_phones
from .test_trunk_calls_ha import lab as lab, trunks as trunks
from .test_sip_capabilities_ha import wait_until
from .test_managed_phones_ha import phone_lab as phone_lab, custom_integration_path as custom_integration_path

pytestmark = pytest.mark.ha


@pytest.fixture
async def ringing_group(trunks, registered_phones, request):
    lab = trunks
    kind = request.param
    manager = None
    if kind == "queue":
        entry = lab.hass.config_entries.async_entries("voip_stack")[0]
        manager = QueueManager(lab.hass, entry)
        manager.definitions["controlled-queue"] = QueueDefinition("controlled-queue", "Controlled", ("transfer_b", "transfer_c"))
        manager.policies["controlled-queue"] = QueuePolicy(ring_timeout=5)
        manager.agent_states["controlled-queue"] = {"transfer_b": "available", "transfer_c": "available"}
        lab.runtime.queues = manager
        lab.roster.extend(manager.roster_entries())
    else:
        lab.roster.append(RosterEntry("Controlled", ha_bridge=True,
            metadata={"group_type": "ring", "members": ["transfer_b", "transfer_c"]}))
    caller, first, second = registered_phones
    await asyncio.to_thread(caller.command, "/dial Controlled")
    await wait_until(lambda: any(session.endpoint_claims.get("transfer_b") and session.endpoint_claims.get("transfer_c")
        for session in lab.calls.sessions.values()))
    session = next(iter(lab.calls.sessions.values()))
    await wait_until(lambda: all(any(leg.endpoint_id == identity and leg.state == "remote_ringing"
        for leg in session.legs.values()) for identity in ("transfer_b", "transfer_c")))
    yield SimpleNamespace(lab=lab, caller=caller, first=first, second=second, session=session, manager=manager)
    await asyncio.to_thread(caller.hangup)
    await wait_until(lambda: not lab.calls.sessions)
    if manager is not None:
        await manager.close()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": ("transfer_b", "transfer_c")}], indirect=True)
@pytest.mark.parametrize("ringing_group", ["group", "queue"], indirect=True)
@pytest.mark.parametrize("operation", ["hangup", "decline"])
async def test_sip_caller_control_ends_every_owned_offer(ringing_group, operation):
    env = ringing_group
    session = env.session
    await env.lab.hass.services.async_call("voip_stack", operation, {
        "device_id": "transfer_a", "call_id": session.call_id,
    }, blocking=True)
    await asyncio.wait_for(session.terminated.wait(), 3)
    assert not env.lab.calls.sessions and not session.tasks
    assert session.terminal_reason == ("local_hangup" if operation == "hangup" else "declined")
    assert all(not endpoint.active_call_id for endpoint in env.lab.runtime.endpoints.endpoints)
    assert not env.lab.runtime.rtp_port_pool.get("used")
    if env.manager is not None:
        assert not env.manager.waiting and not env.manager.reservations
    # A repeated command cannot affect another call or revive this generation.
    await env.lab.hass.services.async_call("voip_stack", operation, {
        "device_id": "transfer_a", "call_id": session.call_id,
    }, blocking=True)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": ("transfer_b", "transfer_c")}], indirect=True)
@pytest.mark.parametrize("ringing_group", ["group", "queue"], indirect=True)
@pytest.mark.parametrize("operation", ["hangup", "decline"])
async def test_sip_recipient_control_retires_only_its_fork_offer(ringing_group, operation):
    env = ringing_group
    session = env.session
    await env.lab.hass.services.async_call("voip_stack", operation, {
        "device_id": "transfer_b", "call_id": session.call_id,
    }, blocking=True)
    assert session.live and not session.terminated.is_set()
    assert env.lab.runtime.endpoints.get("transfer_a").active_call_id == session.call_id
    assert not env.lab.runtime.endpoints.get("transfer_b").active_call_id
    assert env.lab.runtime.endpoints.get("transfer_c").active_call_id == session.call_id
    await asyncio.to_thread(env.second.command, "/accept")
    await wait_until(lambda: session.metadata.get("dest_endpoint_id") == "transfer_c")
    relay = env.lab.calls.resource_for(session.call_id, "relay")
    await wait_until(lambda: relay.left_tx_packets > 5 and relay.right_tx_packets > 5)
    assert env.caller.read().count("Call established") == 1
    assert "Call established" not in env.first.read()
    await env.lab.hass.services.async_call("voip_stack", "hangup", {
        "device_id": "transfer_c", "call_id": session.call_id,
    }, blocking=True)
    await asyncio.wait_for(session.terminated.wait(), 3)
    assert not env.lab.calls.sessions and not session.tasks and not env.lab.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_sip_control_rejects_wrong_endpoint_generation_acl_and_unsupported_hardware(trunks, registered_phones, hass_read_only_user):
    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.helpers import device_registry as dr
    from custom_components.voip_stack.physical_phone import normalize_physical_phone, sync_physical_phone_devices
    lab = trunks
    caller = registered_phones[0]
    await asyncio.to_thread(caller.command, "/dial transfer_b")
    await wait_until(lambda: any(session.metadata.get("dest_endpoint_id") == "transfer_b" for session in lab.calls.sessions.values()))
    session = next(iter(lab.calls.sessions.values()))
    for data in ({"device_id": "transfer_c", "call_id": session.call_id},
                 {"device_id": "transfer_b", "call_id": session.call_id, "expected_generation": session.generation + 1}):
        with pytest.raises(ServiceValidationError):
            await lab.hass.services.async_call("voip_stack", "hangup", data, blocking=True)
        assert session.live
    with pytest.raises(Unauthorized):
        await lab.hass.services.async_call("voip_stack", "hangup", {
            "device_id": "transfer_b", "call_id": session.call_id,
        }, blocking=True, context=Context(user_id=hass_read_only_user.id))
    # Exercise the adapter guard too, independently of the outer hangup service.
    with pytest.raises(ServiceValidationError, match="generation"):
        await lab.runtime.phones.control(ServiceCall(lab.hass, "voip_stack", "decline", {
            "device_id": "transfer_b", "call_id": session.call_id, "expected_generation": session.generation + 1,
        }), PhoneOperation.DECLINE, CallControlRequest(call_id=session.call_id))
    with pytest.raises(ServiceValidationError) as denied:
        await lab.hass.services.async_call("voip_stack", "answer", {
            "device_id": "transfer_b", "call_id": session.call_id,
        }, blocking=True)
    assert denied.value.translation_key == "phone_operation_not_supported"
    assert session.live
    # Generic physical inventory resolves a registered line, not a vendor HTTP
    # endpoint. Its second configured line must never be guessed for this call.
    entry = lab.hass.config_entries.async_entries("voip_stack")[0]
    physical = normalize_physical_phone({"name": "Generic Fanvil", "mac": "0200000000ac", "profile_id": "fanvil/x3u-pro",
        "lines": [{"slot": "1", "endpoint_id": "transfer_b"}, {"slot": "2", "endpoint_id": "transfer_c"}]})
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type="physical_phone",
        title=physical["name"], unique_id=physical["phone_id"], data=physical))
    sync_physical_phone_devices(lab.hass, entry)
    device = next(device for device in dr.async_entries_for_config_entry(dr.async_get(lab.hass), entry.entry_id)
        if ("voip_stack", "physical_phone:" + physical["phone_id"]) in device.identifiers)
    logical_devices = {identity: lab.runtime.endpoints.get(identity).device_id for identity in ("transfer_b", "transfer_c")}
    await lab.hass.services.async_call("voip_stack", "hangup", {
        "device_id": device.id, "call_id": session.call_id,
    }, blocking=True)
    await asyncio.wait_for(session.terminated.wait(), 3)
    await lab.hass.services.async_call("voip_stack", "hangup", {
        "device_id": device.id, "call_id": session.call_id, "expected_generation": session.generation,
    }, blocking=True)
    assert {identity: lab.runtime.endpoints.get(identity).device_id for identity in logical_devices} == logical_devices
    with pytest.raises(ServiceValidationError, match="generation"):
        await lab.hass.services.async_call("voip_stack", "hangup", {
            "device_id": device.id, "call_id": session.call_id, "expected_generation": session.generation + 1,
        }, blocking=True)
    with pytest.raises(ServiceValidationError):
        await lab.hass.services.async_call("voip_stack", "hangup", {
            "device_id": "transfer_c", "call_id": session.call_id, "expected_generation": session.generation,
        }, blocking=True)


async def test_physical_device_line_resolution_never_guesses_or_changes_logical_device_ids(phone_lab):
    from homeassistant.config_entries import ConfigSubentry
    from homeassistant.helpers import device_registry as dr
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.physical_phone import normalize_physical_phone, sync_physical_phone_devices
    lab = phone_lab
    for identity in ("line-a", "line-b"):
        lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id=identity, name=identity, username=identity,
            device_id="logical-" + identity, kind=EndpointKind.SIP_ACCOUNT))
    data = normalize_physical_phone({"name": "Generic Fanvil", "mac": "0200000000ab", "profile_id": "fanvil/x3u-pro",
        "lines": [{"slot": "1", "endpoint_id": "line-a"}, {"slot": "2", "endpoint_id": "line-b"}]})
    item = ConfigSubentry(subentry_type="physical_phone", title=data["name"], unique_id=data["phone_id"], data=data)
    lab.hass.config_entries.async_add_subentry(lab.entry, item)
    sync_physical_phone_devices(lab.hass, lab.entry)
    device = next(device for device in dr.async_entries_for_config_entry(dr.async_get(lab.hass), lab.entry.entry_id)
        if ("voip_stack", "physical_phone:" + data["phone_id"]) in device.identifiers)
    with pytest.raises(ServiceValidationError):
        await lab.runtime.phones.resolve_source(ServiceCall(lab.hass, "voip_stack", "hangup", {"device_id": device.id}), PhoneOperation.HANGUP)
    call = ServiceCall(lab.hass, "voip_stack", "hangup", {"device_id": device.id, "line_id": "2"})
    resolved = await lab.runtime.phones.resolve_source(call, PhoneOperation.HANGUP)
    assert resolved.endpoint_id == "line-b"
    lab.runtime.endpoints.claim_call("line-a", "active-a")
    resolved = await lab.runtime.phones.resolve_source(ServiceCall(lab.hass, "voip_stack", "hangup", {
        "device_id": device.id, "call_id": "active-a",
    }), PhoneOperation.HANGUP)
    assert resolved.endpoint_id == "line-a"
    assert lab.runtime.endpoints.get("line-a").device_id == "logical-line-a"
    assert lab.runtime.endpoints.get("line-b").device_id == "logical-line-b"
    lab.runtime.endpoints.release_call("line-a", "active-a")
