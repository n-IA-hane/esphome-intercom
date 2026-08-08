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
        self.assertIn("esp_to_ha_extension_answer_hangup", scenarios)
        self.assertIn("esp_to_ha_extension_cancel", scenarios)
        self.assertTrue(
            all("esp" in scenario.requires for scenario in scenarios.values())
        )
        self.assertTrue(
            any("ha" in scenario.requires for scenario in scenarios.values())
        )

    def test_matrix_covers_groups_dnd_trunk_and_self_call(self) -> None:
        scenarios = runner.SCENARIOS
        self.assertTrue(
            any("ring_group" in scenario.requires for scenario in scenarios.values())
        )
        self.assertTrue(
            any(
                "conference_group" in scenario.requires
                for scenario in scenarios.values()
            )
        )
        self.assertTrue(
            any("dnd" in scenario.requires for scenario in scenarios.values())
        )
        self.assertTrue(
            any("trunk" in scenario.requires for scenario in scenarios.values())
        )
        self.assertTrue(
            any("busy" in scenario.requires for scenario in scenarios.values())
        )
        self.assertIn(
            "direct_media",
            scenarios["esp_to_esp_bidirectional"].requires,
        )

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


class EspPairQualificationBehaviorTest(unittest.IsolatedAsyncioTestCase):
    async def test_esp_to_ha_answer_uses_selected_phone_identity(self) -> None:
        ctx = SimpleNamespace(
            cleanup=AsyncMock(),
            esp=SimpleNamespace(service=AsyncMock()),
            ha=SimpleNamespace(service=AsyncMock()),
            args=SimpleNamespace(ha_extension="666"),
            capture=unittest.mock.Mock(),
        )
        ringing = {
            "state": "ringing",
            "call_id": "call-1",
            "device_id": "phone-device",
        }

        with (
            unittest.mock.patch.object(
                runner,
                "wait_esp_voip_state",
                AsyncMock(),
            ),
            unittest.mock.patch.object(
                runner,
                "wait_softphone_state",
                AsyncMock(
                    side_effect=[ringing, {"state": "in_call"}, {"state": "idle"}]
                ),
            ),
        ):
            await runner.scenario_esp_to_ha_extension_answer_hangup(ctx)

        self.assertEqual(
            ctx.ha.service.await_args_list,
            [
                call(
                    "voip_stack",
                    "answer",
                    {"call_id": "call-1", "device_id": "phone-device"},
                ),
                call(
                    "voip_stack",
                    "hangup",
                    {"call_id": "call-1", "device_id": "phone-device"},
                ),
            ],
        )

    async def test_pair_scenario_covers_both_directions_and_hangup_owners(
        self,
    ) -> None:
        events: list[tuple[str, str, object]] = []

        class Device:
            def __init__(self, key: str, name: str) -> None:
                self.spec = SimpleNamespace(key=key, name=name)
                self.values = {"voip_state": "idle", "auto_answer": False}
                self.other: "Device | None" = None

            async def service(self, name: str, data=None) -> None:
                events.append((self.spec.key, name, data))
                assert self.other is not None
                if name == "start_call":
                    self.values["voip_state"] = "in_call"
                    self.other.values["voip_state"] = "in_call"
                elif name == "hangup_call":
                    self.values["voip_state"] = "idle"
                    self.other.values["voip_state"] = "idle"

            async def switch(self, object_id: str, value: bool) -> None:
                self.values[object_id] = value

            async def wait(self, object_id: str, wanted, **_kwargs) -> None:
                self.assert_state(object_id, wanted)

            def assert_state(self, object_id: str, wanted) -> None:
                if self.values[object_id] not in wanted:
                    raise AssertionError((object_id, wanted, self.values[object_id]))

            def snapshot(self):
                return {"device": self.spec.key, "state": self.values["voip_state"]}

        primary = Device("p4", "Waveshare P4 Touch")
        peer = Device("ws3", "Waveshare S3 Audio")
        primary.other = peer
        peer.other = primary

        class PeerContext:
            async def __aenter__(self):
                return peer

            async def __aexit__(self, *_args):
                return None

        ctx = SimpleNamespace(
            esp=primary,
            args=SimpleNamespace(
                peer_esp="ws3",
                peer_esp_host="",
                peer_esp_api_port=None,
            ),
            artifacts=[],
        )
        with (
            unittest.mock.patch.object(runner, "EspApi", return_value=PeerContext()),
            unittest.mock.patch.object(runner.asyncio, "sleep", AsyncMock()),
        ):
            await runner.scenario_esp_to_esp_bidirectional(ctx)

        self.assertEqual(
            [event for event in events if event[1] == "start_call"],
            [
                ("p4", "start_call", {"dest": "Waveshare S3 Audio"}),
                ("ws3", "start_call", {"dest": "Waveshare P4 Touch"}),
            ],
        )
        self.assertEqual(
            [event[0] for event in events if event[1] == "hangup_call"],
            ["ws3", "ws3"],
        )
        self.assertEqual(primary.values["voip_state"], "idle")
        self.assertEqual(peer.values["voip_state"], "idle")
        self.assertEqual(len(ctx.artifacts), 2)

    async def test_trunk_scenario_uses_explicit_trunk_dial_prefix(self) -> None:
        esp = SimpleNamespace(
            service=AsyncMock(),
            snapshot=unittest.mock.Mock(
                return_value={"destination": "**3519968203"}
            ),
        )
        ctx = SimpleNamespace(
            esp=esp,
            args=SimpleNamespace(
                allow_trunk=True,
                trunk_number="3519968203",
            ),
            cleanup=AsyncMock(),
            capture=unittest.mock.Mock(),
        )
        with unittest.mock.patch.object(
            runner, "wait_esp_voip_state", AsyncMock()
        ):
            await runner.scenario_esp_to_trunk_cancel(ctx)

        self.assertEqual(
            esp.service.await_args_list,
            [
                call("start_call", {"dest": "**3519968203"}),
                call("decline_call", {"reason": "qualification_cancel"}),
            ],
        )


if __name__ == "__main__":
    unittest.main()
