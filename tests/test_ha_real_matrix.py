"""Contract tests for the real isolated Home Assistant matrix runner."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]


def load_runner() -> ModuleType:
    path = ROOT / "scripts/run_ha_real_matrix.py"
    spec = importlib.util.spec_from_file_location("run_ha_real_matrix", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_final_response_scenario_is_a_real_sipp_dialog() -> None:
    runner = load_runner()

    scenario = runner._final_response_scenario(486)

    assert "INVITE sip:[service]@[remote_ip]:[remote_port]" in scenario
    assert '<recv response="486" timeout="8000" />' in scenario
    assert "CSeq: 1 ACK" in scenario


def test_manual_baresip_config_changes_only_answer_policy(tmp_path: Path) -> None:
    runner = load_runner()
    source = tmp_path / "source"
    source.mkdir()
    (source / "accounts").write_text(
        '"Sink" <sip:sink@example>;answermode=auto;ptime=20\n',
        encoding="utf-8",
    )
    (source / "config").write_text("audio_player null\n", encoding="utf-8")

    destination = runner._manual_baresip_config(source, tmp_path / "manual")

    assert "answermode=manual" in (destination / "accounts").read_text()
    assert (destination / "config").read_text() == "audio_player null\n"


def test_matrix_declares_required_route_and_answer_cases() -> None:
    runner = load_runner()

    assert runner.ROUTE_ACTIONS == (
        "default",
        "decline",
        "busy",
        "cancel",
        "forward",
        "bridge",
    )
    assert runner.ANSWER_CASES == (
        "registered_sip_auto_answer_on_caller_bye",
        "registered_sip_auto_answer_off_callee_bye",
    )
