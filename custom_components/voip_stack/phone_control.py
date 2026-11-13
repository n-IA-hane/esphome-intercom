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
from .phone_endpoint import EndpointKind, PhoneEndpoint, ha_client_endpoint
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
            allow_terminated=operation in {PhoneOperation.HANGUP, PhoneOperation.DECLINE},
        )
        if command.already_terminated:
            return PhoneActionResult(
                operation=operation, phone=phone, call_id=command.call_id, state="idle"
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


class SipAccountPhoneAdapter:
    """Hang up or decline HA-owned dialogs.

    Answer and originate remain unsupported without a verified device control
    capability; SIP registration alone does not provide either operation.
    """

    async def originate(self, phone, request, *, call):
        raise _unsupported(phone, PhoneOperation.ORIGINATE)

    async def control(self, phone, operation, request, *, call):
        from .dial_fork import ForkAnswerGate
        from .endpoint_lifecycle import call_registry
        from .endpoint_session import TerminationIntent, TerminationInitiator
        from .endpoint_termination import EndpointTerminationHandler
        from .runtime_data import endpoint_directory

        registry = call_registry(call.hass)
        endpoint = endpoint_directory(call.hass).get(phone.endpoint_id)
        call_id = request.call_id or str(getattr(endpoint, "active_call_id", "") or "")
        expected = call.data.get("expected_generation")
        if call_id and registry.is_terminated_for_endpoint(call_id, phone.endpoint_id):
            if expected is not None and not registry.is_terminated(call_id, generation=int(expected)):
                raise ServiceValidationError("The selected call has ended or changed generation")
            return PhoneActionResult(operation=operation, phone=phone, call_id=call_id, state="idle")
        session = registry.get_session(call_id) if call_id else None
        if session is None or not session.live:
            raise ServiceValidationError("Select an active PBX call belonging to this phone")
        if expected is not None and session.generation != int(expected):
            raise ServiceValidationError("The selected call has ended or changed generation")
        token = session.token
        fork = registry.resource_for(session.call_id, "fork_answer")
        attempts = [attempt for attempt in fork.attempts if attempt.endpoint_id == phone.endpoint_id] if isinstance(fork, ForkAnswerGate) else []
        if phone.endpoint_id not in session.endpoint_claims:
            if attempts and not any(leg.endpoint_id == phone.endpoint_id and not leg.closed for leg in session.legs.values()):
                return PhoneActionResult(operation=operation, phone=phone, call_id=session.call_id, state="idle")
            raise ServiceValidationError("The selected call belongs to another phone endpoint")
        source = session.endpoint_claims[phone.endpoint_id] == "source"
        legs = [leg for leg in session.legs.values() if leg.endpoint_id == phone.endpoint_id and not leg.closed]
        connected = any(leg.state == "in_call" or leg.phase in {"answered", "held"}
            or (leg.dialog is not None and getattr(leg.dialog, "dialog", None) is not None) for leg in legs)
        candidates = [leg for leg in legs if any(attempt.client is leg.dialog for attempt in attempts)]
        reason = request.reason or ("local_hangup" if operation is PhoneOperation.HANGUP else "declined")
        if operation is PhoneOperation.DECLINE:
            status = int(call.data.get("status") or 603)
            reason = str(call.data.get("decline_reason") or "").strip() or {
                486: "busy", 487: "cancelled", 603: "declined",
            }.get(status, reason)
        if not source and not connected and candidates:
            if len(candidates) != 1:
                raise ServiceValidationError("Select an unambiguous SIP leg")
            await registry.close_leg(session.call_id, candidates[0].leg_id, reason=reason)
            if registry.get_session(session.call_id, generation=token.generation) is session and session.live:
                registry.release_endpoint_claim(session.call_id, phone.endpoint_id)
        else:
            intent = TerminationIntent(reason, initiator=TerminationInitiator.LOCAL_USER)
            if operation is PhoneOperation.DECLINE and not session.answer_committed:
                intent = TerminationIntent.final_response(reason, status, TerminationInitiator.LOCAL_USER)
            await EndpointTerminationHandler(call.hass).terminate(session.call_id, intent)
        return PhoneActionResult(operation=operation, phone=phone, call_id=session.call_id, state="idle")

class EspHomePhoneAdapter:
    """Control a physical ESPHome phone through native API actions."""

    async def originate(
        self,
        phone: PhoneHandle,
        request: OriginateRequest,
        *,
        call: ServiceCall,
    ) -> PhoneActionResult:
        if call.data.get("trunk_id"):
            from .esphome_origination import originate_with_trunk
            call_id = await originate_with_trunk(call, phone, request.destination)
            return PhoneActionResult(operation=PhoneOperation.ORIGINATE, phone=phone,
                destination=request.destination, call_id=call_id)
        device = phone.transport_data
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
        if operation is PhoneOperation.HANGUP and request.call_id:
            from .endpoint_lifecycle import call_registry
            from .endpoint_session import TerminationIntent
            from .endpoint_termination import EndpointTerminationHandler

            registry = call_registry(call.hass)
            session = registry.sessions.get(
                registry.resolve_session_id(request.call_id)
            )
            if session is not None and session.live:
                await EndpointTerminationHandler(call.hass).terminate(
                    session.call_id,
                    TerminationIntent.bye(request.reason or "local_hangup"),
                )
                return PhoneActionResult(
                    operation=operation,
                    phone=phone,
                    call_id=session.call_id,
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
            EndpointKind.SIP_ACCOUNT: SipAccountPhoneAdapter(),
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
        phone = await self.resolve_source(call, PhoneOperation.ORIGINATE)
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

        phone = await self.resolve_source(call, operation)
        return await self._adapters[phone.kind].control(
            phone,
            operation,
            request,
            call=call,
        )

    async def resolve_source(
        self,
        call: ServiceCall,
        operation: PhoneOperation | None = None,
    ) -> PhoneHandle:
        """Resolve and authorize one local phone through the common boundary."""

        phone = await self._resolve_source(call, operation)
        if operation is not None and not phone.supports(operation):
            raise _unsupported(phone, operation)
        if phone.kind is EndpointKind.ESPHOME:
            device = phone.transport_data
            entities = device.get("entities") or {}
            role = (
                "call"
                if operation in {None, PhoneOperation.ORIGINATE, PhoneOperation.ANSWER}
                else "decline"
            )
            entity_id = str(entities.get(role) or "").strip()
            await async_require_phone_service_control(
                call.hass,
                call,
                device=device,
                action_entity_ids=(entity_id,) if entity_id else (),
            )
        else:
            await async_require_phone_service_control(
                call.hass,
                call,
                endpoint=phone.transport_data,
            )
        return phone

    def set_preferred_phone_device_id(self, device_id: str) -> None:
        """Update the derived default without persisting another account list."""
        self._preferred_phone_device_id = str(device_id or "")

    async def _resolve_source(self, call: ServiceCall, operation=None) -> PhoneHandle:
        selector = str(call.data.get("device_id") or "").strip()
        if not selector:
            selector = self._preferred_phone_device_id
        if not selector:
            from .runtime_data import preferred_browser_phone, runtime_data
            if runtime_data(self._hass) is not None:
                preferred = preferred_browser_phone(self._hass)
                if preferred is not None:
                    selector = preferred.device_id
            else:
                # Standalone legacy browser registries have no SIP/account
                # association owner. Do not guess among ordinary SIP accounts.
                legacy = [item for item in self._endpoints.endpoints if item.kind is EndpointKind.BROWSER]
                if len(legacy) == 1:
                    selector = legacy[0].device_id
        if not selector:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="phone_selection_required",
            )
        endpoint = self._endpoints.resolve(selector)
        if endpoint is not None:
            if endpoint.kind is EndpointKind.SIP_ACCOUNT:
                from .runtime_data import runtime_data
                from .websocket_api import _account_browser_media
                runtime = runtime_data(self._hass)
                call_id = str(call.data.get("call_id") or endpoint.active_call_id or "")
                media = bool(runtime is not None and runtime.sip is not None
                    and _account_browser_media(runtime, runtime.sip, endpoint.endpoint_id, call_id))
                if operation in {PhoneOperation.ORIGINATE, PhoneOperation.ANSWER} or media:
                    return self._endpoint_handle(ha_client_endpoint(endpoint), ALL_PHONE_OPERATIONS)
                return self._endpoint_handle(endpoint, frozenset({PhoneOperation.HANGUP, PhoneOperation.DECLINE}))
            if endpoint.kind.is_softphone:
                capabilities = ALL_PHONE_OPERATIONS
                return self._endpoint_handle(endpoint, capabilities)

        for entry in self._hass.config_entries.async_entries(DOMAIN):
            runtime = getattr(entry, "runtime_data", None)
            if getattr(runtime, "endpoints", None) is not self._endpoints:
                continue
            from .physical_phone import physical_phone_for_device, physical_line_endpoint
            physical = physical_phone_for_device(self._hass, entry, selector)
            if physical is not None:
                try:
                    endpoint = physical_line_endpoint(runtime, physical,
                        line_id=str(call.data.get("line_id") or ""), call_id=str(call.data.get("call_id") or ""))
                except ValueError as error:
                    raise ServiceValidationError("Select an unambiguous SIP line using line_id or call_id") from error
                if endpoint.kind is not EndpointKind.SIP_ACCOUNT:
                    raise ServiceValidationError("Physical phone lines must bind configured SIP accounts")
                return self._endpoint_handle(endpoint, frozenset({PhoneOperation.HANGUP, PhoneOperation.DECLINE}))

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
