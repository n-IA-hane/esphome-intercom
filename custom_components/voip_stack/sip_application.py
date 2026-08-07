"""Application-level SIP methods owned by the integration runtime."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.core import HomeAssistant

from .const import EVENT_SIP_MESSAGE
from .core import sip
from .sip_listener import SipRequestResult
from .sip_registrar import SipRegistrar


@dataclass(slots=True)
class SipApplicationMethods:
    """Handle non-call requests without adding another SIP state machine."""

    hass: HomeAssistant
    registrar: SipRegistrar

    async def handle(
        self,
        request: sip.SipMessage,
        addr: tuple[str, int],
        transport: str,
    ) -> SipRequestResult:
        if request.method != "MESSAGE":
            return SipRequestResult(405, "Method Not Allowed")
        account = self.registrar.account_for_source(addr, transport)
        if account is None:
            return SipRequestResult(403, "Forbidden")
        content_type = request.header("Content-Type").split(";", 1)[0].lower()
        if content_type != "text/plain":
            return SipRequestResult(415, "Unsupported Media Type")
        try:
            message = request.body.decode("utf-8")
            recipient = sip.parse_sip_uri(request.uri).user
        except (UnicodeDecodeError, ValueError, sip.SipError):
            return SipRequestResult(400, "Bad Request")
        self.hass.bus.async_fire(
            EVENT_SIP_MESSAGE,
            {
                "sender": account.username,
                "recipient": recipient,
                "content_type": content_type,
                "message": message,
            },
        )
        return SipRequestResult()
