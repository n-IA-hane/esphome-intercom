#!/usr/bin/env python3
"""Contract tests for the live HA/ESP qualification runner."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, call
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "live_voip_qualification.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("live_voip_qualification", TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load live VoIP qualification runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = _load_tool()


class LiveVoipQualificationContractTest(unittest.TestCase):
    def test_default_group_names_are_unique_to_the_selected_run(self) -> None:
        args = SimpleNamespace(
            esp="p4",
            ring_group=None,
            conference_group=None,
        )
        runner.apply_isolated_group_defaults(args, stamp="123456")
        self.assertEqual(args.ring_group, "q-p4-ring-123456")
        self.assertEqual(
            args.conference_group,
            "q-p4-conference-123456",
        )
        self.assertLessEqual(len(args.conference_group), 32)

    def test_partial_baseline_failure_restores_original_device_state(self) -> None:
        text = AsyncMock(side_effect=[None, RuntimeError("write failed")] + [None] * 3)
        esp = SimpleNamespace(
            values={
                "voip_extension": "1000",
                "voip_ring_groups": "home ring",
                "voip_conference_groups": "home conference",
                "voip_ring_on_conference": False,
                "do_not_disturb": False,
                "auto_answer": False,
            },
            text=text,
            switch=AsyncMock(),
        )
        ctx = SimpleNamespace(
            esp=esp,
            ws=SimpleNamespace(
                softphone_state=AsyncMock(
                    return_value={
                        "extension": "666",
                        "groups": {
                            "ring_group": "",
                            "conference_group": "home conference",
                            "conference_ring": False,
                        },
                    }
                )
            ),
            ha=SimpleNamespace(service=AsyncMock()),
            cleanup=AsyncMock(),
            args=SimpleNamespace(
                esp_extension="1000",
                ring_group="q-p4-ring-123456",
                conference_group="q-p4-conference-123456",
                ha_extension="666",
            ),
        )

        with self.assertRaisesRegex(RuntimeError, "write failed"):
            asyncio.run(runner.set_baseline(ctx))

        self.assertIn(
            call("voip_ring_groups", "home ring"),
            text.await_args_list,
        )

    def test_peer_identity_normalizes_sip_and_friendly_name_spacing(self) -> None:
        self.assertEqual(
            runner.norm("Waveshare P4 Touch"),
            runner.norm("Waveshare_P4_Touch"),
        )

    def test_candidate_revision_records_commit_and_dirty_state(self) -> None:
        with unittest.mock.patch.object(
            runner.subprocess,
            "check_output",
            side_effect=["abc123\n", " M custom_components/voip_stack/a.py\n"],
        ):
            self.assertEqual(
                runner.candidate_revision(),
                {"commit": "abc123", "dirty": True},
            )

    def test_matrix_covers_real_ha_and_esp_paths(self) -> None:
        scenarios = runner.SCENARIOS
        self.assertIn("ha_to_esp_extension_answer_hangup", scenarios)
        self.assertIn("esp_to_ha_extension_cancel", scenarios)
        self.assertTrue(all("esp" in scenario.requires for scenario in scenarios.values()))
        self.assertTrue(any("ha" in scenario.requires for scenario in scenarios.values()))

    def test_matrix_covers_groups_dnd_trunk_and_self_call(self) -> None:
        scenarios = runner.SCENARIOS
        self.assertTrue(any("ring_group" in scenario.requires for scenario in scenarios.values()))
        self.assertTrue(any("conference_group" in scenario.requires for scenario in scenarios.values()))
        self.assertTrue(any("dnd" in scenario.requires for scenario in scenarios.values()))
        self.assertTrue(any("trunk" in scenario.requires for scenario in scenarios.values()))
        self.assertTrue(any("busy" in scenario.requires for scenario in scenarios.values()))

    def test_every_scenario_has_visible_terminal_or_state_assertions(self) -> None:
        for scenario in runner.SCENARIOS.values():
            with self.subTest(scenario=scenario.id):
                self.assertTrue(scenario.assertions)
                self.assertTrue(
                    any(
                        token in scenario.assertions
                        for token in (
                            "esp_idle",
                            "both_idle",
                            "cleanup_idle",
                            "ha_terminal_reason",
                            "winner_not_group_label",
                        )
                    ),
                    scenario.assertions,
                )


if __name__ == "__main__":
    unittest.main()
