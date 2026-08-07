#!/usr/bin/env python3
"""Call-scoped runtime diagnostics tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "runtime_diagnostics.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("voip_stack_runtime_diagnostics", MODULE)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load runtime diagnostics")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Task:
    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class _Registry:
    def snapshot(self) -> dict:
        return {
            "resource_counts": {
                "sessions": 1,
                "legs": 2,
                "pending_routes": 0,
            },
            "call_ids": ["call-1"],
            "pending_call_ids": [],
            "media_call_ids": ["call-1"],
        }


class RuntimeDiagnosticsTest(unittest.TestCase):
    def test_snapshot_combines_registry_media_ports_owners_and_tasks(self) -> None:
        diagnostics = _load_module()
        sessions = {"audio": {"call-1": object()}, "video": {"call-1": object()}}
        owners = {"audio": {"phone|call-1": object()}, "video": {}}
        media = SimpleNamespace(
            sessions_for=lambda channel: sessions[channel],
            owners_for=lambda channel: owners[channel],
            identity_locks={"phone|call-1": object()},
            transcoder=None,
        )

        snapshot = diagnostics.runtime_resource_snapshot(
            _Registry(),
            detailed=True,
            browser_media=media,
            rtp_port_pool={"used": {40002, 40004}},
            call_artifacts=SimpleNamespace(
                named_task_count=lambda name: 1,
            ),
            runtime_tasks={_Task(False), _Task(True)},
        )

        counts = snapshot["resource_counts"]
        self.assertEqual(counts["sessions"], 1)
        self.assertEqual(counts["active_audio_sessions"], 1)
        self.assertEqual(counts["active_video_sessions"], 1)
        self.assertEqual(counts["audio_ws_owners"], 1)
        self.assertEqual(counts["video_ws_owners"], 0)
        self.assertEqual(counts["media_identity_locks"], 1)
        self.assertEqual(counts["allocated_rtp_ports"], 2)
        self.assertEqual(counts["forward_tasks"], 1)
        self.assertEqual(counts["call_deadlines"], 1)
        self.assertEqual(counts["runtime_tasks"], 1)
        self.assertFalse(snapshot["call_scoped_quiescent"])
        self.assertEqual(snapshot["allocated_rtp_ports"], [40002, 40004])
        self.assertEqual(snapshot["call_ids"]["audio_sessions"], ["call-1"])

    def test_idle_snapshot_is_call_scoped_quiescent(self) -> None:
        diagnostics = _load_module()

        snapshot = diagnostics.runtime_resource_snapshot(None)

        self.assertTrue(snapshot["call_scoped_quiescent"])
        self.assertEqual(snapshot["resource_counts"]["allocated_rtp_ports"], 0)


if __name__ == "__main__":
    unittest.main()
