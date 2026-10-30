"""Mailbox deposit uses the existing local application and SIP session owners."""

from __future__ import annotations

import asyncio
from contextvars import copy_context
from contextlib import aclosing
from dataclasses import dataclass
import logging
import math
import struct
from typing import ClassVar

from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .automation_context import current_execution
from .call_projection import publish_bridge_projection
from .endpoint_lifecycle import call_registry
from .endpoint_session import CleanupStage, TerminationIntent
from .local_application import LocalCallApplication, attach_local_application
from .mailbox import (EVENT_VOICEMAIL_CHANGED, EVENT_VOICEMAIL_RECEIVED, get_archive,
                      mailbox_settings, selected_mailbox, message_audio_reference, mailbox_display_name)
from .roster import RosterEntry
from .session_cleanup import async_wait_for_cleanup

_LOGGER = logging.getLogger(__name__)


async def _recording_tone():
    """A short, faded 1 kHz prompt at the local application's PCM contract."""
    for start in range(0, 3200, 320):
        yield struct.pack("<320h", *(int(4000 * math.sin(2 * math.pi * index / 16)
                                         * min(1, index / 80, (3199 - index) / 80))
                                     for index in range(start, start + 320)))


@dataclass(slots=True)
class VoicemailCall(LocalCallApplication):
    owner: ClassVar[str] = "voicemail"
    cleanup_stage: ClassVar[CleanupStage] = CleanupStage.RESERVATION
    mailbox_id: str = ""
    recorded_caller: str = ""
    settings: dict | None = None
    skip_greeting: bool = False
    writer: object | None = None
    detach_capture: object | None = None
    close_task: asyncio.Task | None = None
    removed: bool = False

    async def run(self):
        registry = call_registry(self.hass)
        try:
            self.require_current()
            archive = await get_archive(self.hass)
            self.writer = await archive.begin(self.mailbox_id, caller=self.recorded_caller or self.session.caller)
            async with asyncio.timeout(30):
                await self.answer()
                if not self.media.can_receive:
                    raise ServiceValidationError("The caller cannot send voicemail audio")
                if self.playback is not None:
                    await self.playback.wait_ready()
                greeting = str((self.settings or {}).get("greeting_media") or "")
                if greeting and not self.skip_greeting:
                    from .announcement_media import async_audio_stream
                    try:
                        async with aclosing(async_audio_stream(self.hass, greeting)) as chunks:
                            await self.media.play_pcm_stream(chunks)
                    except Exception:
                        _LOGGER.warning("Mailbox greeting unavailable; using recording tone mailbox=%s", self.mailbox_id)
                await self.media.play_pcm_stream(_recording_tone())
                await self.media.tx_queue.join()
            self.require_current()
            started = self.hass.loop.time()
            self.detach_capture = self.media.decoder.add_pcm_observer(
                lambda pcm, fmt: self.writer.offer(pcm, fmt, offset=self.hass.loop.time() - started)
            )
            publish_bridge_projection(self.hass, self.session, event_type="voicemail_recording", route_kind="voicemail")
            try:
                await asyncio.wait_for(self.writer.failed.wait(), float((self.settings or {}).get("max_seconds", 180)))
            except TimeoutError:
                pass
            registry.request_termination(self.session.call_id, TerminationIntent("voicemail_complete", public_state="idle"),
                                         generation=self.session.generation)
        except asyncio.CancelledError:
            if self.session.live:
                registry.request_termination(self.session.call_id, TerminationIntent("voicemail_cancelled", public_state="idle"),
                                             generation=self.session.generation)
            raise
        except Exception:
            _LOGGER.exception("Voicemail application failed call_id=%s", self.session.call_id)
            registry.request_termination(self.session.call_id, TerminationIntent("voicemail_unavailable", public_state="idle", response_status=503),
                                         generation=self.session.generation)
        finally:
            if self.detach_capture is not None:
                self.detach_capture()
                self.detach_capture = None

    async def _finish(self):
        if self.detach_capture is not None:
            self.detach_capture()
            self.detach_capture = None
        if self.writer is None:
            return
        entry = await self.writer.finish()
        if entry is not None and not self.removed:
            archive = await get_archive(self.hass)
            if self.removed:
                return
            duration = max(segment["start"] + segment["frames"] / segment["sample_rate"] for segment in entry["segments"])
            self.hass.bus.async_fire(EVENT_VOICEMAIL_RECEIVED, {
                "mailbox_id": self.mailbox_id, "message_id": entry["id"], "caller": entry["caller"],
                "duration": duration, "partial": entry["status"] == "partial",
                **message_audio_reference(self.hass, archive, entry),
            })
            self.hass.bus.async_fire(EVENT_VOICEMAIL_CHANGED, {"endpoint_id": self.mailbox_id})

    async def close(self, _reason):
        self.removed = self.removed or _reason == "phone_removed"
        if self.close_task is None:
            self.close_task = asyncio.create_task(self._finish(), name=f"voip-voicemail-save-{self.session.call_id}")
        await async_wait_for_cleanup(self.close_task)


