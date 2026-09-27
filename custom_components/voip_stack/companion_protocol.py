"""Identity and wire contract for native Companion call clients."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

PROTOCOL_VERSION = 1
CAPABILITY_KEY = "native_calls"
COMMAND = "command_call"
CONF_MOBILE_APP_ENTRY_ID = "mobile_app_entry_id"
CALL_PATH = "/api/voip_stack/companion/call"


@dataclass(frozen=True, slots=True)
class CompanionCallToken:
    """Bind a mobile registration to one authoritative call generation."""

    registration_id: str
    call_id: str
    generation: int

    def path(self) -> str:
        return f"{CALL_PATH}?{urlencode({'registration_id': self.registration_id, 'call_id': self.call_id, 'generation': self.generation})}"


def companion_endpoint_id(registration_id: str) -> str:
    """Keep endpoint identity stable through device and phone renames."""
    return f"companion:{registration_id}"
