"""Real browser codec, camera and observer lifecycles on loopback transport."""

import asyncio
import json
from pathlib import Path
import shutil
import struct
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "custom_components/voip_stack/frontend"
VERSION = "video-owner-contract-20261120"
pytestmark = [pytest.mark.js_runtime, pytest.mark.browser_real]


def encoded_picture(encoding, color):
    options = {
        "H264": ["-c:v", "libx264", "-profile:v", "baseline", "-level:v", "3.1",
                 "-tune", "zerolatency", "-f", "h264"],
        "JPEG": ["-c:v", "mjpeg", "-q:v", "2", "-threads", "1", "-f", "image2pipe"],
        "VP8": ["-c:v", "libvpx", "-deadline", "realtime", "-f", "ivf"],
    }
    data = subprocess.check_output([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        f"color=c={color}:s=160x120:r=1", "-frames:v", "1", *options[encoding], "pipe:1",
    ])
    if encoding == "VP8":
        assert data[:4] == b"DKIF" and len(data) > 44
        size = struct.unpack_from("<I", data, 32)[0]
        assert size == len(data) - 44
        return data[44:]
    return data


def outgoing_center(packet, encoding="JPEG"):
    payload = packet[6:]
    if encoding == "VP8":
        assert payload[3:6] == b"\x9d\x01\x2a", "The VP8 sender must start with a key frame"
        payload = (struct.pack("<4sHH4sHHIIII", b"DKIF", 0, 32, b"VP80", 160, 120, 90000, 1, 1, 0)
                   + struct.pack("<IQ", len(payload), int.from_bytes(packet[2:6], "big")) + payload)
    raw = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
        "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1",
    ], input=payload, capture_output=True, check=True).stdout
    assert len(raw) == 160 * 120 * 3
    return list(raw[(60 * 160 + 80) * 3:(60 * 160 + 80) * 3 + 3])


@pytest.mark.skipif(not shutil.which("chromium") or not shutil.which("ffmpeg"),
                    reason="Chromium and FFmpeg are required")
