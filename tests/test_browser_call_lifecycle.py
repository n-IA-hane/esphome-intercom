"""Real Chromium document lifecycles exercise the production browser engine.

Browser media and controlled software peers do not qualify physical devices.
"""

import asyncio
from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "custom_components/voip_stack/frontend"
pytestmark = [pytest.mark.js_runtime, pytest.mark.browser_real]


async def browser_session(check, peer=None):
    from aiohttp import web
    from playwright.async_api import async_playwright

    async def index(_request):
        return web.Response(content_type="text/html", text='''<!doctype html><html><body>
<script type="module">
import {voipStackEngine} from '/voip-stack/voip-stack-engine.js';
window.engine=voipStackEngine;
</script></body></html>''')

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_static("/voip-stack", FRONTEND)
    if peer is not None:
        peer.attach(app)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}"
    try:
        async with async_playwright() as runtime:
            browser = await runtime.chromium.launch(
                executable_path=shutil.which("chromium"), headless=True,
                args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
                      "--autoplay-policy=no-user-gesture-required", "--mute-audio"],
            )
            try:
                context = await browser.new_context()
                await context.grant_permissions(["microphone", "camera"], origin=base)
                await check(context, base)
            finally:
                await browser.close()
    finally:
        if peer is not None:
            await peer.close()
        await runner.cleanup()


async def open_engine(context, base):
    page = await context.new_page()
    await page.goto(base)
    await page.wait_for_function("window.engine")
    return page


@pytest.mark.skipif(shutil.which("chromium") is None, reason="Chromium is unavailable")
@pytest.mark.parametrize("scenario", ["opener_clone", "legacy_migration", "real_reload"])
def test_window_identity_uses_real_navigation_type(scenario):
    pytest.importorskip("playwright.async_api")

    async def check(context, base):
        original = await open_engine(context, base)
        original_id = await original.evaluate("engine.mediaClientId")
        if scenario == "opener_clone":
            await original.evaluate("engine.claimSoftphoneSession('call-A','desk')")
            async with context.expect_page() as opening:
                await original.evaluate("url=>window.open(url,'_blank')", base)
            clone = await opening.value
            await clone.wait_for_function("window.engine")
            clone_id = await clone.evaluate("engine.mediaClientId")
            assert clone_id != original_id, "A real opener clone inherited the live media identity"
            assert await clone.evaluate("performance.getEntriesByType('navigation')[0].type") == "navigate"
            assert await original.evaluate("engine.mediaClientId") == original_id
        elif scenario == "legacy_migration":
            ids = []
            for page in (original, await open_engine(context, base)):
                await page.evaluate("sessionStorage.setItem('voip_stack_media_client_id','legacy-shared-identity-123456')")
                await page.reload()
                await page.wait_for_function("window.engine")
                ids.append(await page.evaluate("engine.mediaClientId"))
            assert ids[0] != ids[1], "Separate real reloads retained a legacy shared identity"
            assert all(value.startswith("media2-") for value in ids)
        else:
            await original.evaluate("engine.claimSoftphoneSession('call-A','desk')")
            await original.reload()
            await original.wait_for_function("window.engine")
            assert await original.evaluate("performance.getEntriesByType('navigation')[0].type") == "reload"
            assert await original.evaluate("engine.mediaClientId") == original_id
            assert await original.evaluate("engine.softphoneCallIdFor('desk')") == "call-A"
            await original.evaluate("history.pushState({},'', '/?view=second')")
            assert await original.evaluate("engine.mediaClientId") == original_id
            fresh = await open_engine(context, base)
            assert await fresh.evaluate("engine.mediaClientId") != original_id

    asyncio.run(browser_session(check))


