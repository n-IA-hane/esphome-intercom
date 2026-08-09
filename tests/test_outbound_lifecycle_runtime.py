#!/usr/bin/env python3
"""Runtime tests for outbound HA softphone SIP lifecycle ownership."""

from __future__ import annotations

import asyncio
from enum import StrEnum
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


class ServiceValidationError(ValueError):
    """Minimal Home Assistant service validation error."""


class CallState(StrEnum):
    IDLE = "idle"
    CALLING = "calling"
    REMOTE_RINGING = "remote_ringing"
    RINGING = "ringing"
    CONNECTING = "connecting"
    IN_CALL = "in_call"
    TERMINATING = "terminating"


class TerminalReason(StrEnum):
    REMOTE_HANGUP = "remote_hangup"
    LOCAL_HANGUP = "local_hangup"


class _WireFormat:
    def __init__(self, token: str) -> None:
        self.token = token
        self.audio_format = self

    def wire_token(self) -> str:
        return self.token


class _Dialog:
    def __init__(self) -> None:
        self.send_format = _WireFormat("L16/16000/1;ptime=20")
        self.recv_format = _WireFormat("L16/16000/1;ptime=20")
        self.local_audio_direction = "sendrecv"
        self.remote_audio_connection_held = False
        self.video_format = None
        self.send_video_format = None
        self.recv_video_format = None
        self.local_video_direction = "inactive"


class _Client:
    def __init__(
        self,
        call_id: str,
        *,
        final: str = "in_call",
        terminal: str = "remote_hangup",
    ) -> None:
        self.dialog_ids = types.SimpleNamespace(call_id=call_id)
        self.dialog = _Dialog() if final == "in_call" else None
        self.final = final
        self.terminal = terminal
        self.connected_party = "Kitchen P4"
        self.last_sip_status_code = 200
        self.last_sip_event = "SIP_RESPONSE"
        self.closed = 0

    async def wait_for_final(self) -> str:
        return self.final

    async def wait_for_dialog_termination(self) -> str:
        return self.terminal

    async def close(self) -> None:
        self.closed += 1


class _Registry:
    def __init__(self) -> None:
        self.sip_clients: dict[str, object] = {}
        self.sessions: dict[str, object] = {}
        self.calls: list[tuple] = []
        self.watchers: dict[str, asyncio.Task] = {}

    @staticmethod
    def resolve_session_id(call_id: str) -> str:
        return call_id

    def sip_client_for(self, call_id: str):
        return self.sip_clients.get(call_id)

    def sip_client_items(self):
        return iter(self.sip_clients.items())

    def terminate_call(self, call_id: str, **kwargs) -> None:
        self.calls.append(("finish", call_id, kwargs))
        self.sessions.pop(call_id, None)

    async def terminate_call_wait(self, call_id: str, **kwargs) -> None:
        self.calls.append(("finish", call_id, kwargs))
        client = self.sip_clients.pop(call_id, None)
        self.sessions.pop(call_id, None)
        self.watchers.pop(call_id, None)
        if client is not None and hasattr(client, "close"):
            await client.close()

    def upsert(self, call_id: str, **kwargs):
        self.calls.append(("upsert", call_id, kwargs))
        session = types.SimpleNamespace(
            call_id=call_id,
            state=kwargs.get("state", ""),
            caller=kwargs.get("caller", ""),
            callee=kwargs.get("callee", ""),
            metadata={"endpoint_id": kwargs.get("endpoint_id", "default")},
        )
        self.sessions[call_id] = session
        return session

    def add_leg(self, call_id: str, leg_id: str, **kwargs) -> None:
        self.calls.append(("leg", call_id, leg_id, kwargs))

    def attach_sip_client(
        self,
        call_id: str,
        leg_id: str,
        client: object,
        **kwargs,
    ) -> None:
        self.calls.append(("attach", call_id, leg_id, kwargs))
        self.sip_clients[call_id] = client

    def attach_client_watcher(self, call_id: str, task: asyncio.Task) -> None:
        self.calls.append(("watcher", call_id))
        self.watchers[call_id] = task


class _Hass:
    def __init__(self) -> None:
        self.data: dict = {}
        self.config = types.SimpleNamespace(location_name="Casa")
        self.artifacts = types.SimpleNamespace(softphone_start_locks={})

    @staticmethod
    def async_create_task(coroutine):
        return asyncio.create_task(coroutine)


