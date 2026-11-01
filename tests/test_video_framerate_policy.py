"""Negotiated FPS are limits; conversion must not manufacture source frames."""

from __future__ import annotations

import json
from pathlib import Path
import re
import subprocess

import pytest

from custom_components.voip_stack.core import sdp
from custom_components.voip_stack import video_transcoder


pytestmark = pytest.mark.ha
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("encoding", ["JPEG", "VP8"])
@pytest.mark.parametrize("fps", [None, 10, 25, 30, 29.97])
def test_transcode_has_only_declared_framerate_ceiling(encoding, fps):
    receiver = sdp.RtpVideoFormat(encoding=encoding, payload_type=26 if encoding == "JPEG" else 104, max_framerate=fps)
    assert video_transcoder._transcode_envelope(receiver)[2] == fps


def test_codec_fmtp_and_macroblock_limits_still_bound_transcode():
    vp8 = sdp.RtpVideoFormat(encoding="VP8", payload_type=104, max_framerate=30, fmtp="max-fr=12;max-fs=1200")
    assert video_transcoder._transcode_envelope(vp8)[2] == 12
    h264 = sdp.RtpVideoFormat(encoding="H264", payload_type=103, max_framerate=60,
                            profile_level_id="42c01e", packetization_mode=1)
    width, height, fps = video_transcoder._transcode_envelope(h264)
    # Level 3.0 permits 40500 macroblocks/s. At the chosen 640x360 this is
    # fractional; rounding down is not part of the codec's receive contract.
    assert fps == 40500 / (((width + 15) // 16) * ((height + 15) // 16))
    assert fps > 44
    extended = sdp.RtpVideoFormat(encoding="H264", payload_type=103, max_framerate=60,
                                 profile_level_id="42c01f", packetization_mode=1, fmtp="max-mbps=108001")
    assert video_transcoder._transcode_envelope(extended)[2] == 108001 / 3600


@pytest.mark.js_runtime
def test_camera_preferences_do_not_override_peer_capability():
    model = ROOT / "custom_components/voip_stack/frontend/voip-stack-video-model.js"
    script = r'''
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";
const module=new vm.SourceTextModule(fs.readFileSync(MODEL,"utf8"));
await module.link(()=>{throw Error("unexpected import");});await module.evaluate();
const {cameraCaptureContract,cameraEncoderContract}=module.namespace;
for(const encoding of ["JPEG","VP8"]){
 for(const limit of [null,10,25,30,29.97]){
  const contract=cameraCaptureContract({encoding,maxFramerate:limit});
  assert.equal(contract.constraints.frameRate.ideal,limit||30);
  assert.equal(contract.constraints.frameRate.max,limit||undefined);
  const configured=cameraEncoderContract(contract,{width:640,height:360,frameRate:30},encoding);
  assert.equal(configured.framerate,limit||30);
  assert.equal(cameraEncoderContract(contract,{width:640,height:360,frameRate:5},encoding).framerate,5);
 }
}
const vp8=cameraCaptureContract({encoding:"VP8",maxFramerate:30,fmtp:"max-fr=12;max-fs=1200"});
assert.equal(vp8.maxFr,12);
const h264=cameraCaptureContract({encoding:"H264",maxFramerate:60,profileLevelId:"42c01e",fmtp:"max-mbps=40500"});
assert.equal(h264.maxFr,40500/(40*23));
const extended=cameraCaptureContract({encoding:"H264",maxFramerate:60,profileLevelId:"42c01f",fmtp:"max-mbps=108001"});
assert.equal(extended.maxFr,108001/3600);
const unknown=cameraCaptureContract({encoding:"JPEG"});
assert.equal(unknown.constraints.frameRate.max,undefined);
assert.equal(cameraEncoderContract(unknown,{width:640,height:360},"JPEG").framerate,30);
'''.replace("MODEL", json.dumps(str(model)))
    result = subprocess.run(["node", "--no-warnings", "--experimental-vm-modules", "--input-type=module", "-e", script],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("source_rate,ceiling,expected_min,expected_max", [
    ("5", 25, 10, 10),
    ("25", 25, 50, 50),
    ("30", 30, 60, 60),
    ("30000/1001", 29.97, 59, 60),
    ("30", 10, 19, 21),
    ("30", 25, 49, 51),
    ("25", None, 50, 50),
])
def test_real_ffmpeg_preserves_slow_source_and_limits_only_when_needed(source_rate, ceiling, expected_min, expected_max):
    receiver = sdp.RtpVideoFormat(encoding="JPEG", payload_type=26, max_framerate=ceiling)
    args = video_transcoder._output_command_args(receiver)
    result = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", f"testsrc2=size=64x48:rate={source_rate}:duration=2",
        *args, "-f", "framecrc", "pipe:1",
    ], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    frames = [line for line in result.stdout.splitlines() if line and not line.startswith("#")]
    assert expected_min <= len(frames) <= expected_max, result.stdout
    if ceiling == 29.97:
        assert "#tb 0: 1001/30000" in result.stdout


def test_real_ffmpeg_preserves_pts_gaps_without_filling_stall():
    receiver = sdp.RtpVideoFormat(encoding="JPEG", payload_type=26, max_framerate=25)
    args = video_transcoder._output_command_args(receiver)
    # A source stall changes timestamps without adding input pictures.
    pos = args.index("-vf") + 1
    args[pos] = "setpts='PTS+if(gte(N,5),2/TB,0)'," + args[pos]
    result = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
        "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=5:duration=2",
        *args, "-f", "framecrc", "pipe:1",
    ], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    pts = [int(line.split(",")[2]) for line in result.stdout.splitlines() if line and not line.startswith("#")]
    assert pts == [0, 1, 2, 3, 4, 15, 16, 17, 18, 19], result.stdout


def test_real_h264_encoder_rate_is_not_the_rtp_clock_and_keeps_pts():
    receiver = sdp.RtpVideoFormat(encoding="H264", payload_type=103,
        profile_level_id="42c00c", packetization_mode=1, max_framerate=10)
    args = video_transcoder._output_command_args(receiver)
    index = args.index("-vf") + 1
    args[index] = "setpts=PTS*9000," + args[index]
    result = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-nostdin",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=90000",
        "-frames:v", "10", *args, "-f", "framecrc", "pipe:1",
    ], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    pts = [int(line.split(",")[2]) for line in result.stdout.splitlines() if line and not line.startswith("#")]
    assert pts == list(range(0, 90000, 9000)), result.stdout
    assert "MB rate (" not in result.stderr


def test_real_h264_slow_source_does_not_advertise_constant_receiver_ceiling():
    receiver = sdp.RtpVideoFormat(encoding="H264", payload_type=103,
        profile_level_id="42c01f", packetization_mode=1, max_framerate=25)
    result = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "info", "-nostdin",
        "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=5:duration=2",
        *video_transcoder._output_command_args(receiver),
        "-bsf:v", "trace_headers", "-f", "framecrc", "pipe:1",
    ], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    pts = [int(line.split(",")[2]) for line in result.stdout.splitlines() if line and not line.startswith("#")]
    assert pts == list(range(10)), result.stdout
    flags = re.findall(r"fixed_frame_rate_flag\s+\d+\s*=\s*(\d+)", result.stderr)
    assert flags and set(flags) == {"0"}, result.stderr
