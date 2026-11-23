"""Home Assistant tests for transport-independent phone action dispatch."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from homeassistant.exceptions import ServiceValidationError
import pytest

from custom_components.voip_stack.endpoint_registry import EndpointRegistry
from custom_components.voip_stack.phone_control import (
    CallControlRequest,
    OriginateRequest,
    PhoneActionResult,
    PhoneAdapterRegistry,
    PhoneOperation,
)
from custom_components.voip_stack.phone_endpoint import (
    EndpointAvailability,
    EndpointKind,
    PhoneEndpoint,
)


from .test_managed_phones_ha import (
    custom_integration_path as custom_integration_path,
    phone_lab as phone_lab,
    managed as managed,
)

pytestmark = pytest.mark.ha


def _call(hass, **data):
    return SimpleNamespace(hass=hass, data=data, context=object())


def _endpoint(
    *,
    endpoint_id: str,
    device_id: str,
    name: str,
    kind: EndpointKind,
) -> PhoneEndpoint:
    return PhoneEndpoint(
        endpoint_id=endpoint_id,
        device_id=device_id,
        name=name,
        kind=kind,
        availability=EndpointAvailability.AVAILABLE,
    )


async def test_esphome_originate_uses_native_action_and_canonical_response(managed) -> None:
    from homeassistant.components.esphome.manager import _async_register_service
    from homeassistant.core import Context, ServiceCall
    from .test_managed_phones_ha import phone_record

    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)
    native = lab.native.runtime_data
    advertised = next(service for service in native.services.values()
                      if service.name == "start_call_on_line")
    _async_register_service(lab.hass, native, native.device_info, advertised)
    before = len(lab.sent)
    call = ServiceCall(lab.hass, "voip_stack", "call", {
        "device_id": lab.device.id, "destination": "P4", "send_video": True,
        "receive_video": True,
    }, context=Context())
    result = await lab.runtime.phones.originate(call, OriginateRequest(
        destination="P4", send_video=True, context=call.context,
    ))
    assert lab.sent[before:] == [("start_call_on_line", {
        "dest": "P4", "line": "ha", "send_video": True, "receive_video": True,
    })]
    response = result.as_service_response()
    assert response["schema_version"] == 2
    assert response["phone"] == {
        "device_id": lab.device.id, "kind": "esphome", "name": phone.display_name,
    }
    assert response["call"]["destination"] == "P4"
    assert not lab.runtime.sip.sessions


async def test_sip_account_originate_uses_ephemeral_ha_view(monkeypatch) -> None:
    from custom_components.voip_stack import phone_control
    endpoint = _endpoint(
        endpoint_id="sip:zoiper",
        device_id="device-zoiper",
        name="Zoiper",
        kind=EndpointKind.SIP_ACCOUNT,
    )
    endpoints = EndpointRegistry()
    endpoints.register(endpoint)
    hass = MagicMock()
    call = _call(hass, device_id=endpoint.device_id, destination="P4")

    originate = AsyncMock()
    monkeypatch.setattr(phone_control, "async_originate_browser_call", originate)
    monkeypatch.setattr(phone_control, "_ha_softphone_state", lambda *_: {"call_id": "account-call", "state": "calling"})
    result = await PhoneAdapterRegistry(hass, endpoints).originate(call, OriginateRequest(destination="P4"))
    assert result.call_id == "account-call"
    view = originate.await_args.kwargs["browser_endpoint"]
    assert view.endpoint_id == endpoint.endpoint_id and view.kind is EndpointKind.BROWSER
    assert endpoints.get(endpoint.endpoint_id) is endpoint and endpoint.kind is EndpointKind.SIP_ACCOUNT


async def test_browser_originate_uses_the_same_result_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.voip_stack import phone_control

    endpoint = _endpoint(
        endpoint_id="casa",
        device_id="device-casa",
        name="Casa",
        kind=EndpointKind.BROWSER,
    )
    endpoints = EndpointRegistry()
    endpoints.register(endpoint)
    originate = AsyncMock()
    monkeypatch.setattr(phone_control, "async_originate_browser_call", originate)
    monkeypatch.setattr(
        phone_control,
        "_ha_softphone_state",
        lambda *_args: {"call_id": "call-1", "state": "calling"},
    )
    hass = MagicMock()
    call = _call(hass, destination="P4")

    result = await PhoneAdapterRegistry(
        hass,
        endpoints,
        preferred_phone_device_id=endpoint.device_id,
    ).originate(
        call,
        OriginateRequest(destination="P4"),
    )

    assert isinstance(result, PhoneActionResult)
    assert result.call_id == "call-1"
    assert result.state == "calling"
    originate.assert_awaited_once_with(
        call,
        endpoint_id=endpoint.endpoint_id,
        browser_endpoint=endpoint,
        force_ha_bridge=False,
    )


async def test_implicit_source_uses_the_only_browser_phone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.voip_stack import phone_control

    endpoints = EndpointRegistry()
    endpoint = _endpoint(
        endpoint_id="casa",
        device_id="device-casa",
        name="Casa",
        kind=EndpointKind.BROWSER,
    )
    endpoints.register(endpoint)
    call = _call(MagicMock(), destination="P4")
    originate = AsyncMock()
    monkeypatch.setattr(phone_control, "async_originate_browser_call", originate)
    monkeypatch.setattr(
        phone_control,
        "_ha_softphone_state",
        lambda *_args: {"call_id": "call-1", "state": "calling"},
    )

    result = await PhoneAdapterRegistry(call.hass, endpoints).originate(
        call,
        OriginateRequest(destination="P4"),
    )

    assert result.phone.device_id == "device-casa"
    originate.assert_awaited_once()


async def test_implicit_source_requires_selection_when_two_phones_exist() -> None:
    endpoints = EndpointRegistry()
    for endpoint_id in ("casa", "test"):
        endpoints.register(
            _endpoint(
                endpoint_id=endpoint_id,
                device_id=f"device-{endpoint_id}",
                name=endpoint_id.title(),
                kind=EndpointKind.BROWSER,
            )
        )
    call = _call(MagicMock(), destination="P4")

    with pytest.raises(ServiceValidationError) as raised:
        await PhoneAdapterRegistry(call.hass, endpoints).originate(
            call,
            OriginateRequest(destination="P4"),
        )

    assert raised.value.translation_key == "phone_selection_required"


async def test_esphome_answer_preserves_context_and_control_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.voip_stack import phone_control

    endpoint = _endpoint(
        endpoint_id="esphome:ws3",
        device_id="device-ws3",
        name="WS3",
        kind=EndpointKind.ESPHOME,
    )
    endpoints = EndpointRegistry()
    endpoints.register(endpoint)
    device = {
        "device_id": endpoint.device_id,
        "name": endpoint.name,
        "entities": {"call": "button.ws3_call"},
    }
    hass = MagicMock()
    call = _call(hass, device_id=endpoint.device_id)
    authorize = AsyncMock()
    invoke = AsyncMock()
    monkeypatch.setattr(
        phone_control,
        "async_resolve_source_device",
        AsyncMock(return_value=device),
    )
    monkeypatch.setattr(
        phone_control,
        "has_action",
        lambda _hass, _device, action: action == "answer_call",
    )
    monkeypatch.setattr(
        phone_control,
        "async_require_phone_service_control",
        authorize,
    )
    monkeypatch.setattr(phone_control, "async_call_action", invoke)

    result = await PhoneAdapterRegistry(hass, endpoints).control(
        call,
        PhoneOperation.ANSWER,
        CallControlRequest(context=call.context),
    )

    assert result.operation is PhoneOperation.ANSWER
    authorize.assert_awaited_once_with(
        hass,
        call,
        device=device,
        action_entity_ids=("button.ws3_call",),
    )
    invoke.assert_awaited_once_with(
        hass,
        device,
        "answer_call",
        {},
        context=call.context,
    )


async def test_esphome_control_requires_the_explicit_native_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.voip_stack import phone_control

    endpoint = _endpoint(
        endpoint_id="esphome:ws3",
        device_id="device-ws3",
        name="WS3",
        kind=EndpointKind.ESPHOME,
    )
    endpoints = EndpointRegistry()
    endpoints.register(endpoint)
    monkeypatch.setattr(
        phone_control,
        "async_resolve_source_device",
        AsyncMock(return_value={"device_id": endpoint.device_id, "name": endpoint.name}),
    )
    monkeypatch.setattr(phone_control, "has_action", lambda *_args: False)
    call = _call(MagicMock(), device_id=endpoint.device_id)

    with pytest.raises(ServiceValidationError) as raised:
        await PhoneAdapterRegistry(call.hass, endpoints).control(
            call,
            PhoneOperation.HANGUP,
            CallControlRequest(context=call.context),
        )

    assert raised.value.translation_key == "phone_operation_not_supported"


async def test_esphome_hangup_terminates_owned_pbx_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.voip_stack import (
        endpoint_lifecycle,
        endpoint_termination,
        phone_control,
    )

    endpoint = _endpoint(
        endpoint_id="esphome:p4",
        device_id="device-p4",
        name="P4",
        kind=EndpointKind.ESPHOME,
    )
    endpoints = EndpointRegistry()
    endpoints.register(endpoint)
    device = {
        "endpoint_id": endpoint.endpoint_id,
        "device_id": endpoint.device_id,
        "name": endpoint.name,
        "entities": {"decline": "button.p4_hangup"},
    }
    session = SimpleNamespace(call_id="p4-call", live=True)
    registry = SimpleNamespace(
        sessions={"p4-call": session},
        resolve_session_id=lambda call_id: call_id,
    )
    termination = SimpleNamespace(terminate=AsyncMock(return_value=True))
    invoke = AsyncMock()
    monkeypatch.setattr(
        phone_control,
        "async_resolve_source_device",
        AsyncMock(return_value=device),
    )
    monkeypatch.setattr(
        phone_control,
        "async_require_phone_service_control",
        AsyncMock(),
    )
    monkeypatch.setattr(phone_control, "has_action", lambda *_args: True)
    monkeypatch.setattr(phone_control, "async_call_action", invoke)
    monkeypatch.setattr(endpoint_lifecycle, "call_registry", lambda _hass: registry)
    monkeypatch.setattr(
        endpoint_termination,
        "EndpointTerminationHandler",
        lambda _hass: termination,
    )
    call = _call(MagicMock(), device_id=endpoint.device_id)

    result = await PhoneAdapterRegistry(call.hass, endpoints).control(
        call,
        PhoneOperation.HANGUP,
        CallControlRequest(
            call_id="p4-call",
            reason="local_hangup",
            context=call.context,
        ),
    )

    assert result.call_id == "p4-call"
    termination.terminate.assert_awaited_once()
    invoke.assert_not_awaited()


@pytest.mark.parametrize("selector_kind", ["username", "account_device", "hardware"])
@pytest.mark.parametrize("operation", list(PhoneOperation))
async def test_linked_sip_account_without_ha_binding_uses_native_controls(
    managed, hass_admin_user, selector_kind, operation,
) -> None:
    """Account Device selectors must reach the same native control as hardware."""
    from copy import deepcopy
    from homeassistant.components.esphome.manager import _async_register_service
    from homeassistant.core import Context, ServiceCall
    from homeassistant.helpers import entity_registry as er
    from custom_components.voip_stack.sensor import PhoneEndpointCallStateSensor
    from custom_components.voip_stack.account_runtime import account_has_ha_binding
    from .test_managed_phones_ha import phone_record, phone_subentry

    lab = managed
    entities = er.async_get(lab.hass)
    for role in ("call", "decline"):
        entities.async_get_or_create("button", "esphome", "84c7bb000001-voip_" + role,
            config_entry=lab.native, device_id=lab.device.id)
    await lab.controller.async_sync()
    phone = phone_record(lab)
    endpoint = lab.runtime.endpoints.require(phone.username)
    assert endpoint.kind is EndpointKind.SIP_ACCOUNT
    assert endpoint.device_id == lab.device.id
    assert endpoint.account_device_id and endpoint.account_device_id != endpoint.device_id
    assert not account_has_ha_binding(lab.hass, endpoint.endpoint_id)
    state = PhoneEndpointCallStateSensor(lab.hass, endpoint, lab.runtime.endpoints)
    entities.async_get_or_create("sensor", "voip_stack", state.unique_id,
        config_entry=lab.entry, config_subentry_id=phone_subentry(lab).subentry_id,
        device_id=endpoint.account_device_id)
    selectors = {"username": endpoint.username, "account_device": endpoint.account_device_id,
                 "hardware": endpoint.device_id}
    native = lab.native.runtime_data
    action = {PhoneOperation.ORIGINATE: "start_call_on_line", PhoneOperation.ANSWER: "answer_call",
              PhoneOperation.DECLINE: "decline_call", PhoneOperation.HANGUP: "hangup_call"}[operation]
    advertised = next(row for row in native.services.values() if row.name == action)
    _async_register_service(lab.hass, native, native.device_info, advertised)
    await lab.hass.async_block_till_done()
    original = {key: deepcopy(subentry.as_dict()) for key, subentry in lab.entry.subentries.items()}
    context = Context(user_id=hass_admin_user.id)
    data = {"device_id": selectors[selector_kind]}
    expected = {}
    if operation is PhoneOperation.ORIGINATE:
        data.update(destination="P4", send_video=False, receive_video=False)
        expected = {"dest": "P4", "line": "ha", "send_video": False, "receive_video": False}
    elif operation is PhoneOperation.DECLINE:
        data["reason"] = "declined"
        expected = {"reason": "declined"}
    call = ServiceCall(lab.hass, "voip_stack", "call" if operation is PhoneOperation.ORIGINATE
                       else operation.value, data, context=context)
    offset = len(lab.sent)
    if operation is PhoneOperation.ORIGINATE:
        result = await lab.runtime.phones.originate(call, OriginateRequest(destination="P4", context=context))
    else:
        result = await lab.runtime.phones.control(call, operation,
            CallControlRequest(reason=data.get("reason", ""), context=context))
    assert result.phone.kind is EndpointKind.ESPHOME
    assert result.phone.endpoint_id == endpoint.endpoint_id
    assert result.phone.device_id == lab.device.id
    assert lab.sent[offset:] == [(action, expected)]
    native_event, = [event for event in lab.events if event["domain"] == "esphome"
                     and event["service"].endswith("_" + action)]
    assert native_event["service_data"] == expected
    assert lab.runtime.endpoints.require(phone.username) is endpoint
    assert {key: subentry.as_dict() for key, subentry in lab.entry.subentries.items()} == original
    assert not lab.runtime.sip.sessions


@pytest.mark.parametrize("declared,explicit_client", [(True, False), (True, True), (False, True)])
async def test_linked_account_explicit_or_declared_ha_answer_keeps_browser_transport(
    managed, hass_admin_user, declared, explicit_client,
) -> None:
    from homeassistant.core import Context, ServiceCall
    from homeassistant.helpers import entity_registry as er
    from custom_components.voip_stack.sensor import PhoneEndpointCallStateSensor
    from custom_components.voip_stack.account_runtime import account_has_ha_binding
    from custom_components.voip_stack.ha_bindings import HaCardBindings
    from .test_managed_phones_ha import phone_record, phone_subentry

    lab = managed
    await lab.controller.async_sync()
    endpoint = lab.runtime.endpoints.require(phone_record(lab).username)
    state = PhoneEndpointCallStateSensor(lab.hass, endpoint, lab.runtime.endpoints)
    er.async_get(lab.hass).async_get_or_create("sensor", "voip_stack", state.unique_id,
        config_entry=lab.entry, config_subentry_id=phone_subentry(lab).subentry_id,
        device_id=endpoint.account_device_id)
    owner = HaCardBindings(lab.hass, lab.entry, lab.runtime, lambda _changed: None)
    lab.runtime.ha_bindings = owner
    try:
        if declared:
            await owner.async_set_popup_binding(hass_admin_user.id, "browser-1234567890abcdef",
                                               endpoint.endpoint_id, True)
        assert account_has_ha_binding(lab.hass, endpoint.endpoint_id) is declared
        data = {"device_id": endpoint.account_device_id}
        if explicit_client:
            data["media_client_id"] = "browser-1234567890abcdef"
        call = ServiceCall(lab.hass, "voip_stack", "answer", data,
                           context=Context(user_id=hass_admin_user.id))
        offset = len(lab.sent)
        handle = await lab.runtime.phones.resolve_source(call, PhoneOperation.ANSWER)
        assert handle.kind is EndpointKind.BROWSER
        assert handle.endpoint_id == endpoint.endpoint_id
        assert handle.device_id == endpoint.account_device_id
        assert lab.runtime.endpoints.require(endpoint.endpoint_id) is endpoint
        assert lab.sent[offset:] == []
    finally:
        await owner.async_close()
        lab.runtime.ha_bindings = None


async def test_linked_account_owned_browser_leg_is_not_replaced_by_native_answer(managed):
    from homeassistant.core import ServiceCall
    from .test_managed_phones_ha import phone_record

    lab = managed
    await lab.controller.async_sync()
    endpoint = lab.runtime.endpoints.require(phone_record(lab).username)
    calls = lab.runtime.sip
    with calls.state_operation():
        session = calls.upsert("browser-answer", state="ringing", owner="ha_softphone",
            caller="Caller", callee=endpoint.name, dest_endpoint_id=endpoint.endpoint_id,
            endpoint_id=endpoint.endpoint_id)
        calls.claim_endpoint(session.call_id, endpoint.endpoint_id, role="destination")
        calls.observe_leg(session.call_id, "browser-recipient", role="ha_softphone",
            endpoint_id=endpoint.endpoint_id, state="ringing")
    call = ServiceCall(lab.hass, "voip_stack", "answer", {
        "device_id": endpoint.device_id, "call_id": session.call_id,
    })
    offset = len(lab.sent)
    handle = await lab.runtime.phones.resolve_source(call, PhoneOperation.ANSWER)
    assert handle.kind is EndpointKind.BROWSER
    assert handle.endpoint_id == endpoint.endpoint_id
    assert lab.sent[offset:] == []
    assert calls.get_session(session.call_id) is session and session.live
    calls.release_endpoint_claim(session.call_id, endpoint.endpoint_id)
