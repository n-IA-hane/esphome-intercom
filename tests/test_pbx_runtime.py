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
SessionPhase = endpoint_session.SessionPhase
RuntimePhase = pbx_runtime.RuntimePhase
SipEndpointRuntime = pbx_runtime.SipEndpointRuntime


def _registry_runtime():
    runtime = SipEndpointRuntime()
    return runtime, runtime


class SipEndpointRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_dark_runtime_has_no_io_and_rejects_calls(self) -> None:
        runtime = SipEndpointRuntime()

        self.assertIs(runtime.phase, RuntimePhase.DARK)
        self.assertIsNone(runtime.component("udp_listener"))
        with self.assertRaisesRegex(RuntimeError, "not active"):
            runtime.create_session("call-1")

    async def test_live_bridge_requires_lifecycle_owner_before_registration(
        self,
    ) -> None:
        registry, runtime = _registry_runtime()
        runtime.activate()
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
        registry.terminate_call("source", reason="cancelled")
        await runtime.shutdown()

    async def test_runtime_owns_generations_and_retires_derived_indexes(self) -> None:
        runtime = SipEndpointRuntime()
        runtime.activate()

        first = runtime.create_session("call-1", origin="trunk")
        first.transition(SessionPhase.ROUTING)
        first.update_metadata(destination="100")
        runtime.observe_leg("call-1", "leg-1", role="sip")
        runtime.event_contexts["leg-1"] = {"source": "test"}
        token = first.token

        self.assertIs(runtime.get_session("call-1", generation=token.generation), first)
        await first.terminate(endpoint_session.TerminationIntent("cancelled"))
        self.assertIsNone(runtime.get_session("call-1"))
        self.assertNotIn("leg-1", runtime.leg_index)
        self.assertNotIn("leg-1", runtime.event_contexts)
        self.assertTrue(runtime.is_terminated("call-1", generation=token.generation))

        second = runtime.create_session("call-1")
        self.assertGreater(second.generation, token.generation)
        self.assertFalse(second.owns(token))

    async def test_duplicate_live_call_id_is_rejected(self) -> None:
        runtime = SipEndpointRuntime()
        runtime.activate()
        runtime.create_session("same")

        with self.assertRaisesRegex(ValueError, "already active"):
            runtime.create_session("same")

    async def test_named_call_task_replacement_has_one_authoritative_owner(
        self,
    ) -> None:
        runtime = SipEndpointRuntime()
        runtime.activate()
        runtime.create_session("call-1")
        first = asyncio.create_task(asyncio.sleep(10))
        second = asyncio.create_task(asyncio.sleep(10))

        self.assertTrue(runtime.replace_task("call-1", first, name="deadline"))
        self.assertTrue(runtime.replace_task("call-1", second, name="deadline"))
        await asyncio.sleep(0)

        self.assertTrue(first.cancelled())
        self.assertIs(runtime.task_for("call-1", "deadline"), second)
        self.assertEqual(runtime.named_task_count("deadline"), 1)

        await runtime.shutdown()
        self.assertTrue(second.cancelled())

    async def test_stale_termination_cannot_remove_new_generation(self) -> None:
        runtime = SipEndpointRuntime()
        runtime.activate()
        first = runtime.create_session("same")
        await first.terminate(endpoint_session.TerminationIntent("done"))
        second = runtime.create_session("same")

        runtime._on_terminated(
            first, await first.terminate(endpoint_session.TerminationIntent("late"))
        )

        self.assertIs(runtime.get_session("same"), second)

    async def test_shutdown_stops_calls_before_endpoint_components(self) -> None:
        events: list[str] = []
        runtime = SipEndpointRuntime()

        async def close(name: str) -> None:
            events.append(name)

        runtime.attach_component("udp_listener", object(), closer=lambda: close("udp"))
        runtime.attach_component("trunk", object(), closer=lambda: close("trunk"))
        runtime.attach_component("extra", object(), closer=lambda: close("extra"))
        runtime.activate()
        session = runtime.create_session("call-1")
        session.add_resource("relay", object(), lambda _reason: close("call"))

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
        registry, runtime = _registry_runtime()
        runtime.activate()

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
        self.assertIs(projected, authoritative)
        self.assertNotIn("pbx_phase", authoritative.metadata)
        self.assertEqual(
            authoritative.legs["door-leg"].kind,
            endpoint_session.LegKind.ESPHOME,
        )

        registry.terminate_call("call-1", reason="cancelled")
        await authoritative.terminated.wait()

        self.assertNotIn("call-1", registry.sessions)
        self.assertIsNone(runtime.get_session("call-1"))

    async def test_registry_delegates_terminal_claim_to_session_owner(self) -> None:
        events: list[str] = []
        registry, runtime = _registry_runtime()
        runtime.activate()
        projected = registry.upsert("call-1", state="in_call", owner="bridge")
        authoritative = runtime.get_session("call-1")
        self.assertIsNotNone(authoritative)
        authoritative.add_resource(
            "relay",
            object(),
            lambda reason: events.append(f"relay:{reason}"),
            stage=endpoint_session.CleanupStage.MEDIA,
        )

        self.assertTrue(
            registry.begin_termination(
                "call-1",
                pbx_runtime.TerminationIntent("remote_hangup"),
            )
        )
        self.assertFalse(
            registry.begin_termination(
                "call-1",
                pbx_runtime.TerminationIntent("duplicate"),
            )
        )
        self.assertIs(authoritative.phase, SessionPhase.TERMINATING)
        self.assertIs(projected, authoritative)
        self.assertEqual(events, [])

        registry.terminate_call("call-1", reason="remote_hangup")
        await authoritative.terminated.wait()

        self.assertEqual(events, ["relay:remote_hangup"])
        self.assertIsNone(runtime.get_session("call-1"))

    async def test_owner_completion_removes_the_call_projection(self) -> None:
        registry, runtime = _registry_runtime()
        runtime.activate()
        registry.upsert("call-1", state="in_call", owner="bridge")

        session = runtime.get_session("call-1")
        self.assertIsNotNone(session)
        result = await session.terminate(
            endpoint_session.TerminationIntent("remote_hangup")
        )

        self.assertIsNotNone(result)
        self.assertNotIn("call-1", registry.sessions)
        self.assertEqual(registry.active_count(), 0)
        self.assertTrue(registry.is_terminated("call-1"))

    async def test_waited_removal_allows_same_call_id_to_change_owner(self) -> None:
        registry, runtime = _registry_runtime()
        runtime.activate()

        first = registry.upsert("call-1", state="ringing", owner="router")
        await registry.terminate_call_wait(
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
        self.assertTrue(registry.is_terminated("call-1", generation=first.generation))
        self.assertTrue(registry.is_generation_current("call-1", second.generation))
        await registry.terminate_call_wait("call-1", reason="test_complete")

    async def test_projected_phase_cannot_override_authoritative_phase(self) -> None:
        registry, runtime = _registry_runtime()
        runtime.activate()

        registry.upsert("call-1", state="new", owner="router")
        projected = registry.upsert("call-1", state="ringing", owner="router")
        authoritative = runtime.get_session("call-1")

        self.assertIsNotNone(authoritative)
        self.assertIs(projected, authoritative)
        self.assertIs(authoritative.phase, SessionPhase.RINGING)
        self.assertNotIn("pbx_phase", authoritative.metadata)

    async def test_late_public_state_cannot_regress_established_call(self) -> None:
        registry, runtime = _registry_runtime()
        runtime.activate()
        session = registry.upsert("call-1", state="in_call")
        registry.transition("call-1", state="ringing")

        self.assertIs(session.phase, SessionPhase.ESTABLISHED)
        self.assertEqual(session.state, "in_call")

    async def test_connecting_call_can_return_to_ringing_after_failed_forward(
        self,
    ) -> None:
        registry, runtime = _registry_runtime()
        runtime.activate()
        session = registry.upsert("call-1", state="ringing")
        registry.transition("call-1", state="connecting")
        registry.transition("call-1", state="ringing")

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

    async def test_registry_indexes_are_cleaned_by_session_owned_resources(
        self,
    ) -> None:
        events: list[str] = []

        class Client:
            async def terminate(self) -> None:
                events.append("client-terminate")

            async def close(self) -> None:
                events.append("client-close")

        class Relay:
            async def stop(self) -> None:
                events.append("relay-stop")

        registry, runtime = _registry_runtime()
        runtime.activate()
        client = Client()
        relay = Relay()
        registry.upsert("source", state="ringing", owner="router")
        invite = object()
        registry.set_pending_invite("source", invite)
        route_future = asyncio.get_running_loop().create_future()
        route = {"future": route_future, "destination": "100"}
        registry.set_pending_route("source", route)
        parameter_sets = (b"sps", b"pps")
        registry.cache_video_parameter_sets("source", parameter_sets)
        watcher = asyncio.create_task(asyncio.Event().wait())
        registry.register_bridge(
            source_call_id="source",
            dest_call_id="destination",
            client=client,
            lifecycle_task=watcher,
            state="connecting",
        )
        registry.attach_relay("source", relay)
        media = {"call_id": "source"}
        registry.attach_media("source", media)
        authoritative = runtime.get_session("source")

        self.assertIs(registry.sip_clients["destination"], client)
        self.assertEqual(registry.bridge_clients, {"source": "destination"})
        self.assertIs(registry.pending_invites["source"], invite)
        self.assertIs(authoritative.artifacts.pending_invite, invite)
        self.assertIs(registry.pending_routes["source"], route)
        self.assertIs(authoritative.artifacts.pending_route, route)
        self.assertEqual(registry.video_parameter_sets["source"], parameter_sets)
        self.assertEqual(authoritative.artifacts.video_parameter_sets, parameter_sets)
        self.assertEqual(
            authoritative.metadata["bridge_dest_call_id"],
            "destination",
        )
        self.assertIs(registry.client_watchers["destination"], watcher)
        self.assertIs(
            authoritative.named_tasks["client_watcher:destination"],
            watcher,
        )
        self.assertIs(registry.relays["source"], relay)
        self.assertIs(registry.softphone_media["source"], media)
        self.assertEqual(
            [resource.name for resource in authoritative.resources],
            ["relay:source", "softphone_media:source"],
        )

        registry.terminate_call("source", reason="cancelled")
        await authoritative.terminated.wait()
        await asyncio.sleep(0)

        self.assertTrue(watcher.cancelled())
        self.assertEqual(events, ["relay-stop", "client-terminate", "client-close"])
        self.assertEqual(registry.relays, {})
        self.assertEqual(registry.softphone_media, {})
        self.assertEqual(registry.sip_clients, {})
        self.assertEqual(registry.client_watchers, {})
        self.assertEqual(registry.bridge_clients, {})
        self.assertEqual(registry.pending_invites, {})
        self.assertEqual(registry.pending_routes, {})
        self.assertTrue(route_future.cancelled())
        self.assertEqual(registry.video_parameter_sets, {})

    async def test_authoritative_session_owns_endpoint_claims_and_cleanup(self) -> None:
        class Endpoints:
            def __init__(self) -> None:
                self.active: dict[str, str] = {}

            def get(self, endpoint_id: str):
                return endpoint_id if endpoint_id in {"caller", "callee"} else None

            def claim_call(self, endpoint_id: str, call_id: str) -> None:
                if endpoint_id in self.active:
                    raise ValueError("busy")
                self.active[endpoint_id] = call_id

            def release_call(self, endpoint_id: str, call_id: str) -> bool:
                if self.active.get(endpoint_id) != call_id:
                    return False
                self.active.pop(endpoint_id)
                return True

        endpoints = Endpoints()
        registry, runtime = _registry_runtime()
        runtime.activate()
        registry.bind_endpoint_registry(endpoints)
        registry.upsert("call-1", state="connecting", owner="router")

        registry.claim_endpoint("call-1", "caller", role="source")
        registry.claim_endpoint("call-1", "callee", role="destination")

        authoritative = runtime.get_session("call-1")
        self.assertEqual(
            authoritative.endpoint_claims,
            {"caller": "source", "callee": "destination"},
        )
        self.assertEqual(
            registry.endpoint_claims, {"call-1": authoritative.endpoint_claims}
        )

        registry.terminate_call("call-1", reason="remote_hangup")
        await authoritative.terminated.wait()

        self.assertEqual(endpoints.active, {})
        self.assertEqual(registry.endpoint_claims, {})

    async def test_watcher_that_ends_call_is_not_cancelled_by_own_cleanup(self) -> None:
        registry, runtime = _registry_runtime()
        runtime.activate()
        registry.upsert("call-1", state="ringing", owner="router")
        completed = asyncio.Event()

        async def watcher_body() -> None:
            registry.terminate_call(
                "call-1",
                reason="remote_hangup",
            )
            await asyncio.sleep(0)
            completed.set()

        watcher = asyncio.create_task(watcher_body())
        registry.attach_client_watcher("call-1", watcher)
        await watcher

        self.assertTrue(completed.is_set())
        self.assertFalse(watcher.cancelled())
