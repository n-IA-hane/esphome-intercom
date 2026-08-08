"""RFC 3263 SIP server discovery with one ordered failover contract."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import random
import socket
import time
from typing import Awaitable, Callable, Iterable

from .sip import SipError, SipUri


@dataclass(frozen=True, slots=True)
class SipServerTarget:
    host: str
    port: int
    transport: str
    addresses: tuple[str, ...]
    priority: int = 0
    weight: int = 0


@dataclass(frozen=True, slots=True)
class NaptrRecord:
    order: int
    preference: int
    service: str
    replacement: str


@dataclass(frozen=True, slots=True)
class SrvRecord:
    priority: int
    weight: int
    port: int
    target: str


DnsQuery = Callable[[str, str], Awaitable[tuple[tuple[object, ...], float]]]
AddressQuery = Callable[[str, int, str], Awaitable[tuple[str, ...]]]

_NAPTR_TRANSPORTS = {
    "SIP+D2U": "UDP",
    "SIP+D2T": "TCP",
    "SIPS+D2T": "TLS",
}
_SRV_SERVICE = {
    "UDP": "_sip._udp",
    "TCP": "_sip._tcp",
    "TLS": "_sips._tcp",
}


class SipServerResolver:
    """Resolve one SIP URI into every standards-ordered connection target."""

    def __init__(
        self,
        *,
        dns_query: DnsQuery | None = None,
        address_query: AddressQuery | None = None,
        random_source: random.Random | None = None,
    ) -> None:
        self._dns_query = dns_query or self._query_dns
        self._address_query = address_query or self._query_addresses
        self._random = random_source or random.Random()
        self._cache: dict[tuple[str, str], tuple[float, tuple[object, ...]]] = {}

    async def resolve(
        self,
        uri: SipUri,
        *,
        transport: str = "",
    ) -> tuple[SipServerTarget, ...]:
        requested = self._requested_transport(uri, transport)
        if uri.port is not None:
            return (
                await self._address_target(
                    uri.host,
                    uri.port,
                    requested or ("TLS" if uri.scheme == "sips" else "UDP"),
                ),
            )

        routes = (
            ((requested, f"{_SRV_SERVICE[requested]}.{uri.host}"),)
            if requested
            else await self._naptr_routes(uri)
        )
        if not routes:
            transports = ("TLS",) if uri.scheme == "sips" else ("UDP", "TCP")
            routes = tuple(
                (candidate, f"{_SRV_SERVICE[candidate]}.{uri.host}")
                for candidate in transports
            )
        discovered: list[SipServerTarget] = []
        for candidate, service_name in routes:
            discovered.extend(await self._srv_targets(service_name, candidate))
        if discovered:
            return tuple(discovered)
        fallback = routes[0][0]
        return (
            await self._address_target(
                uri.host,
                5061 if fallback == "TLS" else 5060,
                fallback,
            ),
        )

    @staticmethod
    def _requested_transport(uri: SipUri, configured: str) -> str:
        value = str(configured or "").strip().upper()
        if not value:
            value = next(
                (
                    str(param or "").upper()
                    for key, param in uri.params
                    if key.casefold() == "transport"
                ),
                "",
            )
        if value and value not in {"UDP", "TCP", "TLS"}:
            raise SipError(f"unsupported SIP transport {value!r}")
        if uri.scheme == "sips" and value not in {"", "TLS"}:
            raise SipError("sips URI cannot downgrade to an insecure transport")
        return "TLS" if uri.scheme == "sips" else value

    async def _naptr_routes(self, uri: SipUri) -> tuple[tuple[str, str], ...]:
        records = await self._records(uri.host, "NAPTR")
        ordered = sorted(
            (item for item in records if isinstance(item, NaptrRecord)),
            key=lambda item: (item.order, item.preference),
        )
        routes = tuple(
            (transport, item.replacement.rstrip("."))
            for item in ordered
            if (transport := _NAPTR_TRANSPORTS.get(item.service.upper())) is not None
            and (uri.scheme != "sips" or transport == "TLS")
        )
        return tuple(dict.fromkeys(routes))

    async def _srv_targets(
        self,
        service_name: str,
        transport: str,
    ) -> list[SipServerTarget]:
        records = await self._records(service_name, "SRV")
        grouped: dict[int, list[SrvRecord]] = {}
        for item in records:
            if isinstance(item, SrvRecord) and item.target != ".":
                grouped.setdefault(item.priority, []).append(item)
        targets: list[SipServerTarget] = []
        for priority in sorted(grouped):
            pending = grouped[priority]
            while pending:
                total = sum(max(0, item.weight) for item in pending)
                choice = self._random.uniform(0, total) if total else 0
                selected = pending[-1]
                for item in pending:
                    choice -= max(0, item.weight)
                    if choice <= 0:
                        selected = item
                        break
                pending.remove(selected)
                targets.append(
                    await self._address_target(
                        selected.target.rstrip("."),
                        selected.port,
                        transport,
                        priority=selected.priority,
                        weight=selected.weight,
                    )
                )
        return targets

    async def _address_target(
        self,
        host: str,
        port: int,
        transport: str,
        *,
        priority: int = 0,
        weight: int = 0,
    ) -> SipServerTarget:
        return SipServerTarget(
            host=host,
            port=int(port),
            transport=transport,
            addresses=await self._address_query(host, int(port), transport),
            priority=priority,
            weight=weight,
        )

    async def _records(self, name: str, kind: str) -> tuple[object, ...]:
        key = (name.casefold().rstrip("."), kind)
        now = time.monotonic()
        cached = self._cache.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            records, ttl = await self._dns_query(name, kind)
        except Exception:
            records, ttl = (), 5.0
        self._cache[key] = (now + max(1.0, float(ttl)), records)
        return records

    @staticmethod
    async def _query_addresses(host: str, port: int, transport: str) -> tuple[str, ...]:
        sock_type = socket.SOCK_DGRAM if transport == "UDP" else socket.SOCK_STREAM
        answers = await asyncio.get_running_loop().getaddrinfo(
            host,
            port,
            family=socket.AF_UNSPEC,
            type=sock_type,
        )
        return tuple(dict.fromkeys(str(answer[4][0]) for answer in answers))

    @staticmethod
    async def _query_dns(name: str, kind: str) -> tuple[tuple[object, ...], float]:
        import dns.asyncresolver  # type: ignore[import-not-found]

        answer = await dns.asyncresolver.resolve(name, kind, search=False)
        ttl = float(answer.rrset.ttl if answer.rrset is not None else 30)
        if kind == "SRV":
            records: Iterable[object] = (
                SrvRecord(item.priority, item.weight, item.port, str(item.target))
                for item in answer
            )
        else:
            records = (
                NaptrRecord(
                    item.order,
                    item.preference,
                    bytes(item.service).decode(),
                    str(item.replacement),
                )
                for item in answer
                if bytes(item.flags).decode().upper() == "S"
            )
        return tuple(records), ttl
