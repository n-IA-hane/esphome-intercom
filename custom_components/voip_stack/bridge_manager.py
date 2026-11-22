"""B2BUA bridge lifecycle helpers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .fsm import TerminalReason, sip_public_state, sip_terminal_reason

_LOGGER = logging.getLogger(__name__)


async def _settle_referred_termination(client):
    """A transferor BYE cannot finish its transferee's existing handoff."""
    transaction = getattr(client, "_incoming_refer_task", None)
    if transaction is not None and transaction is not asyncio.current_task() and not transaction.done():
        try:
            await asyncio.shield(transaction)
        except asyncio.CancelledError:
            if not transaction.cancelled():
                raise
        except Exception:
            pass


async def async_watch_owned_sip_dialog(registry, call_id, client, generation):
    """End the current logical owner when one retained dialog terminates."""
    wire_id = client.dialog_ids.call_id
    try:
        reason = await client.wait_for_dialog_termination()
    except asyncio.CancelledError:
        raise
    except Exception:
        reason = "signaling_failed"
    await _settle_referred_termination(client)
    if (registry.is_generation_current(call_id, generation)
            and registry.sip_client_for(wire_id) is client):
        await registry.terminate_call_wait(call_id, reason=reason)


async def async_watch_sip_bridge_destination(
    hass: HomeAssistant,
    *,
    client: Any,
    source_call_id: str,
    terminate_sip_bridge: Callable[..., Awaitable[tuple[bool, str, str, bool, bool]]],
) -> None:
    """Propagate destination dialog termination through one B2BUA bridge."""
    wire_id = client.dialog_ids.call_id
    try:
        terminal = await client.wait_for_dialog_termination()
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning(
            "SIP bridge destination watcher failed call_id=%s dest_call_id=%s: %s",
            source_call_id,
            wire_id,
            err,
        )
        terminal = "error"
    await _settle_referred_termination(client)
    from .endpoint_lifecycle import call_registry
    registry = call_registry(hass)
    session = registry.get_session(source_call_id)
    if (session is None or not session.live
            or registry.sip_client_for(wire_id) is not client
            or wire_id not in session.legs):
        return
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
        wire_id,
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