class SoftwareCallPeer:
    """Controlled signaling and PCM WebSockets, not a Home Assistant server."""

    def __init__(self):
        self.state = {"endpoint_id": "desk", "device_id": "desk-device", "name": "Desk",
                      "state": "idle", "call_id": "", "enabled": True, "available": True,
                      "capabilities": ["audio"], "audio_mode": "full_duplex",
                      "audio_direction": "sendrecv", "sequence": 1}
        self.owner = ""
        self.controls = set()
        self.subscriptions = {}
        self.connections = []
        self.media_sockets = []
        self.attempts = []
        self.reject_starts = 0
        self.delay_calling_event = False
        self.start_reply = asyncio.Event()
        self.start_reply.set()

    def attach(self, app):
        app.router.add_post("/rpc", self.rpc)
        app.router.add_get("/control", self.control)
        app.router.add_get("/api/voip_stack/ws", self.media)

    def snapshot(self, client_id=""):
        # Native HA adds owner status only when a media_client_id was supplied.
        result = dict(self.state)
        if client_id:
            result["media_owner"] = "other" if self.owner and client_id != self.owner else "self"
        return result

    async def broadcast(self, state):
        self.state = {**self.state, "state": state, "sequence": self.state["sequence"] + 1}
        for (socket, ident), request in tuple(self.subscriptions.items()):
            if request["type"] == "voip_stack/subscribe_ha_softphone_state":
                await socket.send_json({"id": ident, "event": self.state})

    async def rpc(self, request):
        from aiohttp import web
        message = await request.json()
        kind = message["type"]
        if kind == "auth/sign_path":
            return web.json_response({"path": message["path"]})
        if kind == "voip_stack/ha_softphone_state":
            return web.json_response(self.snapshot(message.get("media_client_id", "")))
        if kind == "voip_stack/list_devices":
            return web.json_response({"devices": [{**self.state, "softphone": True}],
                                      "entity_ids": {"phonebook": "sensor.voip_phonebook"}})
        if kind == "call_service":
            service, data = message["service"], message["service_data"]
            if service == "call":
                self.attempts.append(dict(data))
                if self.reject_starts:
                    self.reject_starts -= 1
                    return web.json_response({"error": "controlled call service refusal"})
                self.owner = data["media_client_id"]
                self.state["call_id"] = f"software-call-{len(self.attempts)}"
                if self.delay_calling_event:
                    self.state = {**self.state, "state": "calling", "sequence": self.state["sequence"] + 1}
                else:
                    await self.broadcast("calling")
                reply = dict(self.state)
                await self.start_reply.wait()
                return web.json_response(reply)
            if service in {"hangup", "decline"}:
                await self.broadcast("idle")
                return web.json_response({})
        return web.json_response([])

    async def control(self, request):
        from aiohttp import web
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.controls.add(socket)
        try:
            async for message in socket:
                value = message.json()
                key = (socket, value["id"])
                if value["type"] == "subscribe":
                    self.subscriptions[key] = value["request"]
                    if value["request"]["type"] == "voip_stack/subscribe_ha_softphone_state":
                        await socket.send_json({"id": value["id"], "event": self.snapshot()})
                elif value["type"] == "unsubscribe":
                    self.subscriptions.pop(key, None)
        finally:
            self.controls.discard(socket)
            for key in tuple(self.subscriptions):
                if key[0] is socket:
                    self.subscriptions.pop(key, None)
        return socket

    def phone_subscriptions(self):
        return sum(value["type"] == "voip_stack/subscribe_ha_softphone_state"
                   for value in self.subscriptions.values())

    async def media(self, request):
        import math
        import struct
        from aiohttp import web, WSMsgType
        client_id = request.query["client_id"]
        if client_id != self.owner or request.query["call_id"] != self.state["call_id"]:
            return web.Response(status=409)
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.media_sockets.append(socket)
        record = {"client_id": client_id, "call_id": self.state["call_id"], "tx_frames": 0,
                  "rx_frames": 0, "rx_nonzero": 0, "closed": False}
        self.connections.append(record)
        await socket.send_json({**self.state, "tx_format": "48000:s16le:1:20",
                                "rx_format": "48000:s16le:1:20"})

        async def send():
            index = 0
            while not socket.closed:
                samples = [int(5000 * math.sin(2 * math.pi * 697 * (index + n) / 48000))
                           for n in range(960)]
                await socket.send_bytes(b"\x01" + struct.pack("<960h", *samples))
                record["tx_frames"] += 1
                index += 960
                await asyncio.sleep(.02)

        sender = asyncio.create_task(send())
        try:
            async for message in socket:
                if message.type is WSMsgType.BINARY and message.data[:1] == b"\x01":
                    assert len(message.data) == 1921, "Unexpected browser PCM frame format"
                    samples = struct.unpack("<960h", message.data[1:])
                    record["rx_frames"] += 1
                    record["rx_nonzero"] += int(any(samples))
        finally:
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
            record["closed"] = True
        return socket

    async def close(self):
        self.start_reply.set()
        for socket in tuple(self.controls):
            await socket.close()


HARNESS = r'''async () => {
  window.gumCalls=[];window.gumStreams=[];
  const gum=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
  navigator.mediaDevices.getUserMedia=async constraints=>{
    gumCalls.push(constraints);
    if(window.holdMicrophone) await new Promise(resolve=>{window.releaseMicrophone=resolve;});
    const stream=await gum(constraints);gumStreams.push(stream);return stream;
  };
  const control=new WebSocket(location.origin.replace('http','ws')+'/control');
  await new Promise((resolve,reject)=>{control.onopen=resolve;control.onerror=reject;});
  const listeners=new Map();let ident=0;
  control.onmessage=event=>{const value=JSON.parse(event.data);listeners.get(value.id)?.(value.event);};
  const rpc=async message=>{
    const value=await (await fetch('/rpc',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify(message)})).json();
    if(value.error) throw new Error(value.error);return value;
  };
  window.testHass={language:'en',user:{is_admin:true},config:{location_name:'Home',components:['voip_stack']},
    states:{'sensor.voip_phonebook':{state:'1',attributes:{roster_json:JSON.stringify({contacts:[
      {id:'Peer',name:'Peer',metadata:{device_id:'peer-device'}}]})}}},
    callWS:rpc,callService:(domain,service,data)=>rpc({type:'call_service',domain,service,service_data:data}),
    connection:{sendMessagePromise:rpc,subscribeMessage:async(listener,request)=>{
      const id=++ident;listeners.set(id,listener);control.send(JSON.stringify({type:'subscribe',id,request}));
      return ()=>{listeners.delete(id);control.send(JSON.stringify({type:'unsubscribe',id}));};
    }}};
  await import('/voip-stack/voip-stack-card.js');
  window.mountCard=async()=>{
    const card=document.createElement('voip-stack-card');window.card=card;
    card.style.cssText='display:block;width:400px';
    card.setConfig({mode:'ha_softphone',device_id:'desk-device'});
    card.hass=testHass;document.body.append(card);return card;
  };
  await mountCard();
}'''


async def wait_until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(.01)


