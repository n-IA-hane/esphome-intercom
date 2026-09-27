"""Authenticated native call controls, delegated to the existing phone services."""

from __future__ import annotations

import asyncio
import time
from urllib.parse import urlencode

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import Context, callback

from .authorization import require_http_control
from .companion_protocol import CALL_PATH, companion_endpoint_id
from .endpoint_lifecycle import call_registry
from .runtime_data import require_runtime_data, registration_data
from .websocket_api import HA_SOFTPHONE_STATE_EVENT, _ha_softphone_state


class CompanionCallView(HomeAssistantView):
    url = CALL_PATH
    name = "api:voip_stack:companion:call"
    requires_auth = True

    def _resolve(self, request):
        user_id = require_http_control(request)
        hass = request.app["hass"]
        runtime = require_runtime_data(hass)
        manager = runtime.companion_phones
        if manager is None:
            raise web.HTTPServiceUnavailable()
        registration_id = str(request.query.get("registration_id", ""))
        binding = manager.bindings.get(companion_endpoint_id(registration_id))
        if binding is None or binding.user_id != user_id:
            raise web.HTTPForbidden()
        call_id = str(request.query.get("call_id", ""))
        try:
            generation = int(request.query.get("generation", ""))
        except ValueError as err:
            raise web.HTTPBadRequest(text="Missing call generation") from err
        invitation = manager.invitations.get(binding.endpoint_id)
        registry = call_registry(hass)
        endpoint = runtime.endpoints.get(binding.endpoint_id)
        if (invitation is None or invitation.token.call_id != call_id
            or invitation.token.generation != generation
            or endpoint is None or not endpoint.active_call_id
            or registry.resolve_session_id(endpoint.active_call_id) != registry.resolve_session_id(call_id)
            or not registry.is_generation_current(call_id, generation)):
            raise web.HTTPGone(text="Call is no longer active for this phone")
        snapshot = _ha_softphone_state(hass, binding.endpoint_id)
        if snapshot.get("state") == "ringing" and invitation.expires is not None and invitation.expires <= time.monotonic():
            raise web.HTTPGone(text="Call invitation expired")
        return hass, binding, invitation, endpoint, snapshot

    def _description(self, invitation, snapshot, media_path=None):
        outgoing = snapshot.get("direction") == "outgoing"
        if outgoing and snapshot.get("state") == "in_call" and media_path is None:
            media_path = "/api/voip_stack/ws?" + urlencode({
                "endpoint_id": companion_endpoint_id(invitation.token.registration_id),
                "call_id": invitation.token.call_id,
            })
        return web.json_response({
            "id": invitation.token.call_id,
            "state": snapshot.get("state", "idle"),
            "caller": str(snapshot.get("peer_name") or snapshot.get("callee") or invitation.caller) if outgoing else invitation.caller,
            "direction": "outgoing" if outgoing else "incoming",
            "remaining_ms": max(0, round((invitation.expires - time.monotonic()) * 1000)) if invitation.expires is not None else None,
            "media_path": media_path,
        })

    async def get(self, request):
        _, _, invitation, _, snapshot = self._resolve(request)
        return self._description(invitation, snapshot)

    async def post(self, request):
        hass, binding, invitation, endpoint, snapshot = self._resolve(request)
        body = await request.json()
        if not isinstance(body, dict) or body.get("action") not in {"answer", "decline", "hangup"}:
            raise web.HTTPBadRequest(text="Invalid call action")
        action = body["action"]
        data = {"device_id": endpoint.device_id, "call_id": invitation.token.call_id}
        media_path = None
        if action == "answer":
            if snapshot.get("state") != "ringing":
                raise web.HTTPConflict(text="Call is not ringing")
            client_id = str(body.get("client_id", ""))
            from .authorization import _MEDIA_CLIENT_ID
            if not _MEDIA_CLIENT_ID.fullmatch(client_id):
                raise web.HTTPBadRequest(text="Invalid media client identity")
            data.update(send_video=False, media_client_id=client_id)
            media_path = "/api/voip_stack/ws?" + urlencode({
                "endpoint_id": binding.endpoint_id, "call_id": invitation.token.call_id,
                "client_id": client_id,
            })
        changed = asyncio.Event()

        @callback
        def call_changed(event):
            if event.data.get("endpoint_id") == binding.endpoint_id:
                changed.set()

        unsubscribe = hass.bus.async_listen(HA_SOFTPHONE_STATE_EVENT, call_changed)
        try:
            # Revalidate after reading the request body, before issuing any command.
            self._resolve(request)
            await hass.services.async_call("voip_stack", action, data, blocking=True,
                context=Context(user_id=request["hass_user"].id))
            if action != "answer":
                return self._description(invitation, {"state": "idle"})
            # Answering a fork selects its winner before the bridge commits.
            # Wait for the existing projection; do not treat command acceptance
            # as proof that this phone's media is ready.
            async with asyncio.timeout(3):
                while True:
                    changed.clear()
                    _, _, _, _, current = self._resolve(request)
                    if current.get("state") == "in_call":
                        return self._description(invitation, current, media_path)
                    if current.get("state") not in {"ringing", "connecting"}:
                        raise web.HTTPGone(text="Call ended while answering")
                    await changed.wait()
        except TimeoutError as err:
            raise web.HTTPGatewayTimeout(text="Call answer did not complete") from err
        finally:
            unsubscribe()


def register_companion_view(hass):
    registration = registration_data(hass)
    if not registration.companion_view:
        hass.http.register_view(CompanionCallView)
        registration.companion_view = True
