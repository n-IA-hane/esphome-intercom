#!/usr/bin/env python3
"""Switch maintained ESPHome YAMLs between local checkouts and one remote ref."""

from pathlib import Path
import argparse
import re
import subprocess
import sys

ROOT = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
VOICE_PE = ROOT / "yamls/experimental/home-assistant-voice-pe/home-assistant-voice-pe-voip.yaml"
CAMERA_URL = "github://Psix-anp/esphome-esp-video-camera"
REPOS = {
    "ext_components_source": ("github://n-IA-hane/esphome-intercom", ROOT),
    "voip_stack_components_source": ("github://n-IA-hane/esphome-voip-stack", ROOT.parent / "esphome-voip-stack"),
    "audio_stack_components_source": ("github://n-IA-hane/esphome-audio-stack", ROOT.parent / "esphome-audio-stack"),
    "runtime_controller_components_source": ("github://n-IA-hane/esphome-runtime-controller", ROOT.parent / "esphome-runtime-controller"),
}
CAMERA_ROOT = ROOT.parent / "esphome-esp-video-camera"
URLS = {url: path for url, path in REPOS.values()}


def yaml_files():
    files = []
    for path in (ROOT / "yamls").rglob("*.yaml"):
        if ".esphome" in path.parts or path.name == "secrets.yaml":
            continue
        if path.is_relative_to(ROOT / "yamls/debug") or path.name.endswith("_NOT_READY.yaml"):
            continue
        files.append(path)
    return sorted(files)


def camera_files():
    return sorted(
        path for base in (ROOT / "yamls", ROOT / "packages")
        for path in base.rglob("*.yaml")
        if ".esphome" not in path.parts
        and re.search(r"(?m)^\s*components:\s*\[esp_video_camera\]\s*$", path.read_text())
    )


def selected(files, only):
    if not only:
        return files
    target = Path(only)
    target = (ROOT / target if not target.is_absolute() else target).resolve()
    return [path for path in files if path.resolve() == target]


def relative(source, target):
    return subprocess.check_output(
        ["realpath", "--relative-to", str(source), str(target)], text=True
    ).strip()


def repo_for_path(target):
    for url, root in sorted(URLS.items(), key=lambda item: len(str(item[1])), reverse=True):
        try:
            target.relative_to(root.resolve())
            return url, root.resolve()
        except ValueError:
            pass
    return None


def rewrite_package(line, path, ref):
    match = re.match(r"^(\s+[A-Za-z_][A-Za-z0-9_]*:\s*)(!include\s+\S+|\S+)(\s*)$", line)
    if not match:
        return line
    prefix, value, suffix = match.groups()
    if ref == "local":
        for url, repo_root in URLS.items():
            marker = f"{url}/"
            if value.startswith(marker) and "@" in value:
                inner = value[len(marker):].rsplit("@", 1)[0]
                return f"{prefix}!include {relative(path.parent, repo_root / inner)}{suffix}"
        return line
    if value.startswith("!include "):
        target = (path.parent / value.removeprefix("!include ")).resolve()
        repo = repo_for_path(target)
        if repo:
            url, repo_root = repo
            return f"{prefix}{url}/{target.relative_to(repo_root).as_posix()}@{ref}{suffix}"
        return line
    for url in URLS:
        marker = f"{url}/"
        if value.startswith(marker) and "@" in value:
            inner = value[len(marker):].rsplit("@", 1)[0]
            return f"{prefix}{url}/{inner}@{ref}{suffix}"
    return line


def rewrite_yaml(path, ref):
    if ref == "local" and path.resolve() == VOICE_PE.resolve():
        return
    output = []
    for line in path.read_text().splitlines():
        match = re.match(r'^(\s*)([A-Za-z_][A-Za-z0-9_]*_components_source):\s*"[^"]*"\s*$', line)
        if match and match.group(2) in REPOS:
            indent, key = match.groups()
            url, repo_root = REPOS[key]
            value = relative(path.parent, repo_root / "esphome/components") if ref == "local" else f"{url}@{ref}"
            output.append(f'{indent}{key}: "{value}"')
        elif re.match(r'^\s*assets_base:\s*"[^"]*"\s*$', line):
            value = relative(path.parent, ROOT) + "/" if ref == "local" else f"https://github.com/n-IA-hane/esphome-intercom/raw/{ref}/"
            output.append(f'  assets_base: "{value}"')
        else:
            output.append(rewrite_package(line, path, ref))
    path.write_text("\n".join(output) + "\n")


