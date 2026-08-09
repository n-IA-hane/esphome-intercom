"""Stateless Home Assistant projections of authoritative PBX sessions."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Literal, Mapping

from homeassistant.core import HomeAssistant

from .endpoint_session import CallToken, EndpointCallSession, TerminationIntent
from .runtime_data import require_runtime_data


@dataclass(frozen=True, slots=True)
class CallProjectionEvent:
    """One immutable request to project an accepted session mutation."""

    token: CallToken
    scope: Literal["phone", "sip_bridge"]
    endpoint_id: str = ""
    leg_id: str = ""
    intent: TerminationIntent | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def phone(
        cls,
        session: EndpointCallSession,
        endpoint_id: str,
        *,
        leg_id: str = "",
        intent: TerminationIntent | None = None,
        **details: Any,
    ) -> CallProjectionEvent:
        return cls(
            session.token,
            "phone",
            endpoint_id,
            leg_id,
            intent,
            MappingProxyType(dict(details)),
        )

    @classmethod
    def bridge(
        cls,
        session: EndpointCallSession,
        *,
        intent: TerminationIntent | None = None,
        **details: Any,
    ) -> CallProjectionEvent:
        return cls(
            session.token,
            "sip_bridge",
            intent=intent,
            details=MappingProxyType(dict(details)),
        )


_RESERVED_FIELDS = {"state", "sip_state", "call_id", "caller", "callee", "endpoint_id"}


def publish_call_projection(
    hass: HomeAssistant,
    session: EndpointCallSession,
    event: CallProjectionEvent,
) -> bool:
    """Project one current generation without owning or changing its state."""

    runtime_data = require_runtime_data(hass)
    runtime = runtime_data.sip
    if (
        runtime is None
        or runtime.get_session(event.token.call_id, generation=event.token.generation)
        is not session
    ):
        return False
    state = event.intent.public_state if event.intent is not None else session.state
    if event.leg_id:
        leg = session.legs.get(event.leg_id)
        if leg is None:
            return False
        state = event.intent.public_state if event.intent is not None else leg.state
    details = {
        key: value
        for key, value in event.details.items()
        if key not in _RESERVED_FIELDS
    }
    metadata = session.metadata
    if event.scope == "phone":
        from .websocket_api import _set_ha_softphone_call_state

        endpoint = runtime_data.endpoints.get(event.endpoint_id)
        _set_ha_softphone_call_state(
            hass,
            state,
            endpoint_id=event.endpoint_id,
            session_device_id=str(
                getattr(endpoint, "device_id", "")
                or metadata.get("session_device_id")
                or ""
            ),
            caller=session.caller,
            callee=session.callee,
            call_id=session.call_id,
            **details,
        )
    else:
        from .websocket_api import _set_sip_bridge_call_state

        _set_sip_bridge_call_state(
            hass,
            state,
            call_id=session.call_id,
            dest_call_id=str(metadata.get("bridge_dest_call_id") or ""),
            caller=session.caller,
            callee=session.callee,
            **details,
        )
    return True
