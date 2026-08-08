#!/usr/bin/env python3
"""Qualify audio-first bidirectional H264 re-INVITE against a real P4."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from live_voip_qualification import (  # noqa: E402
    EspApi,
    EspDevice,
    HaRest,
    HaWs,
    LiveContext,
    maybe_await,
    norm,
    qualification_token,
)


FATAL_SERIAL_PATTERNS = (
    "Guru Meditation",
    "panic",
    "watchdog",
    "Video worker stop timeout",
    "retaining sockets",
)
STAT_LABELS = {
    "rx": "H.264 RX stats:",
    "tx": "H.264 TX stats:",
    "session": "Video session totals:",
}
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
ACTIVE_VIDEO = re.compile(r"(?m)^m=video\s+(?!0(?:\s|$))\d+")


def _stats_line(text: str, label: str) -> dict[str, int]:
    matches = [line.split(label, 1)[1] for line in text.splitlines() if label in line]
    if not matches:
        raise AssertionError(f"P4 serial evidence is missing {label}")
    return {key: int(value) for key, value in re.findall(r"(\w+)=(\d+)", matches[-1])}


def parse_serial_metrics(text: str) -> dict[str, object]:
    """Extract privacy-safe P4 presentation and media counters."""

    clean = ANSI.sub("", text)
    fatals = [
        pattern for pattern in FATAL_SERIAL_PATTERNS if pattern.lower() in clean.lower()
    ]
    metrics: dict[str, object] = {
        key: _stats_line(clean, label) for key, label in STAT_LABELS.items()
    }
    metrics["first_keyframe"] = any(
        "H.264 first AU:" in line and "key=YES" in line for line in clean.splitlines()
    )
    metrics["fatal_errors"] = fatals
    return metrics


def parse_ffprobe(payload: dict[str, object]) -> dict[str, object]:
    streams = payload.get("streams")
    if not isinstance(streams, list) or not streams or not isinstance(streams[0], dict):
        raise AssertionError("ffprobe did not find returned P4 video")
    stream = streams[0]
    frames = int(stream.get("nb_read_frames") or stream.get("nb_frames") or 0)
    result = {
        "codec": str(stream.get("codec_name") or "").lower(),
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "frames": frames,
    }
    if result["codec"] != "h264" or min(result["width"], result["height"]) <= 0:
        raise AssertionError(f"returned P4 stream is not decodable H264: {result}")
    if frames < 3:
        raise AssertionError(f"returned P4 stream has fewer than 3 frames: {result}")
    return result


def validate_cycle(
    peer: dict[str, object],
    serial_metrics: dict[str, object],
    returned_video: dict[str, object],
    *,
    termination_owner: str,
) -> dict[str, object]:
    """Validate independent SIP, media, P4 presentation and hangup evidence."""

    statuses = [int(value) for value in peer.get("sip_statuses", [])]
    transitions = [
        item
        for item in peer.get("video_transitions", [])
        if isinstance(item, dict) and item.get("name") == "reinvite"
    ]
    checks = {
        "peer_ok": peer.get("ok") is True,
        "initial_audio_only": peer.get("initial_codec") == "audio"
        and not ACTIVE_VIDEO.search(str(peer.get("answer_sdp") or "")),
        "initial_answered": bool(statuses) and statuses[-1] == 200,
        "reinvite_answered": int(peer.get("reinvite_status") or 0) == 200,
        "h264_sendrecv": "H264/90000"
        in str(peer.get("reinvite_negotiated_video") or "")
        and peer.get("reinvite_video_direction") == "sendrecv",
        "audio_before_video": bool(transitions)
        and int(transitions[-1].get("audio_tx_packets") or 0) > 0
        and int(transitions[-1].get("audio_rx_packets") or 0) > 0,
        "audio_duplex": int(peer.get("audio_tx_packets") or 0) > 0
        and int(peer.get("audio_rx_packets") or 0) > 0,
        "video_duplex": int(peer.get("video_tx_packets") or 0) > 0
        and int(peer.get("video_rx_packets") or 0) > 0
        and int(peer.get("video_rx_marker_packets") or 0) > 0,
        "termination": (
            200 <= int(peer.get("bye_response_status") or 0) < 300
            if termination_owner == "peer"
            else peer.get("remote_bye") is True
        ),
    }
    rx = serial_metrics.get("rx") or {}
    tx = serial_metrics.get("tx") or {}
    session = serial_metrics.get("session") or {}
    checks.update(
        {
            "p4_presented": serial_metrics.get("first_keyframe") is True
            and int(rx.get("admitted", 0)) > 0
            and int(rx.get("rendered", 0)) >= 3
            and int(rx.get("presented", 0)) >= 3
            and int(rx.get("refresh_done", 0)) >= 3,
            "p4_encoded": int(tx.get("encoded", 0)) >= 3,
            "p4_video_session": int(session.get("tx", 0)) > 0
            and int(session.get("rx", 0)) > 0
            and int(session.get("completed_au", 0)) > 0
            and int(session.get("send_fail", 0)) == 0,
            "serial_clean": not serial_metrics.get("fatal_errors"),
            "returned_h264": returned_video.get("frames", 0) >= 3,
        }
    )
    failed = sorted(key for key, passed in checks.items() if not passed)
    if failed:
        raise AssertionError(
            f"P4 H264 qualification failed checks: {', '.join(failed)}"
        )
    return {
        "checks": checks,
        "sip": {
            "initial_statuses": statuses,
            "initial_codec": peer.get("initial_codec"),
            "reinvite_status": peer.get("reinvite_status"),
            "video_codec": "h264",
            "video_direction": peer.get("reinvite_video_direction"),
            "bye_status": peer.get("bye_response_status"),
            "remote_bye": bool(peer.get("remote_bye")),
        },
        "media": {
            key: int(peer.get(key) or 0)
            for key in (
                "audio_tx_packets",
                "audio_rx_packets",
                "video_tx_packets",
                "video_rx_packets",
                "video_rx_marker_packets",
            )
        }
        | {
            "audio_before_reinvite_tx": int(transitions[-1]["audio_tx_packets"]),
            "audio_before_reinvite_rx": int(transitions[-1]["audio_rx_packets"]),
            "returned_h264_frames": int(returned_video["frames"]),
        },
        "p4": {"rx": rx, "tx": tx, "session": session},
    }


def cleanup_evidence(
    snapshot: dict[str, object], p4_state: object
) -> dict[str, object]:
    resources = dict(
        snapshot.get("runtime_resources")
        or (snapshot.get("media_debug") or {}).get("runtime_resources")
        or {}
    )
    result = {
        "p4_state": norm(p4_state),
        "ha_state": norm(snapshot.get("state")),
        "active_dialogs": int(snapshot.get("active_dialogs") or 0),
        "call_scoped_quiescent": resources.get("call_scoped_quiescent") is True,
        "allocated_rtp_ports": int(
            (resources.get("resource_counts") or {}).get("allocated_rtp_ports") or 0
        ),
    }
    if result != {
        "p4_state": "idle",
        "ha_state": "idle",
        "active_dialogs": 0,
        "call_scoped_quiescent": True,
        "allocated_rtp_ports": 0,
    }:
        raise AssertionError(f"call resources did not quiesce: {result}")
    return result


async def _set_number(esp: EspApi, object_id: str, value: float) -> None:
    entity = esp.entities.get(object_id)
    if entity is None:
        raise AssertionError(f"{esp.spec.key}: number {object_id!r} not exposed")
    await maybe_await(esp.client.number_command(entity.key, value))
    await esp.wait(object_id, {str(float(value))}, timeout=5, exact=True)


@dataclass(frozen=True, slots=True)
class DeviceSettings:
    volume: float
    auto_answer: bool


@asynccontextmanager
async def quiet_p4(esp: EspApi) -> AsyncIterator[DeviceSettings]:
    """Force 1 percent volume and restore both settings on every exit path."""

    settings = DeviceSettings(
        volume=float(esp.values.get("master_volume")),
        auto_answer=norm(esp.values.get("auto_answer")) == "on",
    )
    try:
        await _set_number(esp, "master_volume", 1.0)
        await esp.switch("auto_answer", False)
        yield settings
    finally:
        errors: list[BaseException] = []
        for restore in (
            lambda: esp.switch("auto_answer", settings.auto_answer),
            lambda: _set_number(esp, "master_volume", settings.volume),
        ):
            try:
                await restore()
            except BaseException as error:  # Preserve both restoration attempts.
                errors.append(error)
        if errors:
            raise RuntimeError(
                f"P4 settings restoration failed ({len(errors)} operations)"
            ) from errors[0]


class SerialCapture:
    """Single bounded-lifetime reader for one P4 serial port."""

    def __init__(self, port: str, output: Path) -> None:
        self.port = port
        self.output = output
        self._lines: list[str] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._serial: Any = None

    def __enter__(self) -> "SerialCapture":
        import serial

        self.output.parent.mkdir(parents=True, exist_ok=True)
        self._serial = serial.Serial(
            port=None,
            baudrate=115200,
            timeout=0.2,
            exclusive=True,
            dsrdtr=False,
            rtscts=False,
        )
        self._serial.dtr = False
        self._serial.rts = False
        self._serial.port = self.port
        self._serial.open()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()
        return self

    def _read(self) -> None:
        assert self._serial is not None
        with self.output.open("ab") as stream:
            while not self._stop.is_set():
                raw = self._serial.readline()
                if not raw:
                    continue
                stream.write(raw)
                stream.flush()
                self._lines.append(raw.decode(errors="replace"))

    def mark(self) -> int:
        return len(self._lines)

    def since(self, mark: int) -> str:
        return "".join(self._lines[mark:])

    def wait_cycle(self, mark: int, timeout: float = 3) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self.since(mark)
            if all(label in text for label in STAT_LABELS.values()):
                time.sleep(0.1)
                return self.since(mark)
            time.sleep(0.05)
        return self.since(mark)

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        if self._serial is not None:
            self._serial.close()


async def _drain(
    stream: asyncio.StreamReader, output: Path, event: asyncio.Event
) -> None:
    with output.open("wb") as destination:
        while line := await stream.readline():
            destination.write(line)
            destination.flush()
            if b"reinvite re-INVITE SIP 200" in line:
                event.set()


def _ffprobe(path: Path) -> dict[str, object]:
    executable = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("ffprobe is required for P4 returned-video validation")
    result = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-count_frames",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name,width,height,nb_frames,nb_read_frames",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return parse_ffprobe(json.loads(result.stdout))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def _run_peer_cycle(
    args: argparse.Namespace,
    p4: EspApi,
    cycle: int,
) -> tuple[dict[str, object], str, dict[str, Path]]:
    owner = "peer" if cycle % 2 else "p4"
    prefix = args.out_dir / f"cycle-{cycle}"
    paths = {
        "peer": prefix.with_name(prefix.name + "-peer.json"),
        "video": prefix.with_name(prefix.name + "-p4-return.mkv"),
        "stdout": prefix.with_name(prefix.name + "-peer.stdout.log"),
        "stderr": prefix.with_name(prefix.name + "-peer.stderr.log"),
    }
    command = [
        sys.executable,
        str(ROOT / "tools/sip_video_peer.py"),
        "--host",
        args.sip_host,
        "--port",
        str(args.sip_port),
        "--target",
        args.p4_extension,
        "--local-ip",
        args.local_ip,
        "--audio-codec",
        "l16-16k",
        "--codec",
        "h264",
        "--direction",
        "sendrecv",
        "--add-video-after",
        "1",
        "--expect-reinvite-status",
        "200",
        "--duration",
        str(args.duration),
        "--video-profile",
        "RTP/AVP",
        "--video-rx-file",
        str(paths["video"]),
        "--out",
        str(paths["peer"]),
    ]
    process = await asyncio.create_subprocess_exec(
        *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    assert process.stdout is not None and process.stderr is not None
    reinvite = asyncio.Event()
    drains = (
        asyncio.create_task(_drain(process.stdout, paths["stdout"], reinvite)),
        asyncio.create_task(_drain(process.stderr, paths["stderr"], asyncio.Event())),
    )
    try:
        await p4.wait(
            "voip_state", {"ringing", "incoming", "in_call"}, timeout=30
        )
        if p4.values.get("voip_state") != "in_call":
            await p4.service("answer_call")
            await p4.wait("voip_state", {"in_call"}, timeout=15)
        await asyncio.wait_for(reinvite.wait(), timeout=20)
        if owner == "p4":
            await asyncio.sleep(args.video_hold_seconds)
            await p4.service("hangup_call")
        await asyncio.wait_for(process.wait(), timeout=args.duration + 30)
        await asyncio.gather(*drains)
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except TimeoutError:
                process.kill()
                await process.wait()
        for task in drains:
            if not task.done():
                task.cancel()
        await asyncio.gather(*drains, return_exceptions=True)
    if process.returncode:
        raise RuntimeError(f"sip_video_peer failed with exit code {process.returncode}")
    return json.loads(paths["peer"].read_text(encoding="utf-8")), owner, paths


async def run(args: argparse.Namespace) -> dict[str, object]:
    args.out_dir.mkdir(parents=True, exist_ok=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "schema_version": 1,
        "scenario": "p4-audio-to-bidirectional-h264-reinvite",
        "status": "failed",
        "cycles": [],
    }
    failure: BaseException | None = None
    serial_path = args.out_dir / "p4-serial.log"
    try:
        token = qualification_token(args)
        p4_spec = EspDevice("p4", "P4 HIL", args.p4_host, args.p4_api_port)
        ha = HaRest(args.ha_url, token, insecure=args.insecure)
        with SerialCapture(args.serial_port, serial_path) as serial_capture:
            async with (
                EspApi(p4_spec) as p4,
                HaWs(args.ha_url, token, insecure=args.insecure) as ws,
            ):
                ctx = LiveContext(ha=ha, ws=ws, esp=p4, args=args)
                async with quiet_p4(p4):
                    await ctx.cleanup()
                    try:
                        for cycle in range(1, args.cycles + 1):
                            mark = serial_capture.mark()
                            peer, owner, paths = await _run_peer_cycle(args, p4, cycle)
                            await ctx.cleanup()
                            serial_text = await asyncio.to_thread(
                                serial_capture.wait_cycle, mark
                            )
                            serial_metrics = parse_serial_metrics(serial_text)
                            validated = validate_cycle(
                                peer,
                                serial_metrics,
                                _ffprobe(paths["video"]),
                                termination_owner=owner,
                            )
                            validated.update(
                                {
                                    "cycle": cycle,
                                    "termination_owner": owner,
                                    "cleanup": cleanup_evidence(
                                        await ws.softphone_state(),
                                        p4.values.get("voip_state"),
                                    ),
                                    "artifacts": {
                                        key: {
                                            "path": path.name,
                                            "sha256": _sha256(path),
                                        }
                                        for key, path in paths.items()
                                    },
                                }
                            )
                            report["cycles"].append(validated)
                    finally:
                        await ctx.cleanup()
                report["status"] = "passed"
    except BaseException as error:
        failure = error
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        if serial_path.is_file():
            report["serial_sha256"] = _sha256(serial_path)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if failure is not None:
        raise failure
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-url", required=True)
    parser.add_argument("--token")
    parser.add_argument("--token-file", type=Path, default=Path("/nonexistent"))
    parser.add_argument("--auth-file", type=Path, default=Path("/nonexistent"))
    parser.add_argument("--insecure", action="store_true")
    parser.add_argument("--sip-host", required=True)
    parser.add_argument("--sip-port", type=int, required=True)
    parser.add_argument("--local-ip", required=True)
    parser.add_argument("--p4-host", required=True)
    parser.add_argument("--p4-api-port", type=int, default=6053)
    parser.add_argument("--p4-extension", required=True)
    parser.add_argument("--serial-port", required=True)
    parser.add_argument("--cycles", type=int, default=2)
    parser.add_argument("--duration", type=float, default=10)
    parser.add_argument("--video-hold-seconds", type=float, default=3)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.cycles < 2:
        parser.error("--cycles must be at least 2 to prove redial and both BYE owners")
    return args


def main() -> int:
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        return 130
    except BaseException as error:
        print(f"p4_hil_failed={type(error).__name__}: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
