from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _load_module(name: str):
    if "custom_components" not in sys.modules:
        root_pkg = types.ModuleType("custom_components")
        root_pkg.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root_pkg
    if PKG_NAME not in sys.modules:
        package = types.ModuleType(PKG_NAME)
        package.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = package
    full_name = f"{PKG_NAME}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


_load_module("session_cleanup")
endpoint_session = _load_module("endpoint_session")
pbx_runtime = _load_module("pbx_runtime")
call_registry = _load_module("call_registry")
SessionPhase = endpoint_session.SessionPhase
CallProjectionSnapshot = pbx_runtime.CallProjectionSnapshot
RuntimePhase = pbx_runtime.RuntimePhase
SipEndpointRuntime = pbx_runtime.SipEndpointRuntime


class _Projection:
    def __init__(self) -> None:
        self.published: list[CallProjectionSnapshot] = []
        self.removed: list[CallProjectionSnapshot] = []

    def publish(self, snapshot: CallProjectionSnapshot) -> None:
        self.published.append(snapshot)

    def remove(self, snapshot: CallProjectionSnapshot) -> None:
        self.removed.append(snapshot)


class _BrokenProjection:
    def publish(self, _snapshot: CallProjectionSnapshot) -> None:
        raise RuntimeError("publish failed")

    def remove(self, _snapshot: CallProjectionSnapshot) -> None:
        raise RuntimeError("remove failed")


class SipEndpointRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_dark_runtime_has_no_io_and_rejects_calls(self) -> None:
        runtime = SipEndpointRuntime()

        self.assertIs(runtime.phase, RuntimePhase.DARK)
        self.assertIsNone(runtime.component("udp_listener"))
        with self.assertRaisesRegex(RuntimeError, "not active"):
            runtime.create_session("call-1")

    async def test_live_bridge_requires_lifecycle_owner_before_registration(self) -> None:
        registry = call_registry.CallRegistry()
        runtime = SipEndpointRuntime(projection=registry)
        runtime.activate()
        registry.bind_session_owner(runtime)
        registry.upsert("source", state="ringing", owner="router")

        with self.assertRaisesRegex(ValueError, "requires a lifecycle task"):
            registry.register_bridge(
                source_call_id="source",
                dest_call_id="destination",
                client=object(),
                state="connecting",
            )

        self.assertEqual(registry.bridge_clients, {})
        self.assertEqual(registry.sip_clients, {})
        self.assertNotIn("destination", registry.leg_index)
        registry.finish_and_pop("source", reason="cancelled", state="cancelled")
        await runtime.shutdown()

    async def test_runtime_owns_generations_and_observable_projection(self) -> None:
        projection = _Projection()
        runtime = SipEndpointRuntime(projection=projection)
        runtime.activate()

        first = runtime.create_session("call-1", origin="trunk")
        first.transition(SessionPhase.ROUTING)
        first.update_metadata(destination="100")
        token = first.token

        self.assertIs(
            runtime.get_session("call-1", generation=token.generation), first
        )
        self.assertEqual(
            [item.phase for item in projection.published],
            [SessionPhase.NEW, SessionPhase.ROUTING, SessionPhase.ROUTING],
        )
        self.assertEqual(
            projection.published[-1].metadata,
            {"origin": "trunk", "destination": "100"},
        )

        await first.terminate("cancelled")
        self.assertIsNone(runtime.get_session("call-1"))
        self.assertEqual(projection.removed[-1].generation, token.generation)

        second = runtime.create_session("call-1")
        self.assertGreater(second.generation, token.generation)
        self.assertFalse(second.owns(token))

    async def test_duplicate_live_call_id_is_rejected(self) -> None:
        runtime = SipEndpointRuntime()
        runtime.activate()
        runtime.create_session("same")

        with self.assertRaisesRegex(ValueError, "already active"):
            runtime.create_session("same")

    async def test_stale_termination_cannot_remove_new_generation(self) -> None:
        runtime = SipEndpointRuntime()
        runtime.activate()
        first = runtime.create_session("same")
        await first.terminate("done")
        second = runtime.create_session("same")

        runtime._on_terminated(first, await first.terminate("late"))

        self.assertIs(runtime.get_session("same"), second)

    async def test_shutdown_stops_calls_before_endpoint_components(self) -> None:
        events: list[str] = []
        runtime = SipEndpointRuntime()

        async def close(name: str) -> None:
            events.append(name)

        runtime.attach_component(
            "udp_listener", object(), closer=lambda: close("udp")
        )
        runtime.attach_component(
            "trunk", object(), closer=lambda: close("trunk")
        )
        runtime.attach_component(
            "extra", object(), closer=lambda: close("extra")
        )
        runtime.activate()
        session = runtime.create_session("call-1")
        session.add_resource(
            "relay", object(), lambda _reason: close("call")
        )

        await runtime.shutdown()

        self.assertEqual(events, ["call", "trunk", "udp", "extra"])
        self.assertIs(runtime.phase, RuntimePhase.STOPPED)
        self.assertEqual(runtime.calls, {})

    async def test_shutdown_continues_after_component_close_failure(self) -> None:
        events: list[str] = []
        runtime = SipEndpointRuntime()

        async def broken() -> None:
            events.append("trunk")
            raise OSError("close failed")

        async def close_udp() -> None:
            events.append("udp")

        runtime.attach_component("trunk", object(), closer=broken)
        runtime.attach_component("udp_listener", object(), closer=close_udp)
        runtime.activate()

        await runtime.shutdown()

        self.assertEqual(events, ["trunk", "udp"])
        self.assertIs(runtime.phase, RuntimePhase.STOPPED)

    async def test_projection_failure_cannot_break_call_lifecycle(self) -> None:
        runtime = SipEndpointRuntime(projection=_BrokenProjection())
        runtime.activate()

        session = runtime.create_session("call-1")
        session.transition(SessionPhase.RINGING)
        result = await session.terminate("remote_hangup")

        self.assertEqual(result.reason, "remote_hangup")
        self.assertIs(session.phase, SessionPhase.TERMINATED)
        self.assertIsNone(runtime.get_session("call-1"))

    async def test_shutdown_survives_repeated_waiter_cancellation(self) -> None:
        release = asyncio.Event()
        runtime = SipEndpointRuntime()
        runtime.attach_component("udp_listener", object(), closer=release.wait)
        runtime.activate()

        first = asyncio.create_task(runtime.shutdown())
        second = asyncio.create_task(runtime.shutdown())
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        self.assertFalse(first.done())
        self.assertIs(runtime.phase, RuntimePhase.STOPPING)

        release.set()
        await second
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertIs(runtime.phase, RuntimePhase.STOPPED)

    async def test_registry_is_projection_of_runtime_owned_generation(self) -> None:
        registry = call_registry.CallRegistry()
        runtime = SipEndpointRuntime(projection=registry)
        runtime.activate()
        registry.bind_session_owner(runtime)

        projected = registry.upsert(
            "call-1",
            state="ringing",
            owner="router",
            caller="door",
            callee="home",
        )
        registry.add_leg(
            "call-1",
            "door-leg",
            role="esp",
            state="ringing",
            endpoint_id="door-endpoint",
        )
        authoritative = runtime.get_session("call-1")

        self.assertIsNotNone(authoritative)
        self.assertEqual(projected.generation, authoritative.generation)
        self.assertIs(authoritative.phase, SessionPhase.RINGING)
        self.assertEqual(projected.metadata["pbx_phase"], "ringing")
        self.assertEqual(authoritative.metadata["owner"], "router")
        self.assertEqual(
            authoritative.legs["door-leg"].kind,
            endpoint_session.LegKind.ESPHOME,
        )

        registry.finish_and_pop("call-1", reason="cancelled", state="cancelled")
        await authoritative.terminated.wait()

        self.assertNotIn("call-1", registry.sessions)
        self.assertIsNone(runtime.get_session("call-1"))

    async def test_waited_removal_allows_same_call_id_to_change_owner(self) -> None:
        registry = call_registry.CallRegistry()
        runtime = SipEndpointRuntime(projection=registry)
        runtime.activate()
        registry.bind_session_owner(runtime)

        first = registry.upsert("call-1", state="ringing", owner="router")
        await registry.finish_and_pop_wait(
            "call-1",
            reason="local_group_selected",
        )
        second = registry.upsert(
            "call-1",
            state="connecting",
            owner="local_bridge",
        )

        self.assertGreater(second.generation, first.generation)
        self.assertFalse(registry.is_terminated("call-1"))
        self.assertTrue(
            registry.is_terminated("call-1", generation=first.generation)
        )
        self.assertTrue(registry.is_generation_current("call-1", second.generation))
        await registry.finish_and_pop_wait("call-1", reason="test_complete")

    async def test_projected_phase_cannot_override_authoritative_phase(self) -> None:
        registry = call_registry.CallRegistry()
        runtime = SipEndpointRuntime(projection=registry)
        runtime.activate()
        registry.bind_session_owner(runtime)

        registry.upsert("call-1", state="new", owner="router")
        registry.sessions["call-1"].metadata["pbx_phase"] = "new"
        projected = registry.upsert("call-1", state="ringing", owner="router")
        authoritative = runtime.get_session("call-1")

        self.assertIsNotNone(authoritative)
        self.assertIs(authoritative.phase, SessionPhase.RINGING)
        self.assertNotIn("pbx_phase", authoritative.metadata)
        self.assertEqual(projected.metadata["pbx_phase"], "ringing")

    async def test_late_public_state_cannot_regress_established_call(self) -> None:
        runtime = SipEndpointRuntime()
        runtime.activate()
        session = runtime.create_session("call-1")

        self.assertTrue(runtime.observe_call("call-1", state="in_call"))
        self.assertTrue(runtime.observe_call("call-1", state="ringing"))

        self.assertIs(session.phase, SessionPhase.ESTABLISHED)

    async def test_connecting_call_can_return_to_ringing_after_failed_forward(
        self,
    ) -> None:
        runtime = SipEndpointRuntime()
        runtime.activate()
        session = runtime.create_session("call-1")

        self.assertTrue(runtime.observe_call("call-1", state="ringing"))
        self.assertTrue(runtime.observe_call("call-1", state="connecting"))
        self.assertTrue(runtime.observe_call("call-1", state="ringing"))

        self.assertIs(session.phase, SessionPhase.RINGING)

    async def test_runtime_component_can_be_adopted_after_activation(self) -> None:
        runtime = SipEndpointRuntime()
        runtime.activate()
        trunk = object()

        runtime.adopt_component("trunk", trunk)
        runtime.adopt_component("trunk", trunk)

        self.assertIs(runtime.component("trunk"), trunk)
        self.assertTrue(runtime.release_component("trunk", trunk))
        self.assertIsNone(runtime.component("trunk"))

    async def test_registry_indexes_are_cleaned_by_session_owned_resources(self) -> None:
        events: list[str] = []

        class Client:
            async def terminate(self) -> None:
                events.append("client-terminate")

            async def close(self) -> None:
                events.append("client-close")

        class Relay:
            async def stop(self) -> None:
                events.append("relay-stop")

        registry = call_registry.CallRegistry()
        runtime = SipEndpointRuntime(projection=registry)
        runtime.activate()
        registry.bind_session_owner(runtime)
        client = Client()
        relay = Relay()
        registry.upsert("source", state="ringing", owner="router")
        watcher = asyncio.create_task(asyncio.Event().wait())
        registry.register_bridge(
            source_call_id="source",
            dest_call_id="destination",
            client=client,
            lifecycle_task=watcher,
            state="connecting",
        )
        registry.attach_relay("source", relay)
        authoritative = runtime.get_session("source")

        registry.finish_and_pop("source", reason="cancelled", state="cancelled")
        await authoritative.terminated.wait()
        await asyncio.sleep(0)

        self.assertTrue(watcher.cancelled())
        self.assertEqual(events, ["relay-stop", "client-terminate", "client-close"])
        self.assertEqual(registry.relays, {})
        self.assertEqual(registry.sip_clients, {})
        self.assertEqual(registry.client_watchers, {})
        self.assertEqual(registry.bridge_clients, {})

    async def test_watcher_that_ends_call_is_not_cancelled_by_own_cleanup(self) -> None:
        registry = call_registry.CallRegistry()
        runtime = SipEndpointRuntime(projection=registry)
        runtime.activate()
        registry.bind_session_owner(runtime)
        registry.upsert("call-1", state="ringing", owner="router")
        completed = asyncio.Event()

        async def watcher_body() -> None:
            registry.finish_and_pop(
                "call-1",
                reason="remote_hangup",
                state="idle",
            )
            await asyncio.sleep(0)
            completed.set()

        watcher = asyncio.create_task(watcher_body())
        registry.attach_client_watcher("call-1", watcher)
        await watcher

        self.assertTrue(completed.is_set())
        self.assertFalse(watcher.cancelled())
