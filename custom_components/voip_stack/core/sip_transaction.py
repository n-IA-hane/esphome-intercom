"""Shared RFC 3261 transaction timers for every SIP transport role."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar


SIP_T1 = 0.5
SIP_T2 = 4.0
SIP_TIMER_B = 64 * SIP_T1
SIP_TIMER_H = 64 * SIP_T1

_T = TypeVar("_T")
ResponseReader = Callable[[float], Awaitable[_T | None]]
AsyncSend = Callable[[], Awaitable[None]]


class SipClientTransaction(Generic[_T]):
    """One UAC response timer with UDP retransmission and reliable fallback."""

    def __init__(
        self,
        *,
        transport: str,
        timeout: float,
        t1: float = SIP_T1,
        t2: float = SIP_T2,
    ) -> None:
        if float(timeout) < 0 or float(t1) <= 0 or float(t2) < float(t1):
            raise ValueError("invalid SIP transaction timers")
        self.reliable = str(transport or "UDP").upper() in {"TCP", "TLS", "WS", "WSS"}
        self.timeout = float(timeout)
        self.t1 = float(t1)
        self.t2 = float(t2)
        loop = asyncio.get_running_loop()
        self.deadline = loop.time() + self.timeout
        self.interval = self.t1
        self.next_retransmit = loop.time() + self.interval
        self.retransmissions = 0

    @property
    def remaining(self) -> float:
        return max(0.0, self.deadline - asyncio.get_running_loop().time())

    def restart_retransmissions(self) -> None:
        """Restart Timer A/E after an authenticated request is rebuilt."""

        self.interval = self.t1
        self.next_retransmit = asyncio.get_running_loop().time() + self.interval

    async def receive(
        self,
        reader: ResponseReader[_T],
        retransmit: AsyncSend,
        *,
        retransmit_enabled: bool = True,
    ) -> _T | None:
        """Return one response, retransmitting UDP until deadline when allowed."""

        loop = asyncio.get_running_loop()
        while True:
            remaining = self.deadline - loop.time()
            if remaining <= 0:
                return None
            read_timeout = remaining
            if not self.reliable and retransmit_enabled:
                read_timeout = min(
                    read_timeout,
                    max(0.0, self.next_retransmit - loop.time()),
                )
            try:
                response = await reader(read_timeout)
            except asyncio.TimeoutError:
                response = None
            if response is not None:
                return response
            if (
                self.reliable
                or not retransmit_enabled
                or loop.time() >= self.deadline
            ):
                return None
            await retransmit()
            self.retransmissions += 1
            self.interval = min(self.interval * 2.0, self.t2)
            self.next_retransmit = loop.time() + self.interval


@dataclass(frozen=True, slots=True)
class ServerTransactionResult:
    retransmissions: int
    timed_out: bool


async def async_run_server_transaction(
    *,
    send: Callable[[], bool | None],
    active: Callable[[], bool],
    transport: str,
    timeout: float,
    t1: float = SIP_T1,
    t2: float = SIP_T2,
    retransmit_reliable: bool = False,
) -> ServerTransactionResult:
    """Run Timer G/H or a 2xx retransmit timer until ACK/state completion."""

    if float(timeout) < 0 or float(t1) <= 0 or float(t2) < float(t1):
        raise ValueError("invalid SIP server transaction timers")
    reliable = str(transport or "UDP").upper() in {"TCP", "TLS", "WS", "WSS"}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + float(timeout)
    interval = float(t1)
    retransmissions = 0
    while active():
        remaining = deadline - loop.time()
        if remaining <= 0:
            return ServerTransactionResult(retransmissions, True)
        await asyncio.sleep(min(interval, remaining))
        if not active():
            return ServerTransactionResult(retransmissions, False)
        if loop.time() >= deadline:
            return ServerTransactionResult(retransmissions, True)
        if not reliable or retransmit_reliable:
            if send() is not False:
                retransmissions += 1
            interval = min(interval * 2.0, float(t2))
    return ServerTransactionResult(retransmissions, False)
