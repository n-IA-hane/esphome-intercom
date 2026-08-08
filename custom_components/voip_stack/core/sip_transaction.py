"""Shared RFC 3261 transaction timers for every SIP transport role."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, TypeVar

from . import sip


SIP_T1 = 0.5
SIP_T2 = 4.0
SIP_TIMER_B = 64 * SIP_T1
SIP_TIMER_F = 64 * SIP_T1
SIP_TIMER_H = 64 * SIP_T1

_T = TypeVar("_T")
ResponseReader = Callable[[float], Awaitable[_T | None]]
AsyncSend = Callable[[], Awaitable[None]]
SyncSend = Callable[[], bool | None]
SessionRefreshSend = Callable[
    ...,
    Awaitable[sip.SipMessage | None],
]


type SipTransactionKey = tuple[str, int, str]


async def async_refresh_session(
    state: sip.SipSessionTimer,
    send: SessionRefreshSend,
    *,
    local_role: str,
    now: Callable[[], float],
) -> str:
    """Run the single bounded RFC 4028 refresh policy for every SIP role."""

    for _attempt in range(2):
        response = await send(
            "UPDATE",
            extra_headers=(
                ("Supported", "timer"),
                ("Session-Expires", f"{state.interval};refresher={local_role}"),
            ),
        )
        result = sip.apply_session_refresh_response(
            state,
            response,
            local_role=local_role,
            now=now(),
        )
        if result != "retry":
            return result
    return "failed"


def transaction_key(message: sip.SipMessage) -> SipTransactionKey | None:
    """Return a message transaction key, or none for malformed input."""
    try:
        cseq = sip.parse_cseq(message.header("CSeq"))
        vias = message.header_values("Via")
        branch = sip.parse_via(vias[0] if vias else "").branch
    except (TypeError, ValueError, sip.SipError):
        return None
    return cseq.method, cseq.number, branch


def matches_response(
    message: sip.SipMessage,
    *,
    method: str,
    cseq: int,
    branch: str,
) -> bool:
    key = transaction_key(message) if message.is_response else None
    return bool(
        key is not None
        and key == (method.upper(), int(cseq), branch)
        and branch
    )


def same_request_transaction(
    current: sip.SipMessage,
    previous: sip.SipMessage | None,
) -> bool:
    if previous is None or current.method != previous.method:
        return False
    key = transaction_key(current)
    previous_key = transaction_key(previous)
    return bool(
        key is not None
        and previous_key is not None
        and key[0] == current.method
        and previous_key[0] == previous.method
        and key[2]
        and key == previous_key
    )


def matches_invite_error_ack(
    ack: sip.SipMessage,
    invite: sip.SipMessage,
) -> bool:
    ack_key = transaction_key(ack)
    invite_key = transaction_key(invite)
    if (
        ack.method != "ACK"
        or ack.header("Call-ID") != invite.header("Call-ID")
        or ack_key is None
        or invite_key is None
        or not ack_key[2]
        or ack_key[1] != invite_key[1]
        or invite_key[0] != "INVITE"
        or ack_key[2] != invite_key[2]
    ):
        return False
    try:
        ack_via = sip.parse_via(ack.header_values("Via")[0])
        invite_via = sip.parse_via(invite.header_values("Via")[0])
    except (IndexError, TypeError, ValueError, sip.SipError):
        return False
    return ack_via.host == invite_via.host and ack_via.port == invite_via.port


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


class SipInvite2xxTransaction:
    """Own one INVITE 2xx retransmission timer and its matching ACK."""

    def __init__(self) -> None:
        self.request: sip.SipMessage | None = None
        self.cseq = 0
        self.retransmissions = 0
        self.task: asyncio.Task[None] | None = None

    @property
    def active(self) -> bool:
        return self.request is not None

    def cancel(self) -> None:
        task = self.task
        self.task = None
        self.request = None
        self.cseq = 0
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    def acknowledge(
        self,
        request: sip.SipMessage,
        matches_dialog: Callable[[sip.SipMessage], bool],
    ) -> bool:
        if self.request is None or request.method != "ACK":
            return False
        try:
            cseq = sip.parse_cseq(request.header("CSeq"))
        except (TypeError, ValueError, sip.SipError):
            return False
        if cseq.method != "ACK" or cseq.number != self.cseq or not matches_dialog(request):
            return False
        self.cancel()
        return True

    def start(
        self,
        request: sip.SipMessage,
        *,
        transport: str,
        send: SyncSend,
        on_timeout: Callable[[], Awaitable[None]],
        still_owned: Callable[[], bool] = lambda: True,
        timeout: float = SIP_TIMER_B,
        t1: float = SIP_T1,
        t2: float = SIP_T2,
        task_name: str | None = None,
    ) -> bool:
        try:
            cseq = sip.parse_cseq(request.header("CSeq"))
        except (TypeError, ValueError, sip.SipError):
            return False
        if request.method != "INVITE" or cseq.method != "INVITE":
            return False
        self.cancel()
        self.request = request
        self.cseq = cseq.number
        self.retransmissions = 0

        async def run() -> None:
            def retransmit() -> bool | None:
                sent = send()
                if sent is not False:
                    self.retransmissions += 1
                return sent

            try:
                result = await async_run_server_transaction(
                    send=retransmit,
                    active=lambda: self.active and still_owned(),
                    transport=transport,
                    timeout=timeout,
                    t1=t1,
                    t2=t2,
                    retransmit_reliable=True,
                )
                if result.timed_out and self.active:
                    await on_timeout()
            except asyncio.CancelledError:
                return
            finally:
                if self.task is asyncio.current_task():
                    self.task = None

        self.task = asyncio.create_task(run(), name=task_name)
        return True
