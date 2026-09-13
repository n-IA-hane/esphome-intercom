"""Call-owned readiness of the browser receiving a local announcement."""

from __future__ import annotations

import asyncio


class BrowserPlayback:
    """Only the currently attached media connection can acknowledge readiness."""

    def __init__(self) -> None:
        self._connection: object | None = None
        self._ready = False
        self._closed = False
        self._changed = asyncio.Event()

    def attach(self, connection: object) -> None:
        if self._closed:
            raise ConnectionError("The announcement call has ended")
        self._connection = connection
        self._ready = False
        self._changed.set()

    def ready(self, connection: object) -> None:
        if not self._closed and self._connection is connection:
            self._ready = True
            self._changed.set()

    def detach(self, connection: object) -> None:
        if self._connection is connection:
            self._connection = None
            self._ready = False
            self._changed.set()

    async def wait_ready(self) -> None:
        while not self._closed:
            if self._ready:
                return
            self._changed.clear()
            await self._changed.wait()
        raise ConnectionError("The announcement call has ended")

    async def close(self, _reason: str) -> None:
        self._closed = True
        self._connection = None
        self._ready = False
        self._changed.set()
