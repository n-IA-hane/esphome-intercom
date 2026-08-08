#!/usr/bin/env python3
"""Run the HIL jobs selected by a qualification plan."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Any, Iterator

import yaml


ROOT = Path(__file__).resolve().parents[1]
HIL_CAPABILITIES = {"hil-s3": "ws3", "hil-p4": "p4"}
SAFE_SNAPSHOT_FIELDS = frozenset(
    {
        "active_calls",
        "active_dialogs",
        "call_scoped_quiescent",
        "media_owners",
        "pending_calls",
        "reserved_rtp_ports",
        "state",
        "task_count",
    }
)


class HilError(RuntimeError):
    """A fail-closed HIL configuration or execution error."""


def _expand(value: str, environment: dict[str, str]) -> str:
    """Expand only explicit environment references and reject missing values."""

    result = value
    while "${" in result:
        prefix, marker, tail = result.partition("${")
        name, closing, suffix = tail.partition("}")
        if not marker or not closing or not name or name not in environment:
            raise HilError(f"missing environment value in hardware map: {value}")
        result = prefix + environment[name] + suffix
    return result


def _command(value: object, environment: dict[str, str]) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise HilError("hardware command must be a non-empty string list")
    return [_expand(item, environment) for item in value]


def _safe_snapshot(payload: object) -> dict[str, object]:
    """Keep only aggregate runtime evidence in the public HIL artifact."""

    if not isinstance(payload, dict):
        raise HilError("snapshot command did not return a JSON object")
    snapshot = {
        key: value
        for key, value in payload.items()
        if key in SAFE_SNAPSHOT_FIELDS and isinstance(value, (bool, int, float, str))
    }
    resources = payload.get("resource_counts")
    if isinstance(resources, dict):
        snapshot["resource_counts"] = {
            str(key): value
            for key, value in resources.items()
            if isinstance(value, (bool, int, float))
        }
    return snapshot


def _is_quiescent(snapshot: dict[str, object]) -> bool:
    explicit = snapshot.get("call_scoped_quiescent")
    if explicit is not None:
        return explicit is True
    return (
        snapshot.get("state") in {None, "idle"}
        and int(snapshot.get("active_calls") or 0) == 0
        and int(snapshot.get("active_dialogs") or 0) == 0
        and int(snapshot.get("media_owners") or 0) == 0
        and int(snapshot.get("reserved_rtp_ports") or 0) == 0
    )


def _run_json(
    command: list[str], environment: dict[str, str], timeout: float
) -> dict[str, object]:
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        raise HilError(f"snapshot command failed with exit code {result.returncode}")
    try:
        return _safe_snapshot(json.loads(result.stdout))
    except json.JSONDecodeError as error:
        raise HilError("snapshot command returned invalid JSON") from error


def _wait_quiescent(
    command: list[str], environment: dict[str, str], timeout: float
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = _run_json(command, environment, min(5.0, timeout))
        if _is_quiescent(last):
            return last
        time.sleep(0.1)
    raise HilError(f"lab did not reach quiescence, last safe snapshot: {last}")


@contextmanager
def _lab_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise HilError("hardware lab is already reserved") from error
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _select_device(devices: object, capability: str) -> tuple[str, dict[str, object]]:
    if not isinstance(devices, dict):
        raise HilError("hardware map does not define devices")
    matches = [
        (str(name), device)
        for name, device in devices.items()
        if isinstance(device, dict)
        and device.get("enabled") is True
        and capability in device.get("capabilities", [])
    ]
    if len(matches) != 1:
        raise HilError(
            f"required capability {capability} needs exactly one enabled device, found {len(matches)}"
        )
    return matches[0]


def _scenario_ids(plan: dict[str, object], executor: str) -> list[str]:
    return [
        str(scenario["id"])
        for scenario in plan.get("scenarios", [])
        if isinstance(scenario, dict) and executor in scenario.get("executors", [])
    ]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _stop_process(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=3)


def _merge_peak(peak: dict[str, object], sample: dict[str, object]) -> None:
    for key, value in sample.items():
        if isinstance(value, dict):
            target = peak.setdefault(key, {})
            if isinstance(target, dict):
                _merge_peak(target, value)
        elif isinstance(value, bool):
            peak[key] = bool(peak.get(key)) or value
        elif isinstance(value, (int, float)):
            peak[key] = max(float(peak.get(key, value)), value)
        else:
            peak[key] = value


def _run_scenario(
    scenario_id: str,
    command: list[str],
    snapshot_command: list[str],
    environment: dict[str, str],
    interval: float,
    snapshot_timeout: float,
    scenario_timeout: float,
) -> dict[str, object]:
    pre = _wait_quiescent(snapshot_command, environment, snapshot_timeout)
    started = time.monotonic()
    peak: dict[str, object] = {}
    sample_count = 0
    timed_out = False
    with (
        tempfile.TemporaryFile(mode="w+t") as stdout_file,
        tempfile.TemporaryFile(mode="w+t") as stderr_file,
    ):
        process = subprocess.Popen(
            command,
            cwd=ROOT,
            env=environment,
            stdout=stdout_file,
            stderr=stderr_file,
            text=True,
        )
        try:
            while process.poll() is None:
                if time.monotonic() - started >= scenario_timeout:
                    _stop_process(process)
                    timed_out = True
                    break
                sample = _run_json(
                    snapshot_command, environment, min(5.0, snapshot_timeout)
                )
                _merge_peak(peak, sample)
                sample_count += 1
                time.sleep(interval)
            process.wait(timeout=1)
        except BaseException:
            _stop_process(process)
            raise
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read()
        stderr = stderr_file.read()
    post = _wait_quiescent(snapshot_command, environment, snapshot_timeout)
    result = {
        "scenario": scenario_id,
        "status": "passed" if process.returncode == 0 and not timed_out else "failed",
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "duration_seconds": round(time.monotonic() - started, 3),
        "snapshots": {"pre": pre, "peak": peak, "post": post},
        "snapshot_samples": sample_count,
        "stdout": {"bytes": len(stdout.encode()), "sha256": _digest(stdout)},
        "stderr": {"bytes": len(stderr.encode()), "sha256": _digest(stderr)},
    }
    return result


def run_hil(
    plan: dict[str, object],
    hardware: dict[str, object],
    *,
    environment: dict[str, str],
    selected_job: str | None = None,
) -> dict[str, object]:
    if hardware.get("schema_version") != 1:
        raise HilError("hardware map schema is unsupported")
    required_jobs = [
        job for job in plan.get("required_jobs", []) if job in HIL_CAPABILITIES
    ]
    if selected_job is not None:
        if selected_job not in HIL_CAPABILITIES:
            raise HilError(f"unsupported HIL job: {selected_job}")
        required_jobs = [job for job in required_jobs if job == selected_job]
    artifact: dict[str, object] = {
        "schema_version": 1,
        "plan_id": plan.get("plan_id"),
        "head": plan.get("head"),
        "status": "skipped" if not required_jobs else "running",
        "jobs": {},
    }
    if not required_jobs:
        artifact["skip_reason"] = "qualification plan does not require hardware"
        return artifact

    lock_value = hardware.get("lock_file")
    if not isinstance(lock_value, str) or not lock_value:
        raise HilError("hardware map does not define lock_file")
    snapshot = hardware.get("snapshot")
    if not isinstance(snapshot, dict):
        raise HilError("hardware map does not define snapshot collection")
    snapshot_command = _command(snapshot.get("command"), environment)
    interval = float(snapshot.get("interval_seconds", 0.25))
    timeout = float(snapshot.get("timeout_seconds", 8))
    if interval <= 0 or timeout <= 0:
        raise HilError("snapshot timing must be positive")

    with _lab_lock(Path(_expand(lock_value, environment))):
        for job in required_jobs:
            executor = HIL_CAPABILITIES[job]
            device_name, device = _select_device(hardware.get("devices"), job)
            volume = float(device.get("volume_percent", 1))
            if volume < 0 or volume > 1:
                raise HilError(
                    f"device {device_name} exceeds the 1 percent volume limit"
                )
            child_environment = dict(environment)
            child_environment["VOIP_TEST_VOLUME_PERCENT"] = str(volume)
            doctor = subprocess.run(
                _command(device.get("doctor"), child_environment),
                cwd=ROOT,
                env=child_environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=timeout,
            )
            if doctor.returncode:
                raise HilError(f"doctor failed for required device {device_name}")
            scenario_ids = _scenario_ids(plan, executor)
            if not scenario_ids:
                raise HilError(
                    f"required job {job} has no planned {executor} scenarios"
                )
            skipped_scenarios = {
                str(scenario["id"]): f"scenario does not require {executor} executor"
                for scenario in plan.get("scenarios", [])
                if isinstance(scenario, dict)
                and executor not in scenario.get("executors", [])
            }
            configured = device.get("scenarios")
            if not isinstance(configured, dict):
                raise HilError(f"device {device_name} has no scenario commands")
            results: list[dict[str, object]] = []
            for scenario_id in scenario_ids:
                scenario = configured.get(scenario_id)
                if not isinstance(scenario, dict):
                    raise HilError(
                        f"required scenario {scenario_id} is not configured for {device_name}"
                    )
                results.append(
                    _run_scenario(
                        scenario_id,
                        _command(scenario.get("command"), child_environment),
                        snapshot_command,
                        child_environment,
                        interval,
                        timeout,
                        float(scenario.get("timeout_seconds", 180)),
                    )
                )
            job_passed = all(result["status"] == "passed" for result in results)
            artifact["jobs"][job] = {
                "status": "passed" if job_passed else "failed",
                "doctor": "passed",
                "device": device_name,
                "capabilities": sorted(
                    str(item) for item in device.get("capabilities", [])
                ),
                "volume_percent": volume,
                "scenarios": results,
                "skipped_scenarios": skipped_scenarios,
            }
            if not job_passed:
                artifact["status"] = "failed"
                return artifact
    artifact["status"] = "passed"
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--hardware-map", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--job", choices=sorted(HIL_CAPABILITIES))
    args = parser.parse_args()
    artifact: dict[str, Any]
    plan: dict[str, Any] = {}
    try:
        plan = json.loads(args.plan.read_text(encoding="utf-8"))
        artifact = run_hil(
            plan,
            yaml.safe_load(args.hardware_map.read_text(encoding="utf-8")),
            environment=dict(os.environ),
            selected_job=args.job,
        )
        status = 0 if artifact["status"] in {"passed", "skipped"} else 2
    except (HilError, OSError, subprocess.SubprocessError, ValueError) as error:
        artifact = {
            "schema_version": 1,
            "plan_id": plan.get("plan_id"),
            "head": plan.get("head"),
            "status": "failed",
            "error": str(error),
        }
        status = 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"hil_artifact={args.output}")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
