"""RFC 3263 server discovery contracts."""

from __future__ import annotations

import random

import pytest

from .voip_phase1_support import sip, sip_resolution

NaptrRecord = sip_resolution.NaptrRecord
SipServerResolver = sip_resolution.SipServerResolver
SrvRecord = sip_resolution.SrvRecord


@pytest.mark.asyncio
async def test_naptr_srv_priority_weight_and_dual_stack_are_one_route() -> None:
    queries: list[tuple[str, str]] = []

    async def dns_query(name: str, kind: str):
        queries.append((name, kind))
        records = {
            ("pbx.example", "NAPTR"): (
                NaptrRecord(10, 10, "SIP+D2T", "_sip._tcp.pbx.example."),
                NaptrRecord(20, 10, "SIP+D2U", "_sip._udp.pbx.example."),
            ),
            ("_sip._tcp.pbx.example", "SRV"): (
                SrvRecord(10, 0, 5070, "primary.example."),
                SrvRecord(20, 0, 5080, "backup.example."),
            ),
            ("_sip._udp.pbx.example", "SRV"): (),
        }.get((name.rstrip("."), kind), ())
        return records, 60

    async def addresses(host: str, _port: int, _transport: str):
        return {
            "primary.example": ("192.0.2.10", "2001:db8::10"),
            "backup.example": ("192.0.2.11",),
        }[host]

    resolver = SipServerResolver(
        dns_query=dns_query,
        address_query=addresses,
        random_source=random.Random(1),
    )
    targets = await resolver.resolve(sip.parse_sip_uri("sip:pbx.example"))

    assert [(item.host, item.port, item.transport) for item in targets] == [
        ("primary.example", 5070, "TCP"),
        ("backup.example", 5080, "TCP"),
    ]
    assert targets[0].addresses == ("192.0.2.10", "2001:db8::10")
    assert queries.count(("pbx.example", "NAPTR")) == 1


@pytest.mark.asyncio
async def test_explicit_port_bypasses_naptr_and_srv() -> None:
    async def unexpected_dns(_name: str, _kind: str):
        raise AssertionError("explicit port must bypass NAPTR and SRV")

    async def addresses(host: str, port: int, transport: str):
        assert (host, port, transport) == ("pbx.example", 5090, "TCP")
        return ("192.0.2.20",)

    target = (
        await SipServerResolver(
            dns_query=unexpected_dns,
            address_query=addresses,
        ).resolve(
            sip.parse_sip_uri("sip:pbx.example:5090;transport=tcp")
        )
    )[0]

    assert target.addresses == ("192.0.2.20",)


@pytest.mark.asyncio
async def test_sips_never_downgrades_transport() -> None:
    resolver = SipServerResolver(
        dns_query=lambda _name, _kind: _empty_dns(),
        address_query=lambda host, port, transport: _address(host, port, transport),
    )

    with pytest.raises(sip.SipError, match="cannot downgrade"):
        await resolver.resolve(
            sip.parse_sip_uri("sips:pbx.example;transport=udp")
        )
    target = (await resolver.resolve(sip.parse_sip_uri("sips:pbx.example")))[0]
    assert (target.port, target.transport) == (5061, "TLS")


async def _empty_dns() -> tuple[tuple[object, ...], float]:
    return (), 30


async def _address(host: str, _port: int, _transport: str) -> tuple[str, ...]:
    return (host,)