async def async_send_to_voicemail(call: ServiceCall) -> None:
    """An explicit automation action transfers its call to one enabled mailbox."""
    from .automation_call import resolve_application_action
    endpoint = await selected_mailbox(call, call.data["mailbox"])
    settings = mailbox_settings(call.hass, endpoint.endpoint_id)
    if not settings.get("enabled", False):
        raise ServiceValidationError("Enable voicemail for the selected phone first")
    await get_archive(call.hass)
    application, bound = await resolve_application_action(call)
    application.claim(bound)
    if application.lock.locked():
        raise ServiceValidationError("Wait for the current announcement before sending the call to voicemail")
    await start_voicemail_application(
        call.hass, application.session, application.invite, endpoint, application.local_ip,
        previous=application, skip_greeting=bool(call.data.get("skip_greeting", False)),
    )


async def start_voicemail_application(hass, session, invite, endpoint, local_ip, *,
                                      previous=None, local_source=None, skip_greeting=False,
                                      mode="deposit", caller_endpoint_id="", display_name=""):
    """Acquire one current session and attach its mailbox deposit application."""
    registry = call_registry(hass)
    session.ensure_live()
    owner, revision, generation = session.owner, session.revision, session.generation
    from .voicemail_config import require_voicemail_config
    from .voicemail_menu import VoicemailMenu

    voice = require_voicemail_config(hass) if mode != "deposit" else {}
    settings = mailbox_settings(hass, endpoint.endpoint_id) if endpoint is not None else {}
    if mode == "deposit" and not settings.get("enabled", False):
        raise ServiceValidationError("Enable voicemail for the selected phone first")
    await get_archive(hass)
    if endpoint is not None and not mailbox_settings(hass, endpoint.endpoint_id).get("enabled"):
        raise ServiceValidationError("The selected mailbox was removed or disabled")
    label = display_name or (endpoint.name if endpoint is not None else "Voicemail")
    identity = endpoint.endpoint_id if endpoint is not None else ""

    recorded_caller = session.callee if previous is not None and previous.outgoing else session.caller
    claimed = registry.transition(
        session.call_id, owner="voicemail", route_kind="voicemail", callee=label,
        expected_owner=owner, expected_revision=revision, expected_generation=generation,
    )
    if claimed is None:
        raise ServiceValidationError("The call changed before voicemail could take control")
    options = ({"recorded_caller": recorded_caller, "skip_greeting": skip_greeting}
               if mode == "deposit" else {"caller_endpoint_id": caller_endpoint_id})
    voicemail = attach_local_application(
        VoicemailCall if mode == "deposit" else VoicemailMenu,
        hass, session, invite, RosterEntry(identity or "mailbox", name=label), local_ip,
        previous=previous, local_source=local_source, mailbox_id=identity,
        settings={**settings, **voice}, **options,
    )
    if previous is not None:
        await previous.close("forwarded")
        registry.release_resource(session.call_id, f"{previous.owner}:{session.call_id}", value=previous)
    context = copy_context()
    context.run(current_execution.set, None)
    context.run(session.create_task, voicemail.run(), name=f"voicemail-{mode}")
    return voicemail


async def route_voicemail_call(runtime, invite, decision, registry, source_endpoint):
    """Receive a SIP deposit through the same session and local media contracts."""
    from .inbound_routing.local import _claim_source
    from .runtime_data import endpoint_directory
    from .sip_listener import SipInviteResult

    mode = decision.entry.metadata.get("mailbox_mode", "deposit")
    identity = (invite.identified_source_endpoint_id if mode == "own"
                else decision.entry.metadata["mailbox_endpoint_id"])
    mailbox = endpoint_directory(runtime.hass).get(identity) if identity else None
    if mode == "own" and mailbox is None:
        return SipInviteResult(403, "Forbidden", to_tag="", decline_reason="unknown_mailbox_caller")
    if mode != "login" and (mailbox is None or not mailbox_settings(runtime.hass, mailbox.endpoint_id).get("enabled", False)):
        return SipInviteResult(480, "Temporarily Unavailable", to_tag="", decline_reason="voicemail_unavailable")
    if mode != "deposit":
        from .voicemail_config import require_voicemail_config
        try:
            require_voicemail_config(runtime.hass)
        except ServiceValidationError:
            return SipInviteResult(503, "Service Unavailable", to_tag="",
                                   decline_reason="voicemail_voice_unavailable")
    if busy := await _claim_source(hass=runtime.hass, registry=registry, invite=invite,
                                  source_endpoint=source_endpoint, state="ringing", route_kind="voicemail"):
        return busy
    label = (mailbox_display_name(mailbox) if mode != "deposit"
             else mailbox.name if mailbox is not None else "Voicemail")
    session = registry.upsert(invite.call_id, state="ringing", owner="router",
                              caller=invite.caller, callee=label, route_kind="voicemail",
                              sip_uri=str(invite.request_uri))
    registry.set_pending_invite(invite.call_id, invite)
    try:
        await start_voicemail_application(runtime.hass, session, invite, mailbox, runtime.local_ip,
                                         mode=mode, caller_endpoint_id=invite.identified_source_endpoint_id,
                                         display_name=label)
    except Exception:
        registry.request_termination(invite.call_id, TerminationIntent("voicemail_unavailable", response_status=503),
                                     generation=session.generation)
        raise
    return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)
