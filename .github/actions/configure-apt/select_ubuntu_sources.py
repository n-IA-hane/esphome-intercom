#!/usr/bin/env python3
"""Copy only Ubuntu archive entries into a job-owned APT source directory."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlsplit


def ubuntu_uri(value: str) -> bool:
    """Retain the hosted image's Ubuntu mirror-list transport and archive URIs."""
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        return False
    if parsed.scheme == "mirror+file":
        return parsed.netloc in {"", "localhost"} and parsed.path == "/etc/apt/apt-mirrors.txt"
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/")
    if parsed.scheme in {"mirror+http", "mirror+https"}:
        return host == "mirrors.ubuntu.com" and path == "/mirrors.txt"
    if parsed.scheme not in {"http", "https"}:
        return False
    if host == "ports.ubuntu.com":
        return path == "/ubuntu-ports"
    official = (host in {"archive.ubuntu.com", "security.ubuntu.com", "old-releases.ubuntu.com"}
                or host.endswith(".archive.ubuntu.com"))
    return path == "/ubuntu" and (official or host == "mirrors.edge.kernel.org")


def fields(stanza: str) -> dict[str, str]:
    result = {}
    current = ""
    for line in stanza.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1].isspace() and current:
            result[current] += " " + line.strip()
            continue
        key, separator, value = line.partition(":")
        if not separator or not key.strip():
            raise ValueError("Malformed APT source stanza")
        current = key.strip().lower()
        if current in result:
            raise ValueError("Duplicate APT source field")
        result[current] = value.strip()
    return result


def verify_trust(options: dict[str, str]) -> None:
    for key in ("trusted", "allow-insecure", "allow-weak", "allow-downgrade-to-insecure"):
        if options.get(key, "").lower() in {"yes", "true", "1"}:
            raise ValueError("Ubuntu source explicitly bypasses signature verification")


def select_deb822(content: str):
    selected, records = [], []
    for stanza in re.split(r"(?:\r?\n)[ \t]*(?:\r?\n)", content):
        values = fields(stanza)
        if values.get("enabled", "yes").lower() == "no":
            continue
        uris = values.get("uris", "").split()
        retained = [uri for uri in uris if ubuntu_uri(uri)]
        if not retained:
            continue
        verify_trust(values)
        if retained != uris:
            # Filter the copy only. Do not rewrite provider-owned files, keys,
            # suites, components, architectures or other source options.
            stanza = re.sub(r"^URIs:[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*",
                            "URIs: " + " ".join(retained), stanza, flags=re.I | re.M)
        selected.append(stanza.strip("\r\n") + "\n\n")
        if "deb" in values.get("types", "").split():
            records.append({"uris": retained, "suites": values.get("suites", "").split(),
                            "components": values.get("components", "").split()})
    return "".join(selected), records


def select_legacy(content: str):
    selected, records = [], []
    pattern = re.compile(r"^\s*(deb(?:-src)?)\s+(?:\[([^]]*)\]\s+)?(\S+)\s+(\S+)\s+([^#\r\n]*)")
    for line in content.splitlines(keepends=True):
        match = pattern.match(line)
        if match is None or not ubuntu_uri(match[3]):
            continue
        options = dict(item.split("=", 1) for item in (match[2] or "").split() if "=" in item)
        verify_trust({key.lower(): value for key, value in options.items()})
        selected.append(line if line.endswith("\n") else line + "\n")
        if match[1] == "deb":
            records.append({"uris": [match[3]], "suites": [match[4]], "components": match[5].split()})
    return "".join(selected), records


def prepare(apt_root: Path, output: Path, codename: str) -> dict:
    if not re.fullmatch(r"[a-z][a-z0-9-]*", codename):
        raise ValueError("Invalid Ubuntu release codename")
    apt_root, output = apt_root.resolve(), output.resolve()
    if output == apt_root or apt_root in output.parents:
        raise ValueError("Job source copies must be outside the provider APT directory")
    if any(char in str(output) for char in '\\"\r\n'):
        raise ValueError("Job source directory cannot be represented by APT configuration")
    if output.exists() and any(output.iterdir()):
        raise ValueError("Job source directory must be empty")
    inputs = [apt_root / "sources.list"]
    inputs += sorted(path for path in (apt_root / "sources.list.d").glob("*")
                     if re.fullmatch(r"[A-Za-z0-9_.-]+\.(?:list|sources)", path.name))
    copies, records = [], []
    for path in inputs:
        if not path.is_file():
            continue
        original = path.read_bytes()
        select = select_deb822 if path.suffix == ".sources" else select_legacy
        content, observed = select(original.decode())
        if content:
            copies.append((path, content.encode(), hashlib.sha256(original).hexdigest()))
            records.extend(observed)
    pockets = {codename, codename + "-updates", codename + "-security"}
    available = {suite: set() for suite in pockets}
    for record in records:
        for suite in pockets.intersection(record["suites"]):
            available[suite].update(record["components"])
    if any(not {"main", "universe"} <= components for components in available.values()):
        raise ValueError("Ubuntu binary sources must include main and universe for release, updates and security")
    sources = output / "sources.list.d"
    sources.mkdir(parents=True)
    copied = []
    for index, (source, content, original_digest) in enumerate(copies):
        target = sources / f"{index:03d}-{source.name}"
        target.write_bytes(content)
        copied.append({"source": str(source), "source_sha256": original_digest,
                       "copy": target.name, "copy_sha256": hashlib.sha256(content).hexdigest()})
    empty = output / "empty.list"
    empty.write_text("")
    configuration = output / "apt.conf"
    configuration.write_text(
        f'Dir::Etc::sourcelist "{empty}";\n'
        f'Dir::Etc::sourceparts "{sources}";\n'
    )
    result = {"codename": codename, "binary_sources": records, "copies": copied,
              "effective_source_list": str(empty), "effective_source_parts": str(sources)}
    (output / "selection.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apt-root", type=Path, default=Path("/etc/apt"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--codename", required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(prepare(args.apt_root, args.output, args.codename), indent=2))
    except (OSError, UnicodeError, ValueError) as error:
        parser.exit(2, f"Ubuntu APT selection failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