@pytest.mark.skipif(shutil.which("chromium") is None, reason="Chromium is unavailable")
@pytest.mark.parametrize("unmount_phase", ["permission", "ringing", "service_reply", "reply_before_event"])
@pytest.mark.timeout(45)
def test_pcm_call_survives_last_view_and_restores_after_reload(unmount_phase, tmp_path):
    import json
    pytest.importorskip("playwright.async_api")
    peer = SoftwareCallPeer()

    async def check(context, base):
        page = await open_engine(context, base)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.evaluate(HARNESS)
        await page.wait_for_function("card._softphoneSnapshot?.endpoint_id === 'desk'")
        await wait_until(lambda: peer.phone_subscriptions() == 1)
        if unmount_phase == "permission":
            await page.evaluate("window.holdMicrophone=true")
        if unmount_phase in {"service_reply", "reply_before_event"}:
            peer.start_reply.clear()
        peer.delay_calling_event = unmount_phase == "reply_before_event"
        await page.get_by_role("button", name="Call", exact=True).click()
        if unmount_phase == "permission":
            await page.wait_for_function("typeof window.releaseMicrophone === 'function' || !!card._errorMsg")
            assert await page.evaluate("!!window.releaseMicrophone"), await page.evaluate("({error:card._errorMsg,scope:card._softphoneRequestScope(),targets:card._softphoneTargets(),intent:engine.mediaIntent,secure:isSecureContext,worklet:typeof AudioWorkletNode,gum:gumCalls.length})")
        else:
            await wait_until(lambda: bool(peer.attempts))
        await page.evaluate("card.remove()")
        await asyncio.sleep(.05)
        assert peer.phone_subscriptions() == 1, "Unmount released the page-owned setup/call subscription"
        if unmount_phase == "permission":
            await page.evaluate("holdMicrophone=false;releaseMicrophone()")
            await wait_until(lambda: bool(peer.attempts))
        if unmount_phase == "reply_before_event":
            peer.start_reply.set()
            await page.wait_for_function("!card._starting")
            claimed = await page.evaluate("engine.softphoneCallIdFor('desk')")
            assert claimed == peer.state["call_id"], (
                "Cached idle before INVITE cannot erase the call returned by the service"
            )
        await peer.broadcast("in_call")
        peer.start_reply.set()
        await wait_until(lambda: bool(peer.connections))
        await page.wait_for_function("engine._audioSession.ready && engine.callId")
        await wait_until(lambda: peer.connections[-1]["rx_nonzero"] >= 4)
        await page.evaluate("""() => {
          window.playbackProbe=engine._audioSession.audioContext.createAnalyser();
          playbackProbe.fftSize=2048;engine._audioSession.playbackNode.connect(playbackProbe);
          window.samplePlayback=()=>{
            const time=new Float32Array(playbackProbe.fftSize);
            const spectrum=new Float32Array(playbackProbe.frequencyBinCount);
            playbackProbe.getFloatTimeDomainData(time);playbackProbe.getFloatFrequencyData(spectrum);
            let peak=1;for(let i=2;i<spectrum.length;i++)if(spectrum[i]>spectrum[peak])peak=i;
            return {rms:Math.sqrt(time.reduce((sum,value)=>sum+value*value,0)/time.length),
              hz:peak*engine._audioSession.audioContext.sampleRate/playbackProbe.fftSize};
          };
        }""")
        await page.wait_for_function("samplePlayback().rms > .02")
        rendered = await page.evaluate("samplePlayback()")
        assert abs(rendered["hz"] - 697) < 35, rendered
        original_id = await page.evaluate("engine.mediaClientId")
        clone_probe = None
        if unmount_phase == "ringing":
            async with context.expect_page() as opening:
                await page.evaluate("url=>window.open(url,'_blank')", base)
            clone = await opening.value
            await clone.wait_for_function("window.engine")
            await clone.evaluate(HARNESS)
            await clone.wait_for_function("card._softphoneSnapshot?.state === 'in_call'")
            await clone.wait_for_function("!card._audioAttachTask && !engine._sessionAttachPromise")
            clone_probe = {"owner_id": original_id,
                           "clone_id": await clone.evaluate("engine.mediaClientId"),
                           "clone_microphone_requests": await clone.evaluate("gumCalls.length")}
            assert clone_probe["clone_id"] != original_id
            assert clone_probe["clone_microphone_requests"] == 0
            assert len(peer.connections) == 1
            await clone.close()
            await wait_until(lambda: peer.phone_subscriptions() == 1)
            assert await page.evaluate("gumStreams[0].getTracks().every(track=>track.readyState==='live')")
        before = peer.connections[-1]["rx_frames"]
        # One continuous browser/PCM witness observes a real 20-second window.
        duration = 20 if unmount_phase == "ringing" else .3
        started = asyncio.get_running_loop().time()
        await asyncio.sleep(duration)
        elapsed = asyncio.get_running_loop().time() - started
        assert peer.connections[-1]["rx_frames"] > before + duration * 30
        assert await page.evaluate("gumCalls.length") == 1
        assert len(peer.connections) == 1
        assert await page.evaluate("gumStreams[0].getTracks().every(track=>track.readyState==='live')")
        await page.evaluate("mountCard()")
        await page.wait_for_function("card._softphoneSnapshot?.state === 'in_call'")
        assert await page.evaluate("gumCalls.length") == 1
        await page.reload()
        await page.wait_for_function("window.engine")
        await page.evaluate(HARNESS)
        await page.wait_for_function("engine._audioSession.ready && engine.callId")
        await wait_until(lambda: len(peer.connections) == 2 and peer.connections[-1]["rx_nonzero"] >= 4)
        assert await page.evaluate("engine.mediaClientId") == original_id
        assert peer.connections[0]["closed"]
        assert {item["call_id"] for item in peer.connections} == {peer.state["call_id"]}
        await page.get_by_role("button", name="Hang up call", exact=True).click()
        await page.wait_for_function("!engine._ws && !engine._audioSession.audioContext && !engine.mediaCleanupPending")
        await wait_until(lambda: all(item["closed"] for item in peer.connections))
        assert await page.evaluate("gumStreams.every(stream=>stream.getTracks().every(track=>track.readyState==='ended'))")
        await page.evaluate("card.remove()")
        await wait_until(lambda: not peer.subscriptions)
        assert not errors, errors
        (tmp_path / f"{unmount_phase}.json").write_text(json.dumps({
            "observed_seconds": elapsed, "rendered_probe": rendered, "active_clone_probe": clone_probe, "pcm_connections": peer.connections,
            "originate_requests": peer.attempts, "browser_errors": errors,
            "scope": "Chromium WebAudio and controlled PCM WebSockets, not native HA/SIP/hardware",
        }, indent=2))

    asyncio.run(browser_session(check, peer=peer))


