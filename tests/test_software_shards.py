"""The software suite shards must partition the selection deterministically."""

from __future__ import annotations

from pathlib import Path
import json
import os
import subprocess
import sys

import pytest
import yaml

from scripts import software_shards as shards

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
TABLE = {
    "split_by_function": ["tests/test_split.py"],
    "external_network": ["tests/test_net.py"],
    "durations": {
        "tests/test_split.py::test_a": 50, "tests/test_split.py::Group": 40,
        "tests/test_big.py": 60, "tests/test_small.py": 5, "tests/test_net.py": 5,
    },
}
NODES = [
    "tests/test_big.py::test_one", "tests/test_big.py::test_two[x-1]",
    "tests/test_new.py::test_unmeasured",
    "tests/test_net.py::test_download",
    "tests/test_small.py::test_one",
    "tests/test_split.py::test_a[1]", "tests/test_split.py::test_a[2::3]",
    "tests/test_split.py::Group::test_b", "tests/test_split.py::Group::test_c",
]


def _manifests(count: int) -> list[dict]:
    return [{"schema": shards.MANIFEST_SCHEMA, "lane": f"shard-{index}-of-{count}",
             "index": index, "count": count, "selection": list(NODES),
             "selected": shards.select(NODES, index, count, TABLE)}
            for index in range(1, count + 1)]


@pytest.mark.parametrize("count", [1, 2, 3, 4])
def test_every_node_runs_in_exactly_one_shard_in_collected_order(count: int) -> None:
    parts = [shards.select(NODES, index, count, TABLE) for index in range(1, count + 1)]
    assert sorted(node for part in parts for node in part) == sorted(NODES)
    assert all(part == [node for node in NODES if node in part] and part for part in parts)
    assert parts == [shards.select(list(NODES), index, count, TABLE)
                     for index in range(1, count + 1)]
    assert shards.verify_manifests(_manifests(count), count=count, table=TABLE) == []


def test_units_keep_files_and_split_functions_together() -> None:
    owners, loads = shards.assign(NODES, 3, TABLE)
    assert set(owners) == {
        "tests/test_big.py", "tests/test_new.py", "tests/test_net.py", "tests/test_small.py",
        "tests/test_split.py::test_a", "tests/test_split.py::Group",
    }
    # The unmeasured file takes the median weight instead of being dropped.
    assert sum(loads) == pytest.approx(60 + 50 + 40 + 5 + 5 + 40)
    assert owners["tests/test_big.py"] != owners["tests/test_split.py::test_a"]


def test_gate_rejects_missing_duplicated_and_divergent_shards(tmp_path: Path) -> None:
    good = _manifests(2)
    assert any("expected manifests" in error
               for error in shards.verify_manifests(good[:1], count=2, table=TABLE))
    lost = json.loads(json.dumps(good))
    moved = lost[0]["selected"].pop()
    assert any("run in no shard" in error for error in shards.verify_manifests(lost))
    lost[1]["selected"].append(moved)
    assert any("committed plan" in error
               for error in shards.verify_manifests(lost, count=2, table=TABLE))
    twice = json.loads(json.dumps(good))
    twice[1]["selected"].append(twice[0]["selected"][0])
    assert any("more than one shard" in error for error in shards.verify_manifests(twice))
    drift = json.loads(json.dumps(good))
    drift[1]["selection"].append("tests/test_late.py::test_x")
    assert any("different full selection" in error for error in shards.verify_manifests(drift))
    assert any("unsharded collection" in error
               for error in shards.verify_manifests(good, selections=[NODES[1:]]))
    report = tmp_path / "report.xml"
    cases = "".join(
        '<testcase classname="{}" name="{}"/>'.format(*shards.junit_identity(node))
        for node in good[0]["selected"][1:])
    report.write_text(f"<testsuites><testsuite>{cases}</testsuite></testsuites>")
    errors = shards.verify_manifests(good, reports={good[0]["lane"]: report})
    assert any("does not match the shard selection" in error for error in errors)
    assert any("missing JUnit report" in error for error in errors)


def test_invalid_shard_requests_are_rejected() -> None:
    for value in ("0/4", "5/4", "1/7", "1", "a/b"):
        with pytest.raises(shards.ShardError):
            shards.parse_shard(value)
    with pytest.raises(shards.ShardError):
        shards.assign(NODES[:1], 2, TABLE)


def test_committed_table_and_workflow_agree() -> None:
    table = shards.load_table()
    assert all((ROOT / path).is_file()
               for key in ("split_by_function", "external_network") for path in table[key])
    # Local external-network lanes share the host stack, so they must not bind ports.
    binds = ("socket", ".bind(", "create_server", "create_datagram_endpoint", "start_server")
    assert not [path for path in table["external_network"]
                if any(word in (ROOT / path).read_text() for word in binds)]
    jobs = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]
    count = int(jobs["software"]["env"]["SOFTWARE_SHARDS"])
    assert jobs["software"]["strategy"]["matrix"]["shard"] == list(range(1, count + 1))
    assert jobs["software"]["strategy"]["fail-fast"] is False
    assert 1 < count <= shards.MAX_SHARDS
    gate = jobs["software-suite"]
    # The release verifier requires one job named exactly "software".
    assert gate["name"] == "software" and gate["needs"] == "software"
    assert gate["env"]["SOFTWARE_SHARDS"] == str(count)
    commands = "\n".join(step.get("run", "") for step in gate["steps"])
    assert "software_shards.py verify" in commands and "--selection" in commands
    assert "check_test_results.py" in commands
    assert 'test "$SHARD_RESULT" = success' in commands


def test_runner_forwards_one_shard_and_refuses_other_suites(tmp_path: Path) -> None:
    recorder = tmp_path / "python"
    recorded = tmp_path / "arguments.json"
    recorder.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "if sys.argv[1:3] == ['-m', 'pytest']:\n"
        "    open(os.environ['RECORDED_ARGS'], 'w').write(json.dumps(sys.argv[3:]))\n"
    )
    recorder.chmod(0o755)
    env = {**os.environ, "PYTHON": str(recorder), "RECORDED_ARGS": str(recorded)}
    runner = str(ROOT / "scripts/test_suite.sh")
    result = subprocess.run(
        [runner, "software-full", "--shard", "2/4", "--shard-manifest", "out.json"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    arguments = json.loads(recorded.read_text())
    assert arguments[arguments.index("--shard") + 1] == "2/4"
    assert arguments[arguments.index("--shard-manifest") + 1] == "out.json"
    assert arguments[arguments.index("-p") + 1] == "scripts.software_shards_plugin"
    recorded.unlink()
    plain = subprocess.run([runner, "software-full"], cwd=ROOT, env=env,
                           capture_output=True, text=True, timeout=10)
    assert plain.returncode == 0, plain.stderr
    assert not any("shard" in value for value in json.loads(recorded.read_text()))
    refused = subprocess.run([runner, "fast", "--shard", "1/4"], cwd=ROOT, env=env,
                             capture_output=True, text=True, timeout=10)
    assert refused.returncode == 2 and "only to software-full" in refused.stderr
