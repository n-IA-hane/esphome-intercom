"""The same telephone mailbox menu for SIP, ESP and HA softphone calls."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import ClassVar
from contextlib import aclosing

from .call_ivr import CallIVR
from .call_projection import publish_bridge_projection
from .core.audio_format import AudioFormat
from .core.media_archive import ArchiveError
from .duration_policy import resolve_service_timeout
from .endpoint_lifecycle import call_registry
from .endpoint_session import TerminationIntent
from .local_application import LocalCallApplication
from .mailbox import get_archive, mailbox_settings, verify_pin, EVENT_VOICEMAIL_CHANGED
from .runtime_data import require_runtime_data
from .voicemail_prompts import phrase

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class VoicemailMenu(LocalCallApplication):
    owner: ClassVar[str] = "voicemail"
    private_dtmf: ClassVar[bool] = True
    mailbox_id: str = ""
    caller_endpoint_id: str = ""
    settings: dict | None = None
    ivr: CallIVR | None = None
    early_digits: list[str] = field(default_factory=list)
    message_ids: tuple[str, ...] = ()
    ready: asyncio.Event = field(default_factory=asyncio.Event)

    def handle_dtmf(self, side, digit, transport):
        if side != "left":
            return
        if self.ivr is None:
            if len(self.early_digits) < 64:
                self.early_digits.append(digit)
        elif not self.ivr.feed(digit):
            call_registry(self.hass).request_termination(
                self.session.call_id, TerminationIntent("invalid_dtmf"), generation=self.session.generation)

    async def say(self, key, **values):
        if not self.ivr.digits.empty():
            return False
        text = phrase(self.settings["language"], key, **values)
        options = {"voice": self.settings["voice"]} if self.settings.get("voice") else None
        async with asyncio.timeout(resolve_service_timeout(self.hass, "ivr_prompt_timeout")):
            return await self.ivr.play(self.speech_stream(text, self.settings["tts_entity_id"], self.settings["language"], options))

    async def _authorize(self):
        if not self.mailbox_id:
            await self.say("mailbox")
            number = await self.ivr.collect(timeout=resolve_service_timeout(self.hass, "ivr_interdigit_timeout"))
            endpoint = require_runtime_data(self.hass).endpoints.resolve(number) if number else None
            if endpoint is None:
                await self.say("denied")
                return False
            self.mailbox_id = endpoint.endpoint_id
        settings = mailbox_settings(self.hass, self.mailbox_id)
        if not settings.get("enabled"):
            await self.say("denied")
            return False
        if self.caller_endpoint_id == self.mailbox_id:
            return True
        encoded = str(settings.get("pin_hash") or "")
        if encoded:
            for _attempt in range(3):
                await self.say("pin")
                pin = await self.ivr.collect(maximum=10,
                    timeout=resolve_service_timeout(self.hass, "ivr_interdigit_timeout"))
                if pin is not None and await self.hass.async_add_executor_job(verify_pin, pin, encoded):
                    return True
        await self.say("denied")
        return False

    async def _play_message(self, archive, identity, number):
        try:
            entry = archive.message(identity, self.mailbox_id)
        except ArchiveError:
            await self.say("missing")
            return
        if not await self.say("message", number=number, caller=entry["caller"]):
            return
        played = False
        for index, segment in enumerate(entry["segments"]):
            if not self.ivr.digits.empty():
                break
            fmt = AudioFormat(segment["sample_rate"], {2: "s16le", 3: "s24le", 4: "s32le"}[segment["sample_width"]],
                              segment["channels"], 20)

            async def chunks():
                nonlocal played
                async with aclosing(archive.pcm_frames(identity, self.mailbox_id, index)) as frames:
                    async for pcm in frames:
                        played = True
                        yield pcm

            if not await self.ivr.play(chunks(), source_format=fmt):
                break
        if played:
            try:
                await archive.mark_read(identity, self.mailbox_id)
            except ArchiveError:
                return
            self.hass.bus.async_fire(EVENT_VOICEMAIL_CHANGED, {"endpoint_id": self.mailbox_id})

    async def run(self):
        registry = call_registry(self.hass)
        reason = "voicemail_menu_complete"
        try:
            async with asyncio.timeout(resolve_service_timeout(self.hass, "voicemail_menu_setup_timeout")):
                await self.answer()
                self.media.private_dtmf = True
                self.ivr = CallIVR(self.media, self.session.create_task)
                for digit in self.early_digits:
                    self.ivr.feed(digit)
                self.early_digits.clear()
                if self.playback is not None:
                    await self.playback.wait_ready()
            if not await self._authorize():
                return
            archive = await get_archive(self.hass)
            # Numbering is a snapshot. Another call's delete/new message never
            # turns this session's selected number into a different message.
            messages = self.message_ids = tuple(entry["id"] for entry in archive.list(self.mailbox_id))
            self.ready.set()
            publish_bridge_projection(self.hass, self.session, event_type="voicemail_menu_ready",
                                      route_kind="voicemail")
            if not messages:
                await self.say("empty")
                return
            await self.say("count", count=len(messages))
            selected, play = 0, True
            while self.session.live:
                if play:
                    await self._play_message(archive, messages[selected], selected + 1)
                    await self.say("choices")
                digit = await self.ivr.next_digit(timeout=resolve_service_timeout(self.hass, "ivr_digit_timeout"))
                play = False
                if digit == "#":
                    break
                if digit == "0":
                    value = await self.ivr.collect(timeout=resolve_service_timeout(self.hass, "ivr_interdigit_timeout"))
                    if value and 1 <= int(value) <= len(messages):
                        selected, play = int(value) - 1, True
                    elif value is not None:
                        await self.say("invalid")
                elif digit == "1":
                    if selected + 1 < len(messages):
                        selected, play = selected + 1, True
                    else:
                        await self.say("last")
                elif digit == "2":
                    try:
                        await archive.delete(messages[selected], self.mailbox_id)
                    except ArchiveError:
                        await self.say("missing")
                    else:
                        self.hass.bus.async_fire(EVENT_VOICEMAIL_CHANGED, {"endpoint_id": self.mailbox_id})
                        self.hass.bus.async_fire("voip_stack_voicemail_operation", {
                            "call_id": self.session.call_id, "generation": self.session.generation,
                            "mailbox_id": self.mailbox_id, "message_id": messages[selected], "operation": "deleted",
                        })
                        await self.say("deleted")
                elif digit == "3":
                    play = True
                elif digit == "*":
                    await self.say("count", count=len(archive.list(self.mailbox_id)))
                    await self.say("choices")
                else:
                    await self.say("invalid")
        except TimeoutError:
            reason = "voicemail_menu_timeout"
        except asyncio.CancelledError:
            raise
        except Exception:
            reason = "voicemail_menu_failed"
            _LOGGER.exception("Voicemail menu failed call_id=%s", self.session.call_id)
        finally:
            self.ready.set()
            if self.ivr is not None:
                await self.ivr.close()
            if self.session.live:
                registry.request_termination(self.session.call_id, TerminationIntent(reason, public_state="idle"),
                                             generation=self.session.generation)

    async def close(self, _reason):
        # Wake API readers even when the run task was cancelled before it began.
        self.ready.set()
        if self.ivr is not None:
            await self.ivr.close()
