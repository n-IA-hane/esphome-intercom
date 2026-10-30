"""IMAP protocol metadata, separate from the canonical voicemail content."""

from __future__ import annotations

from copy import deepcopy
import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import time

MAX_UID = 4294967295


@dataclass(frozen=True, slots=True)
class ImapSnapshot:
    uid_validity: int
    next_uid: int
    recent: frozenset[int]
    subscribed: bool
    messages: tuple[tuple[int, dict], ...]


class ImapIndex:
    """One small UID ledger, accessed under the MediaArchive lock.

    This file contains protocol counters and archive UUID-to-UID mappings only.
    Message content and Seen remain in the existing archive records.
    """

    def __init__(self, root: Path, run_io) -> None:
        self.path = root / ".protocol" / "imap-index.json"
        self.run_io = run_io
        self.state: dict | None = None

    @staticmethod
    def _valid_int(value):
        return type(value) is int and 1 <= value <= MAX_UID

    def _load(self):
        from .media_archive import ArchiveError

        if not self.path.exists():
            return {"version": 1, "next_validity": max(1, int(time.time())), "mailboxes": {}}
        try:
            state = json.loads(self.path.read_text())
            if not isinstance(state, dict) or state.get("version") != 1 or not self._valid_int(state["next_validity"]) or not isinstance(state["mailboxes"], dict):
                raise ValueError
            for identity, box in state["mailboxes"].items():
                if (not isinstance(identity, str) or not self._valid_int(box["uid_validity"])
                        or not self._valid_int(box["next_uid"]) or type(box["recent_through"]) is not int
                        or not 0 <= box["recent_through"] < box["next_uid"] or not isinstance(box["uids"], dict)
                        or ("subscribed" in box and type(box["subscribed"]) is not bool)
                        or len(set(box["uids"].values())) != len(box["uids"])):
                    raise ValueError
                if any(not isinstance(key, str) or not self._valid_int(uid) or uid >= box["next_uid"]
                       for key, uid in box["uids"].items()):
                    raise ValueError
            return state
        except (KeyError, TypeError, ValueError, OSError):
            raise ArchiveError("IMAP UID metadata is unavailable") from None

    async def _ready(self):
        if self.state is None:
            self.state = await self.run_io(self._load)

    async def _commit(self, state):
        from ..session_cleanup import async_wait_for_cleanup
        from .media_archive import _atomic_json

        def write():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            _atomic_json(self.path, state, max_bytes=16 * 1024 * 1024)
        async def commit():
            await self.run_io(write)
            self.state = state
        await async_wait_for_cleanup(asyncio.create_task(commit(), name="voip-imap-index-commit"))

    async def snapshot(self, mailbox_id, messages, *, claim_recent=False, subscribed=None):
        from .media_archive import ArchiveError

        await self._ready()
        previous = self.state
        current = {entry["id"]: entry for entry in messages}
        existing = previous["mailboxes"].get(mailbox_id)
        if (existing is not None and set(existing["uids"]) == current.keys()
                and "subscribed" in existing
                and (not claim_recent or existing["recent_through"] == existing["next_uid"] - 1)
                and (subscribed is None or existing["subscribed"] is bool(subscribed))):
            return self._view(existing, current)
        state = deepcopy(previous)
        box = state["mailboxes"].get(mailbox_id)
        if box is None:
            validity = state["next_validity"]
            if validity >= MAX_UID:
                raise ArchiveError("IMAP UID validity counter is exhausted")
            state["next_validity"] = validity + 1
            box = {"uid_validity": validity, "next_uid": 1, "recent_through": 0, "uids": {}, "subscribed": True}
            state["mailboxes"][mailbox_id] = box
        box.setdefault("subscribed", True)
        if subscribed is not None:
            box["subscribed"] = bool(subscribed)
        box["uids"] = {identity: uid for identity, uid in box["uids"].items() if identity in current}
        for entry in sorted(messages, key=lambda item: (item["created_at"], item["id"])):
            if entry["id"] in box["uids"]:
                continue
            if box["next_uid"] >= MAX_UID:
                raise ArchiveError("IMAP UID counter is exhausted")
            box["uids"][entry["id"]] = box["next_uid"]
            box["next_uid"] += 1
        recent = frozenset(uid for uid in box["uids"].values() if uid > box["recent_through"])
        if claim_recent:
            box["recent_through"] = box["next_uid"] - 1
        if state != previous:
            await self._commit(state)
        return self._view(box, current, recent)

    @staticmethod
    def _view(box, current, recent=None):
        if recent is None:
            recent = frozenset(uid for uid in box["uids"].values() if uid > box["recent_through"])
        rows = tuple((uid, current[identity]) for identity, uid in sorted(box["uids"].items(), key=lambda item: item[1]))
        return ImapSnapshot(box["uid_validity"], box["next_uid"], recent, box["subscribed"], rows)

    async def forget(self, mailbox_id):
        await self._ready()
        if mailbox_id not in self.state["mailboxes"]:
            return
        state = deepcopy(self.state)
        state["mailboxes"].pop(mailbox_id)
        await self._commit(state)