def rewrite_camera(path, ref):
    lines = path.read_text().splitlines()
    source = relative(path.parent, CAMERA_ROOT / "components") if ref == "local" else f"{CAMERA_URL}@main"
    for index in range(1, len(lines)):
        if re.fullmatch(r"\s*components:\s*\[esp_video_camera\]\s*", lines[index]):
            match = re.match(r"^(\s*-\s*source:)\s*.*$", lines[index - 1])
            if match:
                lines[index - 1] = f"{match.group(1)} {source}"
    path.write_text("\n".join(lines) + "\n")


def mode(path):
    text = path.read_text()
    remote = any(f'{key}: "{url}@' in text for key, (url, _) in REPOS.items())
    remote |= any(re.search(rf"(?m)^\s+[A-Za-z_][A-Za-z0-9_]*:\s*{re.escape(url)}/", text) for url in URLS)
    local = any(re.search(rf'^\s*{key}:\s*"\.\./', text, re.MULTILINE) for key in REPOS)
    local |= bool(re.search(r"^\s+[A-Za-z_][A-Za-z0-9_]*:\s*!include\s+", text, re.MULTILINE))
    if remote and local:
        return "mixed"
    if remote:
        return "remote"
    if local:
        return "local"
    return "fragment" if not re.search(r"(?m)^esphome:\s*$", text) else "unknown"


def check(expect, only):
    failed = False
    for path in selected(yaml_files(), only):
        current = mode(path)
        exception = expect == "local" and path.resolve() == VOICE_PE.resolve()
        bad = current in {"mixed", "unknown"}
        bad |= bool(expect and current != "fragment" and current != expect and not exception)
        if bad:
            detail = f", expected {expect}" if expect else ""
            print(f"FAIL: {path.relative_to(ROOT)} ({current}{detail})", file=sys.stderr)
            failed = True
        if re.search(r"(?m)^\s*-\s*!include\s+", path.read_text()):
            print(f"FAIL: {path.relative_to(ROOT)} (nested list !include is not portable outside the repo)", file=sys.stderr)
            failed = True
    if not failed:
        print("OK: all YAMLs consistent.", file=sys.stderr)
    return int(failed)


def parse_args():
    args = sys.argv[1:]
    if args[:1] == ["--local"]:
        args = ["local", *args[1:]]
    elif args[:1] == ["--remote"]:
        if len(args) < 2:
            raise SystemExit("error: --remote requires a ref")
        ref, rest = args[1], args[2:]
        if ref == "tag":
            tag = argparse.ArgumentParser(add_help=False)
            tag.add_argument("--tag", required=True)
            tag.add_argument("--file")
            parsed = tag.parse_args(rest)
            args = [parsed.tag, *(["--file", parsed.file] if parsed.file else [])]
        else:
            args = [ref, *rest]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", help="local, a remote ref, status or check")
    parser.add_argument("--file")
    parser.add_argument("--expect", choices=("local", "remote"))
    return parser.parse_args(args)


def main():
    args = parse_args()
    if args.command == "status":
        for path in selected(yaml_files(), args.file):
            print(f"{path.relative_to(ROOT)} {mode(path)}")
        return 0
    if args.command == "check":
        return check(args.expect, args.file)
    files = selected(yaml_files(), args.file)
    for path in files:
        rewrite_yaml(path, args.command)
    cameras = selected(camera_files(), args.file) if args.file else camera_files()
    for path in cameras:
        rewrite_camera(path, args.command)
    for path in files:
        print(f"  {path.relative_to(ROOT)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
