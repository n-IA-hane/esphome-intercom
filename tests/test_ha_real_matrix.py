"""Contract tests for the real isolated Home Assistant matrix runner."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_runner() -> ModuleType:
    path = ROOT / "scripts/run_ha_real_matrix.py"
    spec = importlib.util.spec_from_file_location("run_ha_real_matrix", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_dtmf_runner() -> ModuleType:
    path = ROOT / "scripts/run_dtmf_precedence_lab.py"
    spec = importlib.util.spec_from_file_location("run_dtmf_precedence_lab", path)
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
        "no_action",
        "default",
        "answer_ha",
        "decline",
        "busy",
        "cancel",
        "forward",
        "bridge",
    )
    assert runner.ANSWER_CASES == (
        "registered_sip_peer_auto_answer_on_caller_bye",
        "registered_sip_peer_auto_answer_off_callee_bye",
        "initial_delayed_offer_caller_bye",
    )
    assert runner.POLICY_CASES == (
        "browser_phone_auto_answer_enabled_ha_runtime",
        "browser_phone_auto_answer_disabled_ha_runtime",
        "browser_phone_dnd_enabled",
        "browser_phone_dnd_disabled",
    )
    assert runner.CONCURRENCY_CASES == (
        "stale_route_sequence_is_rejected",
        "concurrent_route_requests_remain_distinct",
    )


def test_external_dtmf_contracts_have_real_executors() -> None:
    runner = load_runner()

    for executor, case_name in runner.EXTERNAL_EXECUTABLE_CONTRACTS.values():
        path = ROOT / executor
        assert path.is_file()
        assert case_name in path.read_text(encoding="utf-8")


def test_dtmf_peer_uses_early_media_rfc4733_and_preserves_bind_host(
    tmp_path: Path,
) -> None:
    runner = load_dtmf_runner()
    source = tmp_path / "source"
    source.mkdir()
    (source / "config").write_text(
        "sip_listen             192.0.2.10:15101\n",
        encoding="utf-8",
    )
    (source / "accounts").write_text("stale\n", encoding="utf-8")

    destination, host = runner._caller_config(source, tmp_path / "peer", port=19999)

    assert host == "192.0.2.10"
    assert "192.0.2.10:19999" in (destination / "config").read_text()
    account = (destination / "accounts").read_text()
    assert "dtmfmode=rtp" in account
    assert "dtmfmode=info" not in account


def test_lab_wrapper_stops_tmux_before_collecting_processes() -> None:
    source = (ROOT / "scripts/with_ha_lab_candidate.sh").read_text()

    assert source.index("tmux kill-session") < source.index("mapfile -t pids")
    assert "ps -eo pid=,comm=,args=" in source
    assert '$2 == "hass" || $2 ~ /^python/' in source
    assert 'nc -z "$sip_ready_host" "$sip_ready_port"' in source
    assert 'kill -KILL "${pids[@]}"' in source


def test_phone_policy_helper_uses_public_service_and_restores_state() -> None:
    runner = load_runner()
    calls: list[tuple[str, str, dict[str, object]]] = []

    class Api:
        enabled = False

        def state(self, _entity_id):
            return {"state": "on" if self.enabled else "off"}

        def service(self, domain, service, data):
            calls.append((domain, service, data))
            self.enabled = bool(data["auto_answer"])

    api = Api()
    result = runner._set_phone_policy(
        api,
        service="set_auto_answer",
        field="auto_answer",
        entity_id="switch.casa_auto_answer",
        device_id="phone-device",
        enabled=True,
    )

    assert result["observed"] == "on"
    assert calls == [
        (
            "voip_stack",
            "set_auto_answer",
            {"device_id": "phone-device", "auto_answer": True},
        ),
        (
            "voip_stack",
            "set_auto_answer",
            {"device_id": "phone-device", "auto_answer": False},
        ),
    ]


def test_runner_resolves_phone_policy_entities_instead_of_naming_the_lab() -> None:
    source = (ROOT / "scripts/run_ha_real_matrix.py").read_text(encoding="utf-8")

    assert "config/entity_registry/list" in source
    assert "config/device_registry/list" in source
    assert 'endpoint_id="default"' not in source
    assert "--policy-endpoint-id" in source
    assert "switch.casa_auto_answer" not in source
    assert "switch.voip_stack_lab_do_not_disturb" not in source


def test_peer_live_wrapper_requires_explicit_policy_endpoint(
    tmp_path: Path,
) -> None:
    environment = dict(os.environ)
    environment["HA_PYTHON"] = "/bin/true"
    environment.pop("VOIP_QUALIFICATION_POLICY_ENDPOINT_ID", None)

    completed = subprocess.run(
        [
            str(ROOT / "scripts/run_peer_live_qualification.sh"),
            str(tmp_path),
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )

    assert completed.returncode == 2
    assert "requires an explicit policy endpoint" in completed.stdout
    wrapper = (ROOT / "scripts/run_peer_live_qualification.sh").read_text()
    assert '--policy-endpoint-id "$policy_endpoint_id"' in wrapper
    assert "wildix_trunk_dtmf_route_to_esp" in wrapper
    assert 'VOIP_SECONDARY_EXTENSION="${P4_EXTENSION:-1000}"' in wrapper


def test_qualification_package_uses_public_selection_and_real_ring_delay() -> None:
    package = (
        ROOT / "qualification/home_assistant/voip_qualification.yaml"
    ).read_text(encoding="utf-8")

    assert "action: voip_stack.select_inbound_destination" in package
    assert "continue_on_error" not in package
    assert "for:\n          seconds: 1" in package


def test_runner_rejects_a_stale_installed_automation_package(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = load_runner()
    installed = tmp_path / "voip_qualification.yaml"
    installed.write_text("stale: true\n", encoding="utf-8")
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_ha_real_matrix.py", "--installed-package", str(installed)],
    )

    try:
        runner.main()
    except RuntimeError as error:
        assert "not running the checked-in qualification package" in str(error)
    else:
        raise AssertionError("stale Home Assistant package was accepted")


def test_local_trunk_uses_absolute_sipp_scenario_with_relative_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = load_runner()
    relative_output = Path("test-output")
    workdir = tmp_path / relative_output
    workdir.mkdir()
    commands: list[list[str]] = []

    class Process:
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout):
            return self.returncode

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: type("Result", (), {"stdout": ""})(),
    )
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda command, **kwargs: commands.append(command) or Process(),
    )
    monkeypatch.setattr(runner.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="scenario inspected"):
        with runner._registered_local_trunk(relative_output, 19999):
            raise RuntimeError("scenario inspected")

    scenario = Path(commands[0][commands[0].index("-sf") + 1])
    assert scenario.is_absolute()
    assert scenario == workdir / "local-trunk-register.xml"
