#!/usr/bin/env python3
"""Authoritative call registry and automation event context tests."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _load_module(name: str):
    if "custom_components" not in sys.modules:
        root_pkg = types.ModuleType("custom_components")
        root_pkg.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root_pkg
    if PKG_NAME not in sys.modules:
        pkg = types.ModuleType(PKG_NAME)
        pkg.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = pkg
    full_name = f"{PKG_NAME}.{name}"
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


call_registry = _load_module("call_registry")
automation_routing = _load_module("automation_routing")
pbx_runtime = _load_module("pbx_runtime")


def _registry():
    owner = pbx_runtime.SipEndpointRuntime(allow_dark_sessions=True)
    registry = call_registry.CallRegistry(owner)
    owner.bind_projection(registry)
    return registry


class _EndpointRegistryStub:
    def __init__(self) -> None:
        self.active: dict[str, str] = {}
        self.releases: list[tuple[str, str]] = []

    def claim_call(self, endpoint_id: str, call_id: str) -> None:
        active_call_id = self.active.get(endpoint_id, "")
        if active_call_id and active_call_id != call_id:
            raise ValueError(f"{endpoint_id} is busy")
        self.active[endpoint_id] = call_id

    def release_call(self, endpoint_id: str, call_id: str) -> bool:
        if self.active.get(endpoint_id) != call_id:
            return False
        self.active.pop(endpoint_id)
        self.releases.append((endpoint_id, call_id))
        return True

    def adopt_transport_call(self, endpoint_id: str, call_id: str) -> None:
        active_call_id = self.active.get(endpoint_id, "")
        if active_call_id and not active_call_id.startswith("physical:"):
            raise ValueError(f"{endpoint_id} is busy")
        self.active[endpoint_id] = call_id


class CallRegistryEventContextTest(unittest.TestCase):
    def test_projection_snapshots_cannot_mutate_the_authoritative_session(self) -> None:
        registry = _registry()
        projected = registry.upsert("call-1", state="ringing", owner="router")
        initial_revision = projected.revision

        snapshot = types.SimpleNamespace(
            call_id="call-1",
            generation=projected.generation,
            phase=types.SimpleNamespace(value="established"),
            terminal_reason="completed",
            metadata={"pbx_phase": "stale", "route_kind": "direct"},
        )
        registry.publish(snapshot)

        self.assertNotIn("pbx_phase", projected.metadata)
        self.assertEqual(projected.route_kind, "")
        self.assertEqual(projected.terminal_reason, "")
        self.assertEqual(projected.revision, initial_revision)

        registry.publish(snapshot)
        self.assertEqual(projected.revision, initial_revision)

        registry.publish(
            types.SimpleNamespace(
                call_id="call-1",
                generation=projected.generation + 1,
                phase=types.SimpleNamespace(value="held"),
                terminal_reason="wrong-generation",
                metadata={"route_kind": "wrong-generation"},
            )
        )
        registry.publish(
            types.SimpleNamespace(
                call_id="",
                generation=1,
                phase=types.SimpleNamespace(value="held"),
                terminal_reason="blank-call",
                metadata={},
            )
        )

        self.assertNotIn("pbx_phase", projected.metadata)
        self.assertEqual(projected.route_kind, "")
        self.assertEqual(projected.terminal_reason, "")
        self.assertEqual(set(registry.sessions), {"call-1"})

        fresh = _registry()
        fresh.publish(
            types.SimpleNamespace(
                call_id="new-call",
                generation=7,
                phase="ringing",
                terminal_reason="",
                metadata={"source": "trunk"},
            )
        )
        self.assertNotIn("new-call", fresh.sessions)

    def test_active_count_filters_terminal_and_ha_softphone_sessions(self) -> None:
        registry = _registry()

        registry.upsert("terminal-first", state="idle", owner="bridge")
        registry.upsert("active-physical", state="in_call", owner="bridge")
        registry.upsert("active-physical-2", state="calling", owner="router")
        registry.upsert("active-browser", state="ringing", owner="ha_softphone")
        registry.add_leg(
            "active-browser",
            "browser-leg",
            role="ha_softphone",
            state="ringing",
        )
        registry.upsert("terminal-last", state="busy", owner="bridge")

        self.assertEqual(registry.active_count(), 3)
        self.assertEqual(registry.active_count(include_ha_softphone=False), 2)

    def test_event_v2_origin_is_transport_stable(self) -> None:
        self.assertEqual(
            automation_routing.canonical_call_origin("trunk", "ring_group"),
            "trunk",
        )
        self.assertEqual(
            automation_routing.canonical_call_origin("remote", "trunk"),
            "trunk",
        )
        self.assertEqual(
            automation_routing.canonical_call_origin("self", "direct"),
            "extension",
        )

    def test_snapshot_exposes_every_owned_runtime_resource(self) -> None:
        registry = _registry()
        registry.bind_endpoint_registry(_EndpointRegistryStub())
        registry.upsert("call-1", state="in_call", owner="bridge")
        registry.add_leg("call-1", "source", role="caller", state="in_call")
        registry.add_leg("call-1", "destination", role="callee", state="in_call")
        registry.set_pending_route("call-1", {})
        registry.set_pending_invite("call-1", object())
        registry.attach_media("call-1", {}, provisional=True)
        registry.attach_media("call-1", {})
        registry.attach_sip_client("call-1", "destination", object())
        watcher = mock.Mock()
        registry.attach_client_watcher("destination", watcher)
        registry.attach_relay("call-1", object())
        registry.set_bridge_link("call-1", "destination")
        registry.claim_endpoint("call-1", "office", role="source")
        registry.claim_endpoint("call-1", "kitchen", role="destination")

        self.assertEqual(
            registry.snapshot()["resource_counts"],
            {
                "sessions": 1,
                "legs": 2,
                "pending_routes": 1,
                "pending_invites": 1,
                "preanswered": 1,
                "softphone_media": 1,
                "sip_clients": 1,
                "client_watchers": 1,
                "relays": 1,
                "bridges": 1,
                "endpoint_claims": 2,
            },
        )

        with self.assertRaisesRegex(RuntimeError, "before PBX shutdown"):
            registry.clear_runtime()

    def test_only_one_terminal_observer_owns_teardown(self) -> None:
        registry = _registry()
        registry.upsert("source", state="in_call", owner="bridge")
        registry.add_leg("source", "destination", role="callee", state="in_call")

        self.assertTrue(registry.begin_termination("destination"))
        self.assertFalse(registry.begin_termination("source"))
        registry.finish_and_pop("source", reason="remote_hangup")
        self.assertFalse(registry.begin_termination("destination"))

    def test_async_generation_stops_owning_call_at_begin_termination(self) -> None:
        registry = _registry()
        session = registry.upsert("call-1", state="in_call", owner="bridge")

        self.assertTrue(registry.is_generation_current("call-1", session.generation))
        self.assertTrue(registry.begin_termination("call-1"))
        self.assertEqual(registry.sessions["call-1"].phase.value, "terminating")
        self.assertFalse(registry.is_generation_current("call-1", session.generation))

    def test_stale_generation_cannot_register_or_resurrect_bridge(self) -> None:
        registry = _registry()
        session = registry.upsert("source", state="ringing", owner="router")
        client = object()

        self.assertTrue(registry.begin_termination("source"))
        registry.finish_and_pop("source", reason="cancelled", state="cancelled")

        attached = registry.register_bridge(
            source_call_id="source",
            dest_call_id="late-destination",
            client=client,
            state="connecting",
            expected_generation=session.generation,
        )

        self.assertIsNone(attached)
        self.assertNotIn("source", registry.sessions)
        self.assertNotIn("source", registry.bridge_clients)
        self.assertNotIn("late-destination", registry.sip_clients)
        self.assertNotIn("late-destination", registry.leg_index)

    def test_wrong_generation_rejects_bridge_before_mutating_indexes(self) -> None:
        registry = _registry()
        session = registry.upsert("source", state="ringing", owner="router")

        attached = registry.register_bridge(
            source_call_id="source",
            dest_call_id="destination",
            client=object(),
            state="connecting",
            expected_generation=session.generation + 1,
        )

        self.assertIsNone(attached)
        self.assertEqual(registry.bridge_clients, {})
        self.assertEqual(registry.sip_clients, {})
        self.assertEqual(session.legs, {})

    def test_sequence_advances_only_for_canonical_state_changes(self) -> None:
        registry = _registry()

        first = registry.event_fields("call-1", "ringing")
        duplicate = registry.event_fields("call-1", "ringing")
        answered = registry.event_fields("call-1", "in_call")

        self.assertEqual(first["sequence"], 1)
        self.assertEqual(first["previous_state"], "")
        self.assertEqual(duplicate, first)
        self.assertEqual(answered["sequence"], 2)
        self.assertEqual(answered["previous_state"], "ringing")

    def test_terminal_event_reports_connected_call_duration(self) -> None:
        registry = _registry()

        with mock.patch(
            "custom_components.voip_stack.call_registry.time.monotonic",
            side_effect=(100.0, 145.4),
        ):
            registry.event_fields("call-1", "in_call")
            ended = registry.event_fields("call-1", "idle")

        self.assertEqual(ended["duration_seconds"], 45)

    def test_terminal_duration_is_frozen_and_resets_for_reused_physical_id(
        self,
    ) -> None:
        registry = _registry()

        with mock.patch(
            "custom_components.voip_stack.call_registry.time.monotonic",
            side_effect=(100.0, 145.4, 200.0, 207.2),
        ):
            registry.event_fields("physical:esp", "in_call")
            first = registry.event_fields("physical:esp", "idle")
            duplicate = registry.event_fields("physical:esp", "idle")
            registry.event_fields("physical:esp", "ringing")
            registry.event_fields("physical:esp", "in_call")
            second = registry.event_fields("physical:esp", "idle")

        self.assertEqual(first["duration_seconds"], 45)
        self.assertEqual(duplicate["duration_seconds"], 45)
        self.assertEqual(second["duration_seconds"], 7)

    def test_terminal_summary_claim_is_once_per_lifecycle(self) -> None:
        registry = _registry()

        registry.event_fields("call-1", "ringing")
        self.assertTrue(registry.claim_terminal_summary("call-1"))
        self.assertFalse(registry.claim_terminal_summary("call-1"))
        registry.event_fields("call-1", "idle")
        registry.event_fields("call-1", "ringing")

        self.assertTrue(registry.claim_terminal_summary("call-1"))

    def test_unanswered_terminal_event_has_no_call_duration(self) -> None:
        registry = _registry()

        registry.event_fields("call-1", "ringing")
        ended = registry.event_fields("call-1", "idle")

        self.assertNotIn("duration_seconds", ended)

    def test_event_schema_v2_exposes_generation_phase_and_call_origin(self) -> None:
        registry = _registry()
        session = registry.upsert(
            "call-1",
            state="connecting",
            owner="router",
            ingress="trunk",
            origin="trunk",
        )

        fields = registry.event_fields("call-1", "connecting")

        self.assertEqual(fields["schema_version"], 2)
        self.assertEqual(fields["generation"], session.generation)
        self.assertEqual(fields["pbx_phase"], "connecting")
        self.assertEqual(fields["ingress"], "trunk")
        self.assertEqual(fields["origin"], "trunk")

    def test_bridge_registration_preserves_transport_provenance(self) -> None:
        registry = _registry()

        registry.register_bridge(
            source_call_id="source",
            dest_call_id="destination",
            client=object(),
            lifecycle_task=mock.MagicMock(),
            state="connecting",
            ingress="trunk",
            origin="trunk",
        )

        fields = registry.event_fields("source", "connecting")
        self.assertEqual(fields["ingress"], "trunk")
        self.assertEqual(fields["origin"], "trunk")

    def test_route_history_is_bounded_and_returned_with_events(self) -> None:
        registry = _registry()
        registry.event_fields("call-1", "route_requested")
        for index in range(10):
            registry.record_route(
                "call-1",
                action="forward",
                destination=str(index),
            )

        event = registry.event_fields("call-1", "connecting")
        self.assertEqual(len(event["route_history"]), 8)
        self.assertEqual(event["route_history"][0]["destination"], "2")
        self.assertEqual(event["route_history"][-1]["destination"], "9")

    def test_leg_id_resolves_to_source_event_context(self) -> None:
        registry = _registry()
        registry.register_bridge(
            source_call_id="source",
            dest_call_id="destination",
            client=object(),
            state="ringing",
        )
        registry.event_fields("source", "ringing")

        self.assertIs(
            registry.event_context("destination"),
            registry.event_context("source"),
        )

    def test_event_fields_for_leg_alias_advances_canonical_context(self) -> None:
        registry = _registry()
        registry.register_bridge(
            source_call_id="source",
            dest_call_id="destination",
            client=object(),
            state="ringing",
        )
        registry.event_fields("source", "ringing")

        fields = registry.event_fields("destination", "in_call")

        self.assertEqual(fields["sequence"], 2)
        self.assertEqual(fields["previous_state"], "ringing")
        self.assertEqual(registry.event_context("source").state, "in_call")
        self.assertNotIn("destination", registry.event_contexts)

    def test_pop_by_leg_alias_removes_alias_event_context(self) -> None:
        registry = _registry()
        registry.event_fields("destination", "queued")
        registry.register_bridge(
            source_call_id="source",
            dest_call_id="destination",
            client=object(),
            state="ringing",
        )
        registry.event_fields("source", "ringing")
        self.assertIn("destination", registry.event_contexts)

        popped = registry.pop("destination")

        self.assertIsNotNone(popped)
        self.assertEqual(registry.event_contexts, {})

    def test_revision_advances_for_owner_and_destination_without_state_change(
        self,
    ) -> None:
        registry = _registry()
        session = registry.upsert(
            "call-1", state="connecting", callee="Home Assistant", owner="ha_softphone"
        )
        initial = session.revision

        redirected = registry.transition(
            "call-1",
            state="connecting",
            owner="router",
            callee="Assist",
            expected_revision=initial,
            expected_owner="ha_softphone",
        )

        self.assertIsNotNone(redirected)
        self.assertEqual(redirected.revision, initial + 1)
        self.assertEqual(redirected.owner, "router")
        self.assertEqual(redirected.callee, "Assist")
        fields = registry.event_fields("call-1", "connecting")
        self.assertEqual(fields["revision"], redirected.revision)
        self.assertEqual(fields["owner"], "router")

    def test_event_fields_use_owned_endpoint_ids_not_display_names(self) -> None:
        registry = _registry()
        endpoints = _EndpointRegistryStub()
        registry.bind_endpoint_registry(endpoints)
        registry.upsert(
            "call-1",
            state="ringing",
            caller="Kitchen",
            source_endpoint_id="front-door",
            dest_endpoint_id="kitchen",
        )
        registry.claim_endpoint("call-1", "hall", role="destination")

        fields = registry.event_fields("call-1", "ringing")

        self.assertEqual(fields["source_endpoint_id"], "front-door")
        self.assertEqual(fields["dest_endpoint_id"], "kitchen")
        self.assertEqual(
            fields["participant_endpoint_ids"],
            ["front-door", "hall", "kitchen"],
        )
        self.assertNotIn("caller", fields)

    def test_stale_revision_or_owner_cannot_mutate_session(self) -> None:
        registry = _registry()
        session = registry.upsert("call-1", state="ringing", owner="ha_softphone")

        self.assertIsNone(
            registry.transition(
                "call-1",
                owner="router",
                expected_revision=session.revision + 1,
                expected_owner="ha_softphone",
            )
        )
        self.assertIsNone(
            registry.transition(
                "call-1",
                owner="router",
                expected_revision=session.revision,
                expected_owner="bridge",
            )
        )
        self.assertEqual(session.owner, "ha_softphone")

    def test_queued_ringing_callback_cannot_resurrect_released_ha_owner(self) -> None:
        registry = _registry()
        session = registry.upsert("call-1", state="ringing", owner="ha_softphone")
        queued_revision = session.revision
        published: list[str] = []

        def queued_ringing_callback() -> None:
            if registry.is_current(
                "call-1", revision=queued_revision, owner="ha_softphone"
            ):
                published.append("ringing")

        registry.transition(
            "call-1",
            state="connecting",
            owner="router",
            expected_revision=queued_revision,
            expected_owner="ha_softphone",
        )
        queued_ringing_callback()

        self.assertEqual(published, [])
        self.assertEqual(registry.sessions["call-1"].owner, "router")

    def test_failed_route_resumes_ha_owner_exactly_once(self) -> None:
        registry = _registry()
        session = registry.upsert("call-1", state="connecting", owner="router")

        resumed = registry.transition(
            "call-1",
            state="ringing",
            owner="ha_softphone",
            expected_revision=session.revision,
            expected_owner="router",
        )
        duplicate = registry.transition(
            "call-1",
            state="ringing",
            owner="ha_softphone",
            expected_revision=session.revision - 1,
            expected_owner="router",
        )

        self.assertIsNotNone(resumed)
        self.assertIsNone(duplicate)
        self.assertEqual(session.owner, "ha_softphone")

    def test_leg_add_replace_remove_and_finish_advance_control_revision(self) -> None:
        registry = _registry()
        session = registry.upsert("call-1", state="connecting", owner="router")
        initial = session.revision
        registry.add_leg("call-1", "leg-1", role="callee", state="ringing")
        after_add = session.revision
        registry.add_leg("call-1", "leg-1", role="callee", state="in_call")
        after_replace = session.revision
        registry.remove_leg("call-1", "leg-1")
        after_remove = session.revision
        registry.finish("call-1", reason="remote_hangup")

        self.assertGreater(after_add, initial)
        self.assertGreater(after_replace, after_add)
        self.assertGreater(after_remove, after_replace)
        self.assertGreater(session.revision, after_remove)
        self.assertEqual(session.owner, "terminal")
        self.assertEqual(session.outcome, "remote_hangup")

    def test_leg_state_never_regresses_aggregate_session_state(self) -> None:
        registry = _registry()
        session = registry.upsert("call-1", state="in_call", owner="bridge")

        registry.add_leg("call-1", "late-ringing-leg", role="callee", state="ringing")

        self.assertEqual(session.state, "in_call")
        self.assertEqual(session.legs["late-ringing-leg"].state, "ringing")

    def test_terminal_tombstones_evict_oldest_deterministically(self) -> None:
        registry = _registry()
        for index in range(call_registry.MAX_TERMINATED_CALL_IDS + 1):
            self.assertTrue(registry.begin_termination(f"call-{index}"))

        self.assertFalse(registry.is_terminated("call-0"))
        self.assertTrue(registry.is_terminated("call-1"))
        self.assertTrue(
            registry.is_terminated(f"call-{call_registry.MAX_TERMINATED_CALL_IDS}")
        )

    def test_generation_guards_async_transition_and_terminal_tombstone(self) -> None:
        registry = _registry()
        session = registry.upsert("call-1", state="ringing", owner="ha_softphone")

        self.assertIsNone(
            registry.transition(
                "call-1",
                state="in_call",
                expected_generation=session.generation + 1,
            )
        )
        self.assertIsNotNone(
            registry.transition(
                "call-1",
                state="in_call",
                expected_generation=session.generation,
            )
        )
        generation = session.generation
        registry.finish_and_pop("call-1", reason="remote_hangup")

        self.assertTrue(registry.is_terminated("call-1", generation=generation))
        self.assertFalse(registry.is_current("call-1", revision=session.revision))

    def test_terminal_pop_removes_event_context_and_pending_indexes(self) -> None:
        registry = _registry()
        registry.upsert("call-1", state="ringing", owner="ha_softphone")
        registry.event_fields("call-1", "ringing")
        registry.set_pending_invite("call-1", object())
        registry.set_pending_route("call-1", {"future": object()})

        registry.finish_and_pop("call-1", reason="remote_hangup")

        self.assertNotIn("call-1", registry.event_contexts)
        self.assertNotIn("call-1", registry.pending_invites)
        self.assertNotIn("call-1", registry.pending_routes)

    def test_pending_route_begins_its_authoritative_call_generation(self) -> None:
        registry = _registry()

        registry.set_pending_route("route-first", {"future": object()})

        session = registry.sessions["route-first"]
        assert session.state == "connecting"
        assert session.owner == "router"
        assert registry.session_owner().get_session("route-first") is not None
        assert "route-first" in registry.pending_routes

    def test_endpoint_claims_are_atomic_and_released_by_leg_teardown(self) -> None:
        registry = _registry()
        endpoints = _EndpointRegistryStub()
        registry.bind_endpoint_registry(endpoints)
        registry.upsert("source", state="connecting", owner="router")
        registry.claim_endpoint("source", "caller", role="source")
        registry.claim_endpoint("source", "callee", role="destination")
        registry.register_bridge(
            source_call_id="source",
            dest_call_id="destination-leg",
            client=object(),
            state="ringing",
        )

        registry.finish_and_pop("destination-leg", reason="remote_hangup")

        self.assertEqual(endpoints.active, {})
        self.assertCountEqual(
            endpoints.releases,
            [("caller", "source"), ("callee", "source")],
        )
        self.assertEqual(registry.endpoint_claims, {})

    def test_busy_endpoint_claim_never_records_partial_ownership(self) -> None:
        registry = _registry()
        endpoints = _EndpointRegistryStub()
        endpoints.active["kitchen"] = "existing"
        registry.bind_endpoint_registry(endpoints)

        with self.assertRaisesRegex(ValueError, "kitchen is busy"):
            registry.claim_endpoint("new-call", "kitchen")

        self.assertEqual(registry.endpoint_claims, {})
        self.assertEqual(endpoints.active, {"kitchen": "existing"})

    def test_clear_runtime_releases_endpoint_claims_before_indexes(self) -> None:
        registry = _registry()
        endpoints = _EndpointRegistryStub()
        registry.bind_endpoint_registry(endpoints)
        registry.upsert("call-1", state="in_call", owner="bridge")
        registry.claim_endpoint("call-1", "office")

        asyncio.run(registry.session_owner().shutdown())
        registry.clear_runtime()

        self.assertEqual(endpoints.active, {})
        self.assertEqual(registry.endpoint_claims, {})
        self.assertEqual(registry.sessions, {})

    def test_source_call_can_adopt_provisional_physical_state_token(self) -> None:
        registry = _registry()
        endpoints = _EndpointRegistryStub()
        endpoints.active["kiosk"] = "physical:kiosk"
        registry.bind_endpoint_registry(endpoints)

        registry.claim_endpoint(
            "sip-call",
            "kiosk",
            role="source",
            adopt_transport=True,
        )

        self.assertEqual(endpoints.active["kiosk"], "sip-call")
        registry.finish_and_pop("sip-call", reason="remote_hangup")
        self.assertEqual(endpoints.active, {})

    def test_controller_identity_is_sticky_and_preserves_first_ha_context(self) -> None:
        registry = _registry()
        registry.upsert("call-1", state="calling", owner="ha_softphone")
        first_context = types.SimpleNamespace(user_id="user-a", id="context-a")
        duplicate_context = types.SimpleNamespace(user_id="user-a", id="context-b")

        session = registry.bind_controller("call-1", context=first_context)
        duplicate = registry.bind_controller("call-1", context=duplicate_context)

        self.assertIs(session, duplicate)
        self.assertEqual(session.metadata["controller_user_id"], "user-a")
        self.assertIs(session.metadata["ha_context"], first_context)

    def test_controller_identity_cannot_be_reassigned_to_another_user(self) -> None:
        registry = _registry()
        registry.upsert("call-1", state="ringing", owner="ha_softphone")
        registry.bind_controller("call-1", user_id="user-a")

        with self.assertRaisesRegex(ValueError, "already controlled"):
            registry.bind_controller("call-1", user_id="user-b")

        self.assertEqual(
            registry.sessions["call-1"].metadata["controller_user_id"],
            "user-a",
        )

    def test_local_bridge_has_one_sticky_controller_per_phone_leg(self) -> None:
        registry = _registry()
        registry.upsert(
            "call-1",
            state="ringing",
            owner="local_bridge",
            local_bridge=True,
        )

        registry.bind_controller("call-1", user_id="user-a", endpoint_id="kitchen")
        registry.bind_controller("call-1", user_id="user-b", endpoint_id="office")

        session = registry.sessions["call-1"]
        self.assertEqual(
            session.metadata["controller_user_ids"],
            {"kitchen": "user-a", "office": "user-b"},
        )
        self.assertNotIn("controller_user_id", session.metadata)
        with self.assertRaisesRegex(ValueError, "endpoint kitchen"):
            registry.bind_controller("call-1", user_id="user-c", endpoint_id="kitchen")

    def test_internal_context_survives_later_admin_media_binding(self) -> None:
        registry = _registry()
        registry.upsert("call-1", state="in_call", owner="ha_softphone")
        automation_context = types.SimpleNamespace(user_id=None, id="automation")

        registry.bind_controller("call-1", context=automation_context)
        session = registry.bind_controller("call-1", user_id="admin")

        self.assertEqual(session.metadata["controller_user_id"], "admin")
        self.assertIs(session.metadata["ha_context"], automation_context)


class AutomationEventTypeTest(unittest.TestCase):
    def test_maps_routing_and_call_lifecycle_to_native_event_types(self) -> None:
        cases = (
            ({"state": "route_requested", "direction": "incoming"}, "route_requested"),
            ({"state": "connecting", "direction": "incoming"}, "state_changed"),
            (
                {
                    "state": "connecting",
                    "direction": "incoming",
                    "event_type": "forwarding",
                },
                "forwarding",
            ),
            ({"state": "connecting", "direction": "outgoing"}, "calling"),
            ({"state": "calling", "direction": "outgoing"}, "outgoing_call"),
            ({"state": "remote_ringing"}, "remote_ringing"),
            ({"state": "in_call"}, "answered"),
            ({"state": "in_call", "direction": "outgoing"}, "connected"),
            ({"state": "idle", "type": "ended"}, "ended"),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(
                    automation_routing.automation_event_type(payload),
                    expected,
                )

    def test_deadline_only_matches_the_armed_state_revision(self) -> None:
        self.assertTrue(
            automation_routing.deadline_is_current(
                "ringing", 3, armed_state="ringing", armed_sequence=3
            )
        )
        self.assertFalse(
            automation_routing.deadline_is_current(
                "in_call", 4, armed_state="ringing", armed_sequence=3
            )
        )
        self.assertFalse(
            automation_routing.deadline_is_current(
                "ringing", 5, armed_state="ringing", armed_sequence=3
            )
        )

    def test_forward_call_id_is_inferred_only_when_unambiguous(self) -> None:
        self.assertEqual(
            automation_routing.resolve_forward_call_id("", {"call-1": {}}, {}),
            "call-1",
        )
        self.assertEqual(
            automation_routing.resolve_forward_call_id(
                "chosen", {"call-1": {}}, {"call-2": object()}
            ),
            "chosen",
        )
        with self.assertRaisesRegex(ValueError, "No forwardable"):
            automation_routing.resolve_forward_call_id("", {}, {})
        with self.assertRaisesRegex(ValueError, "More than one"):
            automation_routing.resolve_forward_call_id(
                "", {"call-1": {}}, {"call-2": object()}
            )

    def test_initial_destination_call_id_is_inferred_only_for_one_pending_route(
        self,
    ) -> None:
        self.assertEqual(
            automation_routing.resolve_pending_route_call_id("", {"call-1": {}}),
            "call-1",
        )
        self.assertEqual(
            automation_routing.resolve_pending_route_call_id(
                "chosen", {"call-1": {}, "call-2": {}}
            ),
            "chosen",
        )
        with self.assertRaisesRegex(ValueError, "No inbound route"):
            automation_routing.resolve_pending_route_call_id("", {})
        with self.assertRaisesRegex(ValueError, "More than one inbound route"):
            automation_routing.resolve_pending_route_call_id(
                "", {"call-1": {}, "call-2": {}}
            )


if __name__ == "__main__":
    unittest.main()
