"""Receive diagnostic I2S/microphone PCM and hardware playback completions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import struct
import time
import wave

HEADER = struct.Struct("<IIIHHIIQ")
STATS = struct.Struct("<IIII")


def receive_exact(connection, size: int) -> bytes:
    result = bytearray()
    while len(result) < size:
        data = connection.recv(size - len(result))
        if not data:
            raise ValueError("audio capture ended before its complete record/footer")
        result.extend(data)
    return bytes(result)


def receive_capture(connection, output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    report = {"started_epoch": time.time(), "streams": {}, "records": 0}
    writers = {}
    formats = {}
    last_sequence = 0
    try:
        if receive_exact(connection, 8) != b"ASTCAP1\n":
            raise ValueError("unsupported audio capture protocol")
        with (output / "records.jsonl").open("w") as records:
            while True:
                kind, sequence, rate, channels, bits, frames, size, timestamp = HEADER.unpack(receive_exact(connection, HEADER.size))
                if size > 128 * 1024 or kind not in (0, 1, 2, 3, 4):
                    raise ValueError("invalid audio capture header")
                payload = receive_exact(connection, size)
                if kind == 0:
                    if size != STATS.size:
                        raise ValueError("invalid capture footer")
                    seen, dropped, conflicts, longest_callback = STATS.unpack(payload)
                    report["stats"] = {"seen": seen, "dropped": dropped, "producer_conflicts": conflicts,
                                       "max_callback_us": longest_callback}
                    report["valid"] = dropped == 0 and conflicts == 0 and seen == last_sequence == report["records"]
                    break
                if sequence != last_sequence + 1:
                    raise ValueError("diagnostic record sequence has a gap")
                last_sequence = sequence
                if not 8000 <= rate <= 192000 or not 1 <= channels <= 8 or bits not in (16, 32):
                    raise ValueError("invalid PCM format")
                if (kind != 3 and size != frames * channels * (bits // 8)) or (kind == 3 and size != 0):
                    raise ValueError("PCM sample count does not match payload")
                record = {"kind": kind, "sequence": sequence, "rate": rate, "channels": channels,
                          "bits": bits, "frames": frames, "timestamp_us": timestamp}
                records.write(json.dumps(record) + "\n")
                report["records"] += 1
                name = {1: "hardware_rx", 2: "processed_mic", 3: "dac_played", 4: "speaker_pcm"}[kind]
                stream = report["streams"].setdefault(name, {"rate": rate, "channels": channels, "bits": bits,
                                                            "frames": 0, "records": 0, "first_timestamp_us": timestamp})
                stream["frames"] += frames
                stream["records"] += 1
                stream["last_timestamp_us"] = timestamp
                if kind == 3:
                    continue
                current_format = (rate, channels, bits)
                if kind not in writers:
                    writer = wave.open(str(output / f"{name}.wav"), "wb")
                    writer.setframerate(rate)
                    writer.setnchannels(channels)
                    writer.setsampwidth(bits // 8)
                    writers[kind] = writer
                    formats[kind] = current_format
                elif formats[kind] != current_format:
                    raise ValueError("PCM format changed during one diagnostic capture")
                writers[kind].writeframesraw(payload)
        report["finished_epoch"] = time.time()
        if report["records"] == 0:
            raise ValueError("audio capture contains no audio or playback records")
        if not report["valid"]:
            raise ValueError("diagnostic capture overflowed or had multiple producers")
        return report
    except Exception as exc:
        report["valid"] = False
        report["error"] = str(exc)
        raise
    finally:
        for writer in writers.values():
            writer.close()
        (output / "capture.json").write_text(json.dumps(report, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--port", type=int, default=19091)
    parser.add_argument("--timeout", type=float, default=45)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("use a new output directory for each capture")
    with socket.create_server(("0.0.0.0", args.port)) as server:
        server.settimeout(args.timeout)
        print("Audio capture receiver ready", flush=True)
        connection, _ = server.accept()
        with connection:
            connection.settimeout(args.timeout)
            result = receive_capture(connection, args.output)
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
