"""pytest plugin that runs one deterministic shard of the collected suite.

Load it with ``-p scripts.software_shards_plugin``. It never changes what is
collected or how a test runs: after marker selection it deselects the tests
owned by other shards and records the full and the kept node IDs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import software_shards


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("software shards")
    group.addoption("--shard", default=None, metavar="INDEX/COUNT",
                    help="run only this deterministic shard of the selection")
    group.addoption("--shard-network", default="all",
                    choices=("all", "isolated", "external"),
                    help="keep only tests that can (isolated) or cannot (external) "
                         "run without public network access")
    group.addoption("--shard-manifest", default=None, metavar="PATH",
                    help="write the full selection and this shard's node IDs")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    shard = config.getoption("--shard")
    network = config.getoption("--shard-network")
    manifest = config.getoption("--shard-manifest")
    if shard is None and network == "all" and manifest is None:
        return
    try:
        table = software_shards.load_table()
        selection = [item.nodeid for item in items]
        index, count = software_shards.parse_shard(shard) if shard else (1, 1)
        loads = [0.0]
        keep = set(selection)
        if shard:
            _, loads = software_shards.assign(selection, count, table)
            keep = set(software_shards.select(selection, index, count, table))
    except (OSError, ValueError) as error:
        raise pytest.UsageError(str(error)) from error
    if network != "all":
        external = network == "external"
        keep = {nodeid for nodeid in keep
                if software_shards.needs_external_network(nodeid, table) == external}
    dropped = [item for item in items if item.nodeid not in keep]
    if dropped:
        items[:] = [item for item in items if item.nodeid in keep]
        config.hook.pytest_deselected(items=dropped)
    if manifest:
        lane = software_shards.lane_name(index, count, network)
        path = Path(manifest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "schema": software_shards.MANIFEST_SCHEMA,
            "lane": lane,
            "index": index,
            "count": count,
            "network": network,
            "predicted_seconds": [round(load, 1) for load in loads],
            "selection": selection,
            "selected": [item.nodeid for item in items],
        }, indent=1) + "\n")
