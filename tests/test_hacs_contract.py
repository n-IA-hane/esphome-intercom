from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess
import sys
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUILD_ARCHIVE = ROOT / "scripts" / "build_hacs_zip.py"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
ARCHIVE_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
ARCHIVE_MODE = stat.S_IFREG | 0o644


def test_hacs_repo_exposes_only_voip_stack_domain() -> None:
    components = [
        path.name
        for path in (ROOT / "custom_components").iterdir()
        if path.is_dir() and not path.name.startswith("__")
    ]

    assert components == ["voip_stack"]


def test_hacs_and_manifest_names_match_current_domain() -> None:
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    manifest = json.loads(
        (ROOT / "custom_components" / "voip_stack" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert hacs["name"] == "VoIP Stack"
    assert hacs["zip_release"] is True
    assert hacs["filename"] == "voip_stack.zip"
    assert manifest["domain"] == "voip_stack"
    assert manifest["name"] == "VoIP Stack"


def test_release_workflow_promotes_the_exact_qualified_hacs_asset() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "release:" in workflow
    assert "types: [published]" in workflow
    assert "workflow_dispatch:" in workflow
    assert "contents: write" in workflow
    assert "python scripts/build_hacs_zip.py" not in workflow
    assert "scripts/verify_release_candidate.py" in workflow
    assert 'qualification-summary-$TAG_SHA' in workflow
    assert 'qualification-result-release-package-$TAG_SHA' in workflow
    assert 'gh release upload "$RELEASE_TAG" voip_stack.zip' in workflow
    assert "--clobber" in workflow
    assert "GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}" in workflow


def _module_exists(module_parts: list[str], members: set[str]) -> bool:
    module_path = "/".join(module_parts)
    return f"{module_path}.py" in members or f"{module_path}/__init__.py" in members


def _package_symbols(package_parts: list[str], sources: dict[str, str]) -> set[str]:
    init_path = "/".join([*package_parts, "__init__.py"])
    if not package_parts:
        init_path = "__init__.py"
    source = sources.get(init_path)
    if source is None:
        return set()

    symbols: set[str] = set()
    for node in ast.walk(ast.parse(source, filename=init_path)):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            symbols.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            symbols.update(
                target.id for target in targets if isinstance(target, ast.Name)
            )
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            symbols.update(
                alias.asname or alias.name.split(".")[0] for alias in node.names
            )
    return symbols


def _assert_relative_imports_resolve(
    members: set[str], sources: dict[str, str]
) -> None:
    for filename, source in sources.items():
        path = Path(filename)
        package = list(path.parent.parts)
        if path.name == "__init__.py":
            package = list(path.parent.parts)
        for node in ast.walk(ast.parse(source, filename=filename)):
            if not isinstance(node, ast.ImportFrom) or node.level == 0:
                continue
            assert node.level <= len(package) + 1, (
                f"relative import escapes archive package in {filename}:{node.lineno}"
            )
            base = package[: len(package) - (node.level - 1)]
            if node.module:
                target = [*base, *node.module.split(".")]
                assert _module_exists(target, members), (
                    f"missing relative import {node.module!r} from {filename}:{node.lineno}"
                )
                continue
            for alias in node.names:
                target = [*base, *alias.name.split(".")]
                assert _module_exists(
                    target, members
                ) or alias.name in _package_symbols(base, sources), (
                    f"missing relative import {alias.name!r} from {filename}:{node.lineno}"
                )


def _assert_javascript_imports_resolve(
    members: set[str], sources: dict[str, str]
) -> None:
    import_pattern = re.compile(r"(?:\bfrom\s+|\bimport\s*\()\s*[`\"'](\./[^?`\"']+)")
    for filename, source in sources.items():
        parent = Path(filename).parent
        for relative in import_pattern.findall(source):
            target = (parent / relative).as_posix()
            assert target in members, (
                f"missing JavaScript import {relative!r} from {filename}"
            )


def test_hacs_release_archive_is_flat_complete_small_and_reproducible(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    for output in (first, second):
        subprocess.run(
            [sys.executable, str(BUILD_ARCHIVE), "--output", str(output)],
            cwd=ROOT,
            check=True,
        )

    assert (
        hashlib.sha256(first.read_bytes()).digest()
        == hashlib.sha256(second.read_bytes()).digest()
    )
    assert first.stat().st_size < 2 * 1024 * 1024

    with zipfile.ZipFile(first) as archive:
        assert archive.testzip() is None
        infos = archive.infolist()
        names = archive.namelist()
        members = set(names)
        assert len(names) == len(members)
        assert names == sorted(names)
        assert "LICENSE" in members
        assert archive.read("LICENSE") == (ROOT / "LICENSE").read_bytes()
        assert "manifest.json" in members
        assert "__init__.py" in members
        assert all(info.date_time == ARCHIVE_TIMESTAMP for info in infos)
        assert all(info.create_system == 3 for info in infos)
        assert all(info.compress_type == zipfile.ZIP_DEFLATED for info in infos)
        assert all((info.external_attr >> 16) == ARCHIVE_MODE for info in infos)
        assert all(not info.is_dir() for info in infos)
        assert all(not Path(name).is_absolute() for name in names)
        assert all(".." not in Path(name).parts for name in names)
        assert all(
            not any(part.startswith(".") for part in Path(name).parts) for name in names
        )
        assert not any(name.startswith("custom_components/") for name in names)
        assert not any("__pycache__" in name for name in names)
        assert not any(
            name.endswith(
                (".bak", ".log", ".pcap", ".pyc", ".pyo", ".swp", ".tmp", ".wav")
            )
            for name in names
        )
        assert not any(
            name.startswith(("assets/", "docs/", "tests/")) for name in names
        )

        sources = {
            name: archive.read(name).decode("utf-8")
            for name in names
            if name.endswith(".py")
        }
        javascript_sources = {
            name: archive.read(name).decode("utf-8")
            for name in names
            if name.endswith(".js")
        }

    for filename, source in sources.items():
        compile(source, filename, "exec")
    _assert_relative_imports_resolve(members, sources)
    _assert_javascript_imports_resolve(members, javascript_sources)


def _minimal_integration_source(tmp_path: Path) -> Path:
    source = tmp_path / "integration"
    source.mkdir()
    (source / "manifest.json").write_text("{}\n", encoding="utf-8")
    (source / "__init__.py").write_text('"""Test integration."""\n', encoding="utf-8")
    return source


@pytest.mark.parametrize(
    ("relative_name", "expected_error"),
    [
        (".env", "hidden paths"),
        ("private/.credentials", "hidden paths"),
        ("call.pcap", "junk suffix"),
        ("capture.wav", "junk suffix"),
        ("debug.log", "junk suffix"),
        ("module.pyc", "junk suffix"),
        ("notes.bin", "unsupported release file type"),
        ("Thumbs.db", "junk file"),
    ],
)
def test_hacs_release_archive_rejects_unsafe_files(
    tmp_path: Path,
    relative_name: str,
    expected_error: str,
) -> None:
    source = _minimal_integration_source(tmp_path)
    unsafe = source / relative_name
    unsafe.parent.mkdir(parents=True, exist_ok=True)
    unsafe.write_text("not release content\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(BUILD_ARCHIVE),
            "--source",
            str(source),
            "--output",
            str(tmp_path / "unsafe.zip"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert not (tmp_path / "unsafe.zip").exists()


def test_hacs_release_archive_rejects_symlinks(tmp_path: Path) -> None:
    source = _minimal_integration_source(tmp_path)
    (source / "linked.py").symlink_to(source / "__init__.py")

    result = subprocess.run(
        [
            sys.executable,
            str(BUILD_ARCHIVE),
            "--source",
            str(source),
            "--output",
            str(tmp_path / "symlink.zip"),
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "cannot contain symlinks" in result.stderr
    assert not (tmp_path / "symlink.zip").exists()


def test_hacs_release_archive_ignores_generated_python_cache(tmp_path: Path) -> None:
    source = _minimal_integration_source(tmp_path)
    cache = source / "__pycache__"
    cache.mkdir()
    (cache / "module.cpython-314.pyc").write_bytes(b"generated cache")
    output = tmp_path / "clean.zip"

    subprocess.run(
        [
            sys.executable,
            str(BUILD_ARCHIVE),
            "--source",
            str(source),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        check=True,
    )

    with zipfile.ZipFile(output) as archive:
        assert not any("__pycache__" in name for name in archive.namelist())
