#!/usr/bin/env python3
"""Exercise the operator card in real laboratory Lovelace, saving screenshots.

Uses the existing HA laboratory and browser authentication. The optional setup
creates three dedicated views, preserving other views. Calls stay between
laboratory browser phones with fake microphones and silent playback. The main
view contains the dashboard and ordinary phonecard for the same owner.

The optional physical-peer mode drives one real dashboard or compact card.
An external operator owns the ESP/SIP actor; private signal files coordinate it
without another server or any ESP API connection in this tool.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from ha_voip_lab.paths import CAPTURE_ROOT  # noqa: E402

from ha_voip_lab.refresh_playwright_auth import refresh_playwright_auth  # noqa: E402
from live_voip_qualification import HaRest, HaWs  # noqa: E402
from ha_voip_lab.browser_audio_capture import BrowserAudioCapture  # noqa: E402


MEDIA_OBSERVER_SCRIPT = """(()=>{
    const originalCapture=navigator.mediaDevices.getUserMedia;
    navigator.mediaDevices.getUserMedia=async function(constraints){
      window.__dashboardCaptureCount=(window.__dashboardCaptureCount||0)+1;
      window.__dashboardCaptureRequests=(window.__dashboardCaptureRequests||[])
        .concat({audio:!!constraints.audio,video:!!constraints.video});
      return originalCapture.call(this,constraints);
    };
    const originalPlay=HTMLMediaElement.prototype.play;
    window.__dashboardPlayEvents=[];
    HTMLMediaElement.prototype.play=function(...args){
      this.muted=true;
      const event={kind:this.tagName,muted:this.muted,status:'pending'};
      if(window.__dashboardPlayEvents.length<64)window.__dashboardPlayEvents.push(event);
      return originalPlay.apply(this,args).then(value=>{
        event.status='resolved';event.paused=this.paused;event.readyState=this.readyState;return value;
      },error=>{event.status='rejected';event.error=error.name;throw error;});
    };
})();"""

MEDIA_SAMPLE_SCRIPT = """(selector='voip-stack-dashboard-card')=>{
    selector ||= 'voip-stack-dashboard-card'; // Playwright's omitted Python arg arrives as null.
    const e=window.__qualificationEngine;
    const canvases=[];
    const visit=(root,inside=false)=>{for(const n of root.querySelectorAll('*')){
      const selected=inside||n.matches?.(selector);
      if(selected&&n.tagName==='CANVAS'&&n.classList.contains('video-canvas')&&n.getClientRects().length){
        const w=n.width,h=n.height;let nonBlack=false;
        if(w&&h){const c=n.getContext('2d');if(c){
          for(let y=1;y<5;y++)for(let x=1;x<5;x++){
            const p=c.getImageData(Math.floor(w*x/5),Math.floor(h*y/5),1,1).data;
            nonBlack ||= p[0]+p[1]+p[2]>12;
          }
        }}
        canvases.push({width:w,height:h,non_black:nonBlack});
      }
      if(n.shadowRoot)visit(n.shadowRoot,selected);
    }};visit(document);
    return {call_id:e?.callId,state:e?._state,context_state:e?._audioContext?.state,
      context_time:e?._audioContext?.currentTime||0,stats:e?.stats||{},canvases,
      play_events:window.__dashboardPlayEvents||[]};
}"""


class DashboardAudioWire:
    """Bounded passive CDP observer, using the maintained PCM wire parser."""

    def __init__(self):
        self.capture = BrowserAudioCapture(capture_pcm=False)
        self.sockets = set()
        self.totals = {side: {"packets": 0, "samples": 0, "bytes": 0} for side in ("tx", "rx")}

    def opened(self, event):
        if urlsplit(event["url"]).path != "/api/voip_stack/ws":
            return
        if len(self.sockets) >= 64:
            raise RuntimeError("Audio observer exceeded its 64-socket budget")
        self.sockets.add(event["requestId"])

    def frame(self, side, event):
        request = event["requestId"]
        if request not in self.sockets:
            return
        item = self.capture.record(request, side, event["response"], event["timestamp"])
        if item and item["kind"] == "pcm":
            self.totals[side]["packets"] += 1
            self.totals[side]["samples"] += item["samples_per_channel"]
            self.totals[side]["bytes"] += item["payload_bytes"]

    def snapshot(self):
        return {side: dict(value) for side, value in self.totals.items()}


def validate_media_progress(before, after, *, video=False):
    """Require actual PCM wire traffic and worklet consumption in one dialog."""
    if not before.get("call_id") or before["call_id"] != after.get("call_id"):
        raise AssertionError("Media observation crossed the call identity")
    if after.get("state") != "IN_CALL" or after.get("context_state") != "running":
        raise AssertionError("The real audio context is not playing an established call")
    if after["context_time"] <= before["context_time"]:
        raise AssertionError("The real audio clock did not advance")
    for side in ("tx", "rx"):
        if after["wire"][side]["samples"] <= before["wire"][side]["samples"]:
            raise AssertionError(f"No real {side} PCM crossed the browser WebSocket")
    for counter in ("sent", "received", "frames_in", "frames_out"):
        if after["stats"].get(counter, 0) <= before["stats"].get(counter, 0):
            raise AssertionError(f"The real audio worklet did not advance {counter}")
    for counter in ("tx_dropped", "frames_drop", "underruns"):
        if after["stats"].get(counter, 0) > before["stats"].get(counter, 0):
            raise AssertionError(f"Audio {counter} increased during the measured window")
    if video:
        for counter in ("received", "rendered"):
            if after["stats"].get("video", {}).get(counter, 0) <= before["stats"].get("video", {}).get(counter, 0):
                raise AssertionError(f"Real video {counter} did not advance")
        if not any(c["width"] > 0 and c["height"] > 0 and c["non_black"] for c in after["canvases"]):
            raise AssertionError("No visible decoded non-black video canvas")


def require_idle_phone(value):
    # Legacy softphone snapshots intentionally retain last_terminal_call_id.
    # Active state and the shared resource owner decide quiescence, not that ID.
    resources = value.get("media_debug", {}).get("runtime_resources", {})
    if value.get("state") != "idle" or resources.get("call_scoped_quiescent") is not True:
        raise RuntimeError("A laboratory phone is not idle and call-scoped quiescent")


def require_physical_peer(value, identity):
    if value.get("endpoint_id") != identity or value.get("kind") not in {"esphome", "sip_account"}:
        raise ValueError("Select one explicit ESP or standard SIP endpoint identity")
    if value.get("availability") != "available" or value.get("state") != "idle" or value.get("calls"):
        raise RuntimeError("The selected physical/SIP peer is not available and idle")
    if value.get("dnd"):
        raise RuntimeError("The selected peer has DND enabled")


def write_signal(directory, phase, value):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / f"{phase}.json"
    # Exclusive creation prevents a stale ready marker from starting an actor.
    with path.open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    path.chmod(0o600)


async def restore_owned_views(ws, original, output, results):
    """Restore only the dedicated fixture views, retaining unrelated LAB edits."""
    paths = {"voip-dashboard-qualification", "voip-dashboard-peer", "voip-dashboard-receiver"}
    try:
        current = (await ws.command({"type": "lovelace/config"}))["result"]
        views = [v for v in current.get("views", []) if v.get("path") not in paths]
        for index, view in enumerate(original.get("views", [])):
            if view.get("path") in paths:
                views.insert(min(index, len(views)), view)
        await ws.command({"type": "lovelace/config/save", "config": {**current, "views": views}})
        actual = (await ws.command({"type": "lovelace/config"}))["result"]
        expected = [v for v in original.get("views", []) if v.get("path") in paths]
        assert [v for v in actual.get("views", []) if v.get("path") in paths] == expected
        results["owned_views_restored"] = True
    except Exception as error:
        results["cleanup_errors"].append(f"restore owned views: {error}")
    finally:
        (output / "result.json").write_text(json.dumps(results, indent=2))


async def qualify_physical(args):
    """One ordinary GUI call. Root owns every ESP action, outside this runner."""
    if urlsplit(args.url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Physical QA also requires the isolated loopback HA laboratory")
    if not 1 <= args.physical_hold_seconds <= 30 or not 10 <= args.physical_wait_seconds <= 120:
        raise ValueError("Physical hold must be 1..30 seconds and actor wait 10..120 seconds")
    if args.fake_video_file and not args.fake_video_file.is_file():
        raise ValueError("The fake camera Y4M file does not exist")
    from playwright.async_api import async_playwright, expect

    args.out.mkdir(parents=True, exist_ok=True, mode=0o700)
    signals = args.signals_dir or args.out / "signals"
    if any((signals / f"{phase}.json").exists() for phase in ("ready", "connected", "measuring", "measured", "done")):
        raise ValueError("Use a fresh signal directory for this physical call")
    refresh_playwright_auth(token_url=args.url, credentials_path=args.credentials,
                            storage_path=args.storage, storage_hass_url=args.url)
    storage = json.loads(args.storage.read_text())
    token = next(json.loads(item["value"])["access_token"]
                 for origin in storage["origins"] if origin["origin"] == args.url
                 for item in origin["localStorage"] if item["name"] == "hassTokens")
    rest = HaRest(args.url, token)
    results = {"mode": "physical-peer", "ui": args.physical_ui, "direction": args.physical_direction,
               "expected_video": args.physical_video, "cleanup_errors": [], "browser_errors": [],
               "quiescent": False,
               "observer": {"real_play": True, "audible_output": False,
                            "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
               "scope": "Real GUI and SIP media, no ESP API, acoustic or physical-panel claim"}
    owned_call = ""

    async def action(name, data):
        response = await asyncio.to_thread(rest._request, "POST", "/api/services/voip_stack/" + name + "?return_response", data)
        return response.get("service_response", {})

    async with HaWs(args.url, token) as ws, AsyncExitStack() as cleanup:
        devices = (await ws.command({"type": "voip_stack/list_devices"}))["result"]["devices"]
        owner = next(d for d in devices if d.get("softphone") and d["endpoint_id"] == args.owner)

        async def owner_state():
            return (await ws.command({"type": "voip_stack/ha_softphone_state", "device_id": owner["device_id"]}))["result"]

        baseline = await owner_state()
        require_idle_phone(baseline)
        target = await action("get_phone_state", {"device_id": args.physical_peer})
        require_physical_peer(target, args.physical_peer)
        results["peer"] = {key: target[key] for key in ("endpoint_id", "device_id", "kind", "name", "extension")}
        results["owner"] = {key: owner[key] for key in ("endpoint_id", "device_id", "name")}
        original = (await ws.command({"type": "lovelace/config"}))["result"]
        checkpoint = args.out / "lovelace-before.json"
        with checkpoint.open("x") as stream:
            json.dump(original, stream, indent=2)
        checkpoint.chmod(0o600)
        cleanup.push_async_callback(restore_owned_views, ws, original, args.out, results)

        async def restore_preferences():
            for service, field in (("set_send_video", "send_video"), ("set_auto_answer", "auto_answer")):
                try:
                    await ws.command({"type": "call_service", "domain": "voip_stack", "service": service,
                                      "service_data": {"device_id": owner["device_id"], field: bool(baseline.get(field))}})
                except Exception as error:
                    results["cleanup_errors"].append(f"restore {field}: {error}")
        cleanup.push_async_callback(restore_preferences)
        card = {"type": "custom:voip-stack-dashboard-card" if args.physical_ui == "dashboard" else "custom:voip-stack-card",
                "device_id": owner["device_id"]}
        if args.physical_ui == "compact":
            card["mode"] = "ha_softphone"
        views = [view for view in original.get("views", []) if view.get("path") != "voip-dashboard-qualification"]
        views.append({"title": "Physical SIP QA", "path": "voip-dashboard-qualification", "cards": [card]})
        await ws.command({"type": "lovelace/config/save", "config": {**original, "views": views}})
        async with async_playwright() as runner:
            browser = await runner.chromium.launch(executable_path=shutil.which("chromium"), headless=True,
                args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream", "--mute-audio",
                      "--autoplay-policy=no-user-gesture-required",
                      *([f"--use-file-for-fake-video-capture={args.fake_video_file.resolve()}"] if args.fake_video_file else [])])
            context = await browser.new_context(storage_state=str(args.storage), permissions=["microphone", "camera"],
                                                viewport={"width": 1100, "height": 1000})
            await context.add_init_script(MEDIA_OBSERVER_SCRIPT)
            page = await context.new_page()
            page.on("pageerror", lambda error: results["browser_errors"].append(str(error)))
            wire = DashboardAudioWire()
            cdp = await context.new_cdp_session(page)
            await cdp.send("Network.enable")
            cdp.on("Network.webSocketCreated", wire.opened)
            cdp.on("Network.webSocketFrameSent", lambda event: wire.frame("tx", event))
            cdp.on("Network.webSocketFrameReceived", lambda event: wire.frame("rx", event))
            selector = "voip-stack-dashboard-card" if args.physical_ui == "dashboard" else "voip-stack-card"
            ui = page.locator(selector)
            phone_card = ui.locator("voip-stack-card[data-dashboard-phone]") if args.physical_ui == "dashboard" else ui

            async def wait_state(expected):
                nonlocal owned_call
                async with asyncio.timeout(args.physical_wait_seconds):
                    while True:
                        value = await owner_state()
                        if value.get("state") != "idle" and value.get("call_id"):
                            projection = await action("get_phone_state", {"device_id": owner["device_id"]})
                            if not any(call.get("peer_endpoint_id") == args.physical_peer for call in projection.get("calls", [])):
                                raise RuntimeError("Observed a call from a different peer; refusing ownership")
                            if owned_call and value["call_id"] != owned_call:
                                raise RuntimeError("The selected physical call identity changed")
                            owned_call = value["call_id"]
                        if value.get("state") == expected:
                            return value
                        await asyncio.sleep(0.1)

            try:
                await page.goto(args.url + "/lovelace/voip-dashboard-qualification")
                await expect(ui).to_have_count(1)
                await expect(ui).to_be_visible()
                await page.evaluate("""async()=>{
                  const urls=[...new Set(performance.getEntriesByType('resource').map(e=>e.name)
                    .filter(u=>new URL(u).pathname.endsWith('/voip-stack-engine.js')))];
                  if(urls.length!==1)throw new Error('Expected one production media engine module');
                  window.__qualificationEngine=(await import(urls[0])).voipStackEngine;
                }""")
                options = ui.locator(".options") if args.physical_ui == "dashboard" else ui.locator('button[aria-controls="voip-settings-panel"]')
                await options.click()
                await phone_card.locator("#auto-answer-cb").set_checked(False)
                await phone_card.locator("#ha-softphone-video-camera-cb").set_checked(args.physical_video in {"sendonly", "sendrecv"})
                await options.click()
                write_signal(signals, "ready", {"owner": results["owner"], "peer": results["peer"],
                                              "direction": args.physical_direction, "ui": args.physical_ui})
                if args.physical_direction == "outgoing":
                    if args.physical_ui == "dashboard":
                        await ui.locator(f'.tile[data-key="endpoint:{args.physical_peer}"] .tile-main').click()
                    else:
                        await phone_card.locator('button[aria-controls="voip-keypad-panel"]').click()
                        await phone_card.locator("input.keypad-input").fill(target["extension"])
                        await phone_card.locator("button.voip-button.call").click()
                else:
                    await wait_state("ringing")
                    answer = ".answer-video" if args.physical_video != "audio" else ".answer"
                    await ui.locator(".owner-call " + answer if args.physical_ui == "dashboard" else "button.voip-button.answer").click()
                connected = await wait_state("in_call")
                write_signal(signals, "connected", {"call_id": owned_call, "peer_endpoint_id": args.physical_peer})
                if args.physical_video == "audio":
                    assert not connected.get("video_active"), connected
                else:
                    assert connected.get("video_direction") == args.physical_video, connected
                async with asyncio.timeout(10):
                    while True:
                        before = await page.evaluate(MEDIA_SAMPLE_SCRIPT, selector)
                        if before["stats"].get("frames_out", 0) > 0:
                            break
                        await asyncio.sleep(0.1)
                before["wire"] = wire.snapshot()
                write_signal(signals, "measuring", {"call_id": owned_call, "hold_seconds": args.physical_hold_seconds})
                await asyncio.sleep(args.physical_hold_seconds)
                after = await page.evaluate(MEDIA_SAMPLE_SCRIPT, selector)
                after["wire"] = wire.snapshot()
                results["media_proof"] = {"before": before, "after": after, "wire_errors": list(wire.capture.errors)}
                assert not wire.capture.errors, wire.capture.errors
                validate_media_progress(before, after, video=args.physical_video in {"recvonly", "sendrecv"})
                if args.physical_video in {"sendonly", "sendrecv"}:
                    assert after["stats"].get("video", {}).get("sent", 0) > before["stats"].get("video", {}).get("sent", 0)
                await page.screenshot(path=str(args.out / "connected.png"), full_page=True)
                write_signal(signals, "measured", {"call_id": owned_call, "media_oracles_passed": True})
                if args.physical_hangup == "dashboard":
                    hangup = ui.locator(".owner-call .hangup") if args.physical_ui == "dashboard" and args.physical_video == "audio" else phone_card.locator("button.voip-button.hangup")
                    await expect(hangup).to_be_visible()
                    await hangup.click()
                await wait_state("idle")
                results["call_id"] = owned_call
            except BaseException as error:
                results["failure"] = str(error)
                await page.screenshot(path=str(args.out / "failure.png"), full_page=True)
                raise
            finally:
                try:
                    current = await owner_state()
                    if current.get("state") != "idle" and owned_call and current.get("call_id") == owned_call:
                        await ws.command({"type": "call_service", "domain": "voip_stack", "service": "hangup",
                                          "service_data": {"device_id": owner["device_id"], "call_id": owned_call}})
                except Exception as error:
                    results["cleanup_errors"].append(f"owned call cleanup: {error}")
                await browser.close()
                try:
                    final = await owner_state()
                    results["final_state"] = final.get("state")
                    results["quiescent"] = final.get("media_debug", {}).get("runtime_resources", {}).get("call_scoped_quiescent")
                except Exception as error:
                    results["cleanup_errors"].append(f"final resource check: {error}")
                write_signal(signals, "done", {"call_id": owned_call, "quiescent": results["quiescent"],
                                             "failure": results.get("failure")})
                (args.out / "result.json").write_text(json.dumps(results, indent=2))
        require_idle_phone(await owner_state())
    assert results.get("owned_views_restored") and not results["cleanup_errors"] and not results["browser_errors"], results
    (args.out / "result.json").write_text(json.dumps(results, indent=2))
    print(json.dumps({"mode": results["mode"], "ui": results["ui"], "peer": results["peer"]["endpoint_id"],
                      "quiescent": results["quiescent"], "owned_views_restored": results["owned_views_restored"]}))


async def qualify(args):
    if getattr(args, "physical_peer", None):
        return await qualify_physical(args)
    video_file = getattr(args, "fake_video_file", None)
    if video_file is not None and not video_file.is_file():
        raise ValueError("The fake camera Y4M file does not exist")
    from playwright.async_api import async_playwright, expect

    if urlsplit(args.url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Use the isolated loopback HA laboratory for this call test")
    args.out.mkdir(parents=True, exist_ok=True, mode=0o700)
    # systemd can report active before HA has opened its HTTP listener.
    host = urlsplit(args.url).hostname
    port = urlsplit(args.url).port or 80
    async with asyncio.timeout(60):
        while True:
            try:
                _reader, writer = await asyncio.open_connection(host, port)
                writer.close()
                await writer.wait_closed()
                break
            except ConnectionRefusedError:
                await asyncio.sleep(0.25)
    refresh_playwright_auth(token_url=args.url, credentials_path=args.credentials,
                            storage_path=args.storage, storage_hass_url=args.url)
    storage = json.loads(args.storage.read_text())
    token = next(json.loads(item["value"])["access_token"]
                 for origin in storage["origins"] if origin["origin"] == args.url
                 for item in origin["localStorage"] if item["name"] == "hassTokens")
    results = {"scenarios": [], "screenshots": [], "browser_errors": [],
               "capture_proof": [], "lifecycle": [], "cleanup_errors": [], "dtmf_controls": [], "call_requests": [],
               "audio_sockets": [], "media_proof": [],
               "observer": {"real_play": True, "audible_output": False,
                            "tool_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}}
    async with HaWs(args.url, token) as ws, AsyncExitStack() as cleanup:
        # HTTP is available before configured integrations finish setting up.
        # Wait for HA's actual startup state before invoking their commands.
        async with asyncio.timeout(60):
            while True:
                config = (await ws.command({"type": "get_config"}))["result"]
                if config.get("state") == "RUNNING" and "voip_stack" in config.get("components", []):
                    break
                await asyncio.sleep(0.25)
        devices = (await ws.command({"type": "voip_stack/list_devices"}))["result"]["devices"]
        browsers = [item for item in devices if item.get("softphone")]
        owner = next(item for item in browsers if item["endpoint_id"] == args.owner)
        peer = next(item for item in browsers if item["endpoint_id"] != args.owner)
        receiver = next(item for item in browsers if item["endpoint_id"] not in {owner["endpoint_id"], peer["endpoint_id"]})
        assert owner.get("extension") and peer.get("extension"), "Lab phones need extensions"
        initial_preferences = {}
        for endpoint in (owner, peer, receiver):
            current = (await ws.command({"type": "voip_stack/ha_softphone_state",
                                       "device_id": endpoint["device_id"]}))["result"]
            require_idle_phone(current)
            initial_preferences[endpoint["device_id"]] = {field: bool(current.get(field)) for field in ("send_video", "auto_answer")}
        config = (await ws.command({"type": "lovelace/config"}))["result"]
        backup = args.out / "lovelace-before.json"
        if backup.exists():
            raise ValueError("Use a fresh output directory to preserve the exact LAB view checkpoint")
        backup.write_text(json.dumps(config, indent=2))
        backup.chmod(0o600)
        cleanup.push_async_callback(restore_owned_views, ws, config, args.out, results)
        if args.setup:
            views = [view for view in config.get("views", [])
                     if view.get("path") not in {"voip-dashboard-qualification", "voip-dashboard-peer", "voip-dashboard-receiver"}]
            views.extend([
                {"title": "VoIP Dashboard", "path": "voip-dashboard-qualification",
                 "type": "sections", "max_columns": 4, "theme": "Frosted Glass Dark", "sections": [
                     {"type": "grid", "column_span": 4, "cards": [
                         {"type": "custom:voip-stack-dashboard-card", "device_id": owner["device_id"],
                          "grid_options": {"columns": 48, "rows": 14}}]},
                     {"type": "grid", "column_span": 4, "cards": [
                         {"type": "custom:voip-stack-card", "mode": "ha_softphone",
                          "device_id": owner["device_id"], "name": "Owner phonecard",
                          "grid_options": {"columns": 48, "rows": 7}}]}]},
                {"title": "Dashboard peer", "path": "voip-dashboard-peer", "theme": "Frosted Glass Dark",
                 "cards": [{"type": "custom:voip-stack-card", "mode": "ha_softphone",
                            "device_id": peer["device_id"], "name": "Dashboard peer"}]},
                {"title": "Dashboard receiver", "path": "voip-dashboard-receiver", "theme": "Frosted Glass Dark",
                 "cards": [{"type": "custom:voip-stack-card", "mode": "ha_softphone",
                            "device_id": receiver["device_id"], "name": "Dashboard receiver"}]},
            ])
            await ws.command({"type": "lovelace/config/save", "config": {**config, "views": views}})
        async with async_playwright() as runner:
            browser = await runner.chromium.launch(executable_path=shutil.which("chromium"), headless=True,
                args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                      "--mute-audio",
                      "--autoplay-policy=no-user-gesture-required",
                      *([f"--use-file-for-fake-video-capture={video_file.resolve()}"] if video_file else [])])
            context = await browser.new_context(storage_state=str(args.storage), permissions=["microphone", "camera"],
                                                 viewport={"width": 1500, "height": 1100})
            await context.add_init_script(MEDIA_OBSERVER_SCRIPT)
            page = await context.new_page()
            wire = DashboardAudioWire()
            cdp = await context.new_cdp_session(page)
            await cdp.send("Network.enable")
            cdp.on("Network.webSocketCreated", wire.opened)
            cdp.on("Network.webSocketFrameSent", lambda event: wire.frame("tx", event))
            cdp.on("Network.webSocketFrameReceived", lambda event: wire.frame("rx", event))
            def observe_websocket(socket):
                if urlsplit(socket.url).path == "/api/voip_stack/ws":
                    record = {"opened": asyncio.get_running_loop().time(), "closed": None}
                    results["audio_sockets"].append(record)
                    socket.on("close", lambda: record.update(closed=asyncio.get_running_loop().time()))
                def observe_control(payload):
                    if not isinstance(payload, str):
                        return
                    try:
                        control = json.loads(payload)
                    except json.JSONDecodeError:
                        return
                    if control.get("type") == "call_service" and control.get("domain") == "voip_stack" and control.get("service") == "call":
                        results["call_requests"].append({"destination":control.get("service_data",{}).get("destination"),
                                                         "device_id":control.get("service_data",{}).get("device_id")})
                    if control.get("type") == "dtmf":
                        results["dtmf_controls"].append({"digit":control.get("digit"), "duration_ms":control.get("duration_ms")})
                socket.on("framesent", observe_control)
            page.on("websocket", observe_websocket)
            remote = await context.new_page()
            receiving_page = await context.new_page()
            for label, browser_page in (("owner", page), ("peer", remote), ("receiver", receiving_page)):
                browser_page.on("pageerror", lambda error, label=label:
                    results["browser_errors"].append(f"{label}: {error}"))
            dashboard = page.locator("voip-stack-dashboard-card")
            owner_card = page.locator("voip-stack-card:not([data-dashboard-phone])")
            peer_card = remote.locator("voip-stack-card")
            receiving_card = receiving_page.locator("voip-stack-card")

            async def shot(name):
                path = args.out / f"{name}.png"
                await page.screenshot(path=str(path), full_page=True)
                results["screenshots"].append(str(path.relative_to(args.out)))

            async def state(value):
                await expect(dashboard).to_have_count(1)
                await expect(dashboard.locator("h2")).to_have_text(owner["name"])
                expected = ({"calling", "remote_ringing", "connecting", "answering",
                             "in_call", "held", "terminating"} if value == "busy" else {value})
                async with asyncio.timeout(20):
                    while True:
                        status = await dashboard.evaluate("""card=>({ready:card._store.ready,
                            device_id:card._owner()?.device_id,state:card._owner()?.state})""")
                        if status["ready"] and status["device_id"] == owner["device_id"] and status["state"] in expected:
                            return
                        await asyncio.sleep(0.05)

            async def peer_start(destination=owner["extension"]):
                # Drive the existing phone card's normal start action, keeping
                # its media intent and identity checks in the production path.
                await peer_card.evaluate("(card, destination) => card._startCall(destination)", destination)

            async def snapshot(endpoint):
                return (await ws.command({"type": "voip_stack/ha_softphone_state",
                    "device_id": endpoint["device_id"]}))["result"]

            proven_audio_calls = set()

            async def media_proof(call_id, *, video=False):
                async with asyncio.timeout(8):
                    while True:
                        before = await page.evaluate(MEDIA_SAMPLE_SCRIPT)
                        if before["stats"].get("frames_out", 0) > 0:
                            break
                        await asyncio.sleep(0.1)
                before["wire"] = wire.snapshot()
                await asyncio.sleep(1.2)
                after = await page.evaluate(MEDIA_SAMPLE_SCRIPT)
                after["wire"] = wire.snapshot()
                proof = {"call_id": call_id, "video": video, "before": before, "after": after,
                         "wire_errors": list(wire.capture.errors)}
                results["media_proof"].append(proof)
                assert after["call_id"] == call_id
                assert not wire.capture.errors, wire.capture.errors
                validate_media_progress(before, after, video=video)

            async def connected(expected_call_id=""):
                async with asyncio.timeout(8):
                    while True:
                        snapshots = [await snapshot(endpoint) for endpoint in (owner, peer)]
                        if all(item["state"] == "in_call" for item in snapshots):
                            assert snapshots[0]["call_id"] == snapshots[1]["call_id"]
                            if expected_call_id:
                                assert snapshots[0]["call_id"] == expected_call_id
                            call_id = snapshots[0]["call_id"]
                            if call_id not in proven_audio_calls:
                                await media_proof(call_id)
                                proven_audio_calls.add(call_id)
                            return snapshots
                        await asyncio.sleep(0.05)

            async def capture_count():
                return await page.evaluate("window.__dashboardCaptureCount||0")

            async def one_capture(before, scenario, expected=1):
                after = await capture_count()
                proof = {"scenario": scenario, "before": before, "after": after,
                         "requests": await page.evaluate("window.__dashboardCaptureRequests||[]")}
                results["capture_proof"].append(proof)
                assert after - before == expected, proof

            async def observe_page_media(browser_page):
                await browser_page.evaluate("""()=>{
                    const e=window.__qualificationEngine;
                    window.__spaMedia={context:e._audioContext,stream:e._mediaStream,ws:e._ws,events:[]};
                    for(const method of (e.__qualificationMediaTraceWrapped?[]:
                        ['close','_cleanupAudio','_connect','_setupAudioOrAbort'])){
                        const original=e[method];
                        if(typeof original!=='function')continue;
                        e[method]=function(...args){
                            const events=window.__spaMedia.events;
                            if(events.length<100)events.push({at:performance.now(),method,
                                reason:typeof args[0]==='string'?args[0]:'',
                                endpoint:this._endpointId,call:this._callId,
                                stack:new Error().stack.split('\\n').slice(1,5)});
                            return original.apply(this,args);
                        };
                    }
                    e.__qualificationMediaTraceWrapped=true;
                    window.__spaSample=()=>({at:performance.now(),active:e.active,state:e._state,
                        endpoint:e._endpointId,call:e._callId,audio_ready:e._audioReady,
                        same_context:e._audioContext===window.__spaMedia.context,
                        context_state:e._audioContext?.state,
                        same_stream:e._mediaStream===window.__spaMedia.stream,
                        tracks:e._mediaStream?.getAudioTracks().map(t=>({id:t.id,state:t.readyState})),
                        same_ws:e._ws===window.__spaMedia.ws,ws_state:e._ws?.readyState,
                        setup_generation:e._audioSetupGeneration,connect_generation:e._connectGeneration,
                        capture_count:window.__dashboardCaptureCount||0,stats:e.stats});
                }""")
            async def spa_other_phone(call_id, *, answer_after_navigation=False):
                """Navigate real Lovelace views without replacing the document or media."""
                await observe_page_media(page)
                before = await page.evaluate("window.__spaSample()")
                pending = before
                samples = []
                try:
                    # This is HA's SPA navigation event, not page.goto/reload.
                    await page.evaluate("""path=>{
                        history.pushState(null,'',path);
                        window.dispatchEvent(new CustomEvent('location-changed',{detail:{replace:false}}));
                    }""", "/lovelace/voip-dashboard-receiver")
                    visible = page.locator("voip-stack-card")
                    await expect(visible).to_have_count(1)
                    await expect(visible).to_be_visible()
                    await page.wait_for_function("""id=>{
                        const find=root=>{for(const n of root.querySelectorAll('*')){
                            if(n.tagName==='VOIP-STACK-CARD'&&n.config?.device_id===id)return true;
                            if(n.shadowRoot&&find(n.shadowRoot))return true;
                        }return false;};return find(document);
                    }""", arg=receiver["device_id"])
                    if answer_after_navigation:
                        await peer_card.evaluate("card=>card._answer()")
                        await connected(call_id)
                        await page.wait_for_function("window.__qualificationEngine._audioReady===true")
                        await page.evaluate("""()=>{
                            const e=window.__qualificationEngine,m=window.__spaMedia;
                            m.context=e._audioContext;m.stream=e._mediaStream;m.ws=e._ws;
                        }""")
                        before = await page.evaluate("window.__spaSample()")
                    for _ in range(50):
                        samples.append(await page.evaluate("window.__spaSample()"))
                        await asyncio.sleep(0.1)
                    await shot("04-spa-pending-answer" if answer_after_navigation else "04-spa-other-phone-during-owner-call")
                    for sample in samples:
                        assert sample["call"] == call_id and sample["endpoint"] == owner["endpoint_id"], sample
                        assert sample["active"] and sample["audio_ready"] and sample["state"] == "IN_CALL", sample
                        assert sample["same_context"] and sample["context_state"] == "running", sample
                        assert sample["same_stream"] and sample["tracks"] == before["tracks"], sample
                        assert sample["same_ws"] and sample["ws_state"] == 1, sample
                        for key in ("setup_generation", "connect_generation", "capture_count"):
                            assert sample[key] == before[key], sample
                    await connected(call_id)
                finally:
                    key = "spa_pending_answer" if answer_after_navigation else "spa_navigation"
                    results[key] = {"pending":pending,"before": before, "samples": samples,
                        "events": await page.evaluate("window.__spaMedia.events")}
                    (args.out / f"{key.replace('_','-')}.json").write_text(json.dumps(results[key], indent=2))
                    await page.evaluate("""()=>{
                        history.pushState(null,'','/lovelace/voip-dashboard-qualification');
                        window.dispatchEvent(new CustomEvent('location-changed',{detail:{replace:false}}));
                    }""")
                    await expect(dashboard.locator("h2")).to_have_text(owner["name"], timeout=20000)
                await connected(call_id)
                results["scenarios"].append("spa-pending-answer-keeps-original-media-owner" if answer_after_navigation
                    else "spa-other-phone-keeps-original-media-owner-and-one-capture")

            async def clone_owned_window(call_id, *, without_claims=False):
                """Exercise the browser's real opener/sessionStorage copy boundary."""
                await observe_page_media(page)
                before = await page.evaluate("window.__spaSample()")
                samples = []
                copied = None
                try:
                    async with page.expect_popup() as opening:
                        if without_claims:
                            await page.evaluate("""()=>{
                                const child=window.open('about:blank','voip-owned-session-copy');
                                child.sessionStorage.removeItem('voip_stack_owned_softphone_calls');
                                child.location.href=location.href;
                            }""")
                        else:
                            await page.evaluate("window.open(location.href,'voip-owned-session-copy')")
                    copied = await opening.value
                    copied.on("websocket", observe_websocket)
                    copied.on("pageerror", lambda error: results["browser_errors"].append(f"copied: {error}"))
                    await expect(copied.locator("voip-stack-dashboard-card h2")).to_have_text(owner["name"], timeout=20000)
                    await bind_page_engine(copied)
                    await observe_page_media(copied)
                    # HA renames both windows to ha-main-window. Read the
                    # actual Page handles instead of reopening the old name.
                    async def identity(browser_page):
                        return await browser_page.evaluate("""async()=>{
                        const hash=async text=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',
                            new TextEncoder().encode(text)))).map(x=>x.toString(16).padStart(2,'0')).join('');
                        return {client:await hash(window.__qualificationEngine._mediaClientId),
                            storage:await hash(sessionStorage.getItem('voip_stack_media_client_id')||''),
                            claims:sessionStorage.getItem('voip_stack_owned_softphone_calls'),
                            window_name:window.name,has_opener:!!window.opener,
                            navigation_type:performance.getEntriesByType('navigation')[0]?.type};
                        }""")
                    parent_identity = await identity(page)
                    child_identity = await identity(copied)
                    identities = {"parent":parent_identity["client"],"child":child_identity["client"],
                        "parent_document":parent_identity,"child_document":child_identity}
                    for _ in range(100):
                        samples.append({"parent":await page.evaluate("window.__spaSample()"),
                                        "child":await copied.evaluate("window.__spaSample()")})
                        await asyncio.sleep(0.1)
                    key = "clone_without_claims" if without_claims else "clone_window"
                    await page.screenshot(path=str(args.out / f"{key}-parent.png"), full_page=True)
                    await copied.screenshot(path=str(args.out / f"{key}-child.png"), full_page=True)
                    results[key] = {"before":before,"identities":identities,"samples":samples,
                        "parent_events":await page.evaluate("window.__spaMedia.events"),
                        "child_events":await copied.evaluate("window.__spaMedia.events")}
                    (args.out / f"{key.replace('_','-')}.json").write_text(json.dumps(results[key], indent=2))
                    assert identities["parent"] != identities["child"], "Independent documents share a media identity"
                    for sample in samples:
                        current = sample["parent"]
                        assert current["call"] == call_id and current["endpoint"] == owner["endpoint_id"], current
                        assert current["same_context"] and current["same_stream"] and current["same_ws"], current
                        assert current["context_state"] == "running" and current["ws_state"] == 1, current
                        assert current["capture_count"] == before["capture_count"], current
                        if without_claims:
                            child = sample["child"]
                            assert not child["active"] and not child["audio_ready"] and child["capture_count"] == 0, child
                    results["scenarios"].append("copied-window-without-claims-stays-passive" if without_claims
                        else "copied-opener-window-keeps-original-media-owner")
                finally:
                    if copied is not None:
                        await copied.close()

            async def reload_owned_window(call_id):
                """A real document reload releases the old socket and recovers the same call."""
                identity = await page.evaluate("""async()=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',
                    new TextEncoder().encode(window.__qualificationEngine._mediaClientId))))
                    .map(x=>x.toString(16).padStart(2,'0')).join('')""")
                await page.reload()
                await expect(dashboard.locator("h2")).to_have_text(owner["name"], timeout=20000)
                await bind_page_engine()
                await page.wait_for_function("""callId=>{
                    const e=window.__qualificationEngine;
                    return e.callId===callId&&e._audioReady&&e._ws?.readyState===1&&e._audioContext?.state==='running';
                }""", arg=call_id, timeout=15000)
                await connected(call_id)
                recovered_identity = await page.evaluate("""async()=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',
                    new TextEncoder().encode(window.__qualificationEngine._mediaClientId))))
                    .map(x=>x.toString(16).padStart(2,'0')).join('')""")
                assert recovered_identity == identity, "A real reload lost its media ownership identity"
                await observe_page_media(page)
                before = await page.evaluate("window.__spaSample()")
                samples = []
                for _ in range(50):
                    samples.append(await page.evaluate("window.__spaSample()"))
                    await asyncio.sleep(0.1)
                proof = {"identity_before":identity,"identity_after":recovered_identity,"before":before,
                    "samples":samples,"events":await page.evaluate("window.__spaMedia.events")}
                results["reload_owned_window"] = proof
                (args.out / "reload-owned-window.json").write_text(json.dumps(proof, indent=2))
                for current in samples:
                    assert current["call"] == call_id and current["endpoint"] == owner["endpoint_id"], current
                    assert current["same_context"] and current["same_stream"] and current["same_ws"], current
                    assert current["context_state"] == "running" and current["ws_state"] == 1, current
                    assert current["capture_count"] == before["capture_count"], current
                await shot("04-reloaded-owner-keeps-call")
                results["scenarios"].append("true-document-reload-recovers-the-owned-call")

            async def bind_page_engine(browser_page=page):
                return await browser_page.evaluate("""async ()=>{
                    const urls=[...new Set(performance.getEntriesByType('resource')
                      .map(entry=>entry.name).filter(name=>new URL(name).pathname.endsWith('/voip-stack-engine.js')))];
                    if(urls.length!==1)throw new Error(`Expected one loaded engine module, got ${urls.length}`);
                    window.__qualificationEngine=(await import(urls[0])).voipStackEngine;
                    return {module_url:urls[0],engine_instances:urls.length};
                }""")

            async def legacy_reload_identities():
                """Two real reloads must not retain a copied pre-migration identity."""
                records = []
                legacy = "tab-legacy-copied-document-qualification"
                url = f"{args.url}/voip-stack/qualification-migration"
                for _ in range(2):
                    migrated = await context.new_page()
                    try:
                        # Intercept only this empty browser fixture. Production
                        # engine modules still come from the existing HA server.
                        await migrated.route(url, lambda route: route.fulfill(
                            content_type="text/html", body="<html><body></body></html>"))
                        await migrated.goto(url)
                        await migrated.evaluate("legacy=>sessionStorage.setItem('voip_stack_media_client_id',legacy)", legacy)
                        await migrated.reload()
                        record = await migrated.evaluate("""async ({module,legacy})=>{
                            const {voipStackEngine:e}=await import(module);
                            const id=e.mediaClientId;
                            const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',
                                new TextEncoder().encode(id)))).map(x=>x.toString(16).padStart(2,'0')).join('');
                            return {identity:hash,retained_legacy:id===legacy,
                                navigation_type:performance.getEntriesByType('navigation')[0]?.type,
                                capture_count:window.__dashboardCaptureCount||0};
                        }""", {"module":results["engine"]["module_url"],"legacy":legacy})
                        records.append(record)
                    finally:
                        await migrated.close()
                results["legacy_reload_migration"] = records
                (args.out / "legacy-reload-migration.json").write_text(json.dumps(records, indent=2))
                assert all(record["navigation_type"] == "reload" and not record["retained_legacy"]
                           and record["capture_count"] == 0 for record in records), records
                assert records[0]["identity"] != records[1]["identity"], records
                results["scenarios"].append("legacy-copied-identities-rotate-on-first-real-reload")

            async def retain_and_remove_views():
                for locator, key in ((dashboard, "dashboard"), (owner_card, "phonecard")):
                    await locator.evaluate("""(node,key)=>{
                        window.__qualificationViews=window.__qualificationViews||{};
                        window.__qualificationViews[key]={node,parent:node.parentNode,next:node.nextSibling};
                        node.remove();
                    }""", key)
                await expect(dashboard).to_have_count(0)
                await expect(owner_card).to_have_count(0)

            async def remount_views():
                await page.evaluate("""()=>{
                    for(const record of Object.values(window.__qualificationViews)){
                        if(!record.parent.isConnected)throw new Error('Lovelace mount parent was replaced');
                        record.parent.insertBefore(record.node,
                          record.next?.parentNode===record.parent?record.next:null);
                    }
                }""")
                await expect(dashboard).to_have_count(1)
                await expect(owner_card).to_have_count(1)

            async def set_view_theme(name):
                configuration = (await ws.command({"type": "lovelace/config"}))["result"]
                view = next(item for item in configuration["views"]
                            if item.get("path") == "voip-dashboard-qualification")
                view["theme"] = name
                await ws.command({"type": "lovelace/config/save", "config": configuration})
                await page.reload()
                await expect(dashboard.locator("h2")).to_have_text(owner["name"], timeout=20000)
                return await dashboard.evaluate("""card=>{
                    const style=getComputedStyle(card);
                    return {text:style.getPropertyValue('--primary-text-color').trim(),
                      background:style.getPropertyValue('--ha-card-background').trim()};
                }""")

            try:
                await page.goto(f"{args.url}/lovelace/voip-dashboard-qualification")
                await remote.goto(f"{args.url}/lovelace/voip-dashboard-peer")
                await receiving_page.goto(f"{args.url}/lovelace/voip-dashboard-receiver")
                await expect(dashboard.locator("h2")).to_have_text(owner["name"], timeout=20000)
                await expect(owner_card).to_be_visible(timeout=20000)
                assert await owner_card.evaluate("card=>card.config.device_id") == owner["device_id"]
                results["engine"] = await bind_page_engine()
                if args.clone_window:
                    await legacy_reload_identities()
                await expect(peer_card).to_be_visible(timeout=20000)
                await expect(receiving_card).to_be_visible(timeout=20000)
                await peer_card.evaluate("async card => { if(card._autoAnswer) await card._toggleAutoAnswer(); }")
                await receiving_card.evaluate("async card => { if(card._autoAnswer) await card._toggleAutoAnswer(); }")
                await owner_card.evaluate("async card => { if(card._autoAnswer) await card._toggleAutoAnswer(); }")
                for phone_card in (owner_card, peer_card, receiving_card):
                    await phone_card.evaluate("async card=>{await card._toggleVideoCamera(false);card._settingsOpen=false;card._render()}")
                await state("idle")
                await shot("01-idle-dark-wide")
                await peer_start()
                await state("ringing")
                await expect(dashboard.locator(".owner-call.ringing")).to_be_visible()
                await shot("02-incoming")
                captures = await capture_count()
                await dashboard.locator(".owner-call .answer").click()
                await state("busy")
                await connected()
                await one_capture(captures, "incoming-answer-with-coexisting-phonecard")
                await expect(dashboard.locator(".owner-call.busy")).to_be_visible()
                await shot("03-in-call")
                await dashboard.locator(".owner-call .hangup").click()
                await state("idle")
                results["scenarios"].append("incoming-answer-hangup")
                captures = await capture_count()
                await peer_start()
                await state("ringing")
                await dashboard.locator(".owner-call .decline").click()
                await state("idle")
                await one_capture(captures, "decline-keeps-microphone-closed", expected=0)
                results["scenarios"].append("incoming-decline")
                target = dashboard.locator(".tile").filter(has=page.locator(".tile-name", has_text=peer["name"]))
                captures = await capture_count()
                await target.locator(".tile-main").click()
                await state("busy")
                if args.spa_navigation:
                    pending_call = (await snapshot(owner))["call_id"]
                    await spa_other_phone(pending_call, answer_after_navigation=True)
                else:
                    await peer_card.evaluate("card => card._answer()")
                await connected()
                await one_capture(captures, "outgoing-with-coexisting-phonecard")
                await expect(dashboard.locator(".owner-call .call-label")).to_be_visible()
                await shot("04-outgoing-connected")
                if args.spa_navigation:
                    await spa_other_phone((await connected())[0]["call_id"])
                if args.clone_window:
                    await clone_owned_window((await connected())[0]["call_id"])
                    await clone_owned_window((await connected())[0]["call_id"], without_claims=True)
                    await reload_owned_window((await connected())[0]["call_id"])
                await dashboard.locator(".owner-call .hangup").click()
                await state("idle")
                assert any(request["destination"] == peer["name"] and request["device_id"] == owner["device_id"]
                           for request in results["call_requests"]), results["call_requests"]
                results["scenarios"].append("outgoing-call-hangup-by-canonical-name")
                await peer_start(receiver["extension"])
                ringing_tile = dashboard.locator(f'.tile[data-key="endpoint:{receiver["endpoint_id"]}"]')
                await expect(ringing_tile).to_have_class(re.compile(r"\bringing\b"))
                await shot("05-directed-pickup-ringing")
                captures = await capture_count()
                await ringing_tile.locator(".pickup").click()
                await state("busy")
                await connected()
                await one_capture(captures, "pickup-with-coexisting-phonecard")
                await shot("05-directed-pickup-connected")
                receiver_snapshot = (await ws.command({"type": "voip_stack/ha_softphone_state",
                    "device_id": receiver["device_id"]}))["result"]
                assert receiver_snapshot["state"] == "idle"
                await dashboard.locator(".owner-call .hangup").click()
                await state("idle")
                results["scenarios"].append("directed-pickup-retargets-exact-call")

                # Keep the actual Lovelace nodes and their real hass binding.
                # The page-level engine must continue while neither view exists.
                captures = await capture_count()
                await target.locator(".tile-main").click()
                await state("busy")
                async with asyncio.timeout(8):
                    while await dashboard.evaluate("card=>!!card._pending"):
                        await asyncio.sleep(0.05)
                calling = await snapshot(owner)
                assert calling["state"] in {"calling", "remote_ringing"}, calling
                call_id = calling["call_id"]
                assert call_id
                await retain_and_remove_views()
                removed = await snapshot(owner)
                assert removed["call_id"] == call_id and removed["state"] in {"calling", "remote_ringing"}
                await peer_card.evaluate("card=>card._answer()")
                await connected(call_id)
                await page.wait_for_function("""callId=>{
                    const engine=window.__qualificationEngine;
                    return engine.active&&engine.callId===callId&&engine._state==='IN_CALL';
                }""", arg=call_id, timeout=10000)
                await one_capture(captures, "calling-unmount-peer-answer-without-views")
                results["lifecycle"].append({"phase": "viewless-in-call", "call_id": call_id,
                    "snapshot": await snapshot(owner), "capture_count": await capture_count()})
                await shot("07-viewless-engine-call-continues")
                await remount_views()
                await state("busy")
                await connected(call_id)
                await one_capture(captures, "remount-after-viewless-answer")
                await shot("08-remounted-same-call")
                results["scenarios"].append("calling-unmount-answer-remount-keeps-call-and-one-capture")

                await retain_and_remove_views()
                await connected(call_id)
                assert await page.evaluate("window.__qualificationEngine.callId") == call_id
                await remount_views()
                await state("busy")
                await one_capture(captures, "in-call-view-recreation")
                await dashboard.locator(".owner-call .hangup").click()
                await state("idle")
                results["scenarios"].append("in-call-view-recreation-keeps-call-and-one-capture")
                # Numeric dialing and settings use the same native child and engine.
                embedded = dashboard.locator("voip-stack-card[data-dashboard-phone]")
                await expect(dashboard.locator(".options")).to_have_text("")
                await expect(dashboard.locator(".options ha-icon")).to_have_attribute("icon", "mdi:cog")
                await dashboard.locator(".keypad").click()
                await expect(embedded.locator(".keypad-panel")).to_be_visible()
                await expect(dashboard.locator(".scroller")).to_be_hidden()
                await embedded.get_by_role("button", name="Clear destination", exact=True).click()
                for digit in peer["extension"]:
                    await embedded.get_by_role("button", name=digit, exact=True).click()
                await expect(embedded.locator(".keypad-input")).to_have_value(peer["extension"])
                await dashboard.screenshot(path=str(args.out / "09-keypad-desktop.png"))
                results["screenshots"].append("09-keypad-desktop.png")
                await page.set_viewport_size({"width":390,"height":1000})
                await expect(dashboard).to_have_class(re.compile(r"\bnarrow\b"))
                await dashboard.screenshot(path=str(args.out / "09-keypad-mobile.png"))
                results["screenshots"].append("09-keypad-mobile.png")
                await page.set_viewport_size({"width":1500,"height":1100})
                await dashboard.locator(".options").click()
                await expect(embedded.locator(".settings-panel")).to_be_visible()
                await expect(embedded.locator(".auto-answer-row").first).to_be_visible()
                await shot("10-native-phone-options")
                await dashboard.locator(".options").click()
                await expect(embedded.locator(".settings-panel")).to_be_hidden()
                await expect(embedded.locator(".keypad-panel")).to_be_visible()
                await expect(dashboard.locator(".phone-view")).to_be_visible()
                captures = await capture_count()
                await embedded.locator(".voip-button.call").click()
                await state("busy")
                await peer_card.evaluate("card=>card._answer()")
                await connected()
                await one_capture(captures, "native-keypad-number-dial")
                # Native dialing intentionally closes number entry before the call.
                # Reopen the same control in its in-call DTMF mode.
                await dashboard.locator(".keypad").click()
                await expect(embedded.locator(".keypad-panel")).to_be_visible()
                await embedded.get_by_role("button", name="4", exact=True).click()
                async with asyncio.timeout(5):
                    while not any(item["digit"] == "4" for item in results["dtmf_controls"]):
                        await asyncio.sleep(0.05)
                await dashboard.locator(".keypad").click()
                await dashboard.locator(".owner-call .hangup").click()
                await state("idle")
                await expect(dashboard.locator(".scroller")).to_be_visible()
                await expect(dashboard.locator(".phone-view")).to_be_hidden()
                results["scenarios"].append("native-keypad-dials-number-and-sends-in-call-dtmf")
                results["scenarios"].append("icon-only-options-toggle-retains-native-keypad")

                if args.video:
                    for phone_card in (embedded, peer_card):
                        await phone_card.evaluate("async card => {await card._toggleVideoCamera(true);card._settingsOpen=false;card._render()}")
                    await page.bring_to_front()
                    await peer_start()
                    await state("ringing")
                    await expect(dashboard.locator(".answer-video")).to_be_visible()
                    await expect(dashboard.locator(".scroller")).to_be_visible()
                    await shot("11-incoming-video-choices")
                    await dashboard.locator(".answer-video").click()
                    await connected()
                    await page.wait_for_function("""()=>{
                        const e=window.__qualificationEngine;
                        const find=(root)=>{for(const node of root.querySelectorAll('*')){
                          if(node.tagName==='VOIP-STACK-DASHBOARD-CARD')return node;
                          if(node.shadowRoot){const found=find(node.shadowRoot);if(found)return found;}
                        }};
                        const d=find(document);
                        return e.videoVisible&&e.stats.video.rendered>0&&d?._phonePresentation.video;
                    }""",timeout=20000)
                    await expect(dashboard.locator(".scroller")).to_be_hidden()
                    await expect(embedded.locator(".video-canvas")).to_be_visible()
                    await embedded.get_by_role("button", name="Keypad", exact=True).click()
                    await expect(embedded.locator(".keypad-panel")).to_be_visible()
                    await embedded.get_by_role("button", name="5", exact=True).click()
                    async with asyncio.timeout(5):
                        while not any(item["digit"] == "5" for item in results["dtmf_controls"]):
                            await asyncio.sleep(0.05)
                    await shot("12-video-native-keypad")
                    await embedded.get_by_role("button", name="Contacts", exact=True).click()
                    await expect(embedded.locator(".keypad-panel")).to_be_hidden()
                    await shot("12-video-replaces-tiles-desktop")
                    await dashboard.screenshot(path=str(args.out / "12-video-card-desktop.png"))
                    results["screenshots"].append("12-video-card-desktop.png")
                    await page.set_viewport_size({"width":390,"height":1000})
                    await expect(dashboard).to_have_class(re.compile(r"\bnarrow\b"))
                    await shot("13-video-replaces-tiles-mobile")
                    await dashboard.screenshot(path=str(args.out / "13-video-card-mobile.png"))
                    results["screenshots"].append("13-video-card-mobile.png")
                    results["video"] = await page.evaluate("""()=>({stats:window.__qualificationEngine.stats.video,
                        encoding:window.__qualificationEngine._video?._encoding,
                        captures:window.__dashboardCaptureRequests})""")
                    video_call_id = (await connected())[0]["call_id"]
                    await media_proof(video_call_id, video=True)
                    captures = await capture_count()
                    await embedded.get_by_role("button", name="Options", exact=True).click()
                    await expect(embedded.locator(".settings-panel")).to_be_visible()
                    await expect(embedded.locator(".runtime-controls")).to_be_hidden()
                    await expect(embedded.locator(".header")).to_be_hidden()
                    await expect(dashboard.locator(".dashboard-header")).to_be_visible()
                    await connected(video_call_id)
                    await shot("13-video-inline-options")
                    await dashboard.locator(".options").click()
                    await expect(embedded.locator(".settings-panel")).to_be_hidden()
                    await expect(dashboard.locator(".dashboard-header")).to_be_hidden()
                    await expect(embedded.locator(".video-canvas")).to_be_visible()
                    await connected(video_call_id)
                    await one_capture(captures, "video-options-keeps-the-same-media-session", expected=0)
                    results["scenarios"].append("video-inline-options-keep-call-and-restore-native-video-controls")
                    # Native hangup follows the audio-level scale animation.
                    # Use its normal DOM click, as the maintained video probe does.
                    hangup = embedded.locator(".voip-button.hangup")
                    await expect(hangup).to_be_visible()
                    await expect(hangup).to_be_enabled()
                    await hangup.evaluate("button=>button.click()")
                    await state("idle")
                    await expect(dashboard.locator(".scroller")).to_be_visible()
                    await expect(dashboard.locator(".phone-view")).to_be_hidden()
                    results["scenarios"].append("real-video-first-frame-replaces-tiles-and-hangup-restores-them")
                    await page.bring_to_front()
                    await peer_start()
                    await state("ringing")
                    await expect(dashboard.locator(".answer-video")).to_be_visible()
                    await dashboard.locator(".owner-call .answer").click()
                    await connected()
                    audio_only = await snapshot(owner)
                    assert not audio_only.get("video_active"), audio_only
                    assert not await page.evaluate("window.__qualificationEngine.videoActive")
                    await expect(dashboard.locator(".scroller")).to_be_visible()
                    await expect(dashboard.locator(".phone-view")).to_be_hidden()
                    await shot("14-video-offer-answered-audio-only")
                    results["audio_only_answer"] = {"video_active":audio_only.get("video_active"),
                        "send_video":audio_only.get("send_video"),"call_id":audio_only.get("call_id")}
                    await dashboard.locator(".owner-call .hangup").click()
                    await state("idle")
                    results["scenarios"].append("video-offer-answered-audio-only-preserves-tiles")
                    for phone_card in (embedded, peer_card):
                        await phone_card.evaluate("card => card._toggleVideoCamera(false)")
                    await embedded.evaluate("card=>{card._settingsOpen=false;card._render()}")

                for width in (1100, 760, 390):
                    await page.set_viewport_size({"width": width, "height": 1000})
                    await shot(f"05-responsive-{width}")
                    assert await dashboard.evaluate("card => card.scrollWidth <= card.clientWidth+1")
                results["scenarios"].append("responsive-no-horizontal-overflow")
                await page.set_viewport_size({"width": 1500, "height": 1100})
                themes = (await ws.command({"type": "frontend/get_themes"}))["result"]["themes"]
                assert "Frosted Glass Light" in themes and "Frosted Glass Dark" in themes
                dark = await dashboard.evaluate("""card=>{
                    const style=getComputedStyle(card);return {text:style.getPropertyValue('--primary-text-color').trim(),
                      background:style.getPropertyValue('--ha-card-background').trim()};
                }""")
                light = await set_view_theme("Frosted Glass Light")
                assert light != dark, {"dark": dark, "light": light}
                results["themes"] = {"dark": dark, "light": light}
                await shot("06-light-surface")
                await set_view_theme("Frosted Glass Dark")
                await shot("09-final-dark-coexistence")
                results["scenarios"].append("actual-frosted-glass-light-and-dark-themes")
            except BaseException as error:
                results["failure"] = str(error)
                await shot("failure")
                (args.out / "result.json").write_text(json.dumps(results, indent=2))
                raise
            finally:
                for endpoint in (owner, peer, receiver):
                    try:
                        current = await snapshot(endpoint)
                        if current.get("call_id") and current.get("state") in {"ringing", "calling", "remote_ringing", "in_call", "connecting", "answering", "held"}:
                            await ws.command({"type": "call_service", "domain": "voip_stack", "service": "hangup",
                                "service_data": {"device_id": endpoint["device_id"], "call_id": current["call_id"]}})
                    except Exception as error:
                        results["cleanup_errors"].append(f"hangup {endpoint['endpoint_id']}: {error}")
                for endpoint in (owner, peer, receiver):
                    try:
                        await ws.command({"type":"call_service","domain":"voip_stack","service":"set_send_video",
                            "service_data":{"device_id":endpoint["device_id"],"send_video":initial_preferences[endpoint["device_id"]]["send_video"]}})
                    except Exception as error:
                        results["cleanup_errors"].append(f"camera preference reset: {error}")
                for endpoint in (owner, peer, receiver):
                    try:
                        await ws.command({"type": "call_service", "domain": "voip_stack", "service": "set_auto_answer",
                            "service_data": {"device_id": endpoint["device_id"],
                                             "auto_answer": initial_preferences[endpoint["device_id"]]["auto_answer"]}})
                    except Exception as error:
                        results["cleanup_errors"].append(f"restore auto answer {endpoint['endpoint_id']}: {error}")
                try:
                    configuration = (await ws.command({"type": "lovelace/config"}))["result"]
                    view = next(item for item in configuration["views"]
                                if item.get("path") == "voip-dashboard-qualification")
                    if view.get("theme") != "Frosted Glass Dark":
                        view["theme"] = "Frosted Glass Dark"
                        await ws.command({"type": "lovelace/config/save", "config": configuration})
                except Exception as error:
                    results["cleanup_errors"].append(f"restore dark theme: {error}")
                await browser.close()
                try:
                    final = await snapshot(owner)
                    results["final_state"] = final["state"]
                    results["runtime_resources"] = final.get("media_debug", {}).get("runtime_resources", {})
                except Exception as error:
                    results["cleanup_errors"].append(f"final resource check: {error}")
                (args.out / "result.json").write_text(json.dumps(results, indent=2))
            resources = results.get("runtime_resources", {})
            assert results.get("final_state") == "idle"
            assert resources.get("call_scoped_quiescent") is True, resources
            assert not results["browser_errors"], results["browser_errors"]
            assert not results["cleanup_errors"], results["cleanup_errors"]
            results["quiescent"] = True
    (args.out / "result.json").write_text(json.dumps(results, indent=2))
    assert not results["cleanup_errors"], results["cleanup_errors"]
    print(json.dumps({"scenarios": results["scenarios"], "screenshots": len(results["screenshots"]),
                      "quiescent": results["quiescent"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    lab = Path.home() / "ha-voip-lab"
    parser.add_argument("--url", default="http://127.0.0.1:18123")
    parser.add_argument("--credentials", type=Path, default=lab / ".credentials")
    parser.add_argument("--storage", type=Path, default=lab / "playwright-storage.json")
    parser.add_argument("--out", type=Path, default=CAPTURE_ROOT / "voip-dashboard")
    parser.add_argument("--owner", default="default")
    parser.add_argument("--video", action="store_true", help="Include a real two-browser video call")
    parser.add_argument("--fake-video-file", type=Path, help="Y4M camera fixture for the browser video call")
    parser.add_argument("--setup", action="store_true", help="Create dedicated lab views before the test")
    parser.add_argument("--spa-navigation", action="store_true", help="Verify media ownership across real Lovelace views")
    parser.add_argument("--clone-window", action="store_true", help="Verify the real opener sessionStorage copy boundary")
    parser.add_argument("--physical-peer", help="Canonical endpoint_id of one existing ESP or standard SIP peer")
    parser.add_argument("--physical-ui", choices=("dashboard", "compact"), default="dashboard")
    parser.add_argument("--physical-direction", choices=("outgoing", "incoming"), default="outgoing")
    parser.add_argument("--physical-video", choices=("audio", "recvonly", "sendonly", "sendrecv"), default="audio",
                        help="Required negotiated video direction local to the browser")
    parser.add_argument("--physical-hangup", choices=("dashboard", "peer"), default="dashboard")
    parser.add_argument("--physical-hold-seconds", type=float, default=5)
    parser.add_argument("--physical-wait-seconds", type=float, default=60)
    parser.add_argument("--signals-dir", type=Path, help="Fresh private ready/connected/measuring/measured/done directory for Root's actor")
    asyncio.run(qualify(parser.parse_args()))


if __name__ == "__main__":
    main()
