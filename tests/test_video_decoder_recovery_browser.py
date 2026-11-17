"""Real Chromium WebCodecs: native close after corrupt H264, valid IDR recovery."""

import asyncio
import base64
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from aiohttp import web

ROOT = Path(__file__).resolve().parents[1]
FRONT = ROOT / "custom_components/voip_stack/frontend"


@pytest.mark.browser_real
@pytest.mark.js_runtime
@pytest.mark.skipif(
    not shutil.which("chromium") or not shutil.which("ffmpeg"),
    reason="real Chromium and FFmpeg required",
)
def test_worker_recovers_native_closed_h264_decoder_on_valid_idr(tmp_path):
    async def exercise():
        from playwright.async_api import async_playwright

        encoded = subprocess.check_output(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=red:s=160x120:r=10",
                "-frames:v",
                "1",
                "-c:v",
                "libx264",
                "-threads",
                "1",
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
        (tmp_path / "valid-idr.h264").write_bytes(encoded)

        async def serve(request):
            name = request.match_info["name"]
            return (
                web.FileResponse(FRONT / name)
                if name
                else web.Response(
                    text='<canvas id="picture" width="160" height="120"></canvas>',
                    content_type="text/html",
                )
            )

        app = web.Application()
        app.router.add_get("/{name:.*}", serve)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        try:
            async with async_playwright() as runtime:
                browser = await runtime.chromium.launch(
                    executable_path=shutil.which("chromium"),
                    headless=True,
                    args=["--no-sandbox"],
                )
                page = await browser.new_page()
                try:
                    await page.goto(
                        "http://127.0.0.1:"
                        + str(site._server.sockets[0].getsockname()[1])
                    )
                    await page.evaluate(
                        """data => {
                      window.good=Uint8Array.from(atob(data), x=>x.charCodeAt(0));
                      window.messages=[];window.frames=[];
                      window.worker=new Worker('/voip-stack-video-worker.js');
                      worker.onmessage=event=>{
                        const m=event.data;
                        if(m.frame){const canvas=document.getElementById('picture');canvas.getContext('2d').drawImage(m.frame,0,0);
                          frames.push({generation:m.generation,timestamp:m.frame.timestamp,pixel:Array.from(canvas.getContext('2d').getImageData(80,60,1,1).data)});m.frame.close();}
                        else messages.push(m);
                      };
                      window.decode=(data,keyFrame,timestamp,generation=7)=>{const bytes=new Uint8Array(data);worker.postMessage({type:'decode',generation,keyFrame,timestamp,buffer:bytes.buffer,offset:0,length:bytes.length},[bytes.buffer]);};
                      worker.postMessage({type:'configure_decoder',generation:7,requestId:1,config:{codec:'avc1.42E01F',optimizeForLatency:true}});
                    }""",
                        base64.b64encode(encoded).decode(),
                    )
                    await page.wait_for_function(
                        'messages.some(m=>m.type==="reply"&&m.requestId===1&&m.ok)',
                        timeout=5000,
                    )
                    await page.evaluate("decode(good,true,0)")
                    await page.wait_for_function("frames.length===1", timeout=5000)
                    # A malformed non-IDR slice after valid decoding produces a native asynchronous EncodingError.
                    await page.evaluate("decode([0,0,0,1,0x41,0],false,100000)")
                    await page.wait_for_function(
                        'messages.some(m=>m.type==="decoder_error")', timeout=5000
                    )
                    await page.evaluate(
                        "decode(good,true,150000,6);decode(good,false,160000)"
                    )
                    await page.wait_for_timeout(100)
                    assert await page.evaluate("frames.length===1")
                    await page.evaluate("decode(good,true,200000)")
                    try:
                        await page.wait_for_function("frames.length===2", timeout=3000)
                        assert await page.evaluate(
                            "frames[1].pixel[0]>150 && frames[1].pixel[1]<50 && frames[1].generation===7"
                        )
                    finally:
                        (tmp_path / "observed.json").write_text(
                            json.dumps(
                                await page.evaluate("({messages,frames})"), indent=2
                            )
                        )
                        await page.screenshot(path=str(tmp_path / "recovery.png"))
                finally:
                    await browser.close()
        finally:
            await runner.cleanup()

    asyncio.run(exercise())
