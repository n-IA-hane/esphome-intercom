#!/usr/bin/env python3
"""Group roster aggregation tests."""

from __future__ import annotations

import importlib.util
import json
from types import SimpleNamespace
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


roster = _load_module("roster")
groups = _load_module("groups")
router = _load_module("router")


class GroupAggregationTest(unittest.TestCase):
    def test_collects_groups_from_peers_and_manual_entries(self) -> None:
        peers = [
            SimpleNamespace(name="Kitchen", conference_group="Conference, Upstairs", conference_ring=True, ring_group="Casa, Night"),
            SimpleNamespace(name="Bedroom", conference_group="", conference_ring=False, ring_group="Casa"),
        ]
        manual = [
            roster.RosterEntry(
                id="Zoiper",
                name="Zoiper",
                metadata={"conference_group": "Conference", "conference_ring": False},
            )
        ]
        collected = groups.collect_groups(peers, manual, [])
        self.assertEqual(collected["Conference"].group_type, groups.GROUP_TYPE_CONFERENCE)
        self.assertEqual(collected["Conference"].members, ["Kitchen", "Zoiper"])
        self.assertEqual(collected["Conference"].ring_members, ["Kitchen"])
        self.assertEqual(collected["Upstairs"].members, ["Kitchen"])
        self.assertEqual(collected["Casa"].group_type, groups.GROUP_TYPE_RING)
        self.assertEqual(collected["Casa"].members, ["Kitchen", "Bedroom"])
        self.assertEqual(collected["Night"].members, ["Kitchen"])

    def test_ha_peer_can_create_and_join_groups(self) -> None:
        ha = SimpleNamespace(name="Casa", is_ha=True, conference_group="CG Casa", conference_ring=True, ring_group="RG Casa")
        ha_only = groups.collect_groups([ha], [], [])
        self.assertEqual(ha_only["CG Casa"].members, ["Casa"])
        self.assertEqual(ha_only["CG Casa"].ring_members, ["Casa"])
        self.assertEqual(ha_only["RG Casa"].members, ["Casa"])

        esp = SimpleNamespace(name="Kitchen", is_ha=False, conference_group="CG Casa", conference_ring=False, ring_group="RG Casa")
        collected = groups.collect_groups([esp, ha], [], [])
        self.assertEqual(collected["CG Casa"].members, ["Kitchen", "Casa"])
        self.assertEqual(collected["CG Casa"].ring_members, ["Casa"])
        self.assertEqual(collected["RG Casa"].members, ["Kitchen", "Casa"])

    def test_conference_wins_type_conflict(self) -> None:
        peers = [
            SimpleNamespace(name="Kitchen", conference_group="Casa", conference_ring=True, ring_group=""),
            SimpleNamespace(name="Bedroom", conference_group="", conference_ring=False, ring_group="Casa"),
        ]
        collected = groups.collect_groups(peers, [], [])
        self.assertEqual(collected["Casa"].group_type, groups.GROUP_TYPE_CONFERENCE)
        self.assertEqual(collected["Casa"].members, ["Kitchen"])
        self.assertEqual(collected["Casa"].ring_members, ["Kitchen"])

    def test_ring_members_survive_when_group_is_promoted_to_conference(self) -> None:
        peers = [
            SimpleNamespace(name="Bedroom", conference_group="", conference_ring=False, ring_group="Casa"),
            SimpleNamespace(name="Kitchen", conference_group="Casa", conference_ring=False, ring_group=""),
        ]
        collected = groups.collect_groups(peers, [], [])
        self.assertEqual(collected["Casa"].group_type, groups.GROUP_TYPE_CONFERENCE)
        self.assertEqual(collected["Casa"].members, ["Bedroom", "Kitchen"])
        self.assertEqual(collected["Casa"].ring_members, ["Bedroom"])

    def test_skips_group_name_colliding_with_existing_contact(self) -> None:
        peers = [SimpleNamespace(name="Kitchen", conference_group="", conference_ring=False, ring_group="Casa")]
        existing = [roster.RosterEntry(id="Casa", name="Casa")]
        collected = groups.collect_groups(peers, [], [], existing_entries=existing)
        self.assertNotIn("Casa", collected)

    def test_router_returns_group_decision_for_group_entry(self) -> None:
        entry = roster.RosterEntry(
            id="Casa",
            name="Casa",
            ha_bridge=True,
            metadata={"group_type": groups.GROUP_TYPE_RING, "members": ["Kitchen"]},
        )
        decision = router.resolve_ha_router("Casa", [entry], trunk_ready=False)
        self.assertEqual(decision.action, router.RouteAction.GROUP)
        self.assertIs(decision.entry, entry)

    def test_name_only_group_entry_survives_roster_json_round_trip(self) -> None:
        entry = roster.RosterEntry(
            id="Conference",
            name="Conference",
            ha_bridge=True,
            metadata={
                "group_type": groups.GROUP_TYPE_CONFERENCE,
                "members": ["Kitchen", "Bedroom"],
                "ring_members": ["Kitchen"],
                "auto": True,
            },
        )
        parsed = roster.parse_roster_json(roster.dump_roster_json([entry]))
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0].id, "Conference")
        self.assertEqual(parsed[0].address, "")
        self.assertEqual(parsed[0].sip_uri, "")
        self.assertTrue(parsed[0].ha_bridge)
        self.assertEqual(parsed[0].metadata["group_type"], groups.GROUP_TYPE_CONFERENCE)
        self.assertEqual(parsed[0].metadata["ring_members"], ["Kitchen"])

        payload = json.loads(
            roster.dump_roster_json(
                [entry],
                voip_stack_version="2026.9.1-dev",
            )
        )
        assert payload["voip_stack_version"] == "2026.9.1-dev"
        self.assertEqual(payload["version"], 2)
        self.assertIn("conference_group", payload["capabilities"])
        self.assertIn("ring_group", payload["capabilities"])


if __name__ == "__main__":
    unittest.main()
