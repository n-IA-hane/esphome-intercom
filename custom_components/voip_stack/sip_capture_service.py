"""HA-native SIP capture action and short-lived authenticated download."""

from __future__ import annotations

from datetime import timedelta
import secrets

from aiohttp import web
import voluptuous as vol

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ServiceValidationError

from .authorization import async_require_service_admin
from .const import DOMAIN
from .sip_capture import SipCapture


async def async_register_sip_capture(hass: HomeAssistant) -> None:
    """Register once with the integration's shared application lifetime."""
    capture = SipCapture()

    class CaptureDownload(HomeAssistantView):
        url = "/api/voip_stack/sip_capture/{capture_id}"
        name = "api:voip_stack:sip_capture"
        requires_auth = True

        async def get(self, request: web.Request, capture_id: str) -> web.Response:
            # The random identifier is available only from the admin action.
            # HA verifies authentication, including its expiring signed URLs.
            if not capture.capture_id or not secrets.compare_digest(
                capture_id, capture.capture_id
            ):
                raise web.HTTPNotFound()
            if capture.active:
                raise web.HTTPConflict(text="Stop the SIP capture before downloading")
            return web.Response(
                body=bytes(capture.buffer),
                content_type="application/vnd.tcpdump.pcap",
                headers={
                    "Content-Disposition": 'attachment; filename="voip-stack-sip.pcap"',
                    "Cache-Control": "no-store",
                    "X-Content-Type-Options": "nosniff",
                },
            )

    async def handle(call: ServiceCall) -> dict:
        await async_require_service_admin(hass, call)
        operation = call.data["operation"]
        if operation == "start":
            try:
                capture.start(call.data["duration"])
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err
        elif operation == "stop":
            capture.stop()
        elif operation == "clear":
            capture.clear()
        result = capture.status()
        if capture.capture_id:
            result["download_url"] = async_sign_path(
                hass,
                f"/api/voip_stack/sip_capture/{capture.capture_id}",
                timedelta(minutes=5),
            )
        return result

    hass.http.register_view(CaptureDownload())
    hass.services.async_register(
        DOMAIN,
        "capture_sip",
        handle,
        schema=vol.Schema(
            {
                vol.Required("operation"): vol.In(["start", "stop", "status", "clear"]),
                vol.Optional("duration", default=120): vol.All(
                    vol.Coerce(int), vol.Range(min=10, max=300)
                ),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, lambda _event: capture.clear())
