#!/usr/bin/env python3
"""Pure logical PhoneEndpoint and EndpointRegistry contract tests."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import importlib.util
import sys
import types
import unittest
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
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


phone_endpoint = _load_module("phone_endpoint")
endpoint_registry = _load_module("endpoint_registry")
config_validation = _load_module("config_validation")


def endpoint(
    endpoint_id: str,
    name: str,
    *,
    kind: str = "browser",
    **changes,
):
    return phone_endpoint.PhoneEndpoint(
        endpoint_id=endpoint_id,
        name=name,
        kind=kind,
        **changes,
    )


class PhoneEndpointTest(unittest.TestCase):
    def test_normalizes_persistent_and_runtime_fields(self) -> None:
        item = endpoint(
            " kitchen ",
            " Kitchen ",
            extension=" 401 ",
            username=" Kitchen-SIP ",
            device_id=" device-1 ",
            entity_ids=[" sensor.kitchen_phone ", "switch.kitchen_dnd"],
            availability="available",
            capabilities=[" Audio ", "VIDEO", "audio", ""],
            dnd="yes",
            ring_group=" Ground Floor ",
            conference_group=" Staff ",
            conference_ring=1,
            active_call_id=" call-1 ",
        )

        self.assertEqual(item.endpoint_id, "kitchen")
        self.assertEqual(item.name, "Kitchen")
        self.assertIs(item.kind, phone_endpoint.EndpointKind.BROWSER)
        self.assertEqual(item.extension, "401")
        self.assertEqual(item.username, "Kitchen-SIP")
        self.assertEqual(item.device_id, "device-1")
        self.assertEqual(
            item.entity_ids,
            frozenset({"sensor.kitchen_phone", "switch.kitchen_dnd"}),
        )
        self.assertEqual(item.capabilities, frozenset({"audio", "video"}))
        self.assertTrue(item.supports(" Video "))
        self.assertTrue(item.dnd)
        self.assertTrue(item.conference_ring)
        self.assertTrue(item.has_active_call)
        self.assertTrue(item.is_available)

    def test_endpoint_snapshot_and_identity_are_immutable(self) -> None:
        item = endpoint(phone_endpoint.DEFAULT_ENDPOINT_ID, "Home Assistant")
        self.assertEqual(item.endpoint_id, "default")
        with self.assertRaises(FrozenInstanceError):
            item.endpoint_id = "replacement"

    def test_public_sip_user_never_exposes_the_opaque_endpoint_id(self) -> None:
        without_extension = endpoint(
            "browser:opaque-id",
            "Cucina",
        )
        with_extension = endpoint(
            "browser:opaque-id",
            "Cucina",
            extension="427",
        )
        account = endpoint(
            "sip:opaque-id",
            "Studio",
            kind="sip_account",
            extension="428",
            username="studio-phone",
        )
        esp = endpoint(
            "esphome:opaque-id",
            "Waveshare P4 Touch",
            kind="esphome",
            username="waveshare-p4-touch",
        )

        self.assertEqual(without_extension.sip_uri_user, "Cucina")
        self.assertEqual(with_extension.sip_uri_user, "427")
        self.assertEqual(account.sip_uri_user, "studio-phone")
        self.assertEqual(esp.sip_uri_user, "waveshare-p4-touch")

    def test_rejects_invalid_endpoint_shapes(self) -> None:
        for kwargs in (
            {"endpoint_id": "", "name": "Kitchen", "kind": "browser"},
            {"endpoint_id": "kitchen", "name": "", "kind": "browser"},
            {"endpoint_id": "kitchen", "name": "Kitchen", "kind": "vendor"},
            {
                "endpoint_id": "kitchen",
                "name": "Kitchen",
                "kind": "browser",
                "dnd": "sometimes",
            },
            {
                "endpoint_id": "kitchen",
                "name": "Kitchen",
                "kind": "browser",
                "offline_policy": "forward",
                "offline_forward_target": "Reception",
            },
        ):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(phone_endpoint.EndpointValidationError):
                    phone_endpoint.PhoneEndpoint(**kwargs)

    def test_forward_policy_is_available_to_sip_accounts(self) -> None:
        item = endpoint(
            "warehouse",
            "Warehouse",
            kind="sip_account",
            offline_policy="forward",
            offline_forward_target="reception",
            ring_group="Building",
            conference_group="Operations",
            conference_ring=True,
        )
        self.assertIs(item.kind, phone_endpoint.EndpointKind.SIP_ACCOUNT)
        self.assertIs(item.offline_policy, phone_endpoint.OfflinePolicy.FORWARD)
        self.assertEqual(item.offline_forward_target, "reception")
        self.assertEqual(item.ring_group, "Building")
        self.assertEqual(item.conference_group, "Operations")


class EndpointRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = endpoint_registry.EndpointRegistry()
        self.kitchen = endpoint(
            "kitchen",
            "Kitchen",
            extension="401",
            username="kitchen-sip",
            device_id="dev-kitchen",
            entity_ids={"sensor.kitchen_phone", "switch.kitchen_dnd"},
            capabilities={"audio", "dtmf"},
        )
        self.registry.register(self.kitchen)

    def test_indexes_every_supported_identity(self) -> None:
        for resolved in (
            self.registry.get("KITCHEN"),
            self.registry.by_device_id("dev-kitchen"),
            self.registry.by_entity_id("sensor.kitchen_phone"),
            self.registry.by_name("kitchen"),
            self.registry.by_extension("401"),
            self.registry.by_username("KITCHEN-SIP"),
            self.registry.resolve("401"),
            self.registry.resolve(
                "dev-kitchen", namespace=endpoint_registry.EndpointLookup.DEVICE_ID
            ),
        ):
            self.assertEqual(resolved, self.kitchen)

    def test_route_namespace_rejects_cross_field_collisions(self) -> None:
        collisions = (
            endpoint("office", "kitchen"),
            endpoint("office", "Office", extension="KITCHEN-SIP"),
            endpoint("office", "Office", username="401"),
        )
        for candidate in collisions:
            with self.subTest(candidate=candidate):
                with self.assertRaises(endpoint_registry.EndpointCollisionError) as ctx:
                    self.registry.register(candidate)
                self.assertEqual(ctx.exception.conflicting_endpoint_id, "kitchen")
        self.assertEqual(len(self.registry), 1)

    def test_route_namespace_uses_the_same_normalization_as_the_router(self) -> None:
        registry = endpoint_registry.EndpointRegistry()
        registry.register(endpoint("kitchen-one", "Kitchen-1"))

        with self.assertRaises(endpoint_registry.EndpointCollisionError):
            registry.register(endpoint("kitchen-two", "Kitchen 1"))

    def test_rejects_stable_device_and_entity_collisions(self) -> None:
        for candidate in (
            endpoint("office", "Office", device_id="DEV-KITCHEN"),
            endpoint(
                "office", "Office", entity_ids={"sensor.kitchen_phone"}
            ),
            endpoint("KITCHEN", "Office"),
        ):
            with self.subTest(candidate=candidate):
                with self.assertRaises(endpoint_registry.EndpointCollisionError):
                    self.registry.register(candidate)
        self.assertEqual(self.registry.endpoints, (self.kitchen,))

    def test_upsert_cannot_change_even_the_casing_of_stable_identity(self) -> None:
        with self.assertRaises(endpoint_registry.EndpointRegistryError):
            self.registry.upsert(endpoint("KITCHEN", "Kitchen Tablet"))
        self.assertEqual(self.registry.endpoints, (self.kitchen,))

    def test_failed_update_is_atomic_and_keeps_old_indexes(self) -> None:
        office = self.registry.register(
            endpoint("office", "Office", extension="402", device_id="dev-office")
        )
        with self.assertRaises(endpoint_registry.EndpointCollisionError):
            self.registry.update("office", extension="401")

        self.assertEqual(self.registry.get("office"), office)
        self.assertEqual(self.registry.by_extension("402"), office)
        self.assertEqual(self.registry.by_extension("401"), self.kitchen)

    def test_update_reindexes_device_entities_and_route_aliases(self) -> None:
        updated = self.registry.update(
            "kitchen",
            name="Kitchen Tablet",
            extension="411",
            username="kiosk-kitchen",
            device_id="dev-kitchen-new",
            entity_ids={"sensor.kitchen_tablet_phone"},
            availability="available",
            dnd=True,
        )

        self.assertIsNone(self.registry.by_name("Kitchen"))
        self.assertIsNone(self.registry.by_extension("401"))
        self.assertIsNone(self.registry.by_username("kitchen-sip"))
        self.assertIsNone(self.registry.by_device_id("dev-kitchen"))
        self.assertIsNone(self.registry.by_entity_id("sensor.kitchen_phone"))
        self.assertEqual(self.registry.by_name("kitchen tablet"), updated)
        self.assertEqual(self.registry.by_extension("411"), updated)
        self.assertEqual(self.registry.by_username("KIOSK-KITCHEN"), updated)
        self.assertEqual(self.registry.by_device_id("dev-kitchen-new"), updated)
        self.assertEqual(
            self.registry.by_entity_id("sensor.kitchen_tablet_phone"), updated
        )
        self.assertTrue(updated.dnd)
        self.assertTrue(updated.is_available)

    def test_unqualified_cross_namespace_ambiguity_is_explicit(self) -> None:
        office = self.registry.register(
            endpoint("office", "Office", device_id="401")
        )
        self.assertEqual(self.registry.by_device_id("401"), office)
        self.assertEqual(self.registry.by_extension("401"), self.kitchen)
        with self.assertRaises(endpoint_registry.EndpointAmbiguousError) as ctx:
            self.registry.resolve("401")
        self.assertEqual(set(ctx.exception.endpoint_ids), {"kitchen", "office"})

    def test_call_claim_is_idempotent_busy_safe_and_guarded_on_release(self) -> None:
        claimed = self.registry.claim_call("kitchen", "call-1")
        duplicate = self.registry.claim_call("kitchen", "call-1")
        self.assertEqual(claimed, duplicate)
        self.assertEqual(claimed.active_call_id, "call-1")

        with self.assertRaises(endpoint_registry.EndpointBusyError) as ctx:
            self.registry.claim_call("kitchen", "call-2")
        self.assertEqual(ctx.exception.active_call_id, "call-1")
        self.assertFalse(self.registry.release_call("kitchen", "call-stale"))
        self.assertEqual(self.registry.require("kitchen").active_call_id, "call-1")
        self.assertTrue(self.registry.release_call("kitchen", "call-1"))
        self.assertEqual(self.registry.require("kitchen").active_call_id, "")

    def test_active_call_cannot_be_bypassed_by_update_upsert_or_remove(self) -> None:
        active = self.registry.claim_call("kitchen", "call-1")
        with self.assertRaises(endpoint_registry.EndpointRegistryError):
            self.registry.update("kitchen", active_call_id="call-2")
        with self.assertRaises(endpoint_registry.EndpointBusyError):
            self.registry.upsert(
                replace(active, active_call_id="call-2")
            )
        with self.assertRaises(endpoint_registry.EndpointBusyError):
            self.registry.remove("kitchen")
        self.assertEqual(self.registry.require("kitchen").active_call_id, "call-1")

    def test_transport_state_uses_existing_routed_claim_then_releases_it(self) -> None:
        self.registry.claim_call("kitchen", "sip-dialog")

        active = self.registry.sync_transport_call(
            "kitchen",
            active=True,
            fallback_call_id="physical:kitchen",
        )
        idle = self.registry.sync_transport_call(
            "kitchen",
            active=False,
            fallback_call_id="physical:kitchen",
        )

        self.assertEqual(active.active_call_id, "sip-dialog")
        self.assertEqual(idle.active_call_id, "")

    def test_transport_state_creates_stable_claim_when_call_id_is_not_exposed(self) -> None:
        active = self.registry.sync_transport_call(
            "kitchen",
            active=True,
            fallback_call_id="physical:kitchen",
        )

        self.assertEqual(active.active_call_id, "physical:kitchen")
        self.registry.sync_transport_call("kitchen", active=False)
        with self.assertRaises(endpoint_registry.EndpointRegistryError):
            self.registry.sync_transport_call(
                "kitchen",
                active=True,
                fallback_call_id="",
            )

    def test_sip_dialog_can_adopt_only_a_provisional_transport_claim(self) -> None:
        self.registry.sync_transport_call(
            "kitchen",
            active=True,
            fallback_call_id="physical:kitchen",
        )

        adopted = self.registry.adopt_transport_call("kitchen", "sip-call")

        self.assertEqual(adopted.active_call_id, "sip-call")
        with self.assertRaises(endpoint_registry.EndpointBusyError):
            self.registry.adopt_transport_call("kitchen", "other-call")

    def test_mutation_events_and_unsubscribe_are_deterministic(self) -> None:
        events = []
        unsubscribe = self.registry.subscribe(events.append)
        office = self.registry.register(endpoint("office", "Office"))
        updated = self.registry.update("office", availability="available")
        removed = self.registry.remove("office")
        unsubscribe()
        unsubscribe()
        self.registry.update("kitchen", dnd=True)

        self.assertEqual(
            [event.event_type for event in events],
            [
                endpoint_registry.EndpointRegistryEventType.REGISTERED,
                endpoint_registry.EndpointRegistryEventType.UPDATED,
                endpoint_registry.EndpointRegistryEventType.REMOVED,
            ],
        )
        self.assertEqual(events[0].endpoint, office)
        self.assertIsNone(events[0].previous)
        self.assertEqual(events[1].endpoint, updated)
        self.assertEqual(events[1].previous, office)
        self.assertEqual(events[2].endpoint, removed)

    def test_missing_and_invalid_operations_raise_typed_errors(self) -> None:
        with self.assertRaises(endpoint_registry.EndpointNotFoundError):
            self.registry.require("missing")
        with self.assertRaises(endpoint_registry.EndpointRegistryError):
            self.registry.claim_call("kitchen", "")
        with self.assertRaises(endpoint_registry.EndpointRegistryError):
            self.registry.update("kitchen", endpoint_id="other")
        with self.assertRaises(endpoint_registry.EndpointRegistryError):
            self.registry.resolve("kitchen", namespace="vendor")


class RouteNamespaceTest(unittest.TestCase):
    def test_router_normalization_prevents_phone_group_shadowing(self) -> None:
        self.assertTrue(
            config_validation.route_namespace_conflicts(
                candidate_routes=["Kitchen-1"],
                existing=[{"ring_group": "Kitchen 1"}],
            )
        )
        self.assertTrue(
            config_validation.route_namespace_conflicts(
                candidate_groups=["Front Desk"],
                existing=[{"name": "front-desk"}],
            )
        )

    def test_group_members_may_intentionally_reuse_same_group_route(self) -> None:
        self.assertFalse(
            config_validation.route_namespace_conflicts(
                candidate_routes=["Kitchen"],
                candidate_groups=["Ground Floor"],
                existing=[
                    {
                        "name": "Office",
                        "metadata": {
                            "ring_group": "Night, Ground-Floor",
                        },
                    }
                ],
            )
        )

    def test_one_mapping_cannot_claim_same_alias_as_phone_and_group(self) -> None:
        self.assertTrue(
            config_validation.route_namespace_conflicts(
                candidate_routes=["Staff"],
                candidate_groups=["staff"],
            )
        )


if __name__ == "__main__":
    unittest.main()
