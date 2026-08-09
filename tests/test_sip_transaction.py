#!/usr/bin/env python3
"""Transport-independent SIP transaction timer contracts."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

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
sip_dialog = _load_module("sip_dialog")


class SipTransactionTest(unittest.IsolatedAsyncioTestCase):
    async def test_dialog_transaction_stops_when_request_retires_dialog(self) -> None:
        active = True
        sends = 0
        request = sip_transaction.sip.parse_message(
            b"UPDATE sip:local@example.test SIP/2.0\r\n"
            b"Call-ID: call-1\r\nCSeq: 2 UPDATE\r\nContent-Length: 0\r\n\r\n"
        )

        def send() -> bool:
            nonlocal sends
            sends += 1
            return True

        async def read(_timeout: float):
            return request, ("192.0.2.2", 5060)

        async def retire(_message, _source) -> None:
            nonlocal active
            active = False

        result = await asyncio.wait_for(
            sip_transaction.async_run_dialog_request_transaction(
                send=send,
                read=read,
                matches=lambda _message: False,
                active=lambda: active,
                transport="UDP",
                timeout=1.0,
                on_request=retire,
            ),
            timeout=0.05,
        )

        self.assertIsNone(result)
        self.assertEqual(sends, 1)

    async def test_remote_offer_media_commit_succeeds_without_rollback(self) -> None:
        commit = AsyncMock()
        rollback = AsyncMock()

        assert await sip_dialog.apply_remote_offer_media(commit, rollback)
        commit.assert_awaited_once_with()
        rollback.assert_not_awaited()

    async def test_remote_offer_media_commit_failure_rolls_back_once(self) -> None:
        commit = AsyncMock(side_effect=RuntimeError("media owner failed"))
        rollback = AsyncMock()

        assert not await sip_dialog.apply_remote_offer_media(commit, rollback)
        commit.assert_awaited_once_with()
        rollback.assert_awaited_once_with()

    async def test_dialog_transaction_handles_requests_and_defers_other_responses(
        self,
    ) -> None:
        messages = iter(
            (
                (
                    sip_transaction.sip.parse_message(
                        b"BYE sip:local@example.test SIP/2.0\r\n"
                        b"Call-ID: call-1\r\nCSeq: 2 BYE\r\nContent-Length: 0\r\n\r\n"
                    ),
                    ("192.0.2.2", 5060),
                ),
                (
                    sip_transaction.sip.parse_message(
                        sip_transaction.sip.build_response(
                            200,
                            "OK",
                            (("Call-ID", "other"), ("CSeq", "7 UPDATE")),
                        )
                    ),
                    ("192.0.2.3", 5060),
                ),
                (
                    sip_transaction.sip.parse_message(
                        sip_transaction.sip.build_response(
                            200,
                            "OK",
                            (("Call-ID", "call-1"), ("CSeq", "8 UPDATE")),
                        )
                    ),
                    ("192.0.2.4", 5060),
                ),
            )
        )
        requests = []
        deferred = []
        sends = 0

        async def read(_timeout):
            return next(messages, None)

        async def on_request(message, source):
            requests.append((message.method, source))

        def send():
            nonlocal sends
            sends += 1
            return True

        response = await sip_transaction.async_run_dialog_request_transaction(
            send=send,
            read=read,
            matches=lambda message: message.header("Call-ID") == "call-1",
            active=lambda: True,
            transport="TCP",
            timeout=0.1,
            on_request=on_request,
            on_unmatched=lambda message, source: deferred.append(
                (message.header("Call-ID"), source)
            ),
        )

        self.assertEqual(response.header("CSeq"), "8 UPDATE")
        self.assertEqual(requests, [("BYE", ("192.0.2.2", 5060))])
        self.assertEqual(deferred, [("other", ("192.0.2.3", 5060))])
        self.assertEqual(sends, 1)

    async def test_dialog_transaction_does_not_send_when_owner_is_inactive(
        self,
    ) -> None:
        sends = 0

        def send():
            nonlocal sends
            sends += 1
            return True

        response = await sip_transaction.async_run_dialog_request_transaction(
            send=send,
            read=lambda _timeout: asyncio.sleep(0, result=None),
            matches=lambda _message: True,
            active=lambda: False,
            transport="UDP",
            timeout=0.01,
        )

        self.assertIsNone(response)
        self.assertEqual(sends, 0)

    async def test_session_refresh_commits_successful_response(self) -> None:
        state = sip_transaction.sip.SipSessionTimer(interval=1800)
        calls = []

        async def send(method, *, extra_headers):
            calls.append((method, extra_headers))
            return sip_transaction.sip.parse_message(
                sip_transaction.sip.build_response(
                    200,
                    "OK",
                    [("Session-Expires", "900;refresher=uas")],
                )
            )

        result = await sip_transaction.SessionTimerDriver(
            state, send, "uac", lambda: 10.0
        ).refresh()

        self.assertEqual(result, "refreshed")
        self.assertEqual(state.interval, 900)
        self.assertFalse(state.local_refresher)
        self.assertEqual(state.deadline, 910.0)
        self.assertEqual(calls[0][0], "UPDATE")
        self.assertIn(("Supported", "timer"), calls[0][1])

    async def test_session_refresh_retries_422_once_with_new_interval(self) -> None:
        state = sip_transaction.sip.SipSessionTimer(interval=90)
        responses = iter(
            (
                sip_transaction.sip.build_response(
                    422,
                    "Session Interval Too Small",
                    [("Min-SE", "180")],
                ),
                sip_transaction.sip.build_response(
                    200,
                    "OK",
                    [("Session-Expires", "180;refresher=uac")],
                ),
            )
        )
        intervals = []

        async def send(_method, *, extra_headers):
            intervals.append(dict(extra_headers)["Session-Expires"])
            return sip_transaction.sip.parse_message(next(responses))

        result = await sip_transaction.SessionTimerDriver(
            state, send, "uac", lambda: 20.0
        ).refresh()

        self.assertEqual(result, "refreshed")
        self.assertEqual(intervals, ["90;refresher=uac", "180;refresher=uac"])
        self.assertEqual(state.refresh_at, 110.0)

    async def test_session_refresh_stops_after_bounded_failure(self) -> None:
        state = sip_transaction.sip.SipSessionTimer(interval=90)
        calls = 0

        async def send(_method, *, extra_headers):
            nonlocal calls
            calls += 1
            self.assertEqual(
                dict(extra_headers)["Session-Expires"],
                "90;refresher=uac",
            )
            return None

        result = await sip_transaction.SessionTimerDriver(
            state, send, "uac", lambda: 0.0
        ).refresh()

        self.assertEqual(result, "session_timer_failed")
        self.assertEqual(calls, 1)

    async def test_session_refresh_allows_at_most_one_422_retry(self) -> None:
        state = sip_transaction.sip.SipSessionTimer(interval=90)
        calls = 0

        async def send(_method, *, extra_headers):
            nonlocal calls
            calls += 1
            self.assertEqual(
                dict(extra_headers)["Session-Expires"],
                f"{90 if calls == 1 else 180};refresher=uac",
            )
            return sip_transaction.sip.parse_message(
                sip_transaction.sip.build_response(
                    422,
                    "Session Interval Too Small",
                    [("Min-SE", "180")],
                )
            )

        result = await sip_transaction.SessionTimerDriver(
            state, send, "uac", lambda: 0.0
        ).refresh()

        self.assertEqual(result, "session_timer_failed")
        self.assertEqual(calls, 2)

    async def test_session_timer_driver_uses_role_deadline_and_refresh_method(
        self,
    ) -> None:
        state = sip_transaction.sip.SipSessionTimer()
        state.configure(
            sip_transaction.sip.SipSessionExpires(90, "uas"),
            local_role="uas",
            now=100.0,
        )
        methods = []

        async def send(method, *, extra_headers):
            methods.append((method, dict(extra_headers)["Session-Expires"]))
            return sip_transaction.sip.parse_message(
                sip_transaction.sip.build_response(
                    200,
                    "OK",
                    [("Session-Expires", "120;refresher=uas")],
                )
            )

        driver = sip_transaction.SessionTimerDriver(
            state, send, "uas", lambda: 145.0, "INVITE"
        )
        self.assertEqual(driver.deadline, 145.0)
        self.assertEqual(await driver.advance(), "refreshed")
        self.assertEqual(methods, [("INVITE", "90;refresher=uas")])
        self.assertEqual(state.refresh_at, 205.0)

    async def test_session_timer_driver_expires_remote_refresher_at_deadline(
        self,
    ) -> None:
        state = sip_transaction.sip.SipSessionTimer()
        state.configure(
            sip_transaction.sip.SipSessionExpires(90, "uac"),
            local_role="uas",
            now=100.0,
        )
        send = AsyncMock()
        driver = sip_transaction.SessionTimerDriver(
            state, send, "uas", lambda: 160.0
        )
        self.assertEqual(driver.deadline, 160.0)
        self.assertEqual(await driver.advance(), "session_timer_expired")
        send.assert_not_awaited()

    def test_dialog_request_uses_remote_target_without_route_set(self) -> None:
        request = sip_dialog.build_dialog_request(
            "UPDATE",
            call_id="call-1",
            local_tag="local",
            remote_tag="remote",
            cseq=4,
            local_uri="sip:local@example.test",
            remote_uri="sip:remote@example.test",
            remote_target_uri="sip:remote@192.0.2.20:5070",
            contact_uri="sip:local@192.0.2.10:5060",
            transport="TCP",
            extra_headers=(("Session-Expires", "90;refresher=uac"),),
        )

        parsed = sip_dialog.sip.parse_message(request.raw)
        self.assertEqual(parsed.uri, "sip:remote@192.0.2.20:5070")
        self.assertEqual(parsed.header("CSeq"), "4 UPDATE")
        self.assertEqual(parsed.header("Session-Expires"), "90;refresher=uac")
        self.assertEqual(parsed.header_values("Route"), [])
        self.assertEqual(request.routing.next_hop_uri, parsed.uri)

    def test_dialog_request_applies_strict_route_once(self) -> None:
        request = sip_dialog.build_dialog_request(
            "PRACK",
            call_id="call-2",
            local_tag="local",
            remote_tag="remote",
            cseq=8,
            local_uri="sip:local@example.test",
            remote_uri="sip:remote@example.test",
            remote_target_uri="sip:remote@192.0.2.20:5070",
            route_set=(
                "<sip:strict.example.test:5080>",
                "<sip:loose.example.test:5090;lr>",
            ),
            extra_headers=(("RAck", "1 7 INVITE"),),
        )

        parsed = sip_dialog.sip.parse_message(request.raw)
        self.assertEqual(parsed.uri, "sip:strict.example.test:5080")
        self.assertEqual(
            parsed.header_values("Route"),
            [
                "<sip:loose.example.test:5090;lr>",
                "<sip:remote@192.0.2.20:5070>",
            ],
        )
        self.assertEqual(parsed.header("RAck"), "1 7 INVITE")

    def test_rfc_client_transaction_deadlines(self) -> None:
        self.assertEqual(sip_transaction.SIP_TIMER_B, 32.0)
        self.assertEqual(sip_transaction.SIP_TIMER_F, 32.0)
        self.assertEqual(sip_transaction.SIP_TIMER_H, 32.0)

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
