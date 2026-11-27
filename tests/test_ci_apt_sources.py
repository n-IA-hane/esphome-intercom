"""Select Ubuntu sources on isolated files, without running APT or sudo."""

from pathlib import Path
import json
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
ACTION = ROOT / ".github/actions/configure-apt"
SELECTOR = ACTION / "select_ubuntu_sources.py"
pytestmark = pytest.mark.architecture


def ubuntu(*, uri="mirror+file:/etc/apt/apt-mirrors.txt", suites="resolute resolute-updates resolute-security",
           components="main restricted universe multiverse", extra=""):
    return (f"Types: deb deb-src\nURIs: {uri}\nSuites: {suites}\nComponents: {components}\n"
            f"Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg\n{extra}")


def run_selector(tmp_path, sources, *, legacy=""):
    apt = tmp_path / "etc/apt"
    parts = apt / "sources.list.d"
    parts.mkdir(parents=True)
    for name, content in sources.items():
        (parts / name).write_bytes(content.encode())
    if legacy:
        (apt / "sources.list").write_bytes(legacy.encode())
    before = {path.relative_to(apt): path.read_bytes() for path in apt.rglob("*") if path.is_file()}
    output = tmp_path / "job-sources"
    result = subprocess.run([sys.executable, str(SELECTOR), "--apt-root", str(apt),
                             "--codename", "resolute", "--output", str(output)],
                            capture_output=True, text=True, check=False)
    after = {path.relative_to(apt): path.read_bytes() for path in apt.rglob("*") if path.is_file()}
    assert before == after, "Provider-owned source files were modified"
    return result, output


def copied_sources(output):
    return "".join(path.read_text() for path in (output / "sources.list.d").iterdir())


def test_ubuntu_selection_excludes_microsoft_and_preserves_all_provider_files(tmp_path):
    microsoft = ("Types: deb\nURIs: https://packages.microsoft.com/ubuntu/26.04/prod\n"
                 "Suites: resolute\nComponents: main\nSigned-By: /usr/share/keyrings/microsoft.gpg\n")
    vendor = "deb [signed-by=/etc/vendor.gpg] https://vendor.example.invalid/packages stable main\n"
    result, output = run_selector(tmp_path, {"ubuntu.sources": ubuntu(), "microsoft-prod.sources": microsoft,
                                           "vendor.list": vendor})
    assert result.returncode == 0, result.stderr
    selected = copied_sources(output)
    assert selected == ubuntu() + "\n"
    assert "packages.microsoft.com" not in selected and "vendor.example.invalid" not in selected
    report = json.loads((output / "selection.json").read_text())
    assert report["binary_sources"] == [{"uris": ["mirror+file:/etc/apt/apt-mirrors.txt"],
        "suites": ["resolute", "resolute-updates", "resolute-security"],
        "components": ["main", "restricted", "universe", "multiverse"]}]
    assert (output / "empty.list").read_bytes() == b""
    assert (output / "apt.conf").read_text() == (
        f'Dir::Etc::sourcelist "{output / "empty.list"}";\n'
        f'Dir::Etc::sourceparts "{output / "sources.list.d"}";\n')


def test_mixed_deb822_stanzas_keep_keys_architectures_security_and_updates(tmp_path):
    archive = ubuntu(uri="https://archive.ubuntu.com/ubuntu/", suites="resolute resolute-updates",
                     extra="Architectures: amd64 amd64v3\n")
    security = ubuntu(uri="https://security.ubuntu.com/ubuntu/", suites="resolute-security")
    vendor = "Types: deb\nURIs: https://packages.microsoft.com/ubuntu/26.04/prod\nSuites: resolute\nComponents: main\n"
    result, output = run_selector(tmp_path, {"mixed.sources": archive + "\n" + vendor + "\n" + security})
    assert result.returncode == 0, result.stderr
    selected = copied_sources(output)
    assert "Architectures: amd64 amd64v3" in selected
    assert selected.count("Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg") == 2
    assert "resolute-security" in selected and "resolute-updates" in selected
    assert "packages.microsoft.com" not in selected