def _load_outbound_lifecycle(
    registry: _Registry,
    states: list[tuple],
    stores: dict[str, dict],
    cleanup: AsyncMock,
):
    if "custom_components" not in sys.modules:
        root = types.ModuleType("custom_components")
        root.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root
    if PKG_NAME not in sys.modules:
        package = types.ModuleType(PKG_NAME)
        package.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = package

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.ServiceValidationError = ServiceValidationError
    const = types.ModuleType(f"{PKG_NAME}.const")
    const.DOMAIN = "voip_stack"
    const.HA_PEER_FALLBACK_NAME = "Home Assistant"
    const.HA_SOFTPHONE_DEVICE_ID = "ha-softphone"
    endpoint_lifecycle = types.ModuleType(f"{PKG_NAME}.endpoint_lifecycle")
    endpoint_lifecycle.call_registry = lambda _hass: registry
    endpoint_session = types.ModuleType(f"{PKG_NAME}.endpoint_session")
    endpoint_session.TerminationInitiator = types.SimpleNamespace(
        INTERNAL="internal",
        LOCAL_USER="local_user",
        REMOTE_PEER="remote_peer",
        RUNTIME="runtime",
    )

    class TerminationIntent:
        def __init__(
            self,
            reason: str,
            *,
            initiator: str = "internal",
            response_status: int = 0,
        ) -> None:
            self.reason = reason
            self.initiator = initiator
            self.response_status = response_status
            self.public_state = "idle" if reason == "remote_hangup" else reason

    endpoint_session.TerminationIntent = TerminationIntent
    endpoint_termination = types.ModuleType(f"{PKG_NAME}.endpoint_termination")

    class EndpointTerminationHandler:
        def __init__(self, _hass) -> None:
            pass

        async def terminate(self, call_id: str, intent: TerminationIntent) -> None:
            await registry.terminate_call_wait(call_id, intent=intent)
            states.append(
                (
                    intent.public_state,
                    {
                        "terminal_reason": intent.reason,
                        "origin": (
                            "remote" if intent.initiator == "remote_peer" else "self"
                        ),
                    },
                )
            )

        async def terminate_reason(
            self,
            call_id: str,
            reason: str,
            initiator: str = "internal",
        ) -> None:
            await self.terminate(
                call_id,
                TerminationIntent(reason, initiator=initiator),
            )

    endpoint_termination.EndpointTerminationHandler = EndpointTerminationHandler
    fsm = types.ModuleType(f"{PKG_NAME}.fsm")
    fsm.CallState = CallState
    fsm.TerminalReason = TerminalReason
    fsm.sip_public_state = lambda state: state
    fsm.sip_terminal_reason = lambda state, public: (
        "remote_hangup" if state == "remote_hangup" else public
    )
    phone_endpoint = types.ModuleType(f"{PKG_NAME}.phone_endpoint")
    phone_endpoint.DEFAULT_ENDPOINT_ID = "default"
    session_cleanup = types.ModuleType(f"{PKG_NAME}.session_cleanup")
    session_cleanup.async_cleanup_sip_runtime = cleanup
    runtime_data = types.ModuleType(f"{PKG_NAME}.runtime_data")
    runtime_data.call_runtime_artifacts = lambda hass: hass.artifacts
    websocket_api = types.ModuleType(f"{PKG_NAME}.websocket_api")
    websocket_api._ha_softphone_store = lambda _hass, endpoint_id: stores.setdefault(
        endpoint_id, {}
    )
    websocket_api._set_ha_softphone_call_state = lambda _hass, state, **kwargs: (
        states.append((state, kwargs))
    )
    call_projection = types.ModuleType(f"{PKG_NAME}.call_projection")

    class CallProjectionEvent:
        @staticmethod
        def phone(session, endpoint_id: str, **details):
            return session, endpoint_id, details

    def publish_call_projection(_hass, session, event) -> bool:
        _source, endpoint_id, details = event
        websocket_api._set_ha_softphone_call_state(
            _hass,
            session.state,
            endpoint_id=endpoint_id,
            caller=session.caller,
            callee=session.callee,
            call_id=session.call_id,
            **details,
        )
        return True

    call_projection.CallProjectionEvent = CallProjectionEvent
    call_projection.publish_call_projection = publish_call_projection

    module_name = f"{PKG_NAME}._test_outbound_lifecycle_runtime"
    spec = importlib.util.spec_from_file_location(
        module_name,
        PKG_DIR / "outbound_lifecycle.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load outbound_lifecycle.py")
    module = importlib.util.module_from_spec(spec)
    dependencies = {
        "homeassistant": homeassistant,
        "homeassistant.core": core,
        "homeassistant.exceptions": exceptions,
        const.__name__: const,
        call_projection.__name__: call_projection,
        endpoint_lifecycle.__name__: endpoint_lifecycle,
        endpoint_session.__name__: endpoint_session,
        endpoint_termination.__name__: endpoint_termination,
        fsm.__name__: fsm,
        phone_endpoint.__name__: phone_endpoint,
        session_cleanup.__name__: session_cleanup,
        runtime_data.__name__: runtime_data,
        websocket_api.__name__: websocket_api,
    }
    with patch.dict(sys.modules, dependencies):
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    return module


class OutboundLifecycleRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.registry = _Registry()
        self.states: list[tuple] = []
        self.stores: dict[str, dict] = {}
        self.cleanup = AsyncMock()
        self.module = _load_outbound_lifecycle(
            self.registry,
            self.states,
            self.stores,
            self.cleanup,
        )
        self.hass = _Hass()

    async def test_terminal_invite_result_closes_and_finishes_immediately(self) -> None:
        client = _Client("call-busy", final="busy")
        self.registry.sip_clients["call-busy"] = client

        await self.module.async_track_outbound_sip_client(
            self.hass,
            client=client,
            result="busy",
            target="Kitchen",
            endpoint_id="phone",
        )

        self.assertEqual(client.closed, 1)
        self.assertNotIn("call-busy", self.registry.sip_clients)
        finish = [item for item in self.registry.calls if item[0] == "finish"]
        self.assertEqual(finish[0][2]["intent"].reason, "busy")

    async def test_ringing_call_reaches_media_then_remote_bye_cleanup(self) -> None:
        client = _Client("call-1")

        await self.module.async_track_outbound_sip_client(
            self.hass,
            client=client,
            result="ringing",
            target="Kitchen",
            endpoint_id="default",
            sip_uri="sip:kitchen@192.0.2.10",
        )
        await self.registry.watchers["call-1"]

        self.assertEqual(client.closed, 1)
        self.assertNotIn("call-1", self.registry.sip_clients)
        self.assertEqual([state for state, _data in self.states], ["in_call", "idle"])
        in_call = self.states[0][1]
        self.assertEqual(in_call["selected_tx_format"], "L16/16000/1;ptime=20")
        self.assertEqual(in_call["video_status"], "inactive")
        ended = self.states[1][1]
        self.assertEqual(ended["terminal_reason"], "remote_hangup")
        self.assertEqual(ended["origin"], "remote")
        self.assertTrue(
            any(
                item[0] == "finish" and item[2]["intent"].reason == "remote_hangup"
                for item in self.registry.calls
            )
        )

    async def test_detached_final_watcher_cannot_resurrect_replaced_call(self) -> None:
        release = asyncio.Event()

        class DelayedClient(_Client):
            async def wait_for_final(self) -> str:
                await release.wait()
                return "in_call"

        client = DelayedClient("call-1")
        await self.module.async_track_outbound_sip_client(
            self.hass,
            client=client,
            result="ringing",
            target="Kitchen",
            endpoint_id="phone",
        )
        watcher = self.registry.watchers["call-1"]
        self.registry.sip_clients["call-1"] = object()
        release.set()
        await watcher

        self.assertEqual(self.states, [])
        self.assertEqual(client.closed, 0)

    async def test_prepare_rejects_active_state_without_touching_clients(self) -> None:
        self.stores["default"] = {"state": "in_call"}
        client = _Client("call-1")
        self.registry.sip_clients["call-1"] = client

        with self.assertRaisesRegex(ServiceValidationError, "already has"):
            await self.module.async_prepare_ha_outbound_call(self.hass, "default")

        self.assertIs(self.registry.sip_clients["call-1"], client)
        self.cleanup.assert_not_awaited()

    async def test_prepare_cleans_only_stale_clients_for_selected_phone(self) -> None:
        self.stores["default"] = {"state": "idle"}
        selected = _Client("call-selected")
        other = _Client("call-other")
        self.registry.sip_clients.update(
            {
                "call-selected": selected,
                "call-other": other,
            }
        )
        self.registry.sessions.update(
            {
                "call-selected": types.SimpleNamespace(
                    metadata={"endpoint_id": "default"}
                ),
                "call-other": types.SimpleNamespace(metadata={"endpoint_id": "office"}),
            }
        )

        await self.module.async_prepare_ha_outbound_call(
            self.hass,
            endpoint_id="default",
        )

        self.cleanup.assert_not_awaited()
        self.assertEqual(selected.closed, 1)
        self.assertNotIn("call-selected", self.registry.sip_clients)
        self.assertIs(self.registry.sip_clients["call-other"], other)


if __name__ == "__main__":
    unittest.main()
