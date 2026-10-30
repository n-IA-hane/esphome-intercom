"""Deterministic virtual MIME views over the existing canonical mailbox WAV."""

from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
import math
from weakref import WeakValueDictionary

from pymap.bytes import Writeable
from pymap.message import BaseLoadedMessage
from pymap.mime import MessageContent
from pymap.parsing.response.fetch import ContentBodyStructure, MultipartBodyStructure, TextBodyStructure

from .core.media_archive import ArchiveError
from .session_cleanup import async_wait_for_cleanup


def _headers(message):
    return message.as_bytes().split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"


@dataclass(frozen=True, slots=True)
class MimeLayout:
    header: bytes
    prefix: bytes
    suffix: bytes
    audio_bytes: int
    encoded_bytes: int
    duration: int | None

    @property
    def size(self):
        return len(self.prefix) + self.encoded_bytes + len(self.suffix)


def mime_layout(record) -> MimeLayout:
    segments = record["segments"]
    if len(segments) != 1:
        raise ArchiveError("The voicemail WAV is not finalized")
    segment = segments[0]
    audio_bytes = 44 + segment["frames"] * segment["channels"] * segment["sample_width"]
    characters = 4 * ((audio_bytes + 2) // 3)
    encoded_bytes = characters + 2 * ((characters + 75) // 76)
    seconds = max((item["start"] + item["frames"] / item["sample_rate"] for item in segments), default=0)
    duration = math.ceil(seconds) if math.isfinite(seconds) and 0 < seconds <= 2147483647 else None
    caller = str(record.get("caller") or "Unknown caller").replace("\r", " ").replace("\n", " ")
    identity = record["id"]
    boundary = "voip-" + identity
    root = EmailMessage(policy=SMTP)
    root["From"] = Address(display_name=caller, username="voicemail", domain="voip-stack.local")
    root["Subject"] = "Voicemail from " + caller
    root["Date"] = format_datetime(datetime.fromtimestamp(record["created_at"], timezone.utc))
    root["Message-ID"] = f"<{identity}@voip-stack.local>"
    root.set_type("multipart/mixed")
    root.set_boundary(boundary)
    if duration is not None:
        root["Content-Duration"] = str(duration)
    header = _headers(root)
    text = EmailMessage(policy=SMTP)
    text.set_content("Voice message from " + caller + ".", charset="utf-8", cte="quoted-printable")
    audio = EmailMessage(policy=SMTP)
    audio.set_type("audio/wav")
    audio["Content-Transfer-Encoding"] = "base64"
    audio.add_header("Content-Disposition", "attachment", filename="voicemail.wav")
    if duration is not None:
        audio["Content-Duration"] = str(duration)
    separator = b"--" + boundary.encode() + b"\r\n"
    prefix = header + separator + text.as_bytes() + b"\r\n" + separator + _headers(audio)
    suffix = b"\r\n--" + boundary.encode() + b"--\r\n"
    return MimeLayout(header, prefix, suffix, audio_bytes, encoded_bytes, duration)


@dataclass(slots=True, weakref_slot=True)
class MimeValue:
    key: tuple[str, str]
    layout: MimeLayout
    content: MessageContent
    parts: dict = field(default_factory=dict)

    @property
    def weight(self):
        # Count each shared bytes object once, including the encoded part that
        # avoids Pymap copying the whole attachment for every partial FETCH.
        unique = {id(value): len(value) for value in self.parts.values()}
        return sum(unique.values())

    def part(self, section, binary, loader):
        key = (tuple(section) if section else None, binary)
        if key not in self.parts:
            self.parts[key] = bytes(loader())
        return Writeable.wrap(self.parts[key])


class MimeCache:
    """Bounded shared LRU, singleflight builds and weak reuse of active leases."""

    def __init__(self, *, max_bytes=64 * 1024 * 1024, max_entries=4, max_pending=4):
        self.max_bytes, self.max_entries, self.max_pending = max_bytes, max_entries, max_pending
        self._lru = OrderedDict()
        self._layouts = OrderedDict()
        self._active = WeakValueDictionary()
        self._pending: dict[tuple[str, str], asyncio.Task] = {}
        self._epochs: dict[str, int] = {}
        self.build_count = 0

    def layout(self, record):
        key = record["mailbox_id"], record["id"]
        active = self._active.get(key)
        if active is not None:
            return active.layout
        if key not in self._layouts:
            self._layouts[key] = mime_layout(record)
        self._layouts.move_to_end(key)
        while len(self._layouts) > 128:
            self._layouts.popitem(last=False)
        return self._layouts[key]

    def _retain(self, value):
        self._lru[value.key] = value
        self._lru.move_to_end(value.key)
        # A single large valid message is still served; repeated partial
        # requests reuse its active lease rather than rebuilding its MIME.
        while len(self._lru) > 1 and (len(self._lru) > self.max_entries or
                sum(item.weight for item in self._lru.values()) > self.max_bytes):
            self._lru.popitem(last=False)
        self._active[value.key] = value

    async def get(self, archive, mailbox_id, record):
        key = mailbox_id, record["id"]
        cached = self._active.get(key)
        if cached is not None:
            self._retain(cached)
            return cached
        task = self._pending.get(key)
        if task is None:
            if len(self._pending) >= self.max_pending:
                raise ArchiveError("Voicemail content is busy")
            epoch = self._epochs.get(mailbox_id, 0)
            layout = self.layout(record)
            task = asyncio.create_task(self._build(archive, key, record, layout, epoch), name="voip-imap-mime")
            self._pending[key] = task
            task.add_done_callback(lambda finished: self._finished(key, finished))
        return await asyncio.shield(task)

    @staticmethod
    def _render(path, key, layout):
        raw = path.read_bytes()
        if len(raw) != layout.audio_bytes:
            raise ArchiveError("The canonical voicemail WAV size is inconsistent")
        encoded = base64.encodebytes(raw).replace(b"\n", b"\r\n")
        if len(encoded) != layout.encoded_bytes:
            raise ArchiveError("The voicemail MIME size is inconsistent")
        wire = layout.prefix + encoded + layout.suffix
        value = MimeValue(key, layout, MessageContent.parse(wire))
        value.parts[(None, False)] = wire
        value.parts[((2,), False)] = encoded
        return value

    async def _build(self, archive, key, record, layout, epoch):
        self.build_count += 1
        path = archive.segment_path(record["id"], key[0])
        value = await archive.run_io(self._render, path, key, layout)
        # A delete or purge cannot publish a late MIME result into the cache.
        archive.message(key[1], key[0])
        if self._epochs.get(key[0], 0) != epoch:
            raise ArchiveError("The voicemail owner changed")
        self._retain(value)
        return value

    def _finished(self, key, task):
        if self._pending.get(key) is task:
            self._pending.pop(key)
        if not task.cancelled():
            task.exception()  # Retrieve an orphaned build's exception as well.

    def invalidate_deleted(self, archive, mailbox_id):
        for key in set(self._active) | set(self._layouts):
            if key[0] != mailbox_id:
                continue
            try:
                archive.message(key[1], mailbox_id)
            except ArchiveError:
                self._lru.pop(key, None)
                self._active.pop(key, None)
                self._layouts.pop(key, None)

    async def forget(self, mailbox_id=None):
        owners = {key[0] for key in (*self._active, *self._pending, *self._layouts)} | set(self._epochs) if mailbox_id is None else {mailbox_id}
        for owner in owners:
            self._epochs[owner] = self._epochs.get(owner, 0) + 1
        for key in tuple(self._layouts):
            if key[0] in owners:
                self._layouts.pop(key)
        for key in tuple(self._lru):
            if key[0] in owners:
                self._lru.pop(key)
        for key in tuple(self._active):
            if key[0] in owners:
                self._active.pop(key, None)
        tasks = [task for key, task in self._pending.items() if key[0] in owners]
        if tasks:
            async def finish():
                await asyncio.gather(*tasks, return_exceptions=True)
            await async_wait_for_cleanup(asyncio.create_task(finish(), name="voip-imap-mime-cleanup"))
        for owner in owners:
            if not any(key[0] == owner for key in self._pending):
                self._epochs.pop(owner, None)


class LoadedVoicemail(BaseLoadedMessage):
    """Public Pymap adapter: stable part bytes and RFC-correct body sizes."""

    def __init__(self, message, requirement, layout, value=None):
        self.layout, self.value = layout, value
        content = value.content if value else MessageContent.parse(layout.prefix + layout.suffix)
        super().__init__(message, requirement, content)

    def get_body(self, section=None, binary=False):
        if self.value is None:
            return super().get_body(section, binary)
        return self.value.part(section, binary, lambda: super(LoadedVoicemail, self).get_body(section, binary))

    def get_size(self, section=None):
        if not section:
            return self.layout.size
        return len(bytes(self.get_body(section, binary=True)))

    def _structure(self, part, section=()):
        parsed = part.header.parsed
        media = part.body.content_type
        params = media.params
        common = (params, parsed.content_disposition, parsed.content_language, parsed.content_location)
        if media.maintype == "multipart":
            return MultipartBodyStructure(media.subtype, *common,
                [self._structure(child, section + (index,)) for index, child in enumerate(part.body.nested, 1)])
        tail = (parsed.content_id, parsed.content_description, parsed.content_transfer_encoding, None)
        # Pymap 0.36.7 counts len(part), including MIME headers. RFC3501's
        # BODYSTRUCTURE octets describe the encoded body, like BODY[section].
        encoded = bytes(self.get_body(section)) if section else bytes(part.body)
        size = self.layout.encoded_bytes if self.value is None and section == (2,) else len(encoded)
        if media.maintype == "text":
            return TextBodyStructure(media.subtype, *common, *tail, size, encoded.count(b"\n"))
        return ContentBodyStructure(media.maintype, media.subtype, *common, *tail, size)

    def get_body_structure(self):
        return self._structure(self.content)
