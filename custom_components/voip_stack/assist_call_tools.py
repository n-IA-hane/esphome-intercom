"""Assist tools bound to an existing call generation and trusted HA context."""

import asyncio

import probatio
from homeassistant.helpers.llm import Tool


def call_for_context(hass, context):
    from .runtime_data import runtime_data
    runtime = runtime_data(hass)
    if runtime is None or runtime.sip is None or context is None:
        return None
    for session in runtime.sip.sessions.values():
        if not session.live:
            continue
        media = runtime.sip.resource_for(session.call_id, "relay")
        conversation = getattr(media, "conversation", None)
        trusted_context = getattr(conversation, "context", None)
        if (trusted_context is not None and trusted_context.id == context.id
                and not getattr(conversation, "detached", False)):
            return session, media
    return None


class CurrentCallTool(Tool):
    """Never accept a model-provided Call-ID, user or privilege selection."""

    def __init__(self, session, media, action):
        self.call_id, self.generation = session.call_id, session.generation
        self.media, self.action = media, action
        self.name = {"status": "VoipCurrentCallStatus", "transfer": "VoipTransferCurrentCall",
                     "hangup": "VoipHangupCurrentCall", "resume": "VoipResumeCurrentCall"}[action]
        self.description = {
            "status": "Read the state and latest transfer result of this telephone conversation.",
            "transfer": "Connect this caller to a configured internal phone, ring group or queue. If unanswered, keep the caller with Assist.",
            "hangup": "End this telephone conversation when the caller asks to finish.",
            "resume": "Cancel this call's pending transfer and resume its Assist conversation.",
        }[action]
        self.parameters = probatio.Schema({probatio.Required("destination"): str}) if action == "transfer" else probatio.Schema({})

    def _current(self, hass, context):
        current = call_for_context(hass, context)
        if current is None:
            return None
        session, media = current
        return session if session.call_id == self.call_id and session.generation == self.generation and media is self.media else None

    async def async_call(self, hass, tool_input, llm_context):
        from .endpoint_lifecycle import call_registry
        from .runtime_data import call_runtime_artifacts, require_runtime_data
        session = self._current(hass, llm_context.context)
        if session is None:
            return {"error": "call_context_expired"}
        registry = call_registry(hass)
        if self.action == "status":
            return {"state": session.state, "transfer_result": session.metadata.get("assist_transfer_result", ""),
                    "destination": session.metadata.get("assist_transfer_destination", "")}
        if self.action == "hangup":
            from .endpoint_session import TerminationIntent
            registry.request_termination(self.call_id, TerminationIntent("assist_hangup"), generation=self.generation)
            return {"status": "ending"}
        task = call_runtime_artifacts(hass).task_for(self.call_id, "forward")
        if self.action == "resume":
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            return {"status": "resumed" if session.live and session.owner == "assist" else "unavailable"}
        if task is not None and not task.done():
            return {"error": "transfer_in_progress"}
        destination = str(tool_input.tool_args.get("destination") or "").strip()
        if not destination or len(destination) > 128:
            return {"error": "invalid_destination"}
        runtime = require_runtime_data(hass)
        from .router import RouteAction, resolve_ha_router
        entries = list(runtime.directory.snapshot()) if runtime.directory is not None else []
        route = resolve_ha_router(destination, entries, trunk_ready=False)
        target = runtime.endpoints.get(str((route.entry.metadata if route.entry else {}).get("endpoint_id") or ""))
        allowed = target is not None or route.action is RouteAction.QUEUE
        if route.action is RouteAction.GROUP and route.entry is not None:
            allowed = all(runtime.endpoints.resolve(member) is not None for member in route.entry.metadata.get("members", ()))
        if not allowed or route.action in {RouteAction.REJECT, RouteAction.BUSY, RouteAction.TRUNK, RouteAction.DIRECT}:
            return {"error": "destination_not_allowed"}
        if registry.forward_call is None:
            return {"error": "routing_unavailable"}
        session.update_metadata(assist_transfer_destination=destination, assist_transfer_result="pending")
        await registry.forward_call(call_id=self.call_id, destination=destination, on_failure="resume")
        task = call_runtime_artifacts(hass).task_for(self.call_id, "forward")
        if task is not None:
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                if not (task.cancelled() and session.live and session.owner == "assist"
                        and not asyncio.current_task().cancelling()):
                    raise
        if session.live and session.owner == "assist":
            session.update_metadata(assist_transfer_result="not_answered")
            return {"status": "not_answered", "resumed": True}
        return {"status": "connected" if session.live else "ended"}


def current_call_tools(hass, context):
    current = call_for_context(hass, context)
    if current is None:
        return []
    return [CurrentCallTool(*current, action) for action in ("status", "transfer", "hangup", "resume")]
