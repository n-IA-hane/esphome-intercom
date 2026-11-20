#!/usr/bin/env python3
"""Deterministic, duration-balanced shards for the software-full suite.

The serial suite stays the reference. A shard only deselects tests owned by
another shard, so collection, import order and markers are unchanged.

Planning rules:
  * the unit is the test file, so module fixtures and loaders stay together;
  * a file listed under ``split_by_function`` is planned per top-level test
    function or class (all parameters of one function stay together);
  * units are placed largest first on the least loaded shard, ties broken by
    name and shard number, so the plan depends only on the collected node IDs
    and the committed duration table;
  * a unit without a measured duration gets the median known weight. It is
    always assigned, never dropped.

Commands:
  plan              print the plan for node IDs read from a collection log
  verify            prove that shard manifests partition the full selection
  update-durations  rebuild the duration table from JUnit reports
  run               run all shards locally, each in its own network namespace
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
DURATIONS = Path(__file__).with_name("software_shard_durations.json")
MANIFEST_SCHEMA = 1
MAX_SHARDS = 6
PLUGIN = "scripts.software_shards_plugin"
# Same visual escape pytest applies to JUnit attribute values.
_JUNIT_ILLEGAL = re.compile(
    "[^\u0009\u000a\u000d -~\u0080-퟿-�က0-ჿff]"
)


class ShardError(ValueError):
    """A shard plan, manifest or report violates the partition contract."""


def parse_shard(value: str) -> tuple[int, int]:
    """Parse ``INDEX/COUNT`` with a one-based index."""
    match = re.fullmatch(r"([0-9]+)/([0-9]+)", value)
    if not match:
        raise ShardError(f"Shard must be INDEX/COUNT, got {value!r}")
    index, count = int(match.group(1)), int(match.group(2))
    if not 1 <= count <= MAX_SHARDS or not 1 <= index <= count:
        raise ShardError(
            f"Shard {value} is outside 1..COUNT with COUNT at most {MAX_SHARDS}"
        )
    return index, count


def load_table(path: Path = DURATIONS) -> dict:
    """Load the committed duration table."""
    table = json.loads(path.read_text())
    durations = table.get("durations")
    if not isinstance(durations, dict) or not all(
        isinstance(value, (int, float)) and value >= 0 for value in durations.values()
    ):
        raise ShardError(f"{path}: durations must map units to seconds")
    for key in ("split_by_function", "external_network"):
        if not isinstance(table.get(key, []), list):
            raise ShardError(f"{path}: {key} must be a list of test files")
    return table


def file_of(nodeid: str) -> str:
    return nodeid.split("::", 1)[0]


def unit_of(nodeid: str, split_files: frozenset[str] | set[str]) -> str:
    """Return the planning unit that owns a node ID."""
    path = file_of(nodeid)
    if path not in split_files:
        return path
    parts = nodeid.partition("[")[0].split("::")
    return "::".join(parts[:2]) if len(parts) > 1 else path


def assign(nodeids: list[str], count: int, table: dict) -> tuple[dict[str, int], list[float]]:
    """Map every planning unit to a one-based shard and return predicted loads."""
    split_files = frozenset(table.get("split_by_function", []))
    durations = table["durations"]
    units = sorted({unit_of(nodeid, split_files) for nodeid in nodeids})
    if len(units) < count:
        raise ShardError(f"{len(units)} test units cannot fill {count} shards")
    known = [float(durations[unit]) for unit in units if unit in durations]
    default = statistics.median(known) if known else 1.0
    weights = {unit: float(durations.get(unit, default)) for unit in units}
    loads = [0.0] * count
    owners: dict[str, int] = {}
    for unit in sorted(units, key=lambda unit: (-weights[unit], unit)):
        target = min(range(count), key=lambda shard: (loads[shard], shard))
        loads[target] += weights[unit]
        owners[unit] = target + 1
    return owners, loads


def select(nodeids: list[str], index: int, count: int, table: dict) -> list[str]:
    """Return the node IDs of one shard in their collected order."""
    owners, _ = assign(nodeids, count, table)
    split_files = frozenset(table.get("split_by_function", []))
    return [nodeid for nodeid in nodeids if owners[unit_of(nodeid, split_files)] == index]


def lane_name(index: int, count: int, network: str = "all") -> str:
    return f"shard-{index}-of-{count}" + ("" if network == "all" else f"-{network}")


def needs_external_network(nodeid: str, table: dict) -> bool:
    return file_of(nodeid) in set(table.get("external_network", []))


def junit_identity(nodeid: str) -> tuple[str, str]:
    """Return the (classname, name) pair pytest writes for a node ID."""
    path, bracket, parameters = nodeid.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    names[-1] += bracket + parameters

    def escape(text: str) -> str:
        return _JUNIT_ILLEGAL.sub(
            lambda found: f"#x{ord(found.group()):02X}"
            if ord(found.group()) <= 0xFF else f"#x{ord(found.group()):04X}",
            text,
        )

    return escape(".".join(names[:-1])), escape(names[-1])


def junit_cases(path: Path) -> Counter[tuple[str, str]]:
    root = ET.parse(path).getroot()
    if root.tag not in {"testsuites", "testsuite"}:
        raise ShardError(f"{path}: not a JUnit test report")
    return Counter(
        (case.get("classname", ""), case.get("name", "")) for case in root.iter("testcase")
    )


def read_collection(path: Path) -> list[str]:
    """Read node IDs from ``pytest --collect-only -q`` output."""
    nodeids = [
        line.strip() for line in path.read_text().splitlines()
        if "::" in line and line.startswith("tests/")
    ]
    if not nodeids:
        raise ShardError(f"{path}: no collected node IDs")
    return nodeids


def _duplicates(values: list[str]) -> list[str]:
    return sorted(value for value, seen in Counter(values).items() if seen > 1)


def verify_manifests(
    manifests: list[dict],
    *,
    count: int | None = None,
    table: dict | None = None,
    selections: list[list[str]] | None = None,
    reports: dict[str, Path] | None = None,
    allow_empty: bool = False,
) -> list[str]:
    """Return every violation of the exactly-once contract."""
    errors: list[str] = []
    if not manifests:
        return ["no shard manifests"]
    reference = manifests[0]["selection"]
    duplicated = _duplicates(reference)
    if duplicated:
        errors.append(f"full selection repeats {len(duplicated)} node IDs: {duplicated[:3]}")
    lanes = [manifest["lane"] for manifest in manifests]
    if _duplicates(lanes):
        errors.append(f"duplicate shard manifests: {_duplicates(lanes)}")
    for manifest in manifests:
        if manifest.get("schema") != MANIFEST_SCHEMA:
            errors.append(f"{manifest['lane']}: unsupported manifest schema")
        if manifest["selection"] != reference:
            errors.append(f"{manifest['lane']}: collected a different full selection")
    for position, selection in enumerate(selections or [], 1):
        if selection != reference:
            missing = sorted(set(selection) - set(reference))[:3]
            extra = sorted(set(reference) - set(selection))[:3]
            errors.append(
                f"unsharded collection {position} differs from the shard selection "
                f"(only unsharded: {missing}, only sharded: {extra})"
            )
    if count is not None:
        expected = [lane_name(index, count) for index in range(1, count + 1)]
        if sorted(lanes) != sorted(expected):
            errors.append(f"expected manifests {expected}, got {sorted(lanes)}")
        elif table is not None:
            for manifest in manifests:
                planned = select(reference, manifest["index"], count, table)
                if planned != manifest["selected"]:
                    errors.append(f"{manifest['lane']}: selection differs from the committed plan")
    owned = [nodeid for manifest in manifests for nodeid in manifest["selected"]]
    repeated = _duplicates(owned)
    if repeated:
        errors.append(f"{len(repeated)} node IDs run in more than one shard: {repeated[:3]}")
    unassigned = sorted(set(reference) - set(owned))
    if unassigned:
        errors.append(f"{len(unassigned)} node IDs run in no shard: {unassigned[:3]}")
    foreign = sorted(set(owned) - set(reference))
    if foreign:
        errors.append(f"{len(foreign)} node IDs are outside the full selection: {foreign[:3]}")
    for manifest in manifests:
        lane = manifest["lane"]
        if not manifest["selected"]:
            if not allow_empty:
                errors.append(f"{lane}: selects no tests")
            continue
        if reports is None:
            continue
        report = reports.get(lane)
        if report is None or not report.is_file():
            errors.append(f"{lane}: missing JUnit report")
            continue
        try:
            executed = junit_cases(report)
        except (ET.ParseError, ShardError) as error:
            errors.append(f"{lane}: {error}")
            continue
        expected_cases = Counter(junit_identity(nodeid) for nodeid in manifest["selected"])
        if executed != expected_cases:
            absent = sorted((expected_cases - executed).elements())[:3]
            surplus = sorted((executed - expected_cases).elements())[:3]
            errors.append(
                f"{lane}: JUnit report does not match the shard selection "
                f"(not reported: {absent}, unexpected: {surplus})"
            )
    return errors


def load_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text())
    for key in ("lane", "selection", "selected"):
        if key not in manifest:
            raise ShardError(f"{path}: manifest lacks {key}")
    return manifest


def _results(directory: Path, lane: str) -> tuple[Path, Path]:
    return directory / f"software-{lane}.json", directory / f"software-{lane}.xml"


def command_plan(args: argparse.Namespace) -> int:
    table = load_table(args.durations)
    nodeids = read_collection(args.collection)
    owners, loads = assign(nodeids, args.count, table)
    split_files = frozenset(table.get("split_by_function", []))
    unknown = sorted(unit for unit in owners if unit not in table["durations"])
    for index, load in enumerate(loads, 1):
        cases = sum(1 for nodeid in nodeids if owners[unit_of(nodeid, split_files)] == index)
        units = sum(1 for owner in owners.values() if owner == index)
        print(f"shard {index}/{args.count}: predicted {load:7.1f} s, {units} units, {cases} tests")
    print(f"total predicted {sum(loads):.1f} s, {len(nodeids)} tests, {len(owners)} units")
    print(f"units without a measured duration: {len(unknown)}")
    for unit in unknown:
        print(f"  {unit} -> shard {owners[unit]}")
    if args.list:
        for unit in sorted(owners, key=lambda unit: (owners[unit], unit)):
            print(f"{owners[unit]} {unit}")
    return 0


def command_verify(args: argparse.Namespace) -> int:
    table = load_table(args.durations)
    paths = sorted(args.results_dir.glob("software-shard-*.json"))
    manifests = [load_manifest(path) for path in paths]
    reports = None
    if not args.no_reports:
        reports = {manifest["lane"]: _results(args.results_dir, manifest["lane"])[1]
                   for manifest in manifests}
    selections = [read_collection(path) for path in args.selection]
    errors = verify_manifests(
        manifests, count=args.count, table=table, selections=selections, reports=reports,
    )
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    total = len(manifests[0]["selection"])
    for manifest in sorted(manifests, key=lambda manifest: manifest["lane"]):
        print(f"{manifest['lane']}: {len(manifest['selected'])} tests")
    print(
        f"{len(manifests)} shards cover {total} selected tests exactly once"
        + (f", matching {len(selections)} unsharded collections" if selections else "")
        + ("" if reports is None else ", and every JUnit report matches its shard")
    )
    return 0


def command_update_durations(args: argparse.Namespace) -> int:
    table = load_table(args.durations)
    split_files = set(table.get("split_by_function", []))
    durations: Counter[str] = Counter()
    for report in args.reports:
        for case in ET.parse(report).getroot().iter("testcase"):
            parts = case.get("classname", "").split(".")
            for end in range(len(parts), 0, -1):
                path = "/".join(parts[:end]) + ".py"
                if (ROOT / path).is_file():
                    break
            else:
                raise ShardError(f"{report}: no test file for {case.get('classname')}")
            rest = parts[end:] + [case.get("name", "").partition("[")[0]]
            unit = f"{path}::{rest[0]}" if path in split_files else path
            durations[unit] += float(case.get("time", "0"))
    table["durations"] = {unit: round(durations[unit], 1) for unit in sorted(durations)}
    table["source"] = args.source
    args.durations.write_text(json.dumps(table, indent=2) + "\n")
    print(f"{args.durations}: {len(durations)} units, {sum(durations.values()):.1f} s")
    return 0


def _isolation_prefix() -> list[str]:
    """Own network namespace with loopback, keeping the caller's user identity."""
    if shutil.which("unshare") is None or shutil.which("ip") is None:
        raise ShardError("Parallel shards need unshare and ip for network isolation")
    script = (
        "ip link set lo up && "
        f"exec unshare --user --map-user={os.getuid()} --map-group={os.getgid()} -- \"$@\""
    )
    prefix = ["unshare", "--user", "--map-root-user", "--net", "--", "sh", "-c", script, "sh"]
    probe = subprocess.run([*prefix, "true"], capture_output=True, text=True, check=False)
    if probe.returncode != 0:
        raise ShardError(
            "Parallel shards need unprivileged network namespaces: " + probe.stderr.strip()
        )
    return prefix


