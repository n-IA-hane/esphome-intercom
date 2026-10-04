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


async def test_esphome_originate_uses_native_action_and_canonical_response(
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
        "endpoint_id": endpoint.endpoint_id,
        "device_id": endpoint.device_id,
        "name": endpoint.name,
        "route_id": "ws3",
        "entities": {"call": "button.ws3_call"},
    }
    hass = MagicMock()
    call = _call(hass, device_id=endpoint.device_id, destination="P4")
    resolve = AsyncMock(return_value=device)
    authorize = AsyncMock()
    invoke = AsyncMock()
    monkeypatch.setattr(phone_control, "async_resolve_source_device", resolve)
    monkeypatch.setattr(phone_control, "has_action", lambda *_args: True)
    monkeypatch.setattr(
        phone_control,
        "async_require_phone_service_control",
        authorize,
    )
    monkeypatch.setattr(phone_control, "async_call_action", invoke)

    result = await PhoneAdapterRegistry(hass, endpoints).originate(
        call,
        OriginateRequest(
            destination="P4",
            send_video=True,
            context=call.context,
        ),
    )

    invoke.assert_awaited_once_with(
        hass,
        device,
        "start_call",
        {"dest": "P4"},
        context=call.context,
    )
    response = result.as_service_response()
    assert response["schema_version"] == 2
    assert response["phone"] == {
        "device_id": endpoint.device_id,
        "kind": "esphome",
        "name": endpoint.name,
    }
    assert response["call"]["destination"] == "P4"


async def test_sip_account_fails_by_capability_without_browser_resolution() -> None:
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

    with pytest.raises(ServiceValidationError) as raised:
        await PhoneAdapterRegistry(hass, endpoints).originate(
            call,
            OriginateRequest(destination="P4"),
        )

    assert raised.value.translation_key == "phone_operation_not_supported"
    assert raised.value.translation_placeholders == {
        "phone": "Zoiper",
        "operation": "originate",
    }


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
