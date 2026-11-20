"""Prepared Chromium playback preserves the first samples after a slow download."""

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.js_runtime, pytest.mark.browser_real]


@pytest.mark.skipif(shutil.which("chromium") is None, reason="Chromium is unavailable")
def test_prepared_browser_renders_initial_samples_after_slow_worklet_load():
    playwright = pytest.importorskip("playwright.sync_api")
    asset_version = "qualified-frontend-revision"
    loaded = []

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path.startswith("/voip-stack/"):
                self.path = self.path.replace("/voip-stack/", "/custom_components/voip_stack/frontend/", 1)
            if "-processor.js" in self.path:
                time.sleep(0.25)
                loaded.append(self.path)
            return super().do_GET()

    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(ROOT)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch(
                executable_path=shutil.which("chromium"), headless=True,
                args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                      "--autoplay-policy=no-user-gesture-required", "--mute-audio"],
            )
            try:
                page = browser.new_page()
                page.goto(f"http://127.0.0.1:{server.server_port}/README.md")
                result = page.evaluate("""async (version) => {
                  const {voipStackEngine: engine} = await import(`/custom_components/voip_stack/frontend/voip-stack-engine.js?v=${encodeURIComponent(version)}`);
                  const started = performance.now();
                  await engine.prepareAudioCall();
                  const preparationMs = performance.now() - started;
                  const context = engine._audioSession.prepared.audioContext;
                  const stream = engine._audioSession.prepared.mediaStream;
                  engine._callId = 'startup-audit';
                  await engine._setupAudio({audio_mode:'full_duplex'}, {
                    call_id:'startup-audit', audio_direction:'sendrecv',
                    tx_format:'48000:s16le:1:20', rx_format:'48000:s16le:1:20'
                  });
                  const reused = engine._audioSession.audioContext === context && engine._audioSession.mediaStream === stream;
                  const frames = [];
                  const tap = context.createScriptProcessor(256, 1, 1);
                  let resolve;
                  const captured = new Promise(done => {resolve = done;});
                  tap.onaudioprocess = event => {
                    frames.push(...event.inputBuffer.getChannelData(0));
                    const first = frames.findIndex(value => Math.abs(value) > 0.001);
                    if (first >= 0 && frames.length - first >= 128) resolve(frames.slice(first, first + 128));
                  };
                  engine._audioSession.playbackNode.connect(tap);
                  tap.connect(context.destination);
                  const expected = [];
                  for (let packet=0; packet<6; packet++) {
                    const buffer = new ArrayBuffer(1921);
                    const data = new DataView(buffer);
                    data.setUint8(0, 1);
                    for (let i=0; i<960; i++) {
                      const sample = 2000 + ((packet*960+i) % 503);
                      data.setInt16(1+i*2, sample, true);
                      expected.push(sample);
                    }
                    engine._handleMessage({data:buffer});
                  }
                  let timer;
                  try {
                    const actual = await Promise.race([captured, new Promise((_,reject) => {
                      timer = setTimeout(() => reject(new Error('No rendered startup audio: '+JSON.stringify({rate:context.sampleRate,state:context.state,frames:frames.length,peak:frames.reduce((peak,value)=>Math.max(peak,Math.abs(value)),0),stats:engine._stats,format:engine._audioSession.rxFormat}))), 5000);
                    })]);
                    return {preparationMs, reused, rate:context.sampleRate,
                      maximumError:Math.max(...actual.slice(0,96).map((value,i)=>{
                        const position = Math.max(0,i*960/Math.round(context.sampleRate*.02)-1);
                        const index = Math.floor(position), fraction=position-index;
                        const reference=expected[index]*(1-fraction)+expected[index+1]*fraction;
                        return Math.abs(value*32768-reference);
                      }))};
                  } finally {
                    clearTimeout(timer);
                    tap.disconnect();
                    await engine.close();
                  }
                }""", asset_version)
            finally:
                browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    assert len(loaded) == 2
    assert all(parse_qs(urlsplit(path).query) == {"v": [asset_version]} for path in loaded)
    assert result["preparationMs"] >= 250
    assert result["reused"]
    assert result["rate"] in (44100, 48000)
    assert result["maximumError"] <= 1
