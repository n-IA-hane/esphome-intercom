from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import sys
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _load(name: str):
    if "custom_components" not in sys.modules:
        package = types.ModuleType("custom_components")
        package.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = package
    if PKG_NAME not in sys.modules:
        package = types.ModuleType(PKG_NAME)
        package.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = package
    full_name = f"{PKG_NAME}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


_load("const")
sdp = _load("sdp")
rtp = _load("rtp")
video_rtp = _load("video_rtp")
video_transcoder = _load("video_transcoder")


class _Hass:
    def __init__(self) -> None:
        self.data: dict = {}


def _parse_multipart_jpegs(data: bytes) -> list[bytes]:
    """Extract length-delimited JPEGs from FFmpeg's mpjpeg test output."""

    frames: list[bytes] = []
    offset = 0
    while offset < len(data):
        header_end = data.find(b"\r\n\r\n", offset)
        if header_end < 0:
            break
        content_length = 0
        for line in data[offset:header_end].decode("ascii", errors="strict").splitlines():
            name, separator, value = line.partition(":")
            if separator and name.strip().lower() == "content-length":
                content_length = int(value.strip())
        if content_length <= 0:
            raise AssertionError("FFmpeg mpjpeg frame has no Content-Length")
        frame_start = header_end + 4
        frame_end = frame_start + content_length
        if frame_end > len(data):
            raise AssertionError("FFmpeg mpjpeg frame is truncated")
        frame = data[frame_start:frame_end]
        if not frame.startswith(b"\xff\xd8") or not frame.endswith(b"\xff\xd9"):
            raise AssertionError("FFmpeg mpjpeg part is not a complete JPEG")
        frames.append(frame)
        offset = frame_end
    return frames


class VideoTranscoderPolicyTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _blocking_jpeg_process():
        output_read = asyncio.Event()

        async def block_output(_separator: bytes) -> bytes:
            output_read.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        async def block_stderr() -> bytes:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        process = mock.Mock()
        process.returncode = None
        process.stdin = mock.Mock()
        process.stdin.drain = mock.AsyncMock()
        process.stdout = mock.Mock()
        process.stdout.readuntil = mock.AsyncMock(side_effect=block_output)
        process.stderr = mock.Mock()
        process.stderr.readline = mock.AsyncMock(side_effect=block_stderr)

        def terminate() -> None:
            process.returncode = 0

        process.terminate.side_effect = terminate
        process.wait = mock.AsyncMock(return_value=0)
        return process, output_read

    def test_ffmpeg_binary_prefers_home_assistant_manager(self) -> None:
        hass = _Hass()
        hass.data["ffmpeg"] = types.SimpleNamespace(binary=" /opt/ha/ffmpeg ")
        with mock.patch.object(video_transcoder.shutil, "which", return_value="/usr/bin/ffmpeg"):
            self.assertEqual(video_transcoder._ffmpeg_binary(hass), "/opt/ha/ffmpeg")

    def test_ffmpeg_binary_falls_back_to_path_and_fails_cleanly(self) -> None:
        hass = _Hass()
        with mock.patch.object(video_transcoder.shutil, "which", return_value="/usr/bin/ffmpeg"):
            self.assertEqual(video_transcoder._ffmpeg_binary(hass), "/usr/bin/ffmpeg")
        with mock.patch.object(video_transcoder.shutil, "which", return_value=None):
            with self.assertRaisesRegex(video_transcoder.VideoTranscoderError, "unavailable"):
                video_transcoder._ffmpeg_binary(hass)

    def test_input_sdp_normalizes_h263p_and_sanitizes_fmtp(self) -> None:
        video_format = sdp.RtpVideoFormat(
            payload_type=102,
            encoding="H263P",
            fmtp="CIF=1\r\na=sendrecv",
        )
        description = video_transcoder._input_sdp(video_format, 45678)
        self.assertIn("m=video 45678 RTP/AVP 102\r\n", description)
        self.assertIn("a=rtpmap:102 H263-1998/90000\r\n", description)
        self.assertIn("a=fmtp:102 CIF=1  a=sendrecv\r\n", description)
        self.assertEqual(description.count("a=sendrecv"), 1)

    def test_input_sdp_rejects_unknown_codec_and_oversized_fmtp(self) -> None:
        with self.assertRaisesRegex(video_transcoder.VideoTranscoderError, "unsupported"):
            video_transcoder._input_sdp(
                sdp.RtpVideoFormat(payload_type=102, encoding="THEORA"),
                45678,
            )
        with self.assertRaisesRegex(video_transcoder.VideoTranscoderError, "safety limit"):
            video_transcoder._input_sdp(
                sdp.RtpVideoFormat(
                    payload_type=102,
                    encoding="H264",
                    fmtp="x" * (video_transcoder._MAX_FMTP_LENGTH + 1),
                ),
                45678,
            )

    async def test_only_one_transcoder_can_own_the_bounded_slot(self) -> None:
        hass = _Hass()
        active = object()
        hass.data[video_transcoder.DOMAIN] = {
            video_transcoder._ACTIVE_TRANSCODER: active,
        }
        contender = video_transcoder.FfmpegVideoTranscoder(
            hass=hass,
            call_id="second",
            input_format=sdp.RtpVideoFormat(payload_type=34, encoding="H263"),
            output_port=45678,
        )
        with self.assertRaisesRegex(video_transcoder.VideoTranscoderError, "another"):
            await contender.async_start()
        self.assertIs(
            hass.data[video_transcoder.DOMAIN][video_transcoder._ACTIVE_TRANSCODER],
            active,
        )

    async def test_jpeg_normalizer_shares_and_releases_transcoder_slot(self) -> None:
        hass = _Hass()
        process, _output_read = self._blocking_jpeg_process()
        normalizer = video_transcoder.FfmpegJpegNormalizer(
            hass=hass,
            call_id="jpeg-slot",
        )
        contender = video_transcoder.FfmpegVideoTranscoder(
            hass=hass,
            call_id="rtp-slot",
            input_format=sdp.RtpVideoFormat(payload_type=34, encoding="H263"),
            output_port=45678,
        )

        with (
            mock.patch.object(video_transcoder, "_ffmpeg_binary", return_value="ffmpeg"),
            mock.patch.object(
                video_transcoder.asyncio,
                "create_subprocess_exec",
                new=mock.AsyncMock(return_value=process),
            ),
        ):
            await normalizer.async_start()
            self.assertIs(
                hass.data[video_transcoder.DOMAIN][
                    video_transcoder._ACTIVE_TRANSCODER
                ],
                normalizer,
            )
            with self.assertRaisesRegex(video_transcoder.VideoTranscoderError, "another"):
                await contender.async_start()
            self.assertIs(
                hass.data[video_transcoder.DOMAIN][
                    video_transcoder._ACTIVE_TRANSCODER
                ],
                normalizer,
            )
            await normalizer.async_close()

        process.terminate.assert_called_once()
        process.wait.assert_awaited_once()
        self.assertNotIn(
            video_transcoder._ACTIVE_TRANSCODER,
            hass.data[video_transcoder.DOMAIN],
        )

    async def test_invalid_jpeg_is_rejected_before_process_start(self) -> None:
        normalizer = video_transcoder.FfmpegJpegNormalizer(
            hass=_Hass(),
            call_id="invalid-jpeg",
        )
        spawn = mock.AsyncMock()
        invalid_frames = (
            b"not-a-jpeg",
            b"\xff\xd8missing-eoi",
            b"missing-soi\xff\xd9",
            b"\xff\xd8"
            + b"\x00" * (video_transcoder._JPEG_NORMALIZE_MAX_BYTES - 3)
            + b"\xff\xd9",
        )

        with mock.patch.object(
            video_transcoder.asyncio,
            "create_subprocess_exec",
            new=spawn,
        ):
            for frame in invalid_frames:
                with (
                    self.subTest(size=len(frame)),
                    self.assertRaisesRegex(
                        video_transcoder.VideoTranscoderError,
                        "invalid browser JPEG",
                    ),
                ):
                    await normalizer.async_normalize(frame)

        spawn.assert_not_awaited()
        self.assertNotIn(
            video_transcoder._ACTIVE_TRANSCODER,
            normalizer.hass.data.get(video_transcoder.DOMAIN, {}),
        )

    async def test_jpeg_normalizer_timeout_closes_process_and_releases_slot(
        self,
    ) -> None:
        hass = _Hass()
        process, output_read = self._blocking_jpeg_process()
        normalizer = video_transcoder.FfmpegJpegNormalizer(
            hass=hass,
            call_id="jpeg-timeout",
        )

        with (
            mock.patch.object(video_transcoder, "_ffmpeg_binary", return_value="ffmpeg"),
            mock.patch.object(
                video_transcoder.asyncio,
                "create_subprocess_exec",
                new=mock.AsyncMock(return_value=process),
            ),
            mock.patch.object(video_transcoder, "_JPEG_NORMALIZE_TIMEOUT", 0.01),
        ):
            with self.assertRaisesRegex(
                video_transcoder.VideoTranscoderError,
                "JPEG normalizer failed",
            ):
                await normalizer.async_normalize(b"\xff\xd8\xff\xd9")

        self.assertTrue(output_read.is_set())
        process.terminate.assert_called_once()
        process.wait.assert_awaited_once()
        self.assertIsNone(normalizer.process)
        self.assertNotIn(
            video_transcoder._ACTIVE_TRANSCODER,
            hass.data[video_transcoder.DOMAIN],
        )

    async def test_cancelled_jpeg_normalization_cleans_up_process_and_slot(
        self,
    ) -> None:
        hass = _Hass()
        process, output_read = self._blocking_jpeg_process()
        normalizer = video_transcoder.FfmpegJpegNormalizer(
            hass=hass,
            call_id="jpeg-cancel",
        )

        with (
            mock.patch.object(video_transcoder, "_ffmpeg_binary", return_value="ffmpeg"),
            mock.patch.object(
                video_transcoder.asyncio,
                "create_subprocess_exec",
                new=mock.AsyncMock(return_value=process),
            ),
        ):
            task = asyncio.create_task(
                normalizer.async_normalize(b"\xff\xd8\xff\xd9")
            )
            await output_read.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        process.terminate.assert_called_once()
        process.wait.assert_awaited_once()
        self.assertIsNone(normalizer.process)
        self.assertNotIn(
            video_transcoder._ACTIVE_TRANSCODER,
            hass.data[video_transcoder.DOMAIN],
        )

    async def test_failed_start_releases_the_transcoder_slot(self) -> None:
        hass = _Hass()
        transcoder = video_transcoder.FfmpegVideoTranscoder(
            hass=hass,
            call_id="failed",
            input_format=sdp.RtpVideoFormat(payload_type=34, encoding="H263"),
            output_port=45678,
        )
        with mock.patch.object(
            video_transcoder,
            "_ffmpeg_binary",
            side_effect=video_transcoder.VideoTranscoderError("missing"),
        ):
            with self.assertRaisesRegex(video_transcoder.VideoTranscoderError, "missing"):
                await transcoder.async_start()
        self.assertNotIn(
            video_transcoder._ACTIVE_TRANSCODER,
            hass.data[video_transcoder.DOMAIN],
        )

    async def test_udp_readiness_waits_for_listener(self) -> None:
        process = types.SimpleNamespace(returncode=None, pid=123)
        port = 45678
        with (
            mock.patch.object(
                video_transcoder,
                "_process_owns_udp_port",
                side_effect=(False, True),
            ) as owns_port,
            mock.patch.object(
                video_transcoder,
                "_FFMPEG_INPUT_BIND_POLL_INTERVAL",
                0,
            ),
        ):
            await video_transcoder._wait_for_udp_listener(process, port)
        self.assertEqual(owns_port.call_count, 2)

    def test_udp_readiness_observes_only_process_owned_socket(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        listener.bind(("127.0.0.1", 0))
        port = int(listener.getsockname()[1])
        try:
            self.assertTrue(
                video_transcoder._process_owns_udp_port(os.getpid(), port)
            )
            self.assertFalse(
                video_transcoder._process_owns_udp_port(os.getpid(), port + 1)
            )
        finally:
            listener.close()

    async def test_udp_readiness_fails_if_process_exits(self) -> None:
        process = types.SimpleNamespace(returncode=1, pid=123)
        with self.assertRaisesRegex(
            video_transcoder.VideoTranscoderError,
            "exited before binding",
        ):
            await video_transcoder._wait_for_udp_listener(
                process,
                video_transcoder._available_udp_port(),
            )

    async def test_udp_readiness_times_out_without_listener(self) -> None:
        process = types.SimpleNamespace(returncode=None, pid=123)
        with (
            mock.patch.object(video_transcoder, "_FFMPEG_INPUT_BIND_TIMEOUT", 0.01),
            mock.patch.object(
                video_transcoder,
                "_process_owns_udp_port",
                return_value=False,
            ),
            self.assertRaisesRegex(
                video_transcoder.VideoTranscoderError,
                "did not bind",
            ),
        ):
            await video_transcoder._wait_for_udp_listener(
                process,
                video_transcoder._available_udp_port(),
            )

    async def test_cleanup_race_still_releases_the_transcoder_slot(self) -> None:
        hass = _Hass()
        transcoder = video_transcoder.FfmpegVideoTranscoder(
            hass=hass,
            call_id="cleanup-race",
            input_format=sdp.RtpVideoFormat(payload_type=34, encoding="H263"),
            output_port=45678,
        )
        process = mock.Mock()
        process.returncode = None
        process.terminate.side_effect = ProcessLookupError
        process.wait = mock.AsyncMock(return_value=0)
        transcoder.process = process
        hass.data[video_transcoder.DOMAIN] = {
            video_transcoder._ACTIVE_TRANSCODER: transcoder,
        }

        await transcoder.async_close()

        process.wait.assert_awaited_once()
        self.assertNotIn(
            video_transcoder._ACTIVE_TRANSCODER,
            hass.data[video_transcoder.DOMAIN],
        )

    async def test_cancelled_start_releases_the_transcoder_slot(self) -> None:
        hass = _Hass()
        transcoder = video_transcoder.FfmpegVideoTranscoder(
            hass=hass,
            call_id="cancelled-start",
            input_format=sdp.RtpVideoFormat(payload_type=34, encoding="H263"),
            output_port=45678,
        )
        entered = asyncio.Event()

        async def blocked_spawn(*_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()

        with (
            mock.patch.object(video_transcoder, "_ffmpeg_binary", return_value="ffmpeg"),
            mock.patch.object(
                video_transcoder.asyncio,
                "create_subprocess_exec",
                side_effect=blocked_spawn,
            ),
        ):
            task = asyncio.create_task(transcoder.async_start())
            await entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertNotIn(
            video_transcoder._ACTIVE_TRANSCODER,
            hass.data[video_transcoder.DOMAIN],
        )

    async def test_cancelled_close_still_kills_process_and_stderr_task(self) -> None:
        hass = _Hass()
        transcoder = video_transcoder.FfmpegVideoTranscoder(
            hass=hass,
            call_id="cancelled-close",
            input_format=sdp.RtpVideoFormat(payload_type=34, encoding="H263"),
            output_port=45678,
        )
        killed = False
        wait_entered = asyncio.Event()

        class Process:
            returncode = None

            def terminate(self) -> None:
                pass

            def kill(self) -> None:
                nonlocal killed
                killed = True

            async def wait(self) -> int:
                wait_entered.set()
                if not killed:
                    await asyncio.Event().wait()
                return 0

        stderr_task = asyncio.create_task(asyncio.Event().wait())
        transcoder.process = Process()  # type: ignore[assignment]
        transcoder._stderr_task = stderr_task  # noqa: SLF001
        hass.data[video_transcoder.DOMAIN] = {
            video_transcoder._ACTIVE_TRANSCODER: transcoder,
        }

        with mock.patch.object(video_transcoder, "_PROCESS_STOP_TIMEOUT", 0.01):
            task = asyncio.create_task(transcoder.async_close())
            await wait_entered.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.assertTrue(killed)
        self.assertTrue(stderr_task.done())
        self.assertNotIn(
            video_transcoder._ACTIVE_TRANSCODER,
            hass.data[video_transcoder.DOMAIN],
        )

    async def test_close_cancels_and_joins_inflight_process_spawn(self) -> None:
        hass = _Hass()
        transcoder = video_transcoder.FfmpegVideoTranscoder(
            hass=hass,
            call_id="start-close-race",
            input_format=sdp.RtpVideoFormat(payload_type=34, encoding="H263"),
            output_port=45678,
        )
        entered = asyncio.Event()
        resume = asyncio.Event()

        class Stdin:
            def write(self, _data: bytes) -> None:
                pass

            async def drain(self) -> None:
                pass

            def close(self) -> None:
                pass

        class Stderr:
            async def readline(self) -> bytes:
                return b""

        class Process:
            def __init__(self) -> None:
                self.stdin = Stdin()
                self.stderr = Stderr()
                self.returncode = None
                self.terminated = 0
                self.waited = 0

            def terminate(self) -> None:
                self.terminated += 1
                self.returncode = 0

            def kill(self) -> None:
                raise AssertionError("graceful termination should complete")

            async def wait(self) -> int:
                self.waited += 1
                return 0

        process = Process()

        async def delayed_spawn(*_args, **_kwargs):
            entered.set()
            try:
                await resume.wait()
            except asyncio.CancelledError:
                # Model process creation finishing while cancellation is
                # already in flight; the returned child still needs reaping.
                await resume.wait()
            return process

        with (
            mock.patch.object(video_transcoder, "_ffmpeg_binary", return_value="ffmpeg"),
            mock.patch.object(
                video_transcoder,
                "_wait_for_udp_listener",
                new=mock.AsyncMock(),
            ),
            mock.patch.object(
                video_transcoder.asyncio,
                "create_subprocess_exec",
                side_effect=delayed_spawn,
            ),
        ):
            start = asyncio.create_task(transcoder.async_start())
            await entered.wait()
            close = asyncio.create_task(transcoder.async_close())
            await asyncio.sleep(0)
            self.assertFalse(close.done())
            resume.set()
            with self.assertRaises(asyncio.CancelledError):
                await start
            await close
            await transcoder.async_close()

        self.assertEqual(process.terminated, 1)
        self.assertEqual(process.waited, 1)
        self.assertIsNone(transcoder.process)
        self.assertIsNone(transcoder._send_socket)  # noqa: SLF001
        self.assertNotIn(
            video_transcoder._ACTIVE_TRANSCODER,
            hass.data[video_transcoder.DOMAIN],
        )
        with self.assertRaisesRegex(video_transcoder.VideoTranscoderError, "closed"):
            await transcoder.async_start()

    async def test_repeated_close_cancellation_waits_for_process_reap(self) -> None:
        hass = _Hass()
        transcoder = video_transcoder.FfmpegVideoTranscoder(
            hass=hass,
            call_id="double-cancel-close",
            input_format=sdp.RtpVideoFormat(payload_type=34, encoding="H263"),
            output_port=45678,
        )
        first_wait = asyncio.Event()
        finish_wait = asyncio.Event()

        class Process:
            returncode = None

            def __init__(self) -> None:
                self.killed = False
                self.wait_calls = 0

            def terminate(self) -> None:
                pass

            def kill(self) -> None:
                self.killed = True

            async def wait(self) -> int:
                self.wait_calls += 1
                if self.wait_calls == 1:
                    first_wait.set()
                    await asyncio.Event().wait()
                await finish_wait.wait()
                self.returncode = 0
                return 0

        process = Process()
        stderr_task = asyncio.create_task(asyncio.Event().wait())
        transcoder.process = process  # type: ignore[assignment]
        transcoder._stderr_task = stderr_task  # noqa: SLF001
        hass.data[video_transcoder.DOMAIN] = {
            video_transcoder._ACTIVE_TRANSCODER: transcoder,
        }

        with mock.patch.object(video_transcoder, "_PROCESS_STOP_TIMEOUT", 0.01):
            close = asyncio.create_task(transcoder.async_close())
            await first_wait.wait()
            close.cancel()
            await asyncio.sleep(0)
            close.cancel()
            await asyncio.sleep(0.02)
            self.assertTrue(process.killed)
            self.assertFalse(close.done())
            finish_wait.set()
            with self.assertRaises(asyncio.CancelledError):
                await close

        self.assertEqual(process.wait_calls, 2)
        self.assertTrue(stderr_task.done())
        self.assertNotIn(
            video_transcoder._ACTIVE_TRANSCODER,
            hass.data[video_transcoder.DOMAIN],
        )


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for JPEG normalization")
class JpegNormalizerFfmpegTests(unittest.IsolatedAsyncioTestCase):
    async def _optimized_jpeg_frames(self, count: int) -> list[bytes]:
        process = await asyncio.create_subprocess_exec(
            shutil.which("ffmpeg") or "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=3",
            "-frames:v",
            str(count),
            "-an",
            "-pix_fmt",
            "yuvj420p",
            "-c:v",
            "mjpeg",
            "-huffman",
            "optimal",
            "-q:v",
            "5",
            "-f",
            "mpjpeg",
            "-boundary_tag",
            "source",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        self.assertEqual(process.returncode, 0, stderr.decode(errors="replace"))
        frames = _parse_multipart_jpegs(stdout)
        self.assertEqual(len(frames), count)
        return frames

    async def test_three_optimized_frames_use_one_process_and_round_trip_rtp(
        self,
    ) -> None:
        frames = await self._optimized_jpeg_frames(3)
        for frame in frames:
            with self.assertRaisesRegex(ValueError, "standard JPEG Huffman"):
                video_rtp.packetize_jpeg(
                    frame,
                    payload_type=26,
                    sequence=1,
                    timestamp=1,
                    ssrc=1,
                )

        hass = _Hass()
        normalizer = video_transcoder.FfmpegJpegNormalizer(
            hass=hass,
            call_id="three-frames",
        )
        process_ids: set[int] = set()
        try:
            for index, frame in enumerate(frames):
                normalized = await normalizer.async_normalize(frame)
                self.assertIsNotNone(normalizer.process)
                assert normalizer.process is not None
                process_ids.add(normalizer.process.pid)

                timestamp = 9000 + index * 3000
                packets = video_rtp.packetize_jpeg(
                    normalized,
                    payload_type=26,
                    sequence=65000 + index * 100,
                    timestamp=timestamp,
                    ssrc=0x12345678,
                    max_payload=600,
                )
                self.assertGreater(len(packets), 1)
                depacketizer = video_rtp.JpegDepacketizer()
                result = None
                for packet in packets:
                    result = depacketizer.push(packet) or result
                self.assertIsNotNone(result)
                assert result is not None
                self.assertEqual(result.encoding, "JPEG")
                self.assertEqual(result.timestamp, timestamp)
                self.assertTrue(result.data.startswith(b"\xff\xd8"))
                self.assertTrue(result.data.endswith(b"\xff\xd9"))
                video_rtp._parse_jpeg_for_rtp(result.data)

            self.assertEqual(len(process_ids), 1)
            self.assertIs(
                hass.data[video_transcoder.DOMAIN][
                    video_transcoder._ACTIVE_TRANSCODER
                ],
                normalizer,
            )
        finally:
            await normalizer.async_close()

        self.assertNotIn(
            video_transcoder._ACTIVE_TRANSCODER,
            hass.data[video_transcoder.DOMAIN],
        )


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for the transcode qualification")
class VideoTranscoderTests(unittest.IsolatedAsyncioTestCase):
    async def _qualify_codec(
        self,
        *,
        video_format,
        encoder: str,
        size: str = "320x180",
        encoder_args: tuple[str, ...] = (),
        output_format=None,
        gop: int = 10,
    ) -> None:
        output = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        output.setblocking(False)
        output.bind(("127.0.0.1", 0))
        output_port = int(output.getsockname()[1])
        source = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        source.setblocking(False)
        source.bind(("127.0.0.1", 0))
        source_port = int(source.getsockname()[1])
        transcoder = video_transcoder.FfmpegVideoTranscoder(
            hass=_Hass(),
            call_id=f"qualification-{video_format.encoding.lower()}",
            input_format=video_format,
            output_port=output_port,
            output_format=(
                output_format
                if output_format is not None
                else video_transcoder._DEFAULT_TRANSCODE_OUTPUT
            ),
        )
        sender = None
        input_packets = 0

        async def forward_input() -> None:
            nonlocal input_packets
            loop = asyncio.get_running_loop()
            while True:
                raw, _addr = await loop.sock_recvfrom(source, 2048)
                input_packets += 1
                transcoder.send_rtp(raw)

        forward_task = asyncio.create_task(forward_input())
        try:
            await transcoder.async_start()
            await asyncio.sleep(0.2)
            sender = await asyncio.create_subprocess_exec(
                shutil.which("ffmpeg") or "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-re",
                "-f", "lavfi",
                "-i", f"testsrc2=size={size}:rate=10",
                "-t", "2.8",
                "-an",
                "-c:v", encoder,
                *encoder_args,
                "-pix_fmt", "yuv420p",
                "-g", str(gop),
                "-f", "rtp",
                "-payload_type", str(video_format.payload_type),
                f"rtp://127.0.0.1:{source_port}?pkt_size=1200",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            loop = asyncio.get_running_loop()
            selected_output = transcoder.output_format
            depacketizer = {
                "H264": video_rtp.H264Depacketizer,
                "JPEG": video_rtp.JpegDepacketizer,
                "VP8": video_rtp.Vp8Depacketizer,
            }[selected_output.encoding]()
            packets = 0
            access_units = []
            deadline = loop.time() + 5.0
            while loop.time() < deadline and len(access_units) < 8:
                try:
                    raw, _addr = await asyncio.wait_for(loop.sock_recvfrom(output, 2048), 0.5)
                except TimeoutError:
                    continue
                packet = rtp.parse_packet(raw)
                self.assertEqual(packet.payload_type, selected_output.payload_type)
                packets += 1
                access_unit = depacketizer.push(packet)
                if access_unit is not None:
                    access_units.append(access_unit)
            stderr = b""
            if sender.returncode is None:
                await asyncio.wait_for(sender.wait(), 5.0)
            if sender.stderr is not None:
                stderr = await sender.stderr.read()
            self.assertEqual(sender.returncode, 0, stderr.decode(errors="replace"))
            diagnostic = "\n".join(transcoder.stderr_tail)
            self.assertGreater(input_packets, 20, diagnostic)
            self.assertGreater(packets, 8, diagnostic)
            self.assertGreaterEqual(len(access_units), 3, diagnostic)
            self.assertTrue(any(item.key_frame for item in access_units))
            timestamps = [item.timestamp for item in access_units]
            self.assertEqual(timestamps, sorted(timestamps))
        finally:
            forward_task.cancel()
            await asyncio.gather(forward_task, return_exceptions=True)
            if sender is not None and sender.returncode is None:
                sender.kill()
                await sender.wait()
            await transcoder.async_close()
            output.close()
            source.close()

    async def test_supported_sip_codec_matrix_transcodes_to_vp8(self) -> None:
        formats = (
            (
                sdp.RtpVideoFormat(payload_type=34, encoding="H263"),
                "h263",
                "352x288",
                (),
            ),
            (
                sdp.RtpVideoFormat(payload_type=102, encoding="H263P"),
                "h263p",
                "352x288",
                (),
            ),
            (
                sdp.RtpVideoFormat(
                    payload_type=102,
                    encoding="H264",
                    profile_level_id="42e01f",
                    packetization_mode=1,
                ),
                "libx264",
                "320x180",
                ("-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "baseline"),
            ),
            (
                sdp.RtpVideoFormat(payload_type=102, encoding="H265"),
                "libx265",
                "320x180",
                (
                    "-preset",
                    "ultrafast",
                    "-x265-params",
                    "log-level=error:keyint=10:min-keyint=10:bframes=0:no-scenecut=1:repeat-headers=1",
                ),
            ),
        )
        for video_format, encoder, size, encoder_args in formats:
            with self.subTest(codec=video_format.encoding):
                await self._qualify_codec(
                    video_format=video_format,
                    encoder=encoder,
                    size=size,
                    encoder_args=encoder_args,
                )

    async def test_h264_and_jpeg_cross_codec_outputs_are_valid_rtp(self) -> None:
        h264_input = sdp.RtpVideoFormat(
            payload_type=102,
            encoding="H264",
            profile_level_id="42c01f",
            packetization_mode=1,
        )
        jpeg_input = sdp.RtpVideoFormat(payload_type=26, encoding="JPEG")
        h264_output = sdp.RtpVideoFormat(
            payload_type=105,
            encoding="H264",
            profile_level_id="42c00c",
            packetization_mode=1,
            max_framerate=10,
        )
        jpeg_output = sdp.RtpVideoFormat(payload_type=26, encoding="JPEG")

        await self._qualify_codec(
            video_format=h264_input,
            encoder="libx264",
            encoder_args=(
                "-preset",
                "ultrafast",
                "-tune",
                "zerolatency",
                "-profile:v",
                "baseline",
            ),
            output_format=jpeg_output,
            gop=100,
        )
        await self._qualify_codec(
            video_format=jpeg_input,
            encoder="mjpeg",
            encoder_args=(
                "-huffman",
                "default",
                "-force_duplicated_matrix",
                "1",
            ),
            output_format=h264_output,
        )


if __name__ == "__main__":
    unittest.main()
