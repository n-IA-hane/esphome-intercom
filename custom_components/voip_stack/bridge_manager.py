"""B2BUA bridge lifecycle helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .endpoint_lifecycle import call_registry
from .fsm import TerminalReason, sip_public_state, sip_terminal_reason

_LOGGER = logging.getLogger(__name__)


async def async_watch_sip_bridge_destination(
    hass: HomeAssistant,
    *,
    client: Any,
    source_call_id: str,
    terminate_sip_bridge: Callable[..., Awaitable[tuple[bool, str, str, bool, bool]]],
) -> None:
    """Propagate destination dialog termination through one B2BUA bridge."""
    try:
        terminal = await client.wait_for_dialog_termination()
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "SIP bridge destination watcher failed call_id=%s dest_call_id=%s: %s",
            source_call_id,
            client.dialog_ids.call_id,
            err,
        )
        terminal = "error"
    terminal_reason = (
        TerminalReason.REMOTE_HANGUP.value
        if terminal == "remote_hangup"
        else sip_terminal_reason(terminal, sip_public_state(terminal))
    )
    (
        bridge_handled,
        resolved_source_call_id,
        dest_call_id,
        _client_closed,
        source_bye,
    ) = await terminate_sip_bridge(
        hass,
        client.dialog_ids.call_id,
        terminal_reason=terminal_reason,
    )
    if bridge_handled:
        _LOGGER.info(
            "SIP bridge destination ended call_id=%s dest_call_id=%s reason=%s source_bye=%s",
            resolved_source_call_id,
            dest_call_id,
            terminal_reason,
            source_bye,
        )


async def async_terminate_sip_bridge(
    hass: HomeAssistant,
    call_id: str,
    *,
    terminal_reason: str = TerminalReason.LOCAL_HANGUP.value,
    send_bye: Callable[[str], bool],
) -> tuple[bool, str, str, bool, bool]:
    """Terminate a B2BUA bridge by either source or destination leg call-id."""
    if not call_id:
        return False, "", "", False, False
    registry = call_registry(hass)
    source_call_id, dest_call_id = registry.bridge_for(call_id)
    if not source_call_id:
        return False, "", "", False, False

    client_present = bool(dest_call_id and registry.sip_clients.get(dest_call_id))
    source_bye = send_bye(source_call_id)
    await registry.terminate_call_wait(
        source_call_id,
        reason=terminal_reason or TerminalReason.LOCAL_HANGUP.value,
    )
    return True, source_call_id, dest_call_id, client_present, source_bye
