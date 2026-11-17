"""Dedicated provisioning credentials, never HA tokens or public entities."""

from aiohttp import web
from homeassistant.components.http import HomeAssistantView, KEY_HASS

from .runtime_data import runtime_data, registration_data


class PhysicalPhoneView(HomeAssistantView):
    url = "/api/voip_stack/phone_provisioning/{phone_id}/{token}/{filename:.*}"
    name = "api:voip_stack:phone_provisioning"
    requires_auth = False

    async def get(self, request, phone_id, token, filename):
        from .phone_profile import safe_profile_path
        runtime = runtime_data(request.app[KEY_HASS])
        manager = runtime.physical_phones if runtime is not None else None
        if manager is None:
            return web.Response(status=404)
        try:
            safe_profile_path(filename)
        except ValueError:
            return web.Response(status=404)
        value = await manager.async_file(phone_id, token, filename)
        if value is None:
            return web.Response(status=404, headers={"Cache-Control": "no-store"})
        format = manager.states.get(phone_id, {}).get("file_formats", {}).get(filename)
        return web.Response(body=value, content_type="application/xml" if format == "xml" else "text/plain",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


def async_register_physical_phone_http(hass):
    registration = registration_data(hass)
    if not registration.physical_phone_view:
        hass.http.register_view(PhysicalPhoneView())
        registration.physical_phone_view = True
