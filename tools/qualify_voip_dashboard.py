#!/usr/bin/env python3
"""Exercise the operator card in real laboratory Lovelace, saving screenshots.

Uses the existing HA laboratory and browser authentication. The optional setup
creates three dedicated views, preserving other views. Calls stay between
laboratory browser phones with fake microphones and silent playback. The main
view contains the dashboard and ordinary phonecard for the same owner.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from ha_voip_lab.refresh_playwright_auth import refresh_playwright_auth  # noqa: E402
from live_voip_qualification import HaWs  # noqa: E402


async def qualify(args):
    from playwright.async_api import async_playwright, expect

    if urlsplit(args.url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Use the isolated loopback HA laboratory for this call test")
    args.out.mkdir(parents=True, exist_ok=True)
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
               "capture_proof": [], "lifecycle": [], "cleanup_errors": [], "dtmf_controls": [], "call_requests": []}
    async with HaWs(args.url, token) as ws:
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
        if args.setup:
            config = (await ws.command({"type": "lovelace/config"}))["result"]
            backup = args.out / "lovelace-before.json"
            if not backup.exists():
                backup.write_text(json.dumps(config, indent=2))
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
                      "--autoplay-policy=no-user-gesture-required"])
            context = await browser.new_context(storage_state=str(args.storage), permissions=["microphone", "camera"],
                                                 viewport={"width": 1500, "height": 1100})
            await context.add_init_script("""navigator.mediaDevices.getUserMedia =
                ((original) => async (constraints) => {
                  window.__dashboardCaptureCount=(window.__dashboardCaptureCount||0)+1;
                  window.__dashboardCaptureRequests=(window.__dashboardCaptureRequests||[])
                    .concat({audio:!!constraints.audio,video:!!constraints.video});
                  return original.call(navigator.mediaDevices,constraints);
                })(navigator.mediaDevices.getUserMedia);
                HTMLMediaElement.prototype.play = function() { this.muted=true;return Promise.resolve(); };
            """)
            page = await context.new_page()
            def observe_websocket(socket):
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

            async def connected(expected_call_id=""):
                async with asyncio.timeout(8):
                    while True:
                        snapshots = [await snapshot(endpoint) for endpoint in (owner, peer)]
                        if all(item["state"] == "in_call" for item in snapshots):
                            assert snapshots[0]["call_id"] == snapshots[1]["call_id"]
                            if expected_call_id:
                                assert snapshots[0]["call_id"] == expected_call_id
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

            async def bind_page_engine():
                return await page.evaluate("""async ()=>{
                    const urls=[...new Set(performance.getEntriesByType('resource')
                      .map(entry=>entry.name).filter(name=>new URL(name).pathname.endsWith('/voip-stack-engine.js')))];
                    if(urls.length!==1)throw new Error(`Expected one loaded engine module, got ${urls.length}`);
                    window.__qualificationEngine=(await import(urls[0])).voipStackEngine;
                    return {module_url:urls[0],engine_instances:urls.length};
                }""")

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
                await peer_card.evaluate("card => card._answer()")
                await connected()
                await one_capture(captures, "outgoing-with-coexisting-phonecard")
                await expect(dashboard.locator(".owner-call .call-label")).to_be_visible()
                await shot("04-outgoing-connected")
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
                            "service_data":{"device_id":endpoint["device_id"],"send_video":False}})
                    except Exception as error:
                        results["cleanup_errors"].append(f"camera preference reset: {error}")
                for endpoint in (owner, peer, receiver):
                    try:
                        await ws.command({"type": "call_service", "domain": "voip_stack", "service": "set_auto_answer",
                            "service_data": {"device_id": endpoint["device_id"],
                                             "auto_answer": bool(endpoint.get("ha_softphone", {}).get("auto_answer"))}})
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
    print(json.dumps({"scenarios": results["scenarios"], "screenshots": len(results["screenshots"]),
                      "quiescent": results["quiescent"]}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    lab = Path.home() / "ha-voip-lab"
    parser.add_argument("--url", default="http://127.0.0.1:18123")
    parser.add_argument("--credentials", type=Path, default=lab / ".credentials")
    parser.add_argument("--storage", type=Path, default=lab / "playwright-storage.json")
    parser.add_argument("--out", type=Path, default=ROOT / "test_captures/voip-dashboard")
    parser.add_argument("--owner", default="default")
    parser.add_argument("--video", action="store_true", help="Include a real two-browser video call")
    parser.add_argument("--setup", action="store_true", help="Create dedicated lab views before the test")
    asyncio.run(qualify(parser.parse_args()))


if __name__ == "__main__":
    main()
