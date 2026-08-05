"""Typed dispatch for public phone control actions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from homeassistant.core import Context, HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN
from .endpoint_registry import EndpointRegistry
from .esphome_actions import (
    async_call_action,
    async_resolve_source_device,
    has_action,
)
from .phone_endpoint import EndpointKind, PhoneEndpoint
from .service_endpoints import async_require_phone_service_control
from .softphone_originate import async_originate_browser_call
from .softphone_answer import async_answer_browser_call
from .softphone_commands import (
    async_decline_browser_call,
    async_resolve_browser_call_command,
)
from .softphone_termination import async_hangup_browser_call
from .websocket_api import _ha_softphone_state


class PhoneOperation(StrEnum):
    """Operations exposed through the common phone service boundary."""

    ORIGINATE = "originate"
    ANSWER = "answer"
    DECLINE = "decline"
    HANGUP = "hangup"


ALL_PHONE_OPERATIONS = frozenset(PhoneOperation)


@dataclass(frozen=True, slots=True)
class PhoneHandle:
    """Resolved local phone and its control surface."""

    endpoint_id: str
    device_id: str
    name: str
    kind: EndpointKind
    capabilities: frozenset[PhoneOperation]
    transport_data: Any = field(default=None, repr=False, compare=False)

    def supports(self, operation: PhoneOperation) -> bool:
        """Return whether this phone supports one public operation."""

        return operation in self.capabilities


@dataclass(frozen=True, slots=True)
class OriginateRequest:
    """Transport-independent request to originate one call."""

    destination: str
    send_video: bool = False
    force_ha_bridge: bool = False
    context: Context | None = None


@dataclass(frozen=True, slots=True)
class CallControlRequest:
    """Transport-independent request targeting one active call."""

    call_id: str = ""
    reason: str = ""
    context: Context | None = None


@dataclass(frozen=True, slots=True)
class PhoneActionResult:
    """Canonical result returned by every phone adapter."""

    operation: PhoneOperation
    phone: PhoneHandle
    destination: str = ""
    accepted: bool = True
    call_id: str = ""
    state: str = "accepted"
    def as_service_response(self) -> dict[str, object]:
        """Serialize the canonical service response."""

        response: dict[str, object] = {
            "schema_version": 2,
            "success": self.accepted,
            "operation": self.operation.value,
            "phone": {
                "device_id": self.phone.device_id,
                "kind": self.phone.kind.value,
                "name": self.phone.name,
            },
            "call": {
                "call_id": self.call_id,
                "state": self.state,
                "destination": self.destination,
            },
        }
        return response


class PhoneAdapter(Protocol):
    """Transport-specific implementation behind the common dispatcher."""

    async def originate(
        self,
        phone: PhoneHandle,
        request: OriginateRequest,
        *,
        call: ServiceCall,
    ) -> PhoneActionResult: ...

    async def control(
        self,
        phone: PhoneHandle,
        operation: PhoneOperation,
        request: CallControlRequest,
        *,
        call: ServiceCall,
    ) -> PhoneActionResult: ...


class BrowserPhoneAdapter:
    """Control a logical Home Assistant browser phone."""

    async def originate(
        self,
        phone: PhoneHandle,
        request: OriginateRequest,
        *,
        call: ServiceCall,
    ) -> PhoneActionResult:
        await async_originate_browser_call(
            call,
            endpoint_id=phone.endpoint_id,
            browser_endpoint=phone.transport_data,
            force_ha_bridge=request.force_ha_bridge,
        )
        snapshot = _ha_softphone_state(call.hass, phone.endpoint_id)
        return PhoneActionResult(
            operation=PhoneOperation.ORIGINATE,
            phone=phone,
            destination=request.destination,
            call_id=str(snapshot.get("call_id") or ""),
            state=str(snapshot.get("state") or "accepted"),
        )

    async def control(
        self,
        phone: PhoneHandle,
        operation: PhoneOperation,
        request: CallControlRequest,
        *,
        call: ServiceCall,
    ) -> PhoneActionResult:
        command = await async_resolve_browser_call_command(
            call.hass,
            call,
            endpoint_id=phone.endpoint_id,
            endpoint=phone.transport_data,
        )
        if operation is PhoneOperation.ANSWER:
            await async_answer_browser_call(call.hass, call, command)
        elif operation is PhoneOperation.DECLINE:
            await async_decline_browser_call(call.hass, call, command)
        else:
            await async_hangup_browser_call(call.hass, command)
        return PhoneActionResult(
            operation=operation,
            phone=phone,
            call_id=command.call_id or request.call_id,
        )


class EspHomePhoneAdapter:
    """Control a physical ESPHome phone through native API actions."""

    async def originate(
        self,
        phone: PhoneHandle,
        request: OriginateRequest,
        *,
        call: ServiceCall,
    ) -> PhoneActionResult:
        device = phone.transport_data
        call_entity = str((device.get("entities") or {}).get("call") or "").strip()
        await async_require_phone_service_control(
            call.hass,
            call,
            device=device,
            action_entity_ids=(call_entity,) if call_entity else (),
        )
        await async_call_action(
            call.hass,
            device,
            "start_call",
            {"dest": request.destination},
            context=request.context,
        )
        return PhoneActionResult(
            operation=PhoneOperation.ORIGINATE,
            phone=phone,
            destination=request.destination,
        )

    async def control(
        self,
        phone: PhoneHandle,
        operation: PhoneOperation,
        request: CallControlRequest,
        *,
        call: ServiceCall,
    ) -> PhoneActionResult:
        device = phone.transport_data
        entities = device.get("entities") or {}
        entity_key = "call" if operation is PhoneOperation.ANSWER else "decline"
        entity_id = str(entities.get(entity_key) or "").strip()
        await async_require_phone_service_control(
            call.hass,
            call,
            device=device,
            action_entity_ids=(entity_id,) if entity_id else (),
        )
        action = {
            PhoneOperation.ANSWER: "answer_call",
            PhoneOperation.DECLINE: "decline_call",
            PhoneOperation.HANGUP: "hangup_call",
        }[operation]
        data = {"reason": request.reason} if operation is PhoneOperation.DECLINE else {}
        if not has_action(call.hass, device, action):
            raise _unsupported(phone, operation)
        await async_call_action(
            call.hass,
            device,
            action,
            data,
            context=request.context,
        )
        return PhoneActionResult(
            operation=operation,
            phone=phone,
            call_id=request.call_id,
        )


def _unsupported(
    phone: PhoneHandle,
    operation: PhoneOperation,
) -> ServiceValidationError:
    return ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="phone_operation_not_supported",
        translation_placeholders={
            "phone": phone.name,
            "operation": operation.value,
        },
    )


class PhoneAdapterRegistry:
    """Resolve one local phone and dispatch through its transport adapter."""

    def __init__(
        self,
        hass: HomeAssistant,
        endpoints: EndpointRegistry,
        *,
        preferred_phone_device_id: str = "",
    ) -> None:
        self._hass = hass
        self._endpoints = endpoints
        self._preferred_phone_device_id = str(
            preferred_phone_device_id or ""
        ).strip()
        self._adapters: dict[EndpointKind, PhoneAdapter] = {
            EndpointKind.BROWSER: BrowserPhoneAdapter(),
            EndpointKind.ESPHOME: EspHomePhoneAdapter(),
        }

    async def originate(
        self,
        call: ServiceCall,
        request: OriginateRequest,
    ) -> PhoneActionResult:
        """Resolve, authorize and originate from exactly one local phone."""

        if not request.destination:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="destination_required",
            )
        phone = await self._resolve_source(call)
        if not phone.supports(PhoneOperation.ORIGINATE):
            raise _unsupported(phone, PhoneOperation.ORIGINATE)
        return await self._adapters[phone.kind].originate(
            phone,
            request,
            call=call,
        )

    async def control(
        self,
        call: ServiceCall,
        operation: PhoneOperation,
        request: CallControlRequest,
    ) -> PhoneActionResult:
        """Dispatch answer, decline or hangup through the selected phone."""

        phone = await self._resolve_source(call)
        if not phone.supports(operation):
            raise _unsupported(phone, operation)
        return await self._adapters[phone.kind].control(
            phone,
            operation,
            request,
            call=call,
        )

    async def _resolve_source(self, call: ServiceCall) -> PhoneHandle:
        selector = str(call.data.get("device_id") or "").strip()
        if not selector:
            selector = self._preferred_phone_device_id
        if not selector:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="phone_selection_required",
            )
        endpoint = self._endpoints.resolve(selector)
        if endpoint is not None:
            if endpoint.kind is EndpointKind.SIP_ACCOUNT:
                return self._endpoint_handle(endpoint, frozenset())
            if endpoint.kind is EndpointKind.BROWSER:
                return self._endpoint_handle(endpoint, ALL_PHONE_OPERATIONS)

        device = await async_resolve_source_device(
            self._hass,
            call,
            selector=selector,
        )
        if device is not None:
            live_endpoint = self._endpoints.by_device_id(
                str(device.get("device_id") or "")
            )
            capabilities = set()
            if has_action(self._hass, device, "start_call"):
                capabilities.add(PhoneOperation.ORIGINATE)
            if has_action(self._hass, device, "answer_call"):
                capabilities.add(PhoneOperation.ANSWER)
            if has_action(self._hass, device, "decline_call"):
                capabilities.add(PhoneOperation.DECLINE)
            if has_action(self._hass, device, "hangup_call"):
                capabilities.add(PhoneOperation.HANGUP)
            return PhoneHandle(
                endpoint_id=str(
                    getattr(live_endpoint, "endpoint_id", "")
                    or device.get("endpoint_id")
                    or f"esphome:{device.get('device_id') or ''}"
                ),
                device_id=str(device.get("device_id") or ""),
                name=str(device.get("name") or "ESPHome phone"),
                kind=EndpointKind.ESPHOME,
                capabilities=frozenset(capabilities),
                transport_data=device,
            )

        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="unknown_phone_device",
        )

    @staticmethod
    def _endpoint_handle(
        endpoint: PhoneEndpoint | None,
        capabilities: frozenset[PhoneOperation],
    ) -> PhoneHandle:
        return PhoneHandle(
            endpoint_id=str(getattr(endpoint, "endpoint_id", "") or ""),
            device_id=str(getattr(endpoint, "device_id", "") or ""),
            name=str(getattr(endpoint, "name", "") or "Home Assistant"),
            kind=getattr(endpoint, "kind", EndpointKind.BROWSER),
            capabilities=capabilities,
            transport_data=endpoint,
        )
