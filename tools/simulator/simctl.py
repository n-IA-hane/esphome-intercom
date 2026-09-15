#!/usr/bin/env python3
"""JSON-RPC client for the VoIP Stack virtual device."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import sys
from typing import Any


DEFAULT_SOCKET = Path("test_captures/simulator/voip-sim.sock")


class SimctlError(RuntimeError):
    pass


def _json_rpc(socket_path: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
    if not socket_path.exists():
        raise SimctlError(
            f"simulator socket not found: {socket_path}. "
            "Start the contract simulator before running scenarios."
        )
    payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(str(socket_path))
        client.sendall(payload + b"\n")
        chunks: list[bytes] = []
        while True:
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
    if not chunks:
        raise SimctlError("simulator returned no response")
    line = b"".join(chunks).split(b"\n", 1)[0]
    response = json.loads(line.decode("utf-8"))
    if "error" in response:
        raise SimctlError(str(response["error"]))
    result = response.get("result")
    if not isinstance(result, dict):
        raise SimctlError(f"invalid simulator result: {response!r}")
    return result


def call(socket_path: Path, method: str, **params: Any) -> dict[str, Any]:
    return _json_rpc(socket_path, method, params)


def doctor(socket_path: Path) -> int:
    checks = {
        "socket": socket_path.exists(),
        "scenarios": Path("tests/simulator/scenarios").is_dir(),
        "audio": Path("tests/simulator/audio").is_dir(),
        "golden": Path("tests/simulator/golden").is_dir(),
    }
    print(json.dumps(checks, indent=2, sort_keys=True))
    return 0 if all(value for key, value in checks.items() if key != "socket") else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    sub.add_parser("snapshot")
    sub.add_parser("reset")
    sub.add_parser("shutdown")
    press = sub.add_parser("press-button")
    press.add_argument("button")
    touch = sub.add_parser("touch")
    touch.add_argument("target")
    advance = sub.add_parser("advance-time")
    advance.add_argument("duration_ms", type=int)
    fault = sub.add_parser("inject-fault")
    fault.add_argument("name")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "doctor":
            return doctor(args.socket)
        if args.cmd == "snapshot":
            print(json.dumps(call(args.socket, "get_snapshot"), indent=2, sort_keys=True))
        elif args.cmd == "reset":
            print(json.dumps(call(args.socket, "reset"), indent=2, sort_keys=True))
        elif args.cmd == "shutdown":
            print(json.dumps(call(args.socket, "shutdown"), indent=2, sort_keys=True))
        elif args.cmd == "press-button":
            print(json.dumps(call(args.socket, "press_button", button=args.button), indent=2, sort_keys=True))
        elif args.cmd == "touch":
            print(json.dumps(call(args.socket, "touch", target=args.target), indent=2, sort_keys=True))
        elif args.cmd == "advance-time":
            print(json.dumps(call(args.socket, "advance_time", duration_ms=args.duration_ms), indent=2, sort_keys=True))
        elif args.cmd == "inject-fault":
            print(json.dumps(call(args.socket, "inject_fault", name=args.name), indent=2, sort_keys=True))
        return 0
    except Exception as err:
        print(f"simctl: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
