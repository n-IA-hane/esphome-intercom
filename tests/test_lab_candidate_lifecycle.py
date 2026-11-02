"""Exercise the lab transaction with an isolated manager and no real services."""

import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/with_ha_lab_candidate.sh"
ORIGINAL_PACKAGE = b"# original\r\nkeep: true\n\x00trailing\n"
CANDIDATE_PACKAGE = b"candidate: true\n"

FAKE_TOOL = r'''
import json, os, pathlib, stat, sys
name = pathlib.Path(sys.argv[0]).name
state_path = pathlib.Path(os.environ["FAKE_STATE"])
state = json.loads(state_path.read_text())
link = pathlib.Path(os.environ["FAKE_LINK"])
package = pathlib.Path(os.environ["FAKE_PACKAGE"])
event = {"tool": name, "args": sys.argv[1:], "active": state["active"],
         "link": os.readlink(link), "package": package.read_bytes().hex(),
         "mode": stat.S_IMODE(package.stat().st_mode)}
with open(os.environ["FAKE_TRACE"], "a") as out:
    out.write(json.dumps(event) + "\n")
def save():
    state_path.write_text(json.dumps(state))
if name == "systemctl":
    op = sys.argv[1]
    assert sys.argv[-1] == os.environ["FAKE_SERVICE"], sys.argv
    if op == "show":
        prop = next(a.split("=", 1)[1] for a in sys.argv if a.startswith("--property="))
        print(state["load"] if prop == "LoadState" else state["active"])
    elif op in ("start", "stop"):
        assert state["load"] == "loaded"
        state[op] += 1
        failure = int(os.environ.get("FAKE_FAIL_" + op.upper() + "_AT", "0")) == state[op]
        state["active"] = "active" if op == "start" else "inactive"
        if failure and op == "start":
            state["active"] = "failed"
        if failure and op == "stop" and os.environ.get("FAKE_STOP_STAYS_ACTIVE"):
            state["active"] = "active"
        save()
        sys.exit(19 if failure else 0)
    else:
        raise AssertionError(sys.argv)
elif name in ("curl", "nc"):
    candidate = os.readlink(link) == os.environ["FAKE_CANDIDATE"]
    blocked = os.environ.get("FAKE_NOT_READY") == ("http" if name == "curl" else "sip")
    sys.exit(1 if state["active"] != "active" or (candidate and blocked) else 0)
elif name == "sleep":
    pass
elif name in ("install", "cp", "mv"):
    state[name] += 1
    save()
    if int(os.environ.get("FAKE_FAIL_" + name.upper() + "_AT", "0")) == state[name]:
        sys.exit(23)
    os.execv("/usr/bin/" + name, [name, *sys.argv[1:]])
else:
    raise AssertionError("Unmanaged process operation: " + name)
'''

COMMAND = r'''
import fcntl, json, os, pathlib, signal, stat, time
state = json.loads(pathlib.Path(os.environ["FAKE_STATE"]).read_text())
link = pathlib.Path(os.environ["FAKE_LINK"])
package = pathlib.Path(os.environ["FAKE_PACKAGE"])
assert state["active"] == "active"
assert os.readlink(link) == os.environ["FAKE_CANDIDATE"]
assert package.read_bytes() == b"candidate: true\n"
assert stat.S_IMODE(package.stat().st_mode) == 0o644
with open(os.environ["HA_LAB_LOCK_FILE"], "a") as lock:
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        pass
    else:
        raise AssertionError("The lab lock was not held during the command")
if os.environ.get("FAKE_COMMAND_WAIT"):
    def stopped(_signal, _frame):
        pathlib.Path(os.environ["FAKE_COMMAND_STOPPED"]).write_text("stopped")
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stopped)
pathlib.Path(os.environ["FAKE_COMMAND_RAN"]).write_text(str(os.getpid()))
if os.environ.get("FAKE_COMMAND_WAIT"):
    while True:
        time.sleep(0.01)
raise SystemExit(int(os.environ.get("FAKE_COMMAND_STATUS", "0")))
'''