@pytest.mark.skipif(shutil.which("chromium") is None, reason="Chromium is unavailable")
def test_failed_start_keeps_phone_and_redials_with_fresh_browser_media(tmp_path):
    import json
    pytest.importorskip("playwright.async_api")
    peer = SoftwareCallPeer()
    peer.reject_starts = 1

    async def check(context, base):
        page = await open_engine(context, base)
        await page.evaluate(HARNESS)
        await page.wait_for_function("card._softphoneSnapshot?.endpoint_id === 'desk'")
        await page.get_by_role("button", name="Call", exact=True).click()
        await page.wait_for_function("card._errorMsg.includes('controlled call service refusal') && !card._starting")
        assert await page.evaluate("card._getSoftphoneEndpointId()") == "desk"
        assert await page.evaluate("card._getConfigDeviceId()") == "desk-device"
        assert await page.evaluate("!engine._mediaIntent && !engine._audioSession.prepared && !engine._ownedSoftphoneCalls.size")
        assert await page.evaluate("gumStreams.every(stream=>stream.getTracks().every(track=>track.readyState==='ended'))")
        assert not peer.connections
        await page.get_by_role("button", name="Call", exact=True).click()
        await wait_until(lambda: len(peer.attempts) == 2)
        await peer.broadcast("in_call")
        await page.wait_for_function("engine._audioSession.ready && engine.callId")
        await wait_until(lambda: bool(peer.connections) and peer.connections[-1]["rx_nonzero"] >= 4)
        assert [item["device_id"] for item in peer.attempts] == ["desk-device", "desk-device"]
        assert len(peer.connections) == 1
        assert await page.evaluate("gumCalls.length") == 2
        await page.get_by_role("button", name="Hang up call", exact=True).click()
        await page.wait_for_function("!engine._ws && !engine._audioSession.audioContext && !engine.mediaCleanupPending")
        await wait_until(lambda: all(item["closed"] for item in peer.connections))
        assert await page.evaluate("gumStreams.every(stream=>stream.getTracks().every(track=>track.readyState==='ended'))")
        await page.evaluate("card.remove()")
        await wait_until(lambda: not peer.subscriptions)
        (tmp_path / "failed-start-redial.json").write_text(json.dumps({
            "originate_requests": peer.attempts, "pcm_connections": peer.connections,
            "scope": "Native Chromium media with a controlled service refusal, not HA or physical media qualification",
        }, indent=2))

    asyncio.run(browser_session(check, peer=peer))


