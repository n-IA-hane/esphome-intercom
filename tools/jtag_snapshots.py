#!/usr/bin/env python3
"""Capture intrusive ESP32-S3 JTAG/GDB stop/resume snapshots.

This is intentionally different from runtime_diag:

* runtime_diag runs inside the firmware and is useful for counters/heap/task
  deltas without stopping the system;
* this tool halts the CPUs through OpenOCD, asks GDB for FreeRTOS threads and
  backtraces, then resumes. Audio will glitch while the target is halted, but
  the stack traces show exactly what loopTask and the other tasks were doing.
"""

from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GDB_CANDIDATES = (
    *sorted(
        (ROOT.parent / ".cache/esphome/idf/tools/xtensa-esp-elf-gdb").glob(
            "*/xtensa-esp-elf-gdb/bin/xtensa-esp32s3-elf-gdb"
        ),
        reverse=True,
    ),
    ROOT.parent
    / ".platformio/tools/tool-xtensa-esp-elf-gdb/bin/xtensa-esp32s3-elf-gdb",
    ROOT.parent
    / ".platformio/packages/tool-xtensa-esp-elf-gdb/bin/xtensa-esp32s3-elf-gdb",
)
DEFAULT_GDB_PYTHON_CANDIDATES = (
    *sorted(
        (ROOT.parent / ".cache/esphome/idf/penvs").glob("*/lib/python*/site-packages"),
        reverse=True,
    ),
    *sorted(
        (ROOT.parent / ".cache/esphome/idf/tools/xtensa-esp-elf-gdb").glob(
            "*/xtensa-esp-elf-gdb/share/gdb/python"
        ),
        reverse=True,
    ),
    ROOT.parent / ".platformio/packages/tool-xtensa-esp-elf-gdb/share/gdb/python",
    ROOT.parent / ".platformio/tools/tool-xtensa-esp-elf-gdb/share/gdb/python",
    ROOT.parent
    / ".espressif/tools/xtensa-esp-elf-gdb/16.2_20250811/xtensa-esp-elf-gdb/share/gdb/python",
)