@pytest.fixture
def lab(tmp_path):
    repo = tmp_path / "candidate repo"
    script = repo / "scripts/with_ha_lab_candidate.sh"
    script.parent.mkdir(parents=True)
    shutil.copy2(HELPER, script)
    component = repo / "custom_components/voip_stack"
    component.mkdir(parents=True)
    source_package = repo / "qualification/home_assistant/voip_qualification.yaml"
    source_package.parent.mkdir(parents=True)
    source_package.write_bytes(CANDIDATE_PACKAGE)
    lab_root = tmp_path / "lab"
    link = lab_root / "config/custom_components/voip_stack"
    link.parent.mkdir(parents=True)
    (lab_root / "original component").mkdir()
    link.symlink_to("../../original component")
    package = lab_root / "config/packages/voip_qualification.yaml"
    package.parent.mkdir(parents=True)
    package.write_bytes(ORIGINAL_PACKAGE)
    package.chmod(0o640)
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"load": "loaded", "active": "active",
                                     "start": 0, "stop": 0, "install": 0, "cp": 0, "mv": 0}))
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    tool = fake_bin / "fake-tool"
    tool.write_text(f"#!{sys.executable}\n" + FAKE_TOOL)
    tool.chmod(0o755)
    for name in ("systemctl", "curl", "nc", "sleep", "install", "cp", "mv",
                 "tmux", "ps", "pgrep", "pkill", "killall"):
        (fake_bin / name).symlink_to(tool.name)
    command = tmp_path / "command.py"
    command.write_text(COMMAND)
    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "HA_LAB_ROOT": str(lab_root), "HA_LAB_LOCK_FILE": str(tmp_path / "lab.lock"),
        "FAKE_SERVICE": "home-assistant-voip-lab.service",
        "FAKE_STATE": str(state_file), "FAKE_LINK": str(link), "FAKE_PACKAGE": str(package),
        "FAKE_CANDIDATE": str(component), "FAKE_TRACE": str(tmp_path / "trace.jsonl"),
        "FAKE_COMMAND_RAN": str(tmp_path / "command-ran"),
        "FAKE_COMMAND_STOPPED": str(tmp_path / "command-stopped"),
    }
    for name in ("HA_LAB_SERVICE", "HA_LAB_URL", "HA_LAB_SIP_READY_HOST", "HA_LAB_SIP_READY_PORT"):
        env.pop(name, None)
    return {"root": tmp_path, "script": script, "command": command, "env": env,
            "state_file": state_file, "link": link, "package": package}


def state(lab):
    return json.loads(lab["state_file"].read_text())


def configure(lab, **changes):
    lab["state_file"].write_text(json.dumps(state(lab) | changes))


def events(lab):
    path = Path(lab["env"]["FAKE_TRACE"])
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def run(lab):
    return subprocess.run(["bash", str(lab["script"]), "--", sys.executable, str(lab["command"])],
                          env=lab["env"], capture_output=True, text=True, timeout=15)