@pytest.mark.skipif(shutil.which("chromium") is None, reason="Chromium is unavailable")
@pytest.mark.timeout(45)
def test_microphone_mute_is_shared_and_survives_media_changes_without_muting_playback(tmp_path):
    import json
    import time
    pytest.importorskip("playwright.async_api")
    peer = SoftwareCallPeer()

    async def check(context, base):
        page = await open_engine(context, base)
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        await page.evaluate(HARNESS)
        await page.wait_for_function("card._softphoneSnapshot?.endpoint_id === 'desk'")
        await page.get_by_role("button", name="Call", exact=True).click()
        await wait_until(lambda: bool(peer.attempts))
        peer.state.update(connected_at=time.time() - 12, peer_name="Peer", callee="Peer", direction="outgoing")
        await peer.broadcast("in_call")
        await page.wait_for_function("engine._audioSession.ready && engine.callId")
        await wait_until(lambda: peer.connections and peer.connections[-1]["rx_nonzero"] >= 4)
        await page.screenshot(path=str(tmp_path / "audio-call.png"), full_page=True)
        mute = page.get_by_role("button", name="Mute microphone", exact=True)
        assert await mute.count() == 1, "The active browser call has no microphone mute control"
        await page.evaluate("""() => {
          window.playbackProbe=engine._audioSession.audioContext.createAnalyser();playbackProbe.fftSize=2048;
          engine._audioSession.playbackNode.connect(playbackProbe);
          window.playbackPower=()=>{
            const data=new Float32Array(playbackProbe.fftSize);playbackProbe.getFloatTimeDomainData(data);
            return Math.sqrt(data.reduce((sum,value)=>sum+value*value,0)/data.length);
          };
        }""")
        await mute.click()
        await page.wait_for_function("engine.microphoneMuted && !engine._audioSession.mediaStream.getAudioTracks()[0].enabled")
        assert await page.get_by_role("button", name="Unmute microphone", exact=True).get_attribute("aria-pressed") == "true"
        await page.wait_for_function("playbackPower() > .02")
        muted_playback = await page.evaluate("playbackPower()")

        async def assert_silent_frames():
            record = peer.connections[-1]
            before = record["rx_frames"]
            await wait_until(lambda: record["rx_frames"] >= before + 5)
            nonzero = record["rx_nonzero"]
            before = record["rx_frames"]
            await wait_until(lambda: record["rx_frames"] >= before + 10)
            assert record["rx_nonzero"] == nonzero, record

        await assert_silent_frames()
        assert not await page.evaluate("engine.setMicrophoneMuted(false,{call_id:'wrong-call',endpoint_id:'desk'})")
        assert await page.evaluate("engine.microphoneMuted")
        await page.evaluate("window.firstCard=card;mountCard()")
        await page.wait_for_function("card._softphoneSnapshot?.state === 'in_call'")
        assert await page.get_by_role("button", name="Unmute microphone", exact=True).count() == 2
        await page.get_by_role("button", name="Unmute microphone", exact=True).last.click()
        assert await page.get_by_role("button", name="Mute microphone", exact=True).count() == 2
        await page.get_by_role("button", name="Mute microphone", exact=True).first.click()
        assert await page.get_by_role("button", name="Unmute microphone", exact=True).count() == 2
        await page.evaluate("firstCard.remove()")
        assert await page.evaluate("firstCard._videoDurationTimer === null")
        await page.evaluate("engine.setMediaDevice('audioinput','')")
        assert await page.evaluate("!engine._audioSession.mediaStream.getAudioTracks()[0].enabled")
        await assert_silent_frames()
        await peer.media_sockets[-1].send_json({"audio_direction": "recvonly"})
        await page.wait_for_function("engine._audioDirection === 'recvonly' && !engine._audioSession.captureNode && engine._audioSession.ready")
        assert await page.evaluate("engine.microphoneMuted")
        await peer.media_sockets[-1].send_json({"audio_direction": "sendrecv"})
        await page.wait_for_function("engine._audioDirection === 'sendrecv' && !!engine._audioSession.captureNode && engine._audioSession.ready")
        assert await page.evaluate("!engine._audioSession.mediaStream.getAudioTracks()[0].enabled")
        await assert_silent_frames()
        assert len(peer.connections) == 1
        await peer.media_sockets[-1].close()
        await wait_until(lambda: len(peer.connections) == 2)
        await page.wait_for_function("engine._audioSession.ready && engine.callId && engine.microphoneMuted")
        await assert_silent_frames()
        assert peer.connections[0]["closed"]
        original_media_client = await page.evaluate("engine.mediaClientId")
        await page.reload()
        await page.wait_for_function("window.engine")
        await page.evaluate(HARNESS)
        await page.wait_for_function("engine._audioSession.ready && engine.callId")
        assert await page.evaluate("engine.microphoneMuted"), "Reloading the same owned call unexpectedly enabled its microphone"
        assert await page.evaluate("engine.mediaClientId") == original_media_client
        assert await page.evaluate("[...engine._ownedSoftphoneCalls.values()].every(value=>typeof value==='string')")
        assert await page.evaluate("!engine._audioSession.mediaStream.getAudioTracks()[0].enabled")
        await wait_until(lambda: len(peer.connections) == 3 and all(item["closed"] for item in peer.connections[:-1]))
        await assert_silent_frames()
        async with context.expect_page() as opening:
            await page.evaluate("url=>window.open(url,'_blank')", base)
        observer = await opening.value
        await observer.wait_for_function("window.engine")
        await observer.evaluate(HARNESS)
        await observer.wait_for_function("card._softphoneSnapshot?.state === 'in_call'")
        assert await observer.get_by_role("button", name="Mute microphone", exact=True).count() == 0
        assert not await observer.evaluate("engine.setMicrophoneMuted(false,{call_id:card._softphoneSnapshot.call_id,endpoint_id:'desk'})")
        assert await observer.evaluate("gumCalls.length") == 0
        await observer.close()
        duration = page.locator(".audio-call-summary .call-duration")
        assert await duration.is_visible()
        previous_duration = await duration.text_content()
        await page.wait_for_function("previous=>card.shadowRoot.querySelector('.audio-call-summary .call-duration').textContent !== previous", arg=previous_duration)
        assert await page.locator(".audio-call-summary").evaluate(
            "(summary) => summary.innerText === summary.querySelector('.call-duration').innerText"
        )
        await page.screenshot(path=str(tmp_path / "audio-call-muted.png"), full_page=True)
        await page.get_by_role("button", name="Hang up call", exact=True).click()
        await page.wait_for_function("!engine._ws && !engine._audioSession.audioContext && !engine.mediaCleanupPending")
        await wait_until(lambda: all(item["closed"] for item in peer.connections))
        assert await page.evaluate("!engine.microphoneMuted && card._videoDurationTimer === null")
        assert await page.evaluate("sessionStorage.getItem('voip_stack_owned_softphone_calls') === null")
        assert await page.evaluate("gumStreams.every(stream=>stream.getTracks().every(track=>track.readyState==='ended'))")
        await page.get_by_role("button", name="Call", exact=True).click()
        await wait_until(lambda: len(peer.attempts) == 2)
        peer.state["connected_at"] = time.time()
        await peer.broadcast("in_call")
        await page.wait_for_function("engine._audioSession.ready && engine.callId && !engine.microphoneMuted")
        assert await page.evaluate("typeof JSON.parse(sessionStorage.getItem('voip_stack_owned_softphone_calls')).desk === 'string'")
        await wait_until(lambda: len(peer.connections) == 4 and peer.connections[-1]["rx_nonzero"] >= 4)
        await page.get_by_role("button", name="Hang up call", exact=True).click()
        await page.wait_for_function("!engine._ws && !engine._audioSession.audioContext && !engine.mediaCleanupPending")
        await page.evaluate("card.remove()")
        await wait_until(lambda: not peer.subscriptions)
        assert not errors, errors
        (tmp_path / "microphone-mute.json").write_text(json.dumps({
            "muted_playback_rms": muted_playback, "pcm_connections": peer.connections,
            "browser_errors": errors, "scope": "Real Chromium with controlled PCM peers; no SIP or physical-device qualification",
        }, indent=2))

    asyncio.run(browser_session(check, peer=peer))


