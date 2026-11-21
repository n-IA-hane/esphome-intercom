"""One automation decision window per stage on the existing call generation."""

import asyncio
import time

from .call_projection import publish_bridge_projection
from .endpoint_lifecycle import call_registry

SIP_ROUTE_DECISION_TIMEOUT = 1.5
_RESULT = "routing_decisions"


async def request_route_window(hass, session, *, invite=None, decision=None,
                               timeout=SIP_ROUTE_DECISION_TIMEOUT, stage="initial", **presentation):
    """Share the original future, deadline and accepted result without a registry."""
    registry = call_registry(hass)
    if registry.get_session(session.call_id, generation=session.generation) is not session or not session.live:
        return {"action": "cancel"}
    scope = (stage, str(getattr(invite, "call_id", "") or session.call_id))
    cached = session.metadata.get(_RESULT) or {}
    if scope in cached:
        return dict(cached[scope])
    existing = registry.artifact_for(session.call_id, "pending_route")
    if existing is not None and existing.get("initial_route_window"):
        result = dict(await asyncio.shield(existing["task"]))
        if existing["decision_scope"] == scope:
            return result
        return await request_route_window(hass, session, invite=invite, decision=decision,
            timeout=timeout, stage=stage, **presentation)
    if existing is not None:
        raise RuntimeError("Another route operation owns this call's decision")
    future = asyncio.get_running_loop().create_future()
    now = time.time()
    deadline = now + float(timeout)
    route = {"future": future, "invite": invite, "decision": decision,
             "created_at": now, "expires_at": deadline, "decision_deadline": deadline,
             "initial_route_window": True, "decision_scope": scope,
             "fallback_destination": presentation.get("fallback_destination", session.callee)}
    registry.set_pending_route(session.call_id, route)

    async def settle():
        try:
            try:
                payload = await asyncio.wait_for(asyncio.shield(future), max(0, deadline - time.time()))
            except TimeoutError:
                payload = {}
                if not future.done():
                    future.set_result(payload)
            payload = dict(payload) if isinstance(payload, dict) else {}
            if session.live:
                results = dict(session.metadata.get(_RESULT) or {})
                results[scope] = payload
                session.update_metadata(**{_RESULT: results})
            return payload
        finally:
            if not future.done():
                future.cancel()
            if registry.artifact_for(session.call_id, "pending_route") is route:
                registry.take_pending_route(session.call_id)

    route["task"] = session.create_task(settle(), name="route-decision")
    publish_bridge_projection(hass, session, route_request=True, phase="route_decision",
        expires_at=deadline, decision_deadline=deadline, decision_timeout_ms=int(float(timeout) * 1000),
        **presentation)
    return dict(await asyncio.shield(route["task"]))
