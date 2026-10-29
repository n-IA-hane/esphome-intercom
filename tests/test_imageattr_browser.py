"""Encode real JPEGs in Chromium using negotiated sizes and the production worker."""
import asyncio
import json
from pathlib import Path
import shutil

import pytest

web = pytest.importorskip("aiohttp.web")
ROOT = Path(__file__).resolve().parents[1]
FRONT = ROOT / "custom_components/voip_stack/frontend"


async def exercise():
    from playwright.async_api import async_playwright

    async def static(request):
        name = request.match_info["name"]
        if not name:
            return web.Response(text="<html><body></body></html>", content_type="text/html")
        return web.FileResponse(FRONT / name)

    app = web.Application()
    app.router.add_get("/{name:.*}", static)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=shutil.which("chromium"), headless=True,
                                               args=["--no-sandbox", "--disable-dev-shm-usage"])
            page = await browser.new_page()
            await page.goto(f"http://127.0.0.1:{port}/")
            result = await page.evaluate("""async () => {
const {VoipStackVideo} = await import('/voip-stack-video.js');
const receiver=(w,h)=>({recv:{wildcard:false,sets:[{x:{min:8,max:w,step:8,values:[]},
 y:{min:8,max:h,step:8,values:[]},q:100,par:null}]}});
const results=[];
for (const [sw,sh,mw,mh] of [[640,480,320,240],[640,360,320,240],[480,640,320,240],
                            [640,480,160,120],[320,240,640,480],[640,480,0,0]]) {
 const v=new VoipStackVideo();v._generation=3;v._senderGeneration=7;
 v._cameraEnabled=v._cameraAllowed=true;
 v._negotiated={send:{encoding:'JPEG',clock_rate:90000,image_attributes:mw?receiver(mw,mh):null}};
 const encoded=[];v._ws={readyState:WebSocket.OPEN,bufferedAmount:0,send:(frame)=>encoded.push(frame)};
 const encoder=await v._createJpegEncoder(sw,sh,3,7);v._encoder=encoder;v._jpegEncoderWorker=encoder.worker;
 const canvas=new OffscreenCanvas(sw,sh),ctx=canvas.getContext('2d');
 ctx.fillStyle='#ff0000';ctx.fillRect(0,0,sw/2,sh);
 ctx.fillStyle='#0000ff';ctx.fillRect(sw/2,0,sw/2,sh);
 v._dispatchJpegFrame(new VideoFrame(canvas,{timestamp:1000000}),encoder.worker,encoder,3,7);
 const started=performance.now();
 while(!encoded.length && performance.now()-started<3000) await new Promise(r=>setTimeout(r,5));
 if(!encoded.length) throw new Error('JPEG encode did not complete');
 const jpeg=encoded[0].slice(6);
 const image=await createImageBitmap(new Blob([jpeg],{type:'image/jpeg'}));
 const decoded=new OffscreenCanvas(image.width,image.height),out=decoded.getContext('2d');out.drawImage(image,0,0);
 const pixel=(x,y)=>Array.from(out.getImageData(x,y,1,1).data);
 results.push({native:[sw,sh],limit:[mw,mh],encoded:[image.width,image.height],plan:encoder.imagePlan,
  red:pixel(Math.floor(image.width/4),Math.floor(image.height/2)),
  blue:pixel(Math.floor(image.width*3/4),Math.floor(image.height/2)),edge:pixel(0,0),
  frames:encoded.length});
 image.close();encoder.close();
}
return results;
}""")
            await browser.close()
            return result
    finally:
        await runner.cleanup()


def test_real_browser_jpeg_sizes_orientation_and_padding(tmp_path):
    result = asyncio.run(exercise())
    (tmp_path / "result.json").write_text(json.dumps(result, indent=2))
    assert [r["encoded"] for r in result] == [[320,240],[320,184],[184,240],[160,120],[320,240],[640,480]]
    for item in result:
        assert item["frames"] == 1
        assert item["red"][0] > 150 and item["red"][2] < 60
        assert item["blue"][2] > 150 and item["blue"][0] < 60
    assert max(result[1]["edge"][:3]) < 80
    assert max(result[2]["edge"][:3]) < 80
    assert result[-1]["plan"] is None