@pytest.mark.skipif(shutil.which("chromium") is None, reason="Chromium is unavailable")
def test_remembering_another_owned_call_preserves_active_and_held_microphone_intent(tmp_path):
    import json
    pytest.importorskip("playwright.async_api")
    peer = SoftwareCallPeer()
    first = {**peer.state, "state": "in_call", "endpoint_id": "desk", "device_id": "desk-device", "call_id": "call-A"}
    second = {**first, "endpoint_id": "consult", "device_id": "consult-device", "call_id": "call-B"}

    async def check(context, base):
        page = await open_engine(context, base)
        peer.owner = await page.evaluate("engine.mediaClientId")
        peer.state = dict(first)
        await page.evaluate("""() => {
          window.capturedStreams=[];
          const capture=navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
          navigator.mediaDevices.getUserMedia=async options=>{
            const stream=await capture(options);capturedStreams.push(stream);return stream;
          };
          engine.configure({callWS:async request=>{
            const result=await(await fetch('/rpc',{method:'POST',headers:{'Content-Type':'application/json'},
              body:JSON.stringify(request)})).json();
            if(result.error)throw Error(result.error);return result;
          }});
          window.savedCalls=()=>JSON.parse(sessionStorage.getItem('voip_stack_owned_softphone_calls')||'{}');
        }""")
        phases = []

        async def resume(state):
            peer.state = dict(state)
            await page.evaluate("state=>engine.resumeSession({...state,audio_mode:'full_duplex'},state.device_id,state)", state)
            await page.wait_for_function("id=>engine._audioSession.ready&&engine.callId===id", arg=state["call_id"])

        async def observe(record):
            initial = record["rx_frames"]
            await wait_until(lambda: record["rx_frames"] >= initial + 5)
            nonzero = record["rx_nonzero"]
            initial = record["rx_frames"]
            await wait_until(lambda: record["rx_frames"] >= initial + 10)
            return record["rx_nonzero"] - nonzero

        try:
            await page.evaluate("engine.claimSoftphoneSession('call-A','desk')")
            await resume(first)
            await wait_until(lambda: peer.connections and peer.connections[-1]["rx_nonzero"] >= 4)
            await page.evaluate("""() => {
              engine.setMicrophoneMuted(true,{call_id:'call-A',endpoint_id:'desk'});
              window.activeContext=engine._audioSession.audioContext;
              engine.claimSoftphoneSession('call-B','consult');
            }""")
            await peer.media_sockets[-1].send_json({"audio_direction": "sendrecv"})
            nonzero = await observe(peer.connections[-1])
            remembered = await page.evaluate("({muted:engine.microphoneMuted,endpoint:engine.endpointId,call:engine.callId,unchangedContext:engine._audioSession.audioContext===activeContext,saved:savedCalls()})")
            phases.append({"phase": "remember-B-while-A-active", "new_nonzero_frames": nonzero, **remembered})
            (tmp_path / "cross-call-mute.json").write_text(json.dumps({"phases": phases, "connections": peer.connections}, indent=2))
            assert nonzero == 0 and remembered["muted"], phases
            assert remembered["unchangedContext"] and len(peer.connections) == 1
            assert remembered["endpoint"] == "desk" and remembered["call"] == "call-A"
            assert remembered["saved"]["desk"] == {"call_id": "call-A", "microphone_muted": True}
            assert remembered["saved"]["consult"] == "call-B"
            await resume(second)
            await wait_until(lambda: len(peer.connections) == 2 and peer.connections[-1]["rx_nonzero"] >= 4)
            assert not await page.evaluate("engine.microphoneMuted")
            assert await page.evaluate("savedCalls().desk.microphone_muted")
            phases.append({"phase": "B-new-call-unmuted", "nonzero_frames": peer.connections[-1]["rx_nonzero"]})
            await resume(first)
            await wait_until(lambda: len(peer.connections) == 3)
            assert await observe(peer.connections[-1]) == 0
            assert await page.evaluate("engine.microphoneMuted&&!engine._audioSession.mediaStream.getAudioTracks()[0].enabled")
            await page.evaluate("engine.releaseSoftphoneSession('call-B','consult')")
            assert await page.evaluate("engine.microphoneMuted&&savedCalls().desk.microphone_muted&&!savedCalls().consult")
            phases.append({"phase": "A-resumed-still-muted"})
        finally:
            await page.evaluate("""async()=>{
              engine.releaseSoftphoneSession('call-A','desk');
              engine.releaseSoftphoneSession('call-B','consult');
              await engine.close('test-complete');
            }""")
        await wait_until(lambda: all(record["closed"] for record in peer.connections))
        assert await page.evaluate("!engine.microphoneMuted&&!engine._audioSession.audioContext&&!engine.mediaCleanupPending&&capturedStreams.every(stream=>stream.getTracks().every(track=>track.readyState==='ended'))")
        assert await page.evaluate("sessionStorage.getItem('voip_stack_owned_softphone_calls')===null")
        (tmp_path / "cross-call-mute.json").write_text(json.dumps({"phases": phases, "connections": peer.connections}, indent=2))

    asyncio.run(browser_session(check, peer=peer))