def test_mixed_uri_field_filters_only_the_copy_and_preserves_signature_key(tmp_path):
    source = ubuntu(uri="https://archive.ubuntu.com/ubuntu/\n https://packages.microsoft.com/ubuntu/26.04/prod")
    result, output = run_selector(tmp_path, {"mixed.sources": source})
    assert result.returncode == 0, result.stderr
    assert copied_sources(output) == ubuntu(uri="https://archive.ubuntu.com/ubuntu/") + "\n"


def test_legacy_sources_keep_each_required_pocket_and_signed_by(tmp_path):
    options = "[arch=amd64 signed-by=/usr/share/keyrings/ubuntu-archive-keyring.gpg]"
    legacy = "# Original file remains unchanged\n" + "".join(
        f"deb {options} https://archive.ubuntu.com/ubuntu {suite} main universe\n"
        for suite in ["resolute", "resolute-updates", "resolute-security"])
    legacy += "deb https://packages.microsoft.com/ubuntu/26.04/prod resolute main\n"
    result, output = run_selector(tmp_path, {}, legacy=legacy)
    assert result.returncode == 0, result.stderr
    selected = copied_sources(output)
    assert selected.count(options) == 3
    assert "packages.microsoft.com" not in selected


@pytest.mark.parametrize("uri", ["https://azure.archive.ubuntu.com/ubuntu/",
    "https://us.archive.ubuntu.com/ubuntu/", "https://mirrors.edge.kernel.org/ubuntu/",
    "mirror+https://mirrors.ubuntu.com/mirrors.txt", "http://ports.ubuntu.com/ubuntu-ports/"])
def test_official_archive_and_existing_mirror_transports_are_retained(tmp_path, uri):
    result, output = run_selector(tmp_path, {"ubuntu.sources": ubuntu(uri=uri)})
    assert result.returncode == 0, result.stderr
    assert uri in copied_sources(output)


@pytest.mark.parametrize("source", [
    ubuntu(suites="resolute resolute-updates"),
    ubuntu(suites="resolute resolute-security"),
    ubuntu(components="main restricted"),
    ubuntu(extra="Enabled: no\n"),
    ubuntu(uri="https://archive.ubuntu.com.attacker.invalid/ubuntu"),
], ids=["missing-security", "missing-updates", "missing-universe", "disabled", "lookalike-host"])
def test_incomplete_or_non_ubuntu_selection_fails_before_writing_output(tmp_path, source):
    result, output = run_selector(tmp_path, {"ubuntu.sources": source})
    assert result.returncode != 0
    assert "release, updates and security" in result.stderr
    assert not output.exists()


@pytest.mark.parametrize("option", ["Trusted: yes", "Allow-Insecure: yes", "Allow-Weak: true",
                                    "Allow-Downgrade-To-Insecure: 1"])
def test_source_signature_bypass_is_never_copied(tmp_path, option):
    result, output = run_selector(tmp_path, {"ubuntu.sources": ubuntu(extra=option + "\n")})
    assert result.returncode != 0 and "bypasses signature verification" in result.stderr
    assert not output.exists()


def test_job_dropin_applies_under_sudo_without_apt_config_environment():
    action = yaml.safe_load((ACTION / "action.yml").read_text())
    script = action["runs"]["steps"][0]["run"]
    assert 'sudo install -m 0644 "$ubuntu_apt/apt.conf" /etc/apt/apt.conf.d/zz-voip-stack-sources' in script
    assert 'apt-config dump Dir::Etc::sourcelist Dir::Etc::sourceparts' in script
    assert 'test "$(apt-config shell selected_list Dir::Etc::sourcelist)"' in script
    assert 'test "$(apt-config shell selected_parts Dir::Etc::sourceparts)"' in script
    assert 'cat "$ubuntu_apt/selection.json"' in script
    assert "APT_CONFIG=" not in script
    assert 'Acquire::Retries "1";' in script
    assert 'Acquire::http::Timeout "15";' in script and 'Acquire::https::Timeout "15";' in script
    assert "AllowUnauthenticated" not in script and "Verify-Peer" not in script
