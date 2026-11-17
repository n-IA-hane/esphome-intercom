"""Execute the CI mirror adjustment on private files, without invoking APT."""

from pathlib import Path
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
MIRROR = b"https://mirrors.edge.kernel.org/ubuntu/"
KERNEL = MIRROR + b"\tpriority:0\n"


def adjust(path):
    action = yaml.safe_load((ROOT / ".github/actions/configure-apt/action.yml").read_text())
    script = action["runs"]["steps"][0]["run"]
    source = script.split("sudo python3 - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
    target = 'Path("/etc/apt/apt-mirrors.txt")'
    assert source.count(target) == 1
    source = source.replace(target, f"Path({str(path)!r})")
    return subprocess.run([sys.executable, "-c", source], capture_output=True, text=True)


@pytest.mark.parametrize("original", [
    b"http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n"
    b"https://archive.ubuntu.com/ubuntu/\tpriority:2\n"
    b"https://security.ubuntu.com/ubuntu/\tpriority:3\n",
    # Synthetic cloud-shaped record, not the undisclosed URI of the AWS job.
    b"https://aws-mirror.example.invalid/ubuntu/\tpriority:1\n",
    b"# Keep comments and provider metadata verbatim\r\n"
    b"https://provider.example.invalid/ubuntu/\tpriority:0 arch:amd64 suite:resolute\r\n"
    b"file:/srv/provider/mirror\tpriority:2\r\n"
    b"\nhttps://fallback.example.invalid/ubuntu/\tcomponent:main",
], ids=["azure", "aws-representative-not-observed", "metadata-and-byte-preservation"])
def test_adds_only_owned_mirror_and_is_idempotent(tmp_path, original):
    path = tmp_path / "mirrors.txt"
    path.write_bytes(original)
    result = adjust(path)
    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == KERNEL + original
    result = adjust(path)
    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == KERNEL + original
    assert list(tmp_path.iterdir()) == [path]


def test_normalizes_only_existing_kernel_priority_and_retains_other_metadata(tmp_path):
    path = tmp_path / "mirrors.txt"
    provider = b"https://provider.example.invalid/ubuntu/\tpriority:1 arch:amd64\n"
    path.write_bytes(provider + MIRROR + b"\tarch:amd64 priority:8 suite:resolute\n")
    expected = provider + MIRROR + b"\tpriority:0 arch:amd64 suite:resolute\n"
    result = adjust(path)
    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == expected
    result = adjust(path)
    assert result.returncode == 0, result.stderr
    assert path.read_bytes() == expected


@pytest.mark.parametrize("original", [b"", b" \t\n", b"# no mirror records\n"])
def test_empty_mirror_file_fails_without_mutation(tmp_path, original):
    path = tmp_path / "mirrors.txt"
    path.write_bytes(original)
    result = adjust(path)
    assert result.returncode != 0 and "no mirror entries" in result.stderr
    assert path.read_bytes() == original


def test_missing_mirror_file_is_not_created(tmp_path):
    path = tmp_path / "missing.txt"
    result = adjust(path)
    assert result.returncode != 0 and "mirror file is missing" in result.stderr
    assert not path.exists()


def test_malformed_uri_syntax_fails_without_mutation(tmp_path):
    path = tmp_path / "mirrors.txt"
    original = b"not-a-uri\n"
    path.write_bytes(original)
    result = adjust(path)
    assert result.returncode != 0 and "invalid URI syntax" in result.stderr
    assert path.read_bytes() == original
