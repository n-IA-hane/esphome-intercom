#!/usr/bin/env python3
"""Completeness checks for the unexecuted SIP scenario catalog."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tests" / "support" / "qualification_matrix.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("voip_qualification_matrix", TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load qualification matrix tool")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


matrix = _load_tool()


class QualificationScenarioCatalogTest(unittest.TestCase):
    def test_catalog_has_no_declared_axis_gaps(self) -> None:
        scenarios = matrix.generate_matrix()
        self.assertGreaterEqual(len(scenarios), 400)
        self.assertEqual(matrix.validate_matrix(scenarios), [])

    def test_catalog_declares_transport_mismatch_and_high_rate_audio(self) -> None:
        scenarios = matrix.generate_matrix()
        self.assertTrue(
            any(
                scenario.caller_transport == "sip_tcp"
                and scenario.callee_transport == "sip_udp"
                and scenario.route == "ha_bridge"
                for scenario in scenarios
            )
        )
        self.assertTrue(any(scenario.tx_format == "48000:s16le:1:10" for scenario in scenarios))
        self.assertTrue(any(scenario.terminal_reason == "media_incompatible" for scenario in scenarios))

    def test_catalog_requires_user_visible_and_protocol_assertions(self) -> None:
        scenarios = matrix.generate_matrix()
        self.assertTrue(any("ha_card_mirror" in scenario.assertions for scenario in scenarios))
        self.assertTrue(any("caller_terminal_screen" in scenario.assertions for scenario in scenarios))
        self.assertTrue(all("debug_sip_trace" in scenario.assertions for scenario in scenarios))

    def test_usage_catalog_declares_every_supported_behavior_axis(self) -> None:
        scenarios = matrix.generate_usage_matrix()

        self.assertGreaterEqual(len(scenarios), 200)
        self.assertEqual(matrix.validate_usage_matrix(scenarios), [])

    def test_usage_catalog_answers_no_automation_and_group_dnd_questions(self) -> None:
        by_id = {scenario.id: scenario for scenario in matrix.generate_usage_matrix()}

        no_automation = by_id["selection-trunk-ha_softphone-automation-off"]
        self.assertEqual(no_automation.expected, "canonical_destination")
        self.assertIn("no_route_request", no_automation.assertions)

        mixed_dnd = by_id["ring-group-esp-ha_dnd_esp_answers"]
        self.assertEqual(mixed_dnd.expected, "first_answer_wins")
        all_dnd = by_id["ring-group-esp-all_dnd"]
        self.assertEqual(all_dnd.expected, "486_dnd")

    def test_explicit_dtmf_always_precedes_automation(self) -> None:
        scenarios = matrix.generate_usage_matrix()
        dtmf = [
            scenario
            for scenario in scenarios
            if scenario.selection == "valid_extension_automation_ignored"
        ]

        self.assertEqual(
            {scenario.destination for scenario in dtmf},
            {"ha_softphone", "esp", "registered_sip", "assist"},
        )
        self.assertTrue(
            all("dtmf_precedes_automation" in scenario.assertions for scenario in dtmf)
        )


if __name__ == "__main__":
    unittest.main()
