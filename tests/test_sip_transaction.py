#!/usr/bin/env python3
"""Transport-independent SIP transaction timer contracts."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _load_module(name: str):
    if "custom_components" not in sys.modules:
        root = types.ModuleType("custom_components")
        root.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root
    if PKG_NAME not in sys.modules:
        package = types.ModuleType(PKG_NAME)
        package.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = package
    core_pkg = types.ModuleType(f"{PKG_NAME}.core")
    core_pkg.__path__ = [str(PKG_DIR / "core")]
    sys.modules.setdefault(f"{PKG_NAME}.core", core_pkg)
    full_name = f"{PKG_NAME}.core.{name}"
    spec = importlib.util.spec_from_file_location(
        full_name, PKG_DIR / "core" / f"{name}.py"
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


sip_transaction = _load_module("sip_transaction")


class SipTransactionTest(unittest.IsolatedAsyncioTestCase):
    async def test_udp_client_retransmits_exponentially_until_response(self) -> None:
        sends: list[float] = []
        response_ready = asyncio.Event()

        async def read(timeout: float):
            try:
                await asyncio.wait_for(response_ready.wait(), timeout)
            except asyncio.TimeoutError:
                return None
            return "200"

        async def send() -> None:
            sends.append(asyncio.get_running_loop().time())
            if len(sends) == 2:
                response_ready.set()

        transaction = sip_transaction.SipClientTransaction(
            transport="UDP", timeout=0.2, t1=0.005, t2=0.02
        )
        response = await transaction.receive(read, send)

        self.assertEqual(response, "200")
        self.assertEqual(transaction.retransmissions, 2)
        self.assertGreaterEqual(sends[1] - sends[0], 0.009)

    async def test_reliable_client_never_retransmits(self) -> None:
        sends = 0

        async def read(_timeout: float):
            return None

        async def send() -> None:
            nonlocal sends
            sends += 1

        transaction = sip_transaction.SipClientTransaction(
            transport="TCP", timeout=0.01, t1=0.001, t2=0.002
        )
        self.assertIsNone(await transaction.receive(read, send))
        self.assertEqual(sends, 0)

    @pytest.mark.fault
    async def test_provisional_response_disables_invite_retransmission(self) -> None:
        sends = 0

        async def read(_timeout: float):
            await asyncio.sleep(0.001)
            return None

        async def send() -> None:
            nonlocal sends
            sends += 1

        transaction = sip_transaction.SipClientTransaction(
            transport="UDP", timeout=0.01, t1=0.001, t2=0.002
        )
        self.assertIsNone(
            await transaction.receive(read, send, retransmit_enabled=False)
        )
        self.assertEqual(sends, 0)

    async def test_server_timer_retransmits_udp_but_not_tcp(self) -> None:
        for transport, expected in (("UDP", True), ("TCP", False)):
            with self.subTest(transport=transport):
                active = True
                sends = 0

                def send() -> bool:
                    nonlocal sends
                    sends += 1
                    return True

                result = await sip_transaction.async_run_server_transaction(
                    send=send,
                    active=lambda current=active: current,
                    transport=transport,
                    timeout=0.007,
                    t1=0.001,
                    t2=0.002,
                )
                self.assertTrue(result.timed_out)
                self.assertEqual(bool(sends), expected)

    @pytest.mark.fault
    async def test_server_timer_stops_without_timeout_when_ack_arrives(self) -> None:
        active = True

        def send() -> bool:
            nonlocal active
            active = False
            return True

        result = await sip_transaction.async_run_server_transaction(
            send=send,
            active=lambda: active,
            transport="UDP",
            timeout=0.1,
            t1=0.001,
            t2=0.002,
        )

        self.assertFalse(result.timed_out)
        self.assertEqual(result.retransmissions, 1)

    async def test_invite_2xx_core_retransmits_even_over_reliable_transport(self) -> None:
        sends = 0
        now = 0.0

        class _Clock:
            @staticmethod
            def time() -> float:
                return now

        async def advance(delay: float) -> None:
            nonlocal now
            now += delay

        def send() -> bool:
            nonlocal sends
            sends += 1
            return True

        with (
            patch.object(sip_transaction.asyncio, "get_running_loop", return_value=_Clock()),
            patch.object(sip_transaction.asyncio, "sleep", side_effect=advance),
        ):
            result = await sip_transaction.async_run_server_transaction(
                send=send,
                active=lambda: True,
                transport="TCP",
                timeout=0.004,
                t1=0.001,
                t2=0.002,
                retransmit_reliable=True,
            )

        self.assertTrue(result.timed_out)
        self.assertGreaterEqual(sends, 1)