@pytest.mark.skipif(shutil.which("chromium") is None, reason="Chromium is unavailable")
def test_retired_output_chooser_cannot_route_a_replacement_call(tmp_path):
    import json
    pytest.importorskip("playwright.async_api")
    peer = SoftwareCallPeer()

    async def check(context, base):
        page = await open_engine(context, base)
        await page.evaluate(HARNESS)
        await page.wait_for_function("card._softphoneSnapshot?.endpoint_id === 'desk'")
        receipt = {}
        try:
            await page.get_by_role("button", name="Call", exact=True).click()
            await wait_until(lambda: bool(peer.attempts))
            await peer.broadcast("in_call")
            await page.wait_for_function("engine._audioSession.ready && engine.callId")
            await wait_until(lambda: peer.connections and peer.connections[-1]["rx_nonzero"] >= 4)
            await page.evaluate("""() => {
              window.firstOutputContext=engine._audioSession.audioContext;
              window.outputRoutes=[];
              const route=AudioContext.prototype.setSinkId;
              if(typeof route!=='function')throw Error('Chromium has no native AudioContext.setSinkId');
              AudioContext.prototype.setSinkId=function(sinkId){
                outputRoutes.push({sinkId,first:this===firstOutputContext,current:this===engine._audioSession.audioContext});
                return route.call(this,sinkId);
              };
              navigator.mediaDevices.selectAudioOutput=()=>new Promise(resolve=>{window.resolveOutputChoice=resolve;});
              window.outputSelection=engine.setMediaDevice('audiooutput','default').then(
                ()=>({status:'resolved'}),error=>({status:'rejected',message:error.message}));
            }""")
            await page.wait_for_function("typeof resolveOutputChoice === 'function'")
            await page.get_by_role("button", name="Hang up call", exact=True).click()
            await page.wait_for_function("!engine._ws && !engine._audioSession.audioContext && !engine.mediaCleanupPending")
            await wait_until(lambda: all(record["closed"] for record in peer.connections))
            await page.get_by_role("button", name="Call", exact=True).click()
            await wait_until(lambda: len(peer.attempts) == 2)
            await peer.broadcast("in_call")
            await page.wait_for_function("engine._audioSession.ready && engine.callId === 'software-call-2'")
            await wait_until(lambda: len(peer.connections) == 2 and peer.connections[-1]["rx_nonzero"] >= 4)
            before = await page.evaluate("""() => {
              window.replacementOutputContext=engine._audioSession.audioContext;
              return {different:replacementOutputContext!==firstOutputContext,
                oldState:firstOutputContext.state,sinkId:replacementOutputContext.sinkId,
                selected:engine.mediaDeviceState.selected.audiooutput,
                active:engine.mediaDeviceState.active.audiooutput,routes:outputRoutes.length};
            }""")
            selection = await page.evaluate("async()=>{resolveOutputChoice({deviceId:'default'});return await outputSelection;}")
            after = await page.evaluate("""() => ({sinkId:replacementOutputContext.sinkId,
              selected:engine.mediaDeviceState.selected.audiooutput,
              active:engine.mediaDeviceState.active.audiooutput,routes:outputRoutes.slice(),
              sameContext:engine._audioSession.audioContext===replacementOutputContext,callId:engine.callId})""")
            receipt.update(before=before, after=after, selection=selection)
            assert before["different"] and before["oldState"] == "closed"
            assert selection["status"] == "rejected" and "superseded" in selection["message"]
            assert after["sameContext"] and after["callId"] == "software-call-2"
            assert len(after["routes"]) == before["routes"], "A retired chooser routed the replacement call"
            assert after["sinkId"] == before["sinkId"]
            assert after["selected"] == before["selected"] and after["active"] == before["active"]
            current = await page.evaluate("""async () => {
              navigator.mediaDevices.selectAudioOutput=async()=>({deviceId:'default'});
              await engine.setMediaDevice('audiooutput','default');
              return {sinkId:replacementOutputContext.sinkId,routes:outputRoutes.slice(),
                selected:engine.mediaDeviceState.selected.audiooutput,
                active:engine.mediaDeviceState.active.audiooutput};
            }""")
            receipt["current_choice"] = current
            assert current["sinkId"] == "default"
            assert current["selected"] == "default" and current["active"] == "default"
            assert len(current["routes"]) == before["routes"] + 1 and current["routes"][-1]["current"]
            frames = peer.connections[-1]["rx_nonzero"]
            await wait_until(lambda: peer.connections[-1]["rx_nonzero"] >= frames + 4)
        finally:
            await page.evaluate("async()=>{if(window.resolveOutputChoice){resolveOutputChoice({deviceId:'default'});await outputSelection;}}")
            await peer.broadcast("idle")
            await page.wait_for_function("!engine._ws && !engine._audioSession.audioContext && !engine.mediaCleanupPending")
            await wait_until(lambda: all(record["closed"] for record in peer.connections))
            assert await page.evaluate("gumStreams.every(stream=>stream.getTracks().every(track=>track.readyState==='ended'))")
            await page.evaluate("card.remove()")
            await wait_until(lambda: not peer.subscriptions)
            receipt["connections"] = peer.connections
            receipt["scope"] = "Native Chromium AudioContext and PCM, controlled deferred chooser; no physical output qualification"
            (tmp_path / "retired-output-chooser.json").write_text(json.dumps(receipt, indent=2))

    asyncio.run(browser_session(check, peer=peer))


@pytest.mark.skipif(shutil.which("chromium") is None, reason="Chromium is unavailable")
def test_card_camera_intent_preserves_unknown_destination_video_contract():
    """The real card and engine must send the user's intent to the public RPC."""
    pytest.importorskip("playwright.async_api")

    async def check(context, base):
        scenarios = [
            (None, ["audio", "dtmf"], True, True, True),
            (None, ["audio", "dtmf"], False, True, False),
            ("", ["audio", "dtmf"], True, False, False),
            ("", ["audio", "dtmf"], True, True, True),
            ("jpeg", ["audio", "dtmf"], True, True, True),
            (None, ["audio", "dtmf", "video"], True, True, True),
            ("__absent__", ["audio", "video"], True, True, True),
            ("", ["audio", "video"], True, False, False),
            ("", ["audio", "video"], True, True, True),
        ]
        for codec, capabilities, camera_intent, video_call, expected in scenarios:
            page = await open_engine(context, base)
            try:
                await page.evaluate("""async ({codec, capabilities, cameraIntent}) => {
                    await import('/voip-stack/voip-stack-card.js');
                    window.rpcCalls=[];
                    const hass={language:'en',user:{is_admin:true},config:{location_name:'Home'},states:{},
                        connection:{subscribeMessage:async()=>()=>{}},
                        callWS:async request=>{
                            if(request.type!=='call_service'||request.domain!=='voip_stack'||request.service!=='call')
                                throw new Error('Unexpected RPC '+JSON.stringify(request));
                            rpcCalls.push(request);
                            return {response:{device_id:'browser-device',endpoint_id:'browser:own',state:'idle'}};
                        }};
                    engine.configure(hass);
                    const card=document.createElement('voip-stack-card');window.card=card;
                    card.setConfig({mode:'ha_softphone',device_id:'browser-device',name:'Browser'});
                    card.style.cssText='display:block;width:420px;height:600px';
                    card._hass=hass;card._softphoneStateLoaded=true;
                    card._softphoneSnapshot={state:'idle',enabled:true,device_id:'browser-device',endpoint_id:'browser:own',
                        capabilities:['audio','video'],video_camera_send_enabled:true,send_video:cameraIntent};
                    card._rosterEntries=[{id:'registered-target',name:'Registered phone',enabled:true,
                        metadata:{endpoint_id:'sip:target',device_id:'target-device',endpoint_kind:'sip_account',
                            username:'registered-target',registered:true,capabilities,
                            ...(codec==='__absent__'?{}:{sip_video_codec:codec})}}];
                    document.body.append(card);
                }""", {"codec": codec, "capabilities": capabilities, "cameraIntent": camera_intent})
                assert await page.evaluate("card._els.destSelect.selectedOptions[0].textContent") == "Registered phone"
                # The explicit control supplies intent; discovery metadata does
                # not negotiate the PBX's final SDP or enable the camera.
                await page.get_by_role("button", name="Video call" if video_call else "Call", exact=True).click()
                await page.wait_for_function("rpcCalls.length === 1")
                request = await page.evaluate("rpcCalls[0]")
                assert request["service_data"]["send_video"] is expected, {
                    "codec": codec, "capabilities": capabilities,
                    "camera_intent": camera_intent, "video_call": video_call, "request": request,
                }
                assert request["service_data"]["receive_video"] is video_call
                # SIP accounts dial their public username while retaining the
                # readable name in the contact selector.
                assert request["service_data"]["destination"] == "registered-target"
                assert request["service_data"]["device_id"] == "browser-device"
                assert request["service_data"]["media_client_id"] == await page.evaluate("engine.mediaClientId")
            finally:
                await page.evaluate("async()=>{card.remove();await engine.close('test-complete')}")
                await page.close()

    asyncio.run(browser_session(check))


