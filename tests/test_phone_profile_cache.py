"""Verified content-addressed assets are immutable and safe across readers."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import stat
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import zipfile

from .test_groups import _load_module

catalog = _load_module("phone_profile_catalog")


def test_parallel_private_writes_publish_without_shared_temporary_file(tmp_path, monkeypatch):
    target = tmp_path / "digest.zip"
    barrier = threading.Barrier(2)
    original = Path.replace

    def replace(source, destination):
        if destination == target:
            barrier.wait(timeout=3)
        return original(source, destination)

    monkeypatch.setattr(Path, "replace", replace)
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(catalog._private_write, target, b"verified fixture") for _ in range(2)]
        for job in jobs:
            job.result(timeout=4)
    assert target.read_bytes() == b"verified fixture"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert list(tmp_path.iterdir()) == [target]


async def test_valid_cached_profile_never_fetches_or_rewrites_assets(tmp_path, monkeypatch):
    async def executor(function, *args):
        return await asyncio.to_thread(function, *args)

    hass = SimpleNamespace(config=SimpleNamespace(path=lambda *parts: str(tmp_path.joinpath(*parts))),
                           async_add_executor_job=executor)
    owner = catalog.PhoneProfileCatalog(hass)
    profile = {"schema_version": 1, "id": "fanvil/fixture", "capabilities": {},
               "outputs": [{"path": "{mac}.cfg", "format": "cfg", "template": "templates/main.jinja2"}]}
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, value in {"profile.json": json.dumps(profile), "LICENSES/MPL-1.1.txt": "fixture",
                            "NOTICE": "fixture", "templates/main.jinja2": "fixture"}.items():
            archive.writestr(name, value)
    raw = buffer.getvalue()
    digest = sha256(raw).hexdigest()
    manifest = {"schema_version": 1, "profiles": [{"id": profile["id"], "status": "software_validated",
        "capabilities": {}, "bundle": {"path": "bundles/fixture.zip", "sha256": digest, "size": len(raw)}}]}
    manifest["catalog_revision"] = sha256(json.dumps(manifest, sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    pin = "a" * 40
    owner.root.mkdir(parents=True)
    (owner.root / (pin + ".json")).write_text(json.dumps(manifest))
    (owner.root / "bundles").mkdir()
    (owner.root / "bundles" / (digest + ".zip")).write_bytes(raw)
    write = Mock(wraps=catalog._private_write)
    monkeypatch.setattr(catalog, "_private_write", write)
    owner._fetch = AsyncMock(side_effect=AssertionError("Valid cache must not fetch"))
    assert (await owner.async_profile(profile["id"], commit=pin))[0] == profile
    owner._fetch.assert_not_awaited()
    write.assert_not_called()
