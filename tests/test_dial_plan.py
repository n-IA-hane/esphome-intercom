#!/usr/bin/env python3
"""Dial-plan contracts for multi-Contact and ring tiers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"
CORE_MODULES = {"sip"}


def _load_module(name: str):
    if "custom_components" not in sys.modules:
        root = types.ModuleType("custom_components")
        root.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root
    if PKG_NAME not in sys.modules:
        package = types.ModuleType(PKG_NAME)
        package.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = package
    is_core = name in CORE_MODULES
    if is_core and f"{PKG_NAME}.core" not in sys.modules:
        core_pkg = types.ModuleType(f"{PKG_NAME}.core")
        core_pkg.__path__ = [str(PKG_DIR / "core")]
        sys.modules[f"{PKG_NAME}.core"] = core_pkg
    full_name = f"{PKG_NAME}.{'core.' if is_core else ''}{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    path = PKG_DIR / ("core" if is_core else "") / f"{name}.py"
    spec = importlib.util.spec_from_file_location(full_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


_load_module("session_cleanup")
_load_module("endpoint_session")
_load_module("dial_fork")
_load_module("sip")
roster = _load_module("roster")
dial_plan = _load_module("dial_plan")


class DialPlanTest(unittest.TestCase):
    def test_multi_contact_q_values_create_successive_tiers(self) -> None:
        entry = roster.RosterEntry(
            id="desk",
            name="Desk",
            sip_uri="sip:desk@192.0.2.10:5060",
            metadata={
                "endpoint_id": "desk-account",
                "sip_contacts": [
                    {"uri": "sip:desk@192.0.2.12:5060", "q": 0.5, "transport": "udp"},
                    {
                        "uri": "sip:desk@192.0.2.11:5060",
                        "q": 1.0,
                        "transport": "udp",
                        "user_agent": "Dahua UAC/3.0",
                    },
                    {"uri": "sip:desk@192.0.2.13:5060", "q": 0.5, "transport": "udp"},
                ],
            },
        )

        targets = dial_plan.build_sip_contact_targets(
            ["desk"], [entry], policy=dial_plan.RingPolicy()
        )

        self.assertEqual([target.tier for target in targets], [0, 1, 1])
        self.assertEqual(targets[0].uri, "sip:desk@192.0.2.11:5060")
        self.assertEqual(targets[0].user_agent, "Dahua UAC/3.0")
        self.assertEqual({target.endpoint_id for target in targets}, {"desk-account"})

    def test_member_tiers_and_exclusion_are_resolved_before_dial(self) -> None:
        entries = [
            roster.RosterEntry(id="a", sip_uri="sip:a@192.0.2.1"),
            roster.RosterEntry(id="b", sip_uri="sip:b@192.0.2.2"),
        ]
        policy = dial_plan.RingPolicy.from_metadata(
            {
                "ring_policy": {
                    "strategy": "sequential",
                    "member_tiers": {"b": 3},
                    "tier_strategies": {"3": "parallel"},
                    "overall_timeout": 20,
                    "step_timeout": 5,
                }
            }
        )

        targets = dial_plan.build_sip_contact_targets(
            ["a", "b"],
            entries,
            policy=policy,
            exclude_endpoint_id="a",
        )

        self.assertEqual(policy.strategy.value, "sequential")
        self.assertEqual(policy.tier_strategies[3].value, "parallel")
        self.assertEqual([(target.member, target.tier) for target in targets], [("b", 3)])

    def test_invalid_contact_set_fails_before_partial_plan(self) -> None:
        entry = roster.RosterEntry(
            id="desk",
            metadata={
                "sip_contacts": [
                    {"uri": "sip:desk@192.0.2.1", "q": 1.0},
                    {"uri": "https://invalid.example", "q": 0.5},
                ]
            },
        )

        with self.assertRaises((ValueError, Exception)):
            dial_plan.build_sip_contact_targets(
                ["desk"], [entry], policy=dial_plan.RingPolicy()
            )

    def test_duplicate_contact_is_deduplicated(self) -> None:
        entry = roster.RosterEntry(
            id="desk",
            metadata={
                "sip_contacts": [
                    {"uri": "sip:desk@192.0.2.1", "q": 1.0},
                    {"uri": "sip:desk@192.0.2.1", "q": 1.0},
                ]
            },
        )

        targets = dial_plan.build_sip_contact_targets(
            ["desk"], [entry], policy=dial_plan.RingPolicy()
        )

        self.assertEqual(len(targets), 1)

    def test_invalid_timeout_and_strategy_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            dial_plan.RingPolicy.from_metadata({"strategy": "random"})
        with self.assertRaises(ValueError):
            dial_plan.RingPolicy.from_metadata(
                {"overall_timeout": 5, "step_timeout": 6}
            )
