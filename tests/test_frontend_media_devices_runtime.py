"""Runtime contracts for browser-owned media device preferences."""

from __future__ import annotations

import json
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import shutil
import subprocess
import threading

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components/voip_stack/frontend/voip-stack-media-devices.js"
VIDEO = MODULE.with_name("voip-stack-video.js")
VIDEO_MODEL = MODULE.with_name("voip-stack-video-model.js")


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_media_device_inventory_preferences_and_permission_probe() -> None:
    script = f"""
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";

const source = fs.readFileSync({json.dumps(str(MODULE))}, "utf8");
class TestCustomEvent extends Event {{
  constructor(type, init) {{ super(type); this.detail = init?.detail; }}
}}
const context = vm.createContext({{ EventTarget, CustomEvent: TestCustomEvent }});
const module = new vm.SourceTextModule(source, {{ context }});
await module.link(() => {{ throw new Error("unexpected import"); }});
await module.evaluate();
const {{ BrowserMediaDevices, exactDeviceConstraint }} = module.namespace;

const stored = new Map();
const stopped = [];
let changes = 0;
let enumerated = [
  {{ kind: "audioinput", deviceId: "mic-a", groupId: "a", label: "" }},
  {{ kind: "audiooutput", deviceId: "speaker-a", groupId: "b", label: "Desk" }},
  {{ kind: "videoinput", deviceId: "front", groupId: "c", label: "Front" }},
  {{ kind: "videoinput", deviceId: "rear", groupId: "c", label: "Rear" }},
];
const constraints = [];
const mediaDevices = new EventTarget();
mediaDevices.enumerateDevices = async () => enumerated;
mediaDevices.getUserMedia = async (value) => {{
  constraints.push(value);
  return {{ getTracks: () => [{{ stop: () => stopped.push(value) }}] }};
}};
const storage = {{
  getItem: (key) => stored.get(key) || null,
  setItem: (key, value) => stored.set(key, value),
}};
const owner = new BrowserMediaDevices({{ mediaDevices, storage }});
owner.addEventListener("change", () => changes++);
await owner.refresh();
assert.equal(owner.state.devices.audioinput[0].label, "Microphone 1");
assert.equal(owner.state.devices.videoinput.length, 2);
assert.equal(owner.has("videoinput", "rear"), true);

owner.commit("videoinput", "front", {{ deviceId: "front", facingMode: "user" }});
owner.commit("audioinput", "mic-a", {{ deviceId: "mic-a" }});
assert.equal(owner.cameraFacingMode(), "user");
assert.equal(owner.preference("audioinput"), "mic-a");
assert.equal(JSON.parse([...stored.values()][0]).selected.videoinput, "front");

const restored = new BrowserMediaDevices({{ mediaDevices, storage }});
assert.equal(restored.preference("videoinput"), "front");
assert.equal(restored.cameraFacingMode(), "user");
await restored.refresh({{ requestPermission: true }});
assert.equal(
  JSON.stringify(constraints),
  JSON.stringify([{{ audio: true }}, {{ video: true, audio: false }}]),
);
assert.equal(stopped.length, 2);

enumerated = enumerated.filter((device) => device.deviceId !== "front");
mediaDevices.dispatchEvent(new Event("devicechange"));
await new Promise((resolve) => setImmediate(resolve));
assert.equal(owner.has("videoinput", "front"), false);
assert.equal(owner.preference("videoinput"), "front");
assert.ok(changes >= 3);
assert.equal(
  JSON.stringify(exactDeviceConstraint({{ echoCancellation: true }}, "mic-a")),
  JSON.stringify({{ echoCancellation: true, deviceId: {{ exact: "mic-a" }} }}),
);
assert.equal(JSON.stringify(exactDeviceConstraint({{ width: 640 }}, "")), '{{"width":640}}');
"""
    completed = subprocess.run(
        [
            "node",
            "--no-warnings",
            "--experimental-vm-modules",
            "--input-type=module",
            "-e",
            script,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.skipif(shutil.which("chromium") is None, reason="Chromium is unavailable")
def test_media_device_inventory_in_real_chromium() -> None:
    playwright = pytest.importorskip("playwright.sync_api")

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        partial(QuietHandler, directory=str(ROOT)),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch(
                headless=True,
                executable_path=shutil.which("chromium"),
                args=[
                    "--use-fake-ui-for-media-stream",
                    "--use-fake-device-for-media-stream",
                    "--autoplay-policy=no-user-gesture-required",
                ],
            )
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{server.server_port}/README.md")
            result = page.evaluate(
                """async (port) => {
                  const { BrowserMediaDevices } = await import(
                    `http://127.0.0.1:${port}/custom_components/voip_stack/frontend/voip-stack-media-devices.js`
                  );
                  const values = new Map();
                  const owner = new BrowserMediaDevices({
                    storage: {
                      getItem: (key) => values.get(key) || null,
                      setItem: (key, value) => values.set(key, value),
                    },
                  });
                  const state = await owner.refresh({ requestPermission: true });
                  const stream = await navigator.mediaDevices.getUserMedia({ audio: true, video: true });
                  const liveBeforeStop = stream.getTracks().every((track) => track.readyState === "live");
                  stream.getTracks().forEach((track) => track.stop());
                  const audio = new AudioContext();
                  const sinkSupported = typeof audio.setSinkId === "function";
                  if (sinkSupported) await audio.setSinkId("");
                  await audio.close();
                  return {
                    microphones: state.devices.audioinput.length,
                    cameras: state.devices.videoinput.length,
                    liveBeforeStop,
                    stopped: stream.getTracks().every((track) => track.readyState === "ended"),
                    sinkSupported,
                  };
                }""",
                server.server_port,
            )
            browser.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert result["microphones"] >= 1
    assert result["cameras"] >= 1
    assert result["liveBeforeStop"] is True
    assert result["stopped"] is True
    assert result["sinkSupported"] is True


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_live_camera_replacement_is_atomic() -> None:
    script = f"""
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";

const videoSource = fs.readFileSync({json.dumps(str(VIDEO))}, "utf8");
const modelSource = fs.readFileSync({json.dumps(str(VIDEO_MODEL))}, "utf8");
const context = vm.createContext({{
  EventTarget, performance, console, Blob, URL, setTimeout, clearTimeout,
  CustomEvent: class CustomEvent extends Event {{
    constructor(type, init) {{ super(type); this.detail = init?.detail; }}
  }},
  WebSocket: {{ OPEN: 1, CONNECTING: 0 }},
  EncodedVideoChunk: class EncodedVideoChunk {{ constructor(init) {{ Object.assign(this, init); }} }},
}});
const model = new vm.SourceTextModule(modelSource, {{ context }});
await model.link(() => {{ throw new Error("unexpected model import"); }});
await model.evaluate();
const videoModule = new vm.SourceTextModule(videoSource, {{
  context,
  initializeImportMeta(meta) {{ meta.url = "https://ha.example/voip-stack-video.js?v=test"; }},
  importModuleDynamically: async () => model,
}});
await videoModule.link(() => {{ throw new Error("unexpected static import"); }});
await videoModule.evaluate();
const Video = videoModule.namespace.VoipStackVideo;

let releaseCameraB;
const cameraBGate = new Promise((resolve) => {{ releaseCameraB = resolve; }});
const readers = [];
const tracks = new Map();
const streamFor = (deviceId) => {{
  const track = {{
    deviceId,
    stopped: false,
    getSettings: () => ({{ deviceId, width: 640, height: 360, frameRate: 15 }}),
    stop() {{ this.stopped = true; }},
  }};
  tracks.set(deviceId, track);
  return {{ getVideoTracks: () => [track], getTracks: () => [track] }};
}};
context.navigator = {{ mediaDevices: {{
  async getUserMedia({{ video }}) {{
    const deviceId = video?.deviceId?.exact || "default";
    if (deviceId === "cam-b") await cameraBGate;
    return streamFor(deviceId);
  }},
}} }};
context.MediaStreamTrackProcessor = class {{
  constructor() {{
    const waiter = [];
    const reader = {{
      read: () => new Promise((resolve) => waiter.push(resolve)),
      cancel: async () => {{ for (const resolve of waiter.splice(0)) resolve({{ done: true }}); }},
    }};
    readers.push(reader);
    this.readable = {{ getReader: () => reader }};
  }}
}};
context.VideoEncoder = class {{
  static async isConfigSupported(config) {{ return {{ supported: true, config }}; }}
  constructor() {{ this.state = "unconfigured"; this.encodeQueueSize = 0; }}
  configure() {{ this.state = "configured"; }}
  encode() {{}}
  close() {{ this.state = "closed"; }}
}};

const video = new Video();
video._generation = 1;
video._active = true;
video._cameraAllowed = true;
video._cameraEnabled = true;
video._negotiated = {{
  can_send: true,
  send: {{ codec: "vp8", encoding: "VP8", max_framerate: 15 }},
}};
video._ws = {{ readyState: 1, bufferedAmount: 0, send() {{}} }};
await video._replaceSender("vp8", 1, "cam-a", true);
const oldEncoder = video._encoder;
const replacement = video.switchCamera("cam-b");
await Promise.resolve();
assert.equal(tracks.get("cam-a").stopped, false);
assert.equal(video._encoder, oldEncoder);
releaseCameraB();
await replacement;
assert.equal(tracks.get("cam-a").stopped, true);
assert.equal(tracks.get("cam-b").stopped, false);
assert.equal(video.cameraSettings.deviceId, "cam-b");
assert.equal(video.canSend, true);
assert.notEqual(video._encoder, oldEncoder);
await video._cleanupSender();
assert.equal(tracks.get("cam-b").stopped, true);
"""
    completed = subprocess.run(
        [
            "node",
            "--no-warnings",
            "--experimental-vm-modules",
            "--input-type=module",
            "-e",
            script,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
