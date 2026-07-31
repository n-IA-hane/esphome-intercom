"""Small bounded-queue helpers for signaling and realtime media paths."""

from __future__ import annotations

import asyncio
from typing import TypeVar


_T = TypeVar("_T")


def drain_queue(queue: asyncio.Queue[_T]) -> int:
    """Discard every currently queued item and return the removed count."""

    removed = 0
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return removed
        removed += 1


def put_drop_oldest(queue: asyncio.Queue[_T], item: _T) -> bool:
    """Enqueue without blocking, keeping the most recent bounded data.

    Returns True when an older item had to be discarded. All callers run on
    the Home Assistant event-loop thread, so the get/put pair is atomic with
    respect to other asyncio tasks (there is no await between the operations).
    """

    dropped = False
    if queue.full():
        try:
            queue.get_nowait()
            dropped = True
        except asyncio.QueueEmpty:
            pass
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        # Defensive for callbacks invoked by an unusual re-entrant transport.
        return True
    return dropped