def test_native_video_codecs_camera_switch_eof_observer_and_redial(tmp_path):
    from aiohttp import web, WSMsgType
    from playwright.async_api import async_playwright

    async def exercise():
        pictures = {codec: encoded_picture(codec, color)
                    for codec, color in [("H264", "red"), ("JPEG", "blue"), ("VP8", "lime")]}
        sockets, packets, controls = [], [], []
        hello = {"can_receive": True, "can_send": True,
                 "receive": {"encoding": "H264", "codec": "avc1.42E01F", "clock_rate": 90000},
                 "send": {"encoding": "JPEG", "codec": "jpeg", "clock_rate": 90000}}

        async def media(request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            sockets.append(socket)
            await socket.send_json(hello)
            async for message in socket:
                if message.type is WSMsgType.BINARY:
                    packets.append(bytes(message.data))
                elif message.type is WSMsgType.TEXT:
                    controls.append(json.loads(message.data))
            return socket

        async def asset(request):
            name = request.match_info["name"]
            if not name:
                return web.Response(text="<html><body></body></html>", content_type="text/html")
            return web.FileResponse(FRONTEND / name)

        async def wait_for(predicate):
            async with asyncio.timeout(5):
                while not predicate():
                    await asyncio.sleep(.01)

        app = web.Application()
        app.router.add_get("/video", media)
        app.router.add_get("/{name:.*}", asset)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
        receipt, errors = {"phases": []}, []
        try:
            async with async_playwright() as runtime:
                browser = await runtime.chromium.launch(executable_path=shutil.which("chromium"), headless=True,
                    args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"])
                try:
                    page = await browser.new_page()
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    await page.goto(base)
                    await page.evaluate("""async version => {
                      window.captures=[];window.workers=[];window.states=[];
                      const NativeWorker=Worker;
                      globalThis.Worker=class extends NativeWorker {
                        constructor(url,options){super(url,options);this.record={url:String(url),terminations:0};workers.push(this.record);}
                        terminate(){this.record.terminations++;return super.terminate();}
                      };
                      const cameras=new Map();
                      for(const [id,color] of [['camera-a','#00ff00'],['camera-b','#0000ff']]){
                        const canvas=document.createElement('canvas');canvas.width=160;canvas.height=120;
                        const paint=()=>{const ctx=canvas.getContext('2d');ctx.fillStyle=color;ctx.fillRect(0,0,160,120);};
                        paint();cameras.set(id,{canvas,paint});
                      }
                      window.paintCamera=setInterval(()=>{for(const camera of cameras.values())camera.paint();},50);
                      navigator.mediaDevices.getUserMedia=async options=>{
                        if(options.audio)throw Error('Unexpected microphone acquisition');
                        const id=options.video?.deviceId?.exact||'camera-a';
                        const stream=cameras.get(id).canvas.captureStream(15),track=stream.getVideoTracks()[0];
                        const settings=track.getSettings.bind(track);
                        track.getSettings=()=>({...settings(),deviceId:id});
                        captures.push({id,stream,track});return stream;
                      };
                      const {VoipStackVideo}=await import('/voip-stack-video.js?v='+version);
                      window.video=new VoipStackVideo();
                      window.canvas=document.createElement('canvas');document.body.append(canvas);video.setCanvas(canvas);
                      video.configure({callWS:async()=>({path:'/video'})},'video-owner');
                      video.addEventListener('state',event=>states.push(event.detail));
                      window.startVideo=id=>video.start({state:'in_call',call_id:id,endpoint_id:'desk',
                        video_active:true,send_video:true});
                      window.pixel=()=>Array.from(canvas.getContext('2d').getImageData(80,60,1,1).data);
                    }""", VERSION)
                    await page.evaluate("startVideo('video-A')")
                    await page.wait_for_function("video.stats.sent>=3")
                    await wait_for(lambda: len(packets) >= 3)
                    assert packets[-1][:2] == b"\x01\x01" and packets[-1][6:8] == b"\xff\xd8"
                    first_pixel = outgoing_center(packets[-1])
                    assert first_pixel[1] > 200 and first_pixel[0] < 30 and first_pixel[2] < 30
                    await page.evaluate("video.switchCamera('camera-b')")
                    before = len(packets)
                    await wait_for(lambda: len(packets) >= before + 3)
                    second_pixel = outgoing_center(packets[-1])
                    assert second_pixel[2] > 200 and second_pixel[0] < 30 and second_pixel[1] < 30
                    assert await page.evaluate("captures[0].track.readyState==='ended' && video.cameraSettings.deviceId==='camera-b'")
                    receipt["camera_switch"] = {"before_pixel": first_pixel, "after_pixel": second_pixel,
                                                "connections": len(sockets)}
                    assert len(sockets) == 1

                    for index, (codec, channel) in enumerate([("H264", 0), ("JPEG", 2), ("VP8", 1)]):
                        if index:
                            generation = await page.evaluate("video._generation")
                            packet_start = len(packets)
                            hello["receive"] = {"encoding": codec, "codec": {"JPEG": "jpeg", "VP8": "vp8"}[codec], "clock_rate": 90000}
                            if codec == "VP8":
                                hello["send"] = {"encoding": "VP8", "codec": "vp8", "clock_rate": 90000}
                            await sockets[-1].send_json({"type": "media_update", **hello})
                            await page.wait_for_function("generation=>video._generation>generation&&video.active", arg=generation)
                        assert not await page.evaluate("video.visible")
                        await sockets[-1].send_bytes(b"\x01\x01" + (9000 * index).to_bytes(4, "big") + pictures[codec])
                        await page.wait_for_function("video.visible")
                        pixel = await page.evaluate("pixel()")
                        assert pixel[channel] > 150 and all(value < 60 for i, value in enumerate(pixel[:3]) if i != channel), pixel
                        await page.screenshot(path=str(tmp_path / f"received-{codec.lower()}.png"))
                        receipt["phases"].append({"codec": codec, "pixel": pixel, "stats": await page.evaluate("video.stats")})
                        assert len(sockets) == 1
                        if codec == "VP8":
                            await page.wait_for_function("states.at(-1).can_send && states.at(-1).send_encoding==='VP8'")
                            await wait_for(lambda: any(packet[1] & 1 for packet in packets[packet_start:]))
                            key = next(packet for packet in packets[packet_start:] if packet[1] & 1)
                            sent_pixel = outgoing_center(key, "VP8")
                            assert sent_pixel[2] > 200 and sent_pixel[0] < 30 and sent_pixel[1] < 30
                            receipt["native_vp8_sender"] = {"pixel": sent_pixel, "bytes": len(key),
                                                            "rtp_timestamp": int.from_bytes(key[2:6], "big")}

                    await page.evaluate("captures.at(-1).track.stop()")
                    await page.wait_for_function("states.at(-1).can_send===false")
                    assert await page.evaluate("video.visible"), "Camera EOF must retain received video"
                    sent = await page.evaluate("video.stats.sent")
                    await page.evaluate("async()=>{await video.setCameraEnabled(false,'desk');await video.setCameraEnabled(true,'desk');}")
                    await page.wait_for_function("sent=>video.stats.sent>=sent+3", arg=sent)
                    await page.evaluate("video.close()")
                    await wait_for(lambda: all(socket.closed for socket in sockets))
                    assert len(sockets) == 1
                    assert await page.evaluate("captures.every(value=>value.track.readyState==='ended') && workers.every(value=>value.terminations===1)")
                    await page.evaluate("startVideo('video-B')")
                    assert len(sockets) == 2, "Redial must open exactly one new video socket"
                    assert not await page.evaluate("video.visible")
                    await page.wait_for_function("video.stats.sent>=3")
                    await sockets[-1].send_bytes(b"\x01\x01" + bytes(4) + pictures["VP8"])
                    await page.wait_for_function("video.visible")
                    await page.evaluate("video.close()")
                    await wait_for(lambda: all(socket.closed for socket in sockets))
                    receipt["final"] = await page.evaluate("""() => ({stats:video.stats,visible:video.visible,
                      active:video.active,tracksEnded:captures.every(value=>value.track.readyState==='ended'),
                      workers:workers.slice(),captureCount:captures.length})""")
                    assert receipt["final"]["tracksEnded"] and not receipt["final"]["active"] and not receipt["final"]["visible"]
                    assert all(value["terminations"] == 1 and f"v={VERSION}" in value["url"] for value in receipt["final"]["workers"])
                    observer = await browser.new_page()
                    await observer.goto(base)
                    receipt["observer"] = await observer.evaluate("""async version=>{
                      let acquisitions=0,signatures=0;
                      navigator.mediaDevices.getUserMedia=async()=>{acquisitions++;throw Error('Observer acquired media');};
                      const {voipStackEngine:engine}=await import('/voip-stack-engine.js?v='+version);
                      const state={state:'in_call',call_id:'owned-elsewhere',endpoint_id:'desk',device_id:'desk-device',
                        video_active:true,send_video:true,media_owner:'other'};
                      engine.configure({callWS:async request=>{
                        if(request.type==='auth/sign_path'){signatures++;throw Error('Observer requested media path');}
                        return state;
                      }});
                      engine.claimSoftphoneSession(state.call_id,'desk');
                      await engine.resumeSession({endpoint_id:'desk'},'desk-device',state);
                      await engine.reconcileSession(state);await engine.close('observer-complete');
                      return {acquisitions,signatures,callId:engine.callId,owned:engine.softphoneCallIdFor('desk')};
                    }""", VERSION)
                    assert receipt["observer"] == {"acquisitions": 0, "signatures": 0, "callId": "", "owned": ""}
                    await observer.close()
                    await page.evaluate("clearInterval(paintCamera)")
                    assert not errors, errors
                    receipt.update(connections=len(sockets), all_sockets_closed=all(socket.closed for socket in sockets),
                                   camera_packets=len(packets), controls=controls, browser_errors=errors,
                                   scope="Actual Chromium codecs, canvas camera and loopback transport; no hardware qualification")
                finally:
                    await browser.close()
        finally:
            await runner.cleanup()
            (tmp_path / "video-lifecycle.json").write_text(json.dumps(receipt, indent=2))

    asyncio.run(exercise())
