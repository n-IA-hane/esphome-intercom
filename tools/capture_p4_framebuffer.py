#!/usr/bin/env python3
"""Receive one RGB565 P4 framebuffer and save native and rotated PNG files."""

from __future__ import annotations

import argparse
import socket
from pathlib import Path

import numpy as np
from PIL import Image


def receive_exact(connection: socket.socket, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = connection.recv(min(65536, remaining))
        if not chunk:
            raise RuntimeError(f"capture ended with {remaining} bytes missing")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=19090)
    parser.add_argument("--rotation", type=int, choices=(0, 90, 180, 270), default=90)
    args = parser.parse_args()
    with socket.create_server((args.bind, args.port), reuse_port=False) as server:
        server.settimeout(30)
        connection, _ = server.accept()
        with connection:
            header = bytearray()
            while not header.endswith(b"\n"):
                header.extend(receive_exact(connection, 1))
                if len(header) > 64:
                    raise RuntimeError("invalid framebuffer header")
            magic, width, height, size = header.decode().split()
            if magic != "P4FB1":
                raise RuntimeError(f"unexpected framebuffer magic {magic!r}")
            width, height, size = int(width), int(height), int(size)
            raw = receive_exact(connection, size)
    pixels = np.frombuffer(raw, dtype="<u2").reshape(height, width)
    rgb = np.empty((height, width, 3), dtype=np.uint8)
    rgb[..., 0] = ((pixels >> 11) & 0x1F) * 255 // 31
    rgb[..., 1] = ((pixels >> 5) & 0x3F) * 255 // 63
    rgb[..., 2] = (pixels & 0x1F) * 255 // 31
    image = Image.fromarray(rgb, "RGB")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    image.save(args.output.with_name(f"{args.output.stem}-native.png"))
    if args.rotation:
        image = image.rotate(-args.rotation, expand=True)
    image.save(args.output)
    print(f"captured={args.output} size={image.width}x{image.height}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
