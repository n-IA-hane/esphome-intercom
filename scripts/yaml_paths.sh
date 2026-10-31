#!/usr/bin/env python3
"""Switch maintained ESPHome YAMLs between local checkouts and one remote ref."""

from pathlib import Path
import argparse
import re
import shlex
import subprocess
import sys

ROOT = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
CAMERA_URL = "github://Psix-anp/esphome-esp-video-camera"
REPOS = {
    "ext_components_source": ("github://n-IA-hane/esphome-intercom", ROOT),
    "voip_stack_components_source": ("github://n-IA-hane/esphome-voip-stack", ROOT.parent / "esphome-voip-stack"),
    "audio_stack_components_source": ("github://n-IA-hane/esphome-audio-stack", ROOT.parent / "esphome-audio-stack"),
    "runtime_controller_components_source": ("github://n-IA-hane/esphome-runtime-controller", ROOT.parent / "esphome-runtime-controller"),
    "esp_screenshot_components_source": ("github://n-IA-hane/esphome-screenshot", ROOT.parent / "esphome-screenshot"),
    "esp_video_renderer_components_source": ("github://n-IA-hane/esphome-video-renderer", ROOT.parent / "esphome-video-renderer"),
    "camera_components_source": (CAMERA_URL, ROOT.parent / "esphome-esp-video-camera"),
}
CAMERA_ROOT = ROOT.parent / "esphome-esp-video-camera"
P4_VOIP_YAML_ROOT = ROOT / "yamls/voip-only/single-bus"
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
        # Private overrides are gitignored and must not be rewritten or audited
        # as maintained profiles. An explicit --file still selects them.
        return [path for path in files if not path.name.endswith("-local.yaml")]
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
    text = path.read_text()
    device_entrypoint = bool(re.search(r"(?m)^\s+device:\s+(?:!include\s+|github://)", text))
    p4 = "waveshare-p4" in text or "p4" in path.name
    overrides = {}
    for key, (url, repo_root) in REPOS.items():
        if key == "camera_components_source" and not p4:
            continue
        component_dir = "components" if key == "camera_components_source" else "esphome/components"
        remote_ref = "main" if key == "camera_components_source" else ref
        overrides[key] = relative(path.parent, repo_root / component_dir) if ref == "local" else f"{url}@{remote_ref}"
    if device_entrypoint:
        overrides["gmf_ai_audio_source"] = str(ROOT.parent / "esp-gmf/elements/gmf_ai_audio") if ref == "local" else ""
    if p4:
        overrides["intercom_idf_root"] = str(ROOT / "idf_components") if ref == "local" else ""
        overrides["intercom_idf_ref"] = "main" if ref == "local" else ref
    output = []
    seen = set()
    for line in text.splitlines():
        match = re.match(r'^(\s*)([A-Za-z_][A-Za-z0-9_]*):\s*[^\n]*$', line)
        if match and match.group(2) in overrides:
            indent, key = match.groups()
            seen.add(key)
            output.append(f'{indent}{key}: "{overrides[key]}"')
        elif re.match(r'^\s*assets_base:\s*"[^"]*"\s*$', line):
            value = relative(path.parent, ROOT) + "/" if ref == "local" else f"https://github.com/n-IA-hane/esphome-intercom/raw/{ref}/"
            output.append(f'  assets_base: "{value}"')
        else:
            output.append(rewrite_package(line, path, ref))
    if device_entrypoint:
        additions = [f'  {key}: "{value}"' for key, value in overrides.items() if key not in seen]
        if "substitutions:" in output:
            index = output.index("substitutions:") + 1
            output[index:index] = additions
        else:
            output = ["substitutions:", *additions, *output]
    path.write_text("\n".join(output) + "\n")


def camera_source_root(path):
    # ESPHome resolves external component paths after package expansion from
    # the top-level YAML, not from the included package file. The maintained
    # P4 codec packages are consumed by YAMLs in this single directory.
    return (
        P4_VOIP_YAML_ROOT
        if path.parent == ROOT / "packages/voip"
        else path.parent
    )


def rewrite_camera(path, ref):
    lines = path.read_text().splitlines()
    source_root = camera_source_root(path)
    source = relative(source_root, CAMERA_ROOT / "components") if ref == "local" else f"{CAMERA_URL}@main"
    for index in range(1, len(lines)):
        if re.fullmatch(r"\s*components:\s*\[esp_video_camera\]\s*", lines[index]):
            if "${camera_components_source}" in lines[index - 1]:
                continue
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
    local |= bool(re.search(r'^\s*intercom_idf_root:\s*"[^"\s][^"]*"', text, re.MULTILINE))
    local |= bool(re.search(r'^\s*gmf_ai_audio_source:\s*"[^"\s][^"]*"', text, re.MULTILINE))
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
        bad = current in {"mixed", "unknown"}
        bad |= bool(expect and current != "fragment" and current != expect)
        if bad:
            detail = f", expected {expect}" if expect else ""
            print(f"FAIL: {path.relative_to(ROOT)} ({current}{detail})", file=sys.stderr)
            failed = True
        if re.search(r"(?m)^\s*-\s*!include\s+", path.read_text()):
            print(f"FAIL: {path.relative_to(ROOT)} (nested list !include is not portable outside the repo)", file=sys.stderr)
            failed = True
    # Includes inside a public package must travel with that repository. Checking
    # only the top-level URL misses undeclared sibling-checkout dependencies.
    for path in sorted((ROOT / "packages").rglob("*.yaml")):
        for include in package_include_files(path):
            target = (path.parent / include).resolve()
            outside = not target.is_relative_to(ROOT.resolve())
            if not target.is_file() or (expect == "remote" and outside):
                print(
                    f"FAIL: {path.relative_to(ROOT)} (package include is missing or not portable: {include})",
                    file=sys.stderr,
                )
                failed = True
    if not failed:
        print("OK: all YAMLs consistent.", file=sys.stderr)
    return int(failed)


def package_include_files(path):
    """Read static scalar/block includes without interpreting secrets or YAML."""
    pending_indent = None
    for line in path.read_text().splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(stripped)
        value = None
        if pending_indent is not None:
            if indent <= pending_indent:
                pending_indent = None
            elif stripped.startswith("file:"):
                value = stripped.removeprefix("file:").strip()
                pending_indent = None
        if match := re.search(r"(?:^|\s)!include(?:\s+(.*))?$", line):
            value = (match.group(1) or "").strip()
            if not value or value.startswith("#"):
                pending_indent = indent
                continue
        if value and "${" not in value and not value.startswith("{"):
            fields = shlex.split(value, comments=True)
            if fields:
                yield fields[0]


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
