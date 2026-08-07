"""Safety contracts for the stateful real ring-group qualification tool."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "ring_group_live_matrix.py"
LOCAL_TOOL = ROOT / "tools" / "local_softphone_live_matrix.py"
SOFTPHONE_TOOL = ROOT / "tools" / "ha_softphone_matrix.py"
INBOUND_TOOL = ROOT / "tools" / "inbound_routing_qualification.py"
CARD_TRACE_TOOL = ROOT / "tools" / "ha_softphone_card_trace.py"
LIVE_VOIP_TOOL = ROOT / "tools" / "live_voip_qualification.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("ring_group_live_matrix", TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load ring-group qualification runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_softphone_tool():
    spec = importlib.util.spec_from_file_location("ha_softphone_matrix", SOFTPHONE_TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load softphone qualification runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_help_is_side_effect_free_and_returns_immediately() -> None:
    completed = subprocess.run(
        [sys.executable, str(TOOL), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert completed.returncode == 0
    assert "--expect-video" in completed.stdout
    assert "--skip-esp-winner" in completed.stdout


def test_direct_route_uses_preanswer_destination_selection() -> None:
    source = TOOL.read_text(encoding="utf-8")
    assert '"action": "voip_stack.select_inbound_destination"' in source
    assert '"action": "voip_stack.forward"' not in source


def test_local_softphone_help_is_side_effect_free_and_returns_immediately() -> None:
    completed = subprocess.run(
        [sys.executable, str(LOCAL_TOOL), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert completed.returncode == 0
    assert "--expect-video" in completed.stdout


def test_softphone_runner_preserves_explicit_ha_origin() -> None:
    env = os.environ.copy()
    env["HA_BASE"] = "http://127.0.0.1:18123"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'tools'); "
            "import ha_softphone_matrix; print(ha_softphone_matrix.HA_BASE)",
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "http://127.0.0.1:18123"


def test_softphone_runner_accepts_an_isolated_inbound_peer() -> None:
    env = os.environ.copy()
    env["INBOUND_CALLER_CONFIG"] = "/tmp/isolated-peer"
    env["INBOUND_TARGET"] = "sip:Casa@127.0.0.1:15060;transport=tcp"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, 'tools'); import ha_softphone_matrix as m; "
            "print(m.WILDIX_CONFIG); print(m.INBOUND_TARGET)",
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.splitlines() == [
        "/tmp/isolated-peer",
        "sip:Casa@127.0.0.1:15060;transport=tcp",
    ]


def test_softphone_runner_selects_dtmf_routed_trunk_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _load_softphone_tool()
    events: list[tuple[str, object]] = []

    class Peer:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def dial(self, target, *, wait_for):
            events.append(("dial", (target, wait_for)))

        def wait_for(self, value, timeout):
            events.append(("wait", (value, timeout)))

        def digits(self, value):
            events.append(("digits", value))

        def close(self):
            events.append(("close", None))

    monkeypatch.setattr(runner, "BareSip", Peer)
    monkeypatch.setattr(runner, "INBOUND_TARGET", "427")
    monkeypatch.setattr(runner, "INBOUND_DTMF_TARGET", "666")

    runner.dial_trunk()

    assert events == [
        (
            "dial",
            (
                "427",
                ("180 Ringing", "183 Session Progress", "Call established"),
            ),
        ),
        ("wait", ("Call established", 10)),
        ("digits", "666"),
    ]


@pytest.mark.parametrize(
    ("module", "attribute", "expected"),
    [
        ("ha_softphone_matrix", "HA_BASE", "http://127.0.0.1:18123"),
        ("local_softphone_live_matrix", "HA_BASE", "http://127.0.0.1:18123"),
        ("ring_group_live_matrix", "HA_BASE", "http://127.0.0.1:18123"),
        ("inbound_routing_qualification", "HA_BASE", "http://127.0.0.1:18123"),
        ("live_voip_qualification", "DEFAULT_HA_URL", "http://127.0.0.1:18123"),
        (
            "ha_softphone_card_trace",
            "DEFAULT_URL",
            "http://127.0.0.1:18123/lovelace/default_view",
        ),
    ],
)
def test_live_tools_default_to_the_isolated_lab(
    module: str,
    attribute: str,
    expected: str,
) -> None:
    env = os.environ.copy()
    env.pop("HA_BASE", None)
    env.pop("HA_URL", None)
    env.pop("HA_CARD_URL", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, 'tools'); import {module}; "
            f"print({module}.{attribute})",
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == expected


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        (SOFTPHONE_TOOL, "--only"),
        (INBOUND_TOOL, "--only"),
        (CARD_TRACE_TOOL, "--url"),
        (LIVE_VOIP_TOOL, "--auth-file"),
    ],
)
def test_live_tool_help_does_not_require_private_auth_helper(
    tool: Path,
    expected: str,
) -> None:
    completed = subprocess.run(
        [sys.executable, str(tool), "--help"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert completed.returncode == 0, completed.stderr
    assert expected in completed.stdout


def test_live_matrix_rejects_concurrent_owners(tmp_path: Path) -> None:
    runner = _load_tool()
    runner.RUN_LOCK = tmp_path / "ring-group.lock"
    with runner._exclusive_run():
        with pytest.raises(
            RuntimeError, match="another ring-group live matrix is already running"
        ):
            with runner._exclusive_run():
                pass


def test_failure_evidence_is_bounded() -> None:
    runner = _load_tool()
    compact = runner._compact_error(RuntimeError("A" * 4000), limit=500)
    assert len(compact) <= 530
    assert "<truncated>" in compact


@pytest.mark.parametrize(
    "enabled,expected_service,expected_state",
    [
        (True, "turn_on", "on"),
        (False, "turn_off", "off"),
    ],
)
def test_inbound_automation_state_is_applied_before_matrix(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
    expected_service: str,
    expected_state: str,
) -> None:
    runner = _load_tool()
    observed: list[tuple[str, str, dict[str, object]]] = []
    state = {"value": "off" if enabled else "on"}

    def fake_service(domain: str, action: str, data: dict[str, object]) -> None:
        observed.append((domain, action, data))
        state["value"] = expected_state

    monkeypatch.setattr(runner, "service", fake_service, raising=False)
    monkeypatch.setattr(
        runner,
        "ha_request",
        lambda _path: {"state": state["value"]},
        raising=False,
    )

    runner._set_inbound_automation(enabled, timeout=0.1)

    expected_data: dict[str, object] = {"entity_id": runner.INBOUND_AUTOMATION}
    if not enabled:
        expected_data["stop_actions"] = True
    assert observed == [("automation", expected_service, expected_data)]