def command_run(args: argparse.Namespace) -> int:
    table = load_table(args.durations)
    parse_shard(f"1/{args.count}")
    if not args.pytest_command:
        raise ShardError("run needs the pytest command after --")
    prefix = _isolation_prefix()
    results = args.results_dir.resolve()
    work = args.work_dir.resolve()
    results.mkdir(parents=True, exist_ok=True)
    for stale in results.glob("software-shard-*"):
        stale.unlink()
    # A namespace has loopback only. Files that download from public hosts keep
    # the host network stack; the table lists them and they bind no ports.
    networks = ("isolated", "external") if table.get("external_network") else ("isolated",)
    lanes: list[tuple[str, list[str], bool]] = [
        (lane_name(index, args.count, network),
         ["--shard", f"{index}/{args.count}", "--shard-network", network],
         network == "isolated")
        for index in range(1, args.count + 1) for network in networks
    ]
    started = time.monotonic()
    running: list[tuple[str, subprocess.Popen, float]] = []
    try:
        for lane, selection, isolated in lanes:
            lane_work = work / lane
            shutil.rmtree(lane_work, ignore_errors=True)
            (lane_work / "tmp").mkdir(parents=True)
            manifest, report = _results(results, lane)
            command = [
                *(prefix if isolated else []), *args.pytest_command,
                "-p", PLUGIN, *selection, "--shard-manifest", str(manifest),
                f"--junitxml={report}", f"--basetemp={lane_work / 'pytest'}",
            ]
            log = (results / f"software-{lane}.log").open("w")
            process = subprocess.Popen(
                command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                env={**os.environ, "TMPDIR": str(lane_work / "tmp")},
                start_new_session=True,
            )
            log.close()
            running.append((lane, process, time.monotonic()))
        status = 0
        pending = dict((lane, (process, begun)) for lane, process, begun in running)
        while pending:
            for lane, (process, begun) in list(pending.items()):
                code = process.poll()
                if code is None:
                    continue
                del pending[lane]
                manifest = _results(results, lane)[0]
                if code == 5 and manifest.is_file() and not load_manifest(manifest)["selected"]:
                    code = 0  # this lane owns no test of its shard
                status = status or code
                tail = (results / f"software-{lane}.log").read_text().strip().splitlines()[-1:]
                print(f"{lane}: exit {code} after {time.monotonic() - begun:.0f} s: "
                      + (tail[0] if tail else "no output"), flush=True)
            time.sleep(0.5)
    except BaseException:
        for _, process, _ in running:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        raise
    print(f"all shards finished in {time.monotonic() - started:.0f} s wall time", flush=True)
    manifests = []
    for lane, _, _ in lanes:
        manifest, _ = _results(results, lane)
        if not manifest.is_file():
            print(f"{lane}: no manifest, see {results / f'software-{lane}.log'}", file=sys.stderr)
            return status or 1
        manifests.append(load_manifest(manifest))
    errors = verify_manifests(
        manifests, reports={lane: _results(results, lane)[1] for lane, _, _ in lanes},
        allow_empty=True,
    )
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        return status or 1
    print(f"{len(lanes)} lanes cover {len(manifests[0]['selection'])} selected tests exactly once")
    if not args.keep_work and status == 0:
        for lane, _, _ in lanes:
            shutil.rmtree(work / lane, ignore_errors=True)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--durations", type=Path, default=DURATIONS)
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan")
    plan.add_argument("--count", type=int, required=True)
    plan.add_argument("--list", action="store_true", help="also print every unit owner")
    plan.add_argument("collection", type=Path)
    plan.set_defaults(handler=command_plan)

    verify = commands.add_parser("verify")
    verify.add_argument("--count", type=int, required=True)
    verify.add_argument("--results-dir", type=Path, required=True)
    verify.add_argument("--selection", type=Path, action="append", default=[],
                        help="unsharded pytest --collect-only -q output to compare")
    verify.add_argument("--no-reports", action="store_true",
                        help="check collection manifests only")
    verify.set_defaults(handler=command_verify)

    update = commands.add_parser("update-durations")
    update.add_argument("--source", required=True, help="where the reports come from")
    update.add_argument("reports", nargs="+", type=Path)
    update.set_defaults(handler=command_update_durations)

    run = commands.add_parser("run")
    run.add_argument("--count", type=int, required=True)
    run.add_argument("--results-dir", type=Path, required=True)
    run.add_argument("--work-dir", type=Path, required=True)
    run.add_argument("--keep-work", action="store_true")
    run.add_argument("pytest_command", nargs=argparse.REMAINDER)
    run.set_defaults(handler=command_run)

    args = parser.parse_args(argv)
    if args.command == "run" and args.pytest_command[:1] == ["--"]:
        args.pytest_command = args.pytest_command[1:]
    try:
        return args.handler(args)
    except (OSError, ET.ParseError, ShardError, json.JSONDecodeError) as error:
        print(error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