@pytest.mark.skipif(shutil.which("chromium") is None, reason="Chromium is unavailable")
@pytest.mark.parametrize("pickup", ["raw_state", "frontend_slot"])
@pytest.mark.timeout(45)
def test_popup_minicard_after_park_preserves_browser_media_ownership(pickup):
    """Presentation follows a resumed call; only the ordinary card acquires media."""
    from aiohttp import web

    class RetainedPickupPeer(SoftwareCallPeer):
        async def rpc(self, request):
            message = await request.json()
            if message.get("type") == "call_service" and message.get("service") == "call":
                data = message["service_data"]
                assert data["destination"] == "701" and data["media_client_id"]
                self.attempts.append(dict(data))
                self.owner = data["media_client_id"]
                self.state["call_id"] = "retained-call-A"
                await self.broadcast("in_call")
                return web.json_response(dict(self.state))
            return await super().rpc(request)

    peer = RetainedPickupPeer()

    async def check(context, base):
        page = await open_engine(context, base)
        page.set_default_timeout(5000)
        await page.evaluate(HARNESS)
        await page.wait_for_function("card._softphoneSnapshot?.endpoint_id === 'desk'")
        await page.evaluate("""async () => {
          card.remove();window.contextCount=0;
          const Context=window.AudioContext;
          window.AudioContext=class extends Context {constructor(...args){super(...args);contextCount++;}};
          class HA extends HTMLElement {
            constructor(){super();this.attachShadow({mode:'open'});this.updateComplete=Promise.resolve();}
            provideHass(element){element.hass=testHass;}
          }
          customElements.define('home-assistant',HA);
          window.root=document.createElement('home-assistant');document.body.append(root);
          root.addEventListener('show-dialog',async event=>{
            const {dialogTag,dialogImport,dialogParams}=event.detail;await dialogImport();
            let dialog=root.shadowRoot.querySelector(dialogTag);
            if(!dialog){dialog=document.createElement(dialogTag);root.provideHass(dialog);root.shadowRoot.append(dialog);}
            dialog.showDialog(dialogParams);
          });
          await import('/voip-stack/voip-stack-popup.js');
          const settings=document.createElement('voip-stack-popup-settings');
          document.body.append(settings);settings.hass=testHass;window.settings=settings;
        }""")
        await page.wait_for_function("settings._select.options.length===2")
        await page.locator("voip-stack-popup-settings select").select_option("desk-device")
        await page.get_by_label("Receive call popups on this device").check()
        await page.get_by_role("button", name="Save", exact=True).click()
        await wait_until(lambda: peer.phone_subscriptions() == 1)
        peer.state.update(call_id="retained-call-A", direction="incoming", caller="Retained peer")
        await peer.broadcast("ringing")
        await page.wait_for_function("!!__voipStackPopup.dialog?._dialog.open")
        await peer.broadcast("idle")
        await page.wait_for_function("__voipStackPopup.state?.state==='idle' && !__voipStackPopup.dialog?._dialog.open")
        baseline = await page.evaluate("({contexts:contextCount,mics:gumCalls.length})")
        try:
            if pickup == "raw_state":
                await peer.broadcast("in_call")
                await page.wait_for_function("__voipStackPopup.state?.state==='in_call'")
                assert await page.evaluate("engine.softphoneCallIdFor('desk')") == ""
                assert await page.evaluate("engine.callId") == ""
                assert await page.evaluate("({contexts:contextCount,mics:gumCalls.length})") == baseline
                assert not peer.connections
                assert await page.get_by_role("button", name="Reopen call", exact=True).is_visible()
            else:
                await page.evaluate("mountCard()")
                await page.wait_for_function("card._softphoneSnapshot?.state==='idle'")
                view = page.locator("body > voip-stack-card")
                await view.get_by_role("button", name="Keypad", exact=True).click()
                await view.get_by_role("textbox", name="Number, name or SIP URI").fill("701")
                await view.get_by_role("button", name="Call", exact=True).click()
                await page.wait_for_function("engine.callId==='retained-call-A' && engine._audioSession.ready")
                await wait_until(lambda: bool(peer.connections) and peer.connections[-1]["rx_nonzero"] >= 4)
                owner = await page.evaluate("engine.mediaClientId")
                assert peer.attempts[-1]["media_client_id"] == owner
                assert await page.evaluate("engine.softphoneCallIdFor('desk')") == "retained-call-A"
                acquired = await page.evaluate("({contexts:contextCount,mics:gumCalls.length})")
                await page.evaluate("card.remove();history.pushState({},'', '/map')")
                assert not await page.evaluate("!!__voipStackPopup.dialog?._dialog.open")
                await page.get_by_role("button", name="Reopen call", exact=True).click()
                await page.wait_for_function("!!__voipStackPopup.dialog?._dialog.open")
                assert await page.evaluate("engine.mediaClientId") == owner
                assert await page.evaluate("engine.callId") == "retained-call-A"
                assert await page.evaluate("({contexts:contextCount,mics:gumCalls.length})") == acquired
                assert len(peer.connections) == 1, "Reopening the popup replaced the existing media socket"
        finally:
            await peer.broadcast("idle")
            await page.wait_for_function("!engine.softphoneCallIdFor('desk') && !engine.callId")
            await wait_until(lambda: all(value["closed"] for value in peer.connections))
            assert await page.evaluate("__voipStackPopup._mini.hidden && !__voipStackPopup.dialog?._dialog.open")
            assert await page.evaluate("__voipStackPopup._timer == null")

    asyncio.run(browser_session(check, peer))
