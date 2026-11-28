"""Bounded, path-safe storage helpers for opt-in audio diagnostics."""

from __future__ import annotations

import contextlib
from contextlib import contextmanager
import hashlib
from pathlib import Path
import secrets
import threading
from collections.abc import Iterator


DEBUG_CAPTURE_MAX_FILES = 24
DEBUG_CAPTURE_MAX_BYTES = 64 * 1024 * 1024
DEBUG_CAPTURE_MAX_PENDING_WRITES = 4
_CAPTURE_IO_LOCK = threading.RLock()
_CAPTURE_WRITE_SLOTS = threading.BoundedSemaphore(
    DEBUG_CAPTURE_MAX_PENDING_WRITES
)
_CAPTURE_SLOT_LOCK = threading.Lock()
_capture_pending_writes = 0
_CAPTURE_GROUP_SUFFIXES = (
    "_ha_ws_rtp_to_browser.wav",
    "_ha_ws_browser_to_rtp.wav",
    "_ha_ws_timing.json",
    "_left_rx.wav",
    "_right_rx.wav",
)


def try_reserve_debug_capture_write() -> bool:
    """Reserve one bounded cross-runtime diagnostic writer without blocking."""

    global _capture_pending_writes
    with _CAPTURE_SLOT_LOCK:
        if not _CAPTURE_WRITE_SLOTS.acquire(blocking=False):
            return False
        _capture_pending_writes += 1
        return True


def release_debug_capture_write() -> None:
    """Release one diagnostic writer reservation."""

    global _capture_pending_writes
    with _CAPTURE_SLOT_LOCK:
        _CAPTURE_WRITE_SLOTS.release()
        _capture_pending_writes -= 1


def debug_capture_pending_writes() -> int:
    """Return the current cross-runtime diagnostic writer occupancy."""

    with _CAPTURE_SLOT_LOCK:
        return _capture_pending_writes


def ensure_debug_capture_dir(directory: Path) -> None:
    """Create the opt-in capture directory with user-only permissions."""

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)


def safe_capture_name(value: object) -> str:
    """Return a collision-resistant stem that cannot escape the debug directory."""

    original = str(value or "call")
    readable = "".join(ch if ch.isascii() and (ch.isalnum() or ch in {"-", "_"}) else "_" for ch in original)
    readable = readable.strip("_")[:64] or "call"
    digest = hashlib.sha256(original.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{readable}_{digest}"


def capture_session_name(value: object) -> str:
    """Return a path-safe stem unique to one diagnostic capture session."""

    return f"{safe_capture_name(value)}_{secrets.token_hex(6)}"


@contextmanager
def debug_capture_transaction(
    directory: Path,
) -> Iterator[None]:
    """Serialize one atomic capture group and its retention pass."""

    with _CAPTURE_IO_LOCK:
        ensure_debug_capture_dir(directory)
        yield


def capture_temp_path(path: Path) -> Path:
    """Return a collision-resistant temporary sibling for an artifact."""

    return path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")


def commit_capture_file(temporary: Path, destination: Path) -> None:
    """Atomically publish a user-private diagnostic artifact."""

    temporary.chmod(0o600)
    temporary.replace(destination)
    destination.chmod(0o600)


def wav_pcm_payload(fmt: object, payload: bytes | bytearray) -> tuple[int, bytes]:
    """Return a WAV-compatible sample width and little-endian PCM payload."""

    width = int(getattr(fmt, "container_bytes_per_sample", 2))
    pcm_format = getattr(getattr(fmt, "pcm_format", None), "value", "")
    if pcm_format != "s24le_in_s32":
        return width, bytes(payload)

    # The runtime format stores sign-extended 24-bit samples right-aligned in a
    # 32-bit container. WAV's packed 24-bit representation omits that fourth
    # sign byte; writing the container as 32-bit PCM would attenuate playback.
    raw = memoryview(payload)
    samples = len(raw) // 4
    packed = bytearray(samples * 3)
    packed[0::3] = raw[0 : samples * 4 : 4]
    packed[1::3] = raw[1 : samples * 4 : 4]
    packed[2::3] = raw[2 : samples * 4 : 4]
    return 3, bytes(packed)


def _capture_group_key(path: Path) -> str:
    """Return the unique session stem shared by one capture's artifacts."""

    name = path.name
    for suffix in _CAPTURE_GROUP_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def prune_debug_captures(
    directory: Path,
    *,
    max_files: int = DEBUG_CAPTURE_MAX_FILES,
    max_bytes: int = DEBUG_CAPTURE_MAX_BYTES,
) -> None:
    """Keep only the newest bounded set of WAV/JSON diagnostic artifacts."""

    with _CAPTURE_IO_LOCK:
        if not directory.exists():
            return
        groups: dict[str, list[tuple[int, int, Path]]] = {}
        for path in directory.iterdir():
            if not path.is_file():
                continue
            if path.suffix.lower() == ".tmp":
                # The transaction lock guarantees that no writer in this
                # process currently owns a temporary file.  Anything left here
                # came from an interrupted/crashed capture and is safe to reap.
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            if path.suffix.lower() not in {".wav", ".json"}:
                continue
            with contextlib.suppress(OSError):
                stat = path.stat()
                groups.setdefault(_capture_group_key(path), []).append(
                    (stat.st_mtime_ns, stat.st_size, path)
                )
        ordered_groups = sorted(
            groups.values(),
            key=lambda group: max(item[0] for item in group),
            reverse=True,
        )
        kept_files = 0
        kept_bytes = 0
        file_limit = max(0, int(max_files))
        byte_limit = max(0, int(max_bytes))
        for group in ordered_groups:
            group_files = len(group)
            group_bytes = sum(item[1] for item in group)
            if (
                kept_files + group_files <= file_limit
                and kept_bytes + group_bytes <= byte_limit
            ):
                kept_files += group_files
                kept_bytes += group_bytes
                continue
            for _mtime, _size, path in group:
                with contextlib.suppress(OSError):
                    path.unlink()
