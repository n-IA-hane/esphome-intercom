"""Decode actual FFmpeg output: a receiver ceiling cannot manufacture pictures."""

import asyncio

import pytest

from custom_components.voip_stack import video_transcoder
from custom_components.voip_stack.core import sdp, video_rtp
from .test_video_transcoder import _parse_multipart_jpegs

pytestmark = [pytest.mark.ha, pytest.mark.slow]


@pytest.mark.parametrize("receiver_fps", [None, 10, 30])
async def test_sparse_ffmpeg_input_keeps_exact_source_picture_count(receiver_fps):
    target = sdp.RtpVideoFormat(payload_type=26, encoding="JPEG", max_framerate=receiver_fps)
    process = await asyncio.create_subprocess_exec("ffmpeg", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=3:duration=1", "-an",
        *video_transcoder._output_command_args(target), "-f", "mpjpeg", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, error = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, error.decode()
        frames = _parse_multipart_jpegs(output)
        assert len(frames) == 3
        assert all(video_rtp.jpeg_dimensions(frame) == (320, 240) for frame in frames)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
