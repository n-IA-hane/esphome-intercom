"""Opt-in, corpus-validated external ITU G.191 G726 reference for test peers.

The reference source/binary stay private. No product codec is used as the peer
encoder or decoder. FFmpeg is used only for standard G.711 PCM conversion.
"""

from __future__ import annotations

from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile

REVISION = "5a9febb4793df3922dffbd0ea68e040de6278de3"
SOURCE_HASHES = {
    "g726.c": "54aa253e701b3fc3c0878857016d58e8bd811a17640daec0d5b649925c5ce44b",
    "g726.h": "d936b4b48b7d1ffb44266e3a22dd4225a76478a36c88afbe18926432e0a0390e",
    "LICENSE.md": "b29d836c9f40f3fed28d42dc0d8a73730fe6362da60c88d948cb915c968f573e",
}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def pack_rtp(codewords):
    """RFC3551 4.5.4: first codeword takes the least significant bits."""
    if len(codewords) % 8 or any(value > 31 for value in codewords):
        raise ValueError("G72640 RTP requires complete five-octet/eight-sample groups")
    output = bytearray()
    pending = bits = 0
    for value in codewords:
        pending |= value << bits
        bits += 5
        while bits >= 8:
            output.append(pending & 255)
            pending >>= 8
            bits -= 8
    assert bits == 0
    return bytes(output)


def unpack_rtp(payload):
    if not payload or len(payload) % 5:
        raise ValueError("G72640 RTP ends inside a five-octet/eight-sample group")
    output = bytearray()
    pending = bits = 0
    for value in payload:
        pending |= value << bits
        bits += 8
        while bits >= 5:
            output.append(pending & 31)
            pending >>= 5
            bits -= 5
    assert bits == 0
    return bytes(output)


def _g711(value, *, encode):
    source = "s16le" if encode else "mulaw"
    target = "mulaw" if encode else "s16le"
    return subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            source,
            "-ar",
            "8000",
            "-ac",
            "1",
            "-i",
            "pipe:0",
            "-f",
            target,
            "pipe:1",
        ],
        input=value,
        capture_output=True,
        check=True,
        timeout=5,
    ).stdout


class ItuG726Reference:
    def __init__(self, root, manifest):
        self.root = root
        self.manifest = manifest
        if (
            manifest["source_revision"] != REVISION
            or manifest["sample_byteorder"] != sys.byteorder
        ):
            raise ValueError("Unapproved ITU source revision or reference byte order")
        for name, digest in SOURCE_HASHES.items():
            entry = manifest["sources"][name]
            if entry["sha256"] != digest or _sha(entry["path"]) != digest:
                raise ValueError(f"ITU reference source hash mismatch: {name}")
        self.binary = Path(manifest["binary"]["path"])
        if (
            not os.access(self.binary, os.X_OK)
            or _sha(self.binary) != manifest["binary"]["sha256"]
        ):
            raise ValueError("ITU reference binary hash mismatch or not executable")
        if _sha(manifest["adapter"]["path"]) != manifest["adapter"]["sha256"]:
            raise ValueError("ITU reference CLI adapter hash mismatch")
        self.golden = self._validate_corpus()

    def symbols(self, mode, value):
        if _sha(self.binary) != self.manifest["binary"]["sha256"]:
            raise ValueError("ITU reference binary changed after qualification")
        order = "<" if sys.byteorder == "little" else ">"
        with tempfile.TemporaryDirectory(prefix="itu-g726-", dir=self.root) as scratch:
            input_path, output_path = (
                Path(scratch) / "input.s16",
                Path(scratch) / "output.s16",
            )
            input_path.write_bytes(struct.pack(order + str(len(value)) + "H", *value))
            subprocess.run(
                [str(self.binary), mode, "5", "0", str(input_path), str(output_path)],
                check=True,
                capture_output=True,
                timeout=5,
            )
            raw = output_path.read_bytes()
            if len(raw) != len(value) * 2:
                raise ValueError("ITU reference returned the wrong sample count")
            result = struct.unpack(order + str(len(value)) + "H", raw)
            if any(item > (31 if mode == "enc" else 255) for item in result):
                raise ValueError(
                    "ITU reference returned invalid codewords/G.711 values"
                )
            return bytes(result)

    def _validate_corpus(self):
        metadata = json.loads(
            (
                Path(__file__).parents[1] / "fixtures/audio_codec_sources.json"
            ).read_text()
        )["files"]
        values = {}
        for name, relative in [
            ("input", "INPUT/NRM.M"),
            ("encoded", "RESET/40/RN40FM.I"),
            ("decoded", "RESET/40/RN40FM.O"),
        ]:
            key = "itu-g726/DISK1/" + relative
            path = self.root / key
            if _sha(path) != metadata[key]:
                raise ValueError(f"ITU G72640 corpus hash mismatch: {key}")
            raw = bytes.fromhex(path.read_text().rstrip("\x1a\n\r"))
            if sum(raw[:-1]) % 255 != raw[-1]:
                raise ValueError("ITU G72640 vector checksum mismatch")
            values[name] = raw[:-1]
        encoded = self.symbols("enc", values["input"])
        decoded = self.symbols("dec", values["encoded"])
        if encoded != values["encoded"] or decoded != values["decoded"]:
            raise ValueError("ITU reference does not pass the unmodified bit corpus")
        return {
            "samples": len(encoded),
            "encoder_bit_exact": True,
            "decoder_bit_exact": True,
            "encoded_sha256": hashlib.sha256(encoded).hexdigest(),
            "decoded_sha256": hashlib.sha256(decoded).hexdigest(),
        }

    @property
    def provenance(self):
        return {
            "source_revision": REVISION,
            "source_hashes": SOURCE_HASHES,
            "binary_sha256": self.manifest["binary"]["sha256"],
            "adapter_sha256": self.manifest["adapter"]["sha256"],
            "corpus": self.golden,
        }

    def encode_pcm(self, pcm):
        return pack_rtp(self.symbols("enc", _g711(pcm, encode=True)))

    def decode_payload(self, payload):
        return _g711(self.symbols("dec", unpack_rtp(payload)), encode=False)


@lru_cache(maxsize=4)
def _load(root):
    root = Path(root)
    path = root / "itu-stl-g726/reference.json"
    if not path.is_file():
        return None
    return ItuG726Reference(root, json.loads(path.read_text()))


def qualified_reference():
    """Absence is an explicit gap; an invalid opted-in reference is a failure."""
    root = os.environ.get("VOIP_CODEC_REFERENCE_DIR")
    return _load(str(Path(root).resolve())) if root else None
