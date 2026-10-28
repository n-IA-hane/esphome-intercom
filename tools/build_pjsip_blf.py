#!/usr/bin/env python3
"""Build the pinned, independent PJSIP BLF peer in a reusable disk cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

REVISION = "6cab30cec44af36ba7c8a45ff7af2f74a8ed288c"
VERSION = "2.16"
DEFAULT_CACHE = Path.home() / ".cache" / "voip-stack" / "pjproject-2.16"
CONFIGURE = [
    "--disable-pjsua2", "--disable-sound", "--disable-video", "--disable-resample",
    "--disable-speex-aec", "--disable-g711-codec", "--disable-l16-codec",
    "--disable-gsm-codec", "--disable-g722-codec", "--disable-g7221-codec",
    "--disable-speex-codec", "--disable-ilbc-codec", "--disable-opus",
    "--disable-libsrtp", "--disable-libyuv", "--disable-libwebrtc", "--disable-upnp",
    "--disable-ssl", "CFLAGS=-O2 -DNDEBUG",
]


def build(cache: Path, jobs: int) -> Path:
    cache.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).with_name("pjsip_blf_client.c")
    binary = cache / "voip-stack-blf-client"
    identity = {"version": VERSION, "revision": REVISION,
                "client_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "configure": CONFIGURE, "driver_build_format": 1}
    manifest = cache / "voip-stack-blf-build.json"
    previous = json.loads(manifest.read_text()) if manifest.is_file() else {}
    revision_file = cache / "voip-stack-upstream-revision"
    if (cache / "configure").is_file() and (
            not revision_file.is_file() or revision_file.read_text().strip() != REVISION):
        raise RuntimeError(f"Refusing an unverified PJSIP source cache: {cache}")
    if previous and previous.get("configure") != CONFIGURE:
        raise RuntimeError(f"PJSIP configure options changed; select a fresh disk cache: {cache}")
    if binary.is_file() and previous == identity:
        return binary
    if not (cache / "configure").is_file():
        archive = cache / "source.tar.gz"
        url = f"https://codeload.github.com/pjsip/pjproject/tar.gz/{REVISION}"
        with urllib.request.urlopen(url, timeout=60) as response, archive.open("wb") as stream:
            shutil.copyfileobj(response, stream)
        with tarfile.open(archive) as tar:
            prefix = f"pjproject-{REVISION}/"
            members = tar.getmembers()
            if not all(member.name == prefix[:-1] or member.name.startswith(prefix) for member in members):
                raise RuntimeError("Unexpected PJSIP archive root")
            for member in members:
                if member.name == prefix[:-1]:
                    continue
                member.name = member.name.removeprefix(prefix)
                if member.name:
                    tar.extract(member, cache, filter="data")
    revision_file.write_text(REVISION + "\n")
    with (cache / "voip-stack-blf-build.log").open("w") as log:
        if not (cache / "build.mak").is_file():
            subprocess.run(["./configure", *CONFIGURE], cwd=cache, stdout=log,
                           stderr=subprocess.STDOUT, check=True)
        if not previous:
            subprocess.run(["make", "dep"], cwd=cache, stdout=log, stderr=subprocess.STDOUT, check=True)
            subprocess.run(["make", f"-j{jobs}", "lib"], cwd=cache, stdout=log,
                           stderr=subprocess.STDOUT, check=True)
        suffix = next((cache / "pjsip/lib").glob("libpjsip-simple-*.a")).name
        suffix = suffix.removeprefix("libpjsip-simple-").removesuffix(".a")
        flags = ["-DPJ_AUTOCONF=1", "-O2", "-g", "-Wall", "-Wextra", "-Werror"]
        includes = [f"-I{cache / name / 'include'}" for name in ("pjsip", "pjlib", "pjlib-util", "pjmedia")]
        libraries = [f"-L{cache / name / 'lib'}" for name in ("pjsip", "pjlib", "pjlib-util")]
        subprocess.run([os.environ.get("CC", "cc"), *flags, *includes, str(source), *libraries,
                        *(f"-l{name}-{suffix}" for name in ("pjsip-ua", "pjsip-simple", "pjsip", "pjlib-util", "pj")),
                        "-luuid", "-lm", "-lrt", "-lpthread", "-o", str(binary)],
                       stdout=log, stderr=subprocess.STDOUT, check=True)
    manifest.write_text(json.dumps(identity, indent=2) + "\n")
    return binary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, default=DEFAULT_CACHE)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    print(build(args.cache.resolve(), args.jobs))


if __name__ == "__main__":
    main()
