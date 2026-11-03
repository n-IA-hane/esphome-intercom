"""Decode actual FFmpeg output: a receiver ceiling cannot manufacture pictures."""

import asyncio
import io

import pytest
from PIL import Image

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


@pytest.mark.parametrize("source,receiving,expected,content", [
    ((352, 288), "[x=[8:8:800],y=[8:8:800]]", (352, 288), (352, 288)),
    ((1280, 720), "[x=[8:8:800],y=[8:8:800]]", (800, 456), (800, 450)),
    ((354, 290), "[x=[8:8:800],y=[8:8:800]]", (360, 296), (354, 290)),
    ((32, 24), "[x=[8,16,24,32],y=[8,16,24,32]]", (32, 24), (32, 24)),
    ((352, 288), "[x=800,y=800]", (800, 800), (352, 288)),
    ((352, 288), "[x=[16:16:800],y=[16:16:800]]", (800, 800), (352, 288)),
    ((352, 288), "[x=[352,800],y=[288,800]]", (800, 800), (352, 288)),
    ((352, 288), "[x=[8:8:800],y=[8:8:800],q=0.5] [x=640,y=640,q=1.0]", (640, 640), (352, 288)),
    ((352, 288), "[x=[8:8:800],y=[8:8:800],par=[1.0-1.1]]", (800, 800), (352, 288)),
    ((352, 288), "*", (352, 288), (352, 288)),
    ((352, 288), None, (352, 288), (352, 288)),
])
async def test_ffmpeg_jpeg_canvas_honors_receiver_without_unnecessary_padding(
    source, receiving, expected, content
):
    attributes = None if receiving is None else sdp.parse_imageattr("26 recv " + receiving)[1]
    target = sdp.RtpVideoFormat(payload_type=26, encoding="JPEG", image_attributes=attributes)
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        f"color=c=red:size={source[0]}x{source[1]}:rate=1:duration=1", "-an",
        *video_transcoder._output_command_args(target), "-f", "mpjpeg", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, error = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, error.decode()
        frames = _parse_multipart_jpegs(output)
        assert len(frames) == 1
        assert video_rtp.jpeg_dimensions(frames[0]) == expected
        with Image.open(io.BytesIO(frames[0])) as image:
            rgb = image.convert("RGB")
            pixels = rgb.tobytes()
            red = Image.frombytes("L", rgb.size, bytes(
                255 if r > 180 and g < 80 and b < 80 else 0
                for r, g, b in zip(pixels[::3], pixels[1::3], pixels[2::3])
            ))
            left, top, right, bottom = red.getbbox()
            assert abs(right - left - content[0]) <= 2
            assert abs(bottom - top - content[1]) <= 2
            assert abs((left + right) / 2 - expected[0] / 2) <= 2
            assert abs((top + bottom) / 2 - expected[1] / 2) <= 2
        if attributes is not None and not attributes.recv.wildcard:
            assert any(item.allows(*expected) for item in attributes.recv.sets)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