def assert_restored(lab, initially_active):
    assert os.readlink(lab["link"]) == "../../original component"
    assert lab["package"].read_bytes() == ORIGINAL_PACKAGE
    assert stat.S_IMODE(lab["package"].stat().st_mode) == 0o640
    assert state(lab)["active"] == ("active" if initially_active else "inactive")
    assert not list(lab["link"].parent.glob(".voip-stack-candidate.*"))
    with open(lab["env"]["HA_LAB_LOCK_FILE"], "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("initially_active", [False, True])
@pytest.mark.parametrize("command_status", [0, 37])
def test_command_runs_on_ready_candidate_and_original_state_returns(lab, initially_active, command_status):
    configure(lab, active="active" if initially_active else "inactive")
    lab["env"]["FAKE_COMMAND_STATUS"] = str(command_status)
    result = run(lab)
    assert result.returncode == command_status, result.stderr
    assert Path(lab["env"]["FAKE_COMMAND_RAN"]).is_file()
    assert_restored(lab, initially_active)
    operations = [e for e in events(lab) if e["tool"] == "systemctl" and e["args"][0] in ("start", "stop")]
    assert [e["args"][0] for e in operations] == (["stop", "start", "stop", "start"] if initially_active else ["stop", "start", "stop"])
    assert operations[0]["link"] == "../../original component"
    assert operations[1]["link"] == lab["env"]["FAKE_CANDIDATE"]
    if initially_active:
        assert operations[-1]["package"] == ORIGINAL_PACKAGE.hex()
        assert operations[-1]["mode"] == 0o640
    assert any(e["tool"] == "curl" and "http://127.0.0.1:18123/" in e["args"] for e in events(lab))
    assert any(e["tool"] == "nc" and e["args"] == ["-z", "127.0.0.1", "15060"] for e in events(lab))


def test_custom_existing_service_is_the_only_owner(lab):
    lab["env"]["HA_LAB_SERVICE"] = lab["env"]["FAKE_SERVICE"] = "private-qualification.service"
    result = run(lab)
    assert result.returncode == 0, result.stderr
    assert_restored(lab, True)


def test_missing_unit_fails_before_file_or_service_mutations(lab):
    configure(lab, load="not-found")
    result = run(lab)
    assert result.returncode == 2
    assert os.readlink(lab["link"]) == "../../original component"
    assert lab["package"].read_bytes() == ORIGINAL_PACKAGE
    assert state(lab)["start"] == state(lab)["stop"] == state(lab)["cp"] == 0
    assert not Path(lab["env"]["HA_LAB_LOCK_FILE"]).exists()
    assert not Path(lab["env"]["FAKE_COMMAND_RAN"]).exists()


@pytest.mark.parametrize("initially_active", [False, True])
@pytest.mark.parametrize("failure", ["backup", "install", "start", "http", "sip"])
def test_setup_and_readiness_failures_restore_files_and_initial_state(lab, initially_active, failure):
    configure(lab, active="active" if initially_active else "inactive")
    if failure in ("http", "sip"):
        lab["env"]["FAKE_NOT_READY"] = failure
    else:
        key = "CP" if failure == "backup" else failure.upper()
        lab["env"][f"FAKE_FAIL_{key}_AT"] = "1"
    result = run(lab)
    assert result.returncode != 0
    assert not Path(lab["env"]["FAKE_COMMAND_RAN"]).exists()
    assert_restored(lab, initially_active)


@pytest.mark.parametrize("signal_number,expected", [(signal.SIGINT, 130), (signal.SIGTERM, 143)])
@pytest.mark.parametrize("initially_active", [False, True])
def test_signals_stop_only_owned_command_and_restore_lab(lab, signal_number, expected, initially_active):
    configure(lab, active="active" if initially_active else "inactive")
    lab["env"]["FAKE_COMMAND_WAIT"] = "1"
    process = subprocess.Popen(["bash", str(lab["script"]), "--", sys.executable, str(lab["command"])],
                               env=lab["env"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 8
        while not Path(lab["env"]["FAKE_COMMAND_RAN"]).exists():
            assert process.poll() is None
            assert time.monotonic() < deadline
            time.sleep(0.01)
        process.send_signal(signal_number)
        _, stderr = process.communicate(timeout=8)
        assert process.returncode == expected, stderr
        assert Path(lab["env"]["FAKE_COMMAND_STOPPED"]).is_file()
        assert_restored(lab, initially_active)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def test_lock_contention_leaves_existing_service_and_files_untouched(lab):
    with open(lab["env"]["HA_LAB_LOCK_FILE"], "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run(lab)
    assert result.returncode == 2
    assert state(lab)["start"] == state(lab)["stop"] == state(lab)["cp"] == 0
    assert_restored(lab, True)


def test_failed_cleanup_is_nonzero_even_after_successful_command(lab):
    lab["env"]["FAKE_FAIL_STOP_AT"] = "2"
    result = run(lab)
    assert result.returncode != 0
    assert Path(lab["env"]["FAKE_COMMAND_RAN"]).is_file()
    assert_restored(lab, True)


def test_initial_stop_failure_does_not_install_candidate_and_restores_active_owner(lab):
    lab["env"]["FAKE_FAIL_STOP_AT"] = "1"
    result = run(lab)
    assert result.returncode != 0
    assert state(lab)["install"] == state(lab)["mv"] == 0
    assert not Path(lab["env"]["FAKE_COMMAND_RAN"]).exists()
    assert_restored(lab, True)


def test_cleanup_start_failure_restores_original_files_and_is_reported(lab):
    lab["env"]["FAKE_FAIL_START_AT"] = "2"
    result = run(lab)
    assert result.returncode != 0
    assert os.readlink(lab["link"]) == "../../original component"
    assert lab["package"].read_bytes() == ORIGINAL_PACKAGE
    assert stat.S_IMODE(lab["package"].stat().st_mode) == 0o640
    assert state(lab)["active"] == "failed"


def test_cleanup_does_not_rewire_files_under_an_owner_that_could_not_stop(lab):
    lab["env"].update(FAKE_FAIL_STOP_AT="2", FAKE_STOP_STAYS_ACTIVE="1")
    result = run(lab)
    assert result.returncode != 0
    assert "Original lab files preserved for recovery" in result.stderr
    backups = list(lab["link"].parent.glob(".voip-stack-candidate.*"))
    assert len(backups) == 1
    assert (backups[0] / "package.original").read_bytes() == ORIGINAL_PACKAGE
    assert os.readlink(backups[0] / "component.original") == "../../original component"
    assert os.readlink(lab["link"]) == lab["env"]["FAKE_CANDIDATE"]
    assert state(lab)["active"] == "active"
