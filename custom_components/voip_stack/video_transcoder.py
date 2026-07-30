"""Bounded FFmpeg helpers for optional SIP video interoperability.

The normal video path never imports a codec library or starts a process.  This
module is used either when config-flow opt-in permits receive transcoding or
when a browser JPEG must be normalized to RFC 2435's fixed Huffman tables.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import logging
import shutil
import socket
from typing import TYPE_CHECKING

from .const import DOMAIN
from .sdp import RtpVideoFormat
from .session_cleanup import async_wait_for_cleanup

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


_LOGGER = logging.getLogger(__name__)
_ACTIVE_TRANSCODER = "video_transcoder_active"
_TRANSCODER_LOCK = "video_transcoder_lock"
_MAX_FMTP_LENGTH = 1024
_PROCESS_STOP_TIMEOUT = 2.0
_JPEG_NORMALIZE_TIMEOUT = 1.5
_JPEG_NORMALIZE_MAX_BYTES = 1024 * 1024
_JPEG_MULTIPART_INPUT_BOUNDARY = "voipstackin"
_JPEG_MULTIPART_OUTPUT_BOUNDARY = "voipstackout"


class VideoTranscoderError(RuntimeError):
    """The optional video transcoder could not be started or used."""


async def _claim_transcoder_slot(hass: HomeAssistant, owner: object) -> None:
    """Bound every FFmpeg video bridge to one integration-owned process slot."""

    bucket = hass.data.setdefault(DOMAIN, {})
    lock = bucket.setdefault(_TRANSCODER_LOCK, asyncio.Lock())
    async with lock:
        active = bucket.get(_ACTIVE_TRANSCODER)
        if active is not None and active is not owner:
            raise VideoTranscoderError("another SIP video transcode is active")
        bucket[_ACTIVE_TRANSCODER] = owner


async def _release_transcoder_slot(hass: HomeAssistant, owner: object) -> None:
    bucket = hass.data.setdefault(DOMAIN, {})
    lock = bucket.setdefault(_TRANSCODER_LOCK, asyncio.Lock())
    async with lock:
        if bucket.get(_ACTIVE_TRANSCODER) is owner:
            bucket.pop(_ACTIVE_TRANSCODER, None)


def _available_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _ffmpeg_binary(hass: HomeAssistant) -> str:
    manager = hass.data.get("ffmpeg")
    configured = str(getattr(manager, "binary", "") or "").strip()
    binary = configured or shutil.which("ffmpeg") or ""
    if not binary:
        raise VideoTranscoderError("FFmpeg is unavailable; continuing audio-only")
    return binary


def _input_sdp(video_format: RtpVideoFormat, port: int) -> str:
    encoding = str(video_format.encoding or "").upper()
    if encoding not in {"H263", "H263P", "H264", "H265", "JPEG", "VP8", "VP9", "AV1"}:
        raise VideoTranscoderError(f"unsupported FFmpeg RTP input codec {encoding}")
    rtp_encoding = {"H263P": "H263-1998"}.get(encoding, encoding)
    fmtp = str(video_format.fmtp or "").replace("\r", " ").replace("\n", " ").strip()
    if len(fmtp) > _MAX_FMTP_LENGTH:
        raise VideoTranscoderError("video fmtp exceeds safety limit")
    lines = [
        "v=0",
        "o=- 0 0 IN IP4 127.0.0.1",
        "s=VoIP Stack transcoder",
        "c=IN IP4 127.0.0.1",
        "t=0 0",
        f"m=video {int(port)} {video_format.transport_profile} {int(video_format.payload_type)}",
        f"a=rtpmap:{int(video_format.payload_type)} {rtp_encoding}/{int(video_format.clock_rate)}",
        "a=recvonly",
    ]
    if fmtp:
        lines.append(f"a=fmtp:{int(video_format.payload_type)} {fmtp}")
    return "\r\n".join(lines) + "\r\n"


@dataclass(slots=True)
class FfmpegVideoTranscoder:
    """One receive-only RTP codec conversion into browser-friendly VP8."""

    hass: HomeAssistant
    call_id: str
    input_format: RtpVideoFormat
    output_port: int
    input_port: int = 0
    process: asyncio.subprocess.Process | None = None
    _send_socket: socket.socket | None = None
    _stderr_task: asyncio.Task[None] | None = None
    stderr_tail: list[str] = field(default_factory=list, init=False)
    _released: bool = field(default=False, init=False)
    _lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _start_task: asyncio.Task[None] | None = field(default=None, init=False)
    _cleanup_task: asyncio.Task[None] | None = field(default=None, init=False)
    _close_requested: bool = field(default=False, init=False)

    async def async_start(self) -> None:
        async with self._lifecycle_lock:
            if self.process is not None and self.process.returncode is None:
                return
            if self._close_requested or self._released:
                raise VideoTranscoderError("SIP video transcoder has already been closed")
            task = self._start_task
            if task is None:
                task = asyncio.create_task(
                    self._async_start_impl(),
                    name=f"voip-video-transcoder-start-{self.call_id}",
                )
                self._start_task = task
        try:
            await task
        finally:
            async with self._lifecycle_lock:
                if self._start_task is task and task.done():
                    self._start_task = None

    async def _async_start_impl(self) -> None:
        await _claim_transcoder_slot(self.hass, self)
        process: asyncio.subprocess.Process | None = None
        send_socket: socket.socket | None = None
        stderr_task: asyncio.Task[None] | None = None
        try:
            self.input_port = _available_udp_port()
            command = [
                _ffmpeg_binary(self.hass),
                "-hide_banner",
                "-loglevel", "warning",
                "-nostdin",
                "-protocol_whitelist", "file,pipe,udp,rtp",
                "-fflags", "+nobuffer+discardcorrupt",
                "-flags", "low_delay",
                "-analyzeduration", "0",
                # SDP already declares codec and payload type. A small probe
                # prevents low-bitrate door cameras from adding seconds of
                # startup latency while FFmpeg waits for 32 KiB of RTP.
                "-probesize", "2048",
                "-f", "sdp",
                "-i", "pipe:0",
                "-map", "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-vf", "fps=15,scale='min(1280,iw)':-2:force_original_aspect_ratio=decrease",
                "-pix_fmt", "yuv420p",
                "-c:v", "libvpx",
                "-deadline", "realtime",
                "-cpu-used", "8",
                "-threads", "1",
                "-b:v", "700k",
                "-maxrate", "900k",
                "-bufsize", "1400k",
                "-g", "30",
                "-keyint_min", "30",
                "-f", "rtp",
                "-payload_type", "103",
                f"rtp://127.0.0.1:{int(self.output_port)}?pkt_size=1200",
            ]
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            assert process.stdin is not None
            process.stdin.write(_input_sdp(self.input_format, self.input_port).encode())
            await process.stdin.drain()
            process.stdin.close()
            send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            send_socket.setblocking(False)
            stderr_task = asyncio.create_task(
                self._drain_stderr(process),
                name=f"voip-video-transcoder-stderr-{self.call_id}",
            )
            async with self._lifecycle_lock:
                if self._close_requested or self._released:
                    raise asyncio.CancelledError
                self.process = process
                self._send_socket = send_socket
                self._stderr_task = stderr_task
                process = None
                send_socket = None
                stderr_task = None
            _LOGGER.info(
                "Started optional SIP video transcode call_id=%s input=%s loopback=%s output=VP8/%s",
                self.call_id,
                self.input_format.wire_token(),
                self.input_port,
                self.output_port,
            )
        except BaseException:
            # Cancellation is a normal unload path and must release the
            # process/pipe as well as the singleton transcode slot.
            cleanup = asyncio.create_task(
                self._dispose_resources(
                    process=process,
                    send_socket=send_socket,
                    stderr_task=stderr_task,
                    release_slot=True,
                ),
                name=f"voip-video-transcoder-start-cleanup-{self.call_id}",
            )
            await async_wait_for_cleanup(cleanup)
            raise

    def send_rtp(self, data: bytes) -> None:
        if self._send_socket is None or self.process is None or self.process.returncode is not None:
            raise VideoTranscoderError("FFmpeg video transcoder stopped")
        self._send_socket.sendto(data, ("127.0.0.1", int(self.input_port)))

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        if process.stderr is None:
            return
        lines_seen = 0
        while line := await process.stderr.readline():
            text = line.decode(errors="replace").rstrip()
            self.stderr_tail.append(text)
            del self.stderr_tail[:-20]
            lines_seen += 1
            if lines_seen & (lines_seen - 1) == 0:
                _LOGGER.debug(
                    "FFmpeg SIP video messages=%s latest=%s",
                    lines_seen,
                    text,
                )

    async def async_close(self) -> None:
        """Finish one idempotent cleanup even if the waiting caller is cancelled."""

        async with self._lifecycle_lock:
            self._close_requested = True
            task = self._cleanup_task
            if task is None:
                task = asyncio.create_task(
                    self._async_close_impl(),
                    name=f"voip-video-transcoder-close-{self.call_id}",
                )
                self._cleanup_task = task
        await async_wait_for_cleanup(task)

    async def _async_close_impl(self) -> None:
        async with self._lifecycle_lock:
            start_task = self._start_task
            if start_task is not None and not start_task.done():
                start_task.cancel()
        if start_task is not None and start_task is not asyncio.current_task():
            await asyncio.gather(start_task, return_exceptions=True)
        async with self._lifecycle_lock:
            process = self.process
            self.process = None
            send_socket = self._send_socket
            self._send_socket = None
            stderr_task = self._stderr_task
            self._stderr_task = None
        await self._dispose_resources(
            process=process,
            send_socket=send_socket,
            stderr_task=stderr_task,
            release_slot=True,
        )
        _LOGGER.info("Stopped optional SIP video transcode call_id=%s", self.call_id)

    async def _dispose_resources(
        self,
        *,
        process: asyncio.subprocess.Process | None,
        send_socket: socket.socket | None,
        stderr_task: asyncio.Task[None] | None,
        release_slot: bool,
    ) -> None:
        """Dispose detached FFmpeg resources and release singleton ownership."""

        try:
            if send_socket is not None:
                send_socket.close()
            if process is not None:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.terminate()
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=_PROCESS_STOP_TIMEOUT
                    )
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
        finally:
            if stderr_task is not None:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
            # Never strand the single bounded transcode slot because FFmpeg
            # exited between the returncode check and process cleanup.
            if release_slot:
                await _release_transcoder_slot(self.hass, self)
            self._released = True


@dataclass(slots=True)
class FfmpegJpegNormalizer:
    """Persistently re-encode browser JPEGs for RFC 2435 packetization.

    Canvas encoders may optimize Huffman tables per frame, while RFC 2435
    types 0/1 require JPEG Annex K's fixed tables. This bounded helper owns no
    SIP or RTP state: it maps one complete JPEG to one complete JPEG and stays
    subordinate to the existing video WebSocket media owner.
    """

    hass: HomeAssistant
    call_id: str
    process: asyncio.subprocess.Process | None = None
    stderr_tail: list[str] = field(default_factory=list, init=False)
    _stderr_task: asyncio.Task[None] | None = field(default=None, init=False)
    _lifecycle_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _frame_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    _cleanup_task: asyncio.Task[None] | None = field(default=None, init=False)
    _close_requested: bool = field(default=False, init=False)
    _released: bool = field(default=False, init=False)

    async def async_start(self) -> None:
        async with self._lifecycle_lock:
            if self.process is not None and self.process.returncode is None:
                return
            if self._close_requested or self._released:
                raise VideoTranscoderError("JPEG normalizer has already been closed")
            await _claim_transcoder_slot(self.hass, self)
            process: asyncio.subprocess.Process | None = None
            stderr_task: asyncio.Task[None] | None = None
            try:
                process = await asyncio.create_subprocess_exec(
                    _ffmpeg_binary(self.hass),
                    "-hide_banner",
                    "-loglevel",
                    "warning",
                    "-nostdin",
                    "-protocol_whitelist",
                    "pipe",
                    "-threads",
                    "1",
                    "-f",
                    "mpjpeg",
                    "-i",
                    "pipe:0",
                    "-map",
                    "0:v:0",
                    "-an",
                    "-sn",
                    "-dn",
                    "-pix_fmt",
                    "yuvj420p",
                    "-c:v",
                    "mjpeg",
                    "-huffman",
                    "default",
                    "-force_duplicated_matrix",
                    "1",
                    "-q:v",
                    "5",
                    "-threads",
                    "1",
                    "-fps_mode",
                    "passthrough",
                    "-flush_packets",
                    "1",
                    "-f",
                    "mpjpeg",
                    "-boundary_tag",
                    _JPEG_MULTIPART_OUTPUT_BOUNDARY,
                    "pipe:1",
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=_JPEG_NORMALIZE_MAX_BYTES * 2,
                )
                stderr_task = asyncio.create_task(
                    self._drain_stderr(process),
                    name=f"voip-jpeg-normalizer-stderr-{self.call_id}",
                )
                if self._close_requested:
                    raise asyncio.CancelledError
                self.process = process
                self._stderr_task = stderr_task
                process = None
                stderr_task = None
                _LOGGER.info(
                    "Started SIP browser JPEG normalizer call_id=%s",
                    self.call_id,
                )
            except BaseException:
                await self._dispose(
                    process=process,
                    stderr_task=stderr_task,
                    release_slot=True,
                )
                raise

    async def async_normalize(self, frame: bytes) -> bytes:
        """Normalize one JPEG with one in-flight frame and pipe backpressure."""

        data = bytes(frame)
        if (
            not 4 <= len(data) <= _JPEG_NORMALIZE_MAX_BYTES
            or not data.startswith(b"\xff\xd8")
            or not data.endswith(b"\xff\xd9")
        ):
            raise VideoTranscoderError("invalid browser JPEG access unit")
        async with self._frame_lock:
            await self.async_start()
            process = self.process
            if (
                process is None
                or process.returncode is not None
                or process.stdin is None
                or process.stdout is None
            ):
                raise VideoTranscoderError("JPEG normalizer stopped")
            prefix = (
                f"--{_JPEG_MULTIPART_INPUT_BOUNDARY}\r\n"
                "Content-Type: image/jpeg\r\n"
                f"Content-Length: {len(data)}\r\n\r\n"
            ).encode()
            try:
                async with asyncio.timeout(_JPEG_NORMALIZE_TIMEOUT):
                    process.stdin.write(prefix)
                    process.stdin.write(data)
                    process.stdin.write(b"\r\n")
                    await process.stdin.drain()
                    header = await process.stdout.readuntil(b"\r\n\r\n")
                    if len(header) > 4096:
                        raise VideoTranscoderError(
                            "JPEG normalizer emitted an oversized header"
                        )
                    content_length = 0
                    content_type = ""
                    for raw_line in header.decode("ascii", errors="strict").splitlines():
                        name, separator, value = raw_line.partition(":")
                        if not separator:
                            continue
                        if name.strip().lower() == "content-length":
                            content_length = int(value.strip())
                        elif name.strip().lower() == "content-type":
                            content_type = value.strip().lower()
                    if (
                        content_type != "image/jpeg"
                        or not 4 <= content_length <= _JPEG_NORMALIZE_MAX_BYTES
                    ):
                        raise VideoTranscoderError(
                            "JPEG normalizer emitted an invalid multipart frame"
                        )
                    output = await process.stdout.readexactly(content_length)
                if not output.startswith(b"\xff\xd8") or not output.endswith(b"\xff\xd9"):
                    raise VideoTranscoderError("JPEG normalizer emitted invalid JPEG")
                return output
            except asyncio.CancelledError:
                cleanup = asyncio.create_task(self.async_close())
                await async_wait_for_cleanup(cleanup)
                raise
            except (
                BrokenPipeError,
                ConnectionError,
                EOFError,
                TimeoutError,
                UnicodeError,
                ValueError,
                VideoTranscoderError,
                asyncio.IncompleteReadError,
                asyncio.LimitOverrunError,
            ) as err:
                cleanup = asyncio.create_task(self.async_close())
                await async_wait_for_cleanup(cleanup)
                raise VideoTranscoderError(
                    f"JPEG normalizer failed: {err}"
                ) from err

    async def _drain_stderr(self, process: asyncio.subprocess.Process) -> None:
        if process.stderr is None:
            return
        lines_seen = 0
        while line := await process.stderr.readline():
            text = line.decode(errors="replace").rstrip()
            self.stderr_tail.append(text)
            del self.stderr_tail[:-20]
            lines_seen += 1
            if lines_seen & (lines_seen - 1) == 0:
                _LOGGER.debug(
                    "FFmpeg SIP JPEG normalizer call_id=%s messages=%s latest=%s",
                    self.call_id,
                    lines_seen,
                    text,
                )

    async def async_close(self) -> None:
        async with self._lifecycle_lock:
            self._close_requested = True
            task = self._cleanup_task
            if task is None:
                process = self.process
                self.process = None
                stderr_task = self._stderr_task
                self._stderr_task = None
                task = asyncio.create_task(
                    self._dispose(
                        process=process,
                        stderr_task=stderr_task,
                        release_slot=True,
                    ),
                    name=f"voip-jpeg-normalizer-close-{self.call_id}",
                )
                self._cleanup_task = task
        await async_wait_for_cleanup(task)

    async def _dispose(
        self,
        *,
        process: asyncio.subprocess.Process | None,
        stderr_task: asyncio.Task[None] | None,
        release_slot: bool,
    ) -> None:
        try:
            if process is not None:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.terminate()
                try:
                    await asyncio.wait_for(
                        process.wait(), timeout=_PROCESS_STOP_TIMEOUT
                    )
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
        finally:
            if stderr_task is not None:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
            if release_slot:
                await _release_transcoder_slot(self.hass, self)
            self._released = True
        _LOGGER.info("Stopped SIP browser JPEG normalizer call_id=%s", self.call_id)
