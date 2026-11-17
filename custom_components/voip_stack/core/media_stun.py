"""Bounded, unauthenticated STUN discovery on the actual media UDP sockets.

RFC 8489 sections 5, 6.2.1, 6.3.3, 14.2 and 14.7 apply. Call before
installing media readers. Borrowed sockets remain bound, nonblocking and open;
only components with a validated IPv4 XOR-MAPPED-ADDRESS are returned. STUN
alone does not guarantee that the discovered mapping is usable by another peer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import ipaddress
import math
import re
import secrets
import socket
import struct
import zlib


_COOKIE = 0x2112A442
_URI = re.compile(r"stun:([^:/?#@\s]+)(?::([0-9]+))?", re.IGNORECASE)
_INITIAL_RTO = 0.5


class _UnsupportedRequiredAttribute(ValueError):
    """RFC 8489 section 6.3.3 fails this transaction, not other servers."""


async def _resolve_server(url: str) -> list[tuple[str, int]]:
    match = _URI.fullmatch(url)
    if match is None:
        return []
    host, explicit_port = match.groups()
    try:
        port = int(explicit_port) if explicit_port else 3478
        if not 0 < port <= 65535:
            return []
        addresses = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM,
            proto=socket.IPPROTO_UDP,
        )
    except (OSError, ValueError, UnicodeError):
        return []
    return list(dict.fromkeys(item[4][:2] for item in addresses))


def _mapped_address(data: bytes, transaction_id: bytes) -> tuple[str, int] | None:
    """Discard malformed/unsupported responses; never infer a missing mapping."""
    if len(data) < 20:
        return None
    kind, size, cookie = struct.unpack_from("!HHI", data)
    if (kind != 0x0101 or cookie != _COOKIE or size % 4
            or len(data) != 20 + size or data[8:20] != transaction_id):
        return None
    mapped = None
    unsupported = False
    offset = 20
    while offset < len(data):
        if offset + 4 > len(data):
            return None
        attribute, length = struct.unpack_from("!HH", data, offset)
        value = offset + 4
        padded_end = value + ((length + 3) & ~3)
        if padded_end > len(data):
            return None
        if attribute == 0x0020:
            if length != 8 or data[value + 1] != 1:
                return None
            # The first (reserved) byte and attribute padding are ignored by
            # receivers, per RFC 8489 sections 14 and 14.1/14.2.
            port = struct.unpack_from("!H", data, value + 2)[0] ^ 0x2112
            packed_ip = struct.unpack_from("!I", data, value + 4)[0] ^ _COOKIE
            ip = ipaddress.IPv4Address(packed_ip)
            if not port or packed_ip < 0x01000000 or ip.is_multicast or ip.is_reserved:
                return None
            if mapped is None:
                mapped = (str(ip), port)
        elif attribute == 0x8028:
            if length != 4 or padded_end != len(data):
                return None
            fingerprint = struct.unpack_from("!I", data, value)[0]
            if fingerprint != zlib.crc32(data[:offset]) ^ 0x5354554E:
                return None
        elif attribute == 0x0001:
            # A legacy MAPPED-ADDRESS may accompany XOR-MAPPED-ADDRESS but is
            # never used as a fallback. No credential/integrity mechanism is
            # negotiated by this limited, unauthenticated usage.
            if length not in (8, 20) or data[value + 1] != (1 if length == 8 else 2):
                return None
        elif attribute < 0x8000:
            unsupported = True
        offset = padded_end
    if unsupported:
        raise _UnsupportedRequiredAttribute
    return mapped


async def _discover_socket(
    sock: socket.socket,
    resolutions: Sequence[asyncio.Task],
    deadline: float,
) -> tuple[str, int] | None:
    loop = asyncio.get_running_loop()
    pending_dns = set(resolutions)
    # Endpoint -> [transaction id, request, next send time, retransmission RTO].
    transactions: dict[tuple[str, int], list] = {}
    receiver: asyncio.Task | None = None
    try:
        while loop.time() < deadline:
            for resolution in tuple(pending_dns):
                if not resolution.done():
                    continue
                pending_dns.remove(resolution)
                for endpoint in resolution.result():
                    if endpoint not in transactions:
                        txid = secrets.token_bytes(12)
                        request = struct.pack("!HHI", 0x0001, 0, _COOKIE) + txid
                        transactions[endpoint] = [txid, request, loop.time(), _INITIAL_RTO]
            if not transactions and not pending_dns:
                return None
            if receiver is None and transactions:
                receiver = asyncio.create_task(loop.sock_recvfrom(sock, 65535))
            for endpoint, transaction in tuple(transactions.items()):
                if loop.time() >= transaction[2]:
                    try:
                        await loop.sock_sendto(sock, transaction[1], endpoint)
                    except OSError:
                        del transactions[endpoint]
                        continue
                    transaction[2] = loop.time() + transaction[3]
                    transaction[3] *= 2
            wakeup = min((item[2] for item in transactions.values()), default=deadline)
            waiters = pending_dns | ({receiver} if receiver is not None else set())
            if not waiters:
                return None
            done, _ = await asyncio.wait(
                waiters, timeout=max(0, min(deadline, wakeup) - loop.time()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if receiver is not None and receiver in done:
                try:
                    data, source = receiver.result()
                except OSError:
                    return None
                receiver = None
                transaction = transactions.get(source)
                if transaction is not None:
                    try:
                        mapped = _mapped_address(data, transaction[0])
                    except _UnsupportedRequiredAttribute:
                        del transactions[source]
                        continue
                    if mapped is not None:
                        return mapped
    finally:
        if receiver is not None:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
    return None


async def discover_media_endpoints(
    sockets: Mapping[str, socket.socket],
    server_urls: Sequence[str],
    *,
    timeout: float = 3.0,
) -> dict[str, tuple[str, int]]:
    """Return validated mappings within one DNS+UDP deadline (partial on failure).

    Each socket must be a distinct, already-bound nonblocking IPv4 UDP socket
    with no other reader. Invalid socket inputs raise ValueError before I/O.
    Cancellation propagates after removing all temporary readers/tasks. No
    sockets, bindings or blocking modes are created, closed or changed here.
    Alternative servers and components run concurrently, so one failed DNS
    lookup or silent component cannot hold up another successful component.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        return {}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    descriptors = set()
    for sock in sockets.values():
        descriptor = sock.fileno()
        if (sock.family != socket.AF_INET or sock.type != socket.SOCK_DGRAM
                or sock.getblocking() or descriptor < 0 or descriptor in descriptors
                or not sock.getsockname()[1]):
            raise ValueError("media STUN requires distinct bound nonblocking IPv4 UDP sockets")
        descriptors.add(descriptor)
    if not sockets or not server_urls:
        return {}
    resolutions = [asyncio.create_task(_resolve_server(url)) for url in dict.fromkeys(server_urls)]
    results: dict[str, tuple[str, int]] = {}

    async def discover_component(name: str, sock: socket.socket) -> None:
        mapped = await _discover_socket(sock, resolutions, deadline)
        if mapped is not None:
            results[name] = mapped

    workers = [asyncio.create_task(discover_component(name, sock)) for name, sock in sockets.items()]
    try:
        async with asyncio.timeout_at(deadline):
            await asyncio.gather(*workers)
    except TimeoutError:
        pass
    finally:
        for task in (*workers, *resolutions):
            task.cancel()
        await asyncio.gather(*workers, *resolutions, return_exceptions=True)
    return results