def _latest_openocd_install() -> tuple[Path, Path] | None:
    roots = sorted(
        (ROOT.parent / ".cache/esphome/idf/tools/openocd-esp32").glob(
            "*/openocd-esp32"
        ),
        reverse=True,
    )
    for root in roots:
        binary = root / "bin/openocd"
        scripts = root / "share/openocd/scripts"
        if binary.exists() and scripts.exists():
            return binary, scripts
    return None


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _gdb_supports_python(executable: str) -> bool:
    probe = subprocess.run(
        [executable, "--batch", "-ex", "python print('codex-gdb-python-ok')"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
        check=False,
    )
    return probe.returncode == 0 and "codex-gdb-python-ok" in probe.stdout


def _find_gdb(explicit: str | None, *, require_python: bool = False) -> str:
    if explicit:
        if require_python and not _gdb_supports_python(explicit):
            raise SystemExit(
                f"GDB lacks Python support required by 'freertos task': {explicit}"
            )
        return explicit
    for candidate in DEFAULT_GDB_CANDIDATES:
        if candidate.exists() and (
            not require_python or _gdb_supports_python(str(candidate))
        ):
            return str(candidate)
    found = shutil.which("xtensa-esp32s3-elf-gdb")
    if found and (not require_python or _gdb_supports_python(found)):
        return found
    if require_python:
        raise SystemExit("Python-capable xtensa-esp32s3-elf-gdb not found")
    raise SystemExit(
        "xtensa-esp32s3-elf-gdb not found; build once with ESPHome/PlatformIO first"
    )


def _find_gdb_python_path(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.exists():
            raise SystemExit(f"GDB Python path not found: {path}")
        return path
    for candidate in DEFAULT_GDB_PYTHON_CANDIDATES:
        if (candidate / "freertos_gdb").exists():
            return candidate
    return None


def _find_elf(explicit: str | None, device: str | None) -> Path:
    if explicit:
        elf = Path(explicit).expanduser().resolve()
        if not elf.exists():
            raise SystemExit(f"ELF not found: {elf}")
        return elf
    candidates = sorted(
        [
            *ROOT.glob("yamls/**/.esphome/build/*/build/firmware.elf"),
            *ROOT.glob("yamls/**/.esphome/build/*/.pioenvs/*/firmware.elf"),
        ],
        key=lambda p: p.stat().st_mtime,
    )
    if device:
        filtered = [p for p in candidates if device in str(p)]
        if filtered:
            return filtered[-1]
    if candidates:
        return candidates[-1]
    raise SystemExit("No firmware.elf found; pass --elf explicitly")


def _remote_openocd_command(
    gdb_port: int, openocd_bin: str, scripts: list[str], config: list[str]
) -> str:
    script_args = " ".join(f"-s {path}" for path in scripts)
    config_args = " ".join(f"-f {path}" for path in config)
    return (
        f"exec {openocd_bin} "
        f"{script_args} "
        f"{config_args} "
        f"-c 'gdb_port {gdb_port}' "
        "-c 'telnet_port disabled' "
        "-c 'tcl_port disabled'"
    )


def _openocd_command(
    gdb_port: int,
    openocd_bin: str,
    scripts: list[str],
    config: list[str],
    commands: list[str],
    post_commands: list[str],
    sudo: bool,
) -> list[str]:
    cmd: list[str] = []
    if sudo:
        cmd.append("sudo")
    cmd.append(openocd_bin)
    for path in scripts:
        cmd.extend(["-s", path])
    for command in commands:
        cmd.extend(["-c", command])
    for path in config:
        cmd.extend(["-f", path])
    for command in post_commands:
        cmd.extend(["-c", command])
    cmd.extend(
        [
            "-c",
            f"gdb_port {gdb_port}",
            "-c",
            "telnet_port disabled",
            "-c",
            "tcl_port disabled",
        ]
    )
    return cmd


def _write_gdb_script(
    path: Path,
    samples: int,
    interval: float,
    backtrace_depth: int,
    freertos: bool,
    gdb_python_path: Path | None,
) -> None:
    lines: list[str] = [
        "set pagination off",
        "set confirm off",
        "set print thread-events off",
        "set remotetimeout 10",
        "target extended-remote 127.0.0.1:3333",
        "set $sample = 0",
        f"while $sample < {samples}",
        '  printf "\\n===== JTAG SNAPSHOT %d =====\\n", $sample',
        "  monitor halt",
        "  info threads",
    ]
    if freertos:
        if gdb_python_path is not None:
            escaped = str(gdb_python_path).replace("\\", "\\\\").replace("'", "\\'")
            lines.insert(
                4,
                f"python import sys; sys.path.insert(0, '{escaped}'); import freertos_gdb",
            )
        else:
            lines.insert(4, "python import freertos_gdb")
        lines.extend(
            [
                '  printf "\\n----- FREERTOS TASKS -----\\n"',
                "  freertos task",
            ]
        )
    lines.extend(
        [
            f"  thread apply all bt {backtrace_depth}",
            "  info registers pc a0 a1 a2 a3 ps",
            "  monitor resume",
            f"  shell sleep {interval:.3f}",
            "  set $sample = $sample + 1",
            "end",
            "detach",
            "quit",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Take intrusive OpenOCD/GDB stop/resume snapshots from an ESP32-S3.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Run OpenOCD locally instead of through SSH",
    )
    parser.add_argument(
        "--remote",
        default="daniele@192.168.1.20",
        help="SSH host connected to the ESP USB-JTAG",
    )
    parser.add_argument(
        "--elf", help="Matching firmware.elf. Required for useful file/line symbols."
    )
    parser.add_argument(
        "--device", default="spotpear", help="Substring used to auto-pick firmware.elf"
    )
    parser.add_argument("--gdb", help="xtensa-esp32s3-elf-gdb path")
    parser.add_argument(
        "--gdb-python-path",
        help="Directory containing freertos_gdb for the selected GDB",
    )
    parser.add_argument(
        "--samples", type=int, default=20, help="Number of stop/resume samples"
    )
    parser.add_argument(
        "--interval", type=float, default=1.0, help="Seconds between samples"
    )
    parser.add_argument(
        "--bt-depth", type=int, default=12, help="Backtrace depth per FreeRTOS thread"
    )
    parser.add_argument(
        "--no-freertos-task",
        action="store_true",
        help="Do not print FreeRTOS task tables",
    )
    parser.add_argument(
        "--out-dir", default="test_captures/jtag_snapshots", help="Output directory"
    )
    parser.add_argument(
        "--keep-openocd-log",
        action="store_true",
        help="Keep the OpenOCD log next to the GDB log",
    )
    parser.add_argument(
        "--openocd-bin",
        help="OpenOCD binary; defaults to the current ESPHome ESP-IDF tool",
    )
    parser.add_argument(
        "--sudo-openocd", action="store_true", help="Run local OpenOCD through sudo"
    )
    parser.add_argument(
        "--openocd-command",
        action="append",
        default=["set ESP_FLASH_SIZE 0"],
        help="OpenOCD command passed before config files; repeatable. Default disables flash probing to avoid live-target reset.",
    )
    parser.add_argument(
        "--openocd-post-command",
        action="append",
        default=[
            "esp32s3.cpu0 configure -event gdb-attach { esp32s3.cpu0 xtensa smpbreak BreakIn BreakOut; halt 1000 }",
            "esp32s3.cpu1 configure -event gdb-attach { esp32s3.cpu1 xtensa smpbreak BreakIn BreakOut; halt 1000 }",
        ],
        help="OpenOCD command passed after config files; repeatable. Defaults override gdb-attach reset for live snapshots.",
    )
    parser.add_argument(
        "--openocd-scripts",
        action="append",
        help="OpenOCD script directory; repeatable. Must match --openocd-bin.",
    )
    parser.add_argument(
        "--openocd-config",
        action="append",
        default=[
            "board/esp32s3-builtin.cfg",
        ],
        help="Remote OpenOCD config file; repeatable and order-sensitive",
    )
    args = parser.parse_args()

    current_openocd = _latest_openocd_install()
    if args.openocd_bin is None:
        if args.local and current_openocd is not None:
            args.openocd_bin = str(current_openocd[0])
        else:
            args.openocd_bin = str(
                ROOT.parent / ".platformio/packages/tool-openocd-esp32/bin/openocd"
            )
    if args.openocd_scripts is None:
        if (
            args.local
            and current_openocd is not None
            and Path(args.openocd_bin).resolve() == current_openocd[0].resolve()
        ):
            args.openocd_scripts = [str(current_openocd[1])]
        else:
            args.openocd_scripts = [
                str(
                    Path(args.openocd_bin).resolve().parent.parent
                    / "share/openocd/scripts"
                )
            ]

    gdb = _find_gdb(args.gdb, require_python=not args.no_freertos_task)
    gdb_python_path = _find_gdb_python_path(args.gdb_python_path)
    elf = _find_elf(args.elf, args.device)
    out_dir = (ROOT / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    gdb_log = out_dir / f"{args.device}-jtag-{stamp}.log"
    openocd_log = out_dir / f"{args.device}-openocd-{stamp}.log"
    local_port = _free_port()

    print(f"ELF: {elf}")
    print(f"GDB: {gdb}")
    if not args.no_freertos_task:
        print(f"GDB Python: {gdb_python_path or 'default sys.path'}")
    print(f"OpenOCD: {'local' if args.local else args.remote}")
    print(f"Output: {gdb_log}")
    print("The target will halt briefly for every sample; audio glitches are expected.")

    if args.local:
        openocd_cmd = _openocd_command(
            local_port,
            args.openocd_bin,
            args.openocd_scripts,
            args.openocd_config,
            args.openocd_command,
            args.openocd_post_command,
            args.sudo_openocd,
        )
        openocd_proc = subprocess.Popen(
            openocd_cmd,
            stdout=open(openocd_log, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            text=True,
        )
    else:
        openocd_cmd = _remote_openocd_command(
            3333, args.openocd_bin, args.openocd_scripts, args.openocd_config
        )
        openocd_proc = subprocess.Popen(
            [
                "ssh",
                "-tt",
                "-L",
                f"{local_port}:127.0.0.1:3333",
                args.remote,
                openocd_cmd,
            ],
            stdout=open(openocd_log, "w", encoding="utf-8"),
            stderr=subprocess.STDOUT,
            text=True,
        )

    try:
        time.sleep(2.0)
        if openocd_proc.poll() is not None:
            keep_log = True
            raise SystemExit(f"OpenOCD exited early; see {openocd_log}")

        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "snapshot.gdb"
            _write_gdb_script(
                script,
                args.samples,
                args.interval,
                args.bt_depth,
                not args.no_freertos_task,
                gdb_python_path,
            )
            # The tunnel maps local_port -> remote 3333, but the script keeps the
            # conventional 3333 to stay readable; patch it at runtime.
            text = script.read_text(encoding="utf-8").replace(
                "127.0.0.1:3333", f"127.0.0.1:{local_port}"
            )
            script.write_text(text, encoding="utf-8")
            with open(gdb_log, "w", encoding="utf-8") as log:
                result = subprocess.run(
                    [gdb, str(elf), "-x", str(script)],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            if result.returncode != 0:
                keep_log = True
                print(
                    f"GDB failed with exit code {result.returncode}; see {gdb_log}",
                    file=sys.stderr,
                )
                return result.returncode
            snapshot_count = gdb_log.read_text(
                encoding="utf-8", errors="replace"
            ).count("===== JTAG SNAPSHOT ")
            if snapshot_count != args.samples:
                keep_log = True
                print(
                    f"GDB produced {snapshot_count}/{args.samples} snapshots; see {gdb_log}",
                    file=sys.stderr,
                )
                return 2
    finally:
        openocd_proc.terminate()
        try:
            openocd_proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            openocd_proc.kill()
        if (
            not args.keep_openocd_log
            and not locals().get("keep_log", False)
            and openocd_log.exists()
        ):
            openocd_log.unlink()

    print(f"Wrote {gdb_log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
