"""Real H.264 WebSocket decoding and presentation lifecycle in Chromium."""

import asyncio
import json
from pathlib import Path
import shutil
import subprocess

import pytest

web = pytest.importorskip("aiohttp.web")
ROOT = Path(__file__).resolve().parents[1]
FRONT = ROOT / "custom_components/voip_stack/frontend"


async def exercise(output):
    from playwright.async_api import async_playwright

    frame = subprocess.check_output(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x120:r=1",
            "-frames:v",
            "1",
            "-c:v",
            "libx264",
            "-profile:v",
            "baseline",
            "-level:v",
            "3.1",
            "-tune",
            "zerolatency",
            "-f",
            "h264",
            "pipe:1",
        ]
    )
    packet = bytes([1, 1]) + bytes(4) + frame
    sockets = []
    camera_packets = []
    hello = {
        "can_receive": True,
        "can_send": False,
        "camera_allowed": False,
        "receive": {
            "encoding": "H264",
            "codec": "avc1.42E01F",
            "payload_type": 96,
            "clock_rate": 90000,
        },
    }

    async def ws(request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        sockets.append(socket)
        await socket.send_json(hello)
        async for message in socket:
            if isinstance(message.data, bytes):
                camera_packets.append(message.data)
        return socket

    async def static(request):
        name = request.match_info["name"]
        if not name:
            return web.Response(
                text='<html><body style="font-family:Arial;background:#ddd"></body></html>',
                content_type="text/html",
            )
        return web.FileResponse(FRONT / name)

    app = web.Application()
    app.router.add_get("/video", ws)
    app.router.add_get("/{name:.*}", static)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    results = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                executable_path=shutil.which("chromium"),
                headless=True,
                args=["--no-sandbox"],
            )
            page = await browser.new_page(viewport={"width": 500, "height": 500})
            await page.goto(f"http://127.0.0.1:{port}/")
            await page.evaluate("""async()=>{
    const {VoipStackVideo}=await import('/voip-stack-video.js');
    const {buildMainCardSkeleton}=await import('/voip-stack-card-view.js');
    const host=document.createElement('div');host.style.cssText='width:360px;height:360px;margin:20px';document.body.append(host);
    const owner={shadowRoot:host.attachShadow({mode:'open'}),_attachEventHandlers(){},_observeLayout(){}};
    buildMainCardSkeleton.call(owner,'video-test');window.e=owner._els;
    for(const key of ['callBtn','answerBtn','declineBtn','placeholderBtn'])e[key].hidden=true;
    e.hangupBtn.hidden=false;e.hangupPeer.textContent='Office';e.hangupDuration.textContent='00:12';e.parkBtn.hidden=false;
    window.v=new VoipStackVideo();v.setCanvas(e.videoCanvas);
    v.configure({callWS:async()=>({path:'/video'})},'test');
    v.addEventListener('state',()=>{e.card.classList.toggle('video-active',v.visible);e.videoCanvas.hidden=!v.visible;});
    window.start=async id=>v.start({call_id:id,endpoint_id:'office',video_active:true,send_video:false});
   }""")
            await page.evaluate('start("initial")')
            await page.wait_for_timeout(500)
            assert not await page.evaluate("v.visible")
            assert not await page.evaluate("e.card.classList.contains('video-active')")
            await page.screenshot(path=str(output / "negotiated-no-media.png"))
            results.append(
                {
                    "case": "H264 negotiated, no media",
                    "visible": False,
                    "stats": await page.evaluate("v.stats"),
                }
            )
            await sockets[-1].send_bytes(packet)
            await page.wait_for_function("v.visible", timeout=10000)
            pixel = await page.evaluate(
                "Array.from(e.videoCanvas.getContext('2d').getImageData(80,60,1,1).data)"
            )
            assert pixel[0] > 150 and pixel[1] < 50 and pixel[2] < 50, pixel
            await page.screenshot(path=str(output / "actual-h264.png"))
            results.append(
                {
                    "case": "real H264 decoded",
                    "pixel": pixel,
                    "stats": await page.evaluate("v.stats"),
                }
            )
            generation = await page.evaluate("v._generation")
            await sockets[-1].send_json({"type": "media_update", **hello})
            await page.wait_for_function(
                "(g)=>v._generation>g && v.active", arg=generation
            )
            assert not await page.evaluate("v.visible")
            assert not await page.evaluate("e.card.classList.contains('video-active')")
            results.append({"case": "renegotiated, no new frame", "visible": False})
            await sockets[-1].send_bytes(packet)
            await page.wait_for_function("v.visible")
            results.append({"case": "new receiver frame", "visible": True})
            await page.evaluate("v.close()")
            assert not await page.evaluate("v.visible")
            await page.evaluate('start("next-call")')
            assert not await page.evaluate("v.visible")
            results.append(
                {
                    "case": "next call, H264 but no frame",
                    "visible": False,
                    "stats": await page.evaluate("v.stats"),
                }
            )
            await page.evaluate("v.close()")
            # Exercise the missing-processor branch with actual DOM playback,
            # VideoFrames, JPEG worker and WebSocket bytes. The test supplies a
            # canvas camera, so no real microphone or camera is requested.
            await page.evaluate("""async()=>{
              window.originalProcessor=globalThis.MediaStreamTrackProcessor;
              globalThis.MediaStreamTrackProcessor=undefined;
              window.cameraCanvas=document.createElement('canvas');cameraCanvas.width=160;cameraCanvas.height=120;
              const paint=()=>{const ctx=cameraCanvas.getContext('2d');ctx.fillStyle='#00ff00';ctx.fillRect(0,0,160,120);};
              paint();window.cameraPaint=setInterval(paint,50);
              window.cameraStream=cameraCanvas.captureStream(15);
              navigator.mediaDevices.getUserMedia=async()=>cameraStream;
              const {VoipStackVideo}=await import('/voip-stack-video.js');
              window.fallbackSender=new VoipStackVideo();
              fallbackSender._generation=3;fallbackSender._cameraAllowed=true;fallbackSender._cameraEnabled=true;
              fallbackSender._negotiated={can_send:true,can_receive:false,
                send:{codec:'jpeg',encoding:'JPEG',clock_rate:90000,payload_type:26}};
              const socket=new WebSocket(location.origin.replace('http','ws')+'/video');
              await new Promise((resolve,reject)=>{socket.onopen=resolve;socket.onerror=reject;});
              fallbackSender._ws=socket;
              await fallbackSender._replaceSender('jpeg',3,'',false);
            }""")
            await page.wait_for_function("fallbackSender.stats.sent>=2", timeout=10000)
            async with asyncio.timeout(3):
                while len(camera_packets) < 2:
                    await asyncio.sleep(0.01)
            jpeg = camera_packets[-1]
            assert jpeg[:2] == bytes([1, 1]) and jpeg[6:8] == b"\xff\xd8"
            decoded = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error",
                "-i", "pipe:0", "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
                input=jpeg[6:], capture_output=True, check=True).stdout
            assert len(decoded) == 160 * 120 * 3
            pixel = list(decoded[(60 * 160 + 80) * 3:(60 * 160 + 80) * 3 + 3])
            assert pixel[1] > 200 and pixel[0] < 30 and pixel[2] < 30
            await page.evaluate("""async()=>{
              clearInterval(cameraPaint);await fallbackSender.close();
              globalThis.MediaStreamTrackProcessor=originalProcessor;
            }""")
            assert await page.evaluate("cameraStream.getTracks().every(track=>track.readyState==='ended')")
            assert await page.evaluate("document.querySelectorAll('video').length") == 0
            results.append({"case": "real fallback DOM camera/JPEG worker/WebSocket", "pixel": pixel,
                            "packets": len(camera_packets), "tracks_ended": True})
            await browser.close()
    finally:
        await runner.cleanup()
    (output / "video-browser-results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


@pytest.mark.js_runtime
@pytest.mark.browser_real
@pytest.mark.skipif(
    not shutil.which("chromium") or not shutil.which("ffmpeg"),
    reason="Chromium and FFmpeg required",
)
def test_h264_visibility_follows_received_frames_across_renegotiation(tmp_path):
    asyncio.run(exercise(tmp_path))
