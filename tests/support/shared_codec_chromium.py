"""External Playwright driver for one isolated native HA fixture."""

import base64
import json
from pathlib import Path
import shutil
import sys
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

base, wav, output, user_id = sys.argv[1:]
assert urlsplit(base).hostname in {"127.0.0.1", "localhost"}
output = Path(output)
with sync_playwright() as runner:
    browser = runner.chromium.launch(
        executable_path=shutil.which("chromium"),
        headless=True,
        args=[
            "--use-fake-ui-for-media-stream",
            "--use-fake-device-for-media-stream",
            "--use-file-for-fake-audio-capture=" + wav,
            "--autoplay-policy=no-user-gesture-required",
            "--mute-audio",
        ],
    )
    context = browser.new_context(
        permissions=["microphone"], viewport={"width": 480, "height": 900}
    )
    page = context.new_page()
    errors = []
    messages = []
    formats = {}
    audio = {}
    media = set()
    calls = {}
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on(
        "console",
        lambda message: (
            print("BROWSER " + message.text, file=sys.stderr, flush=True)
            if message.type in {"warning", "error"}
            else None
        ),
    )
    cdp = context.new_cdp_session(page)
    cdp.send("Network.enable")
    cdp.on(
        "Network.webSocketCreated",
        lambda event: (
            media.add(event["requestId"])
            if "/api/voip_stack/ws?" in event["url"]
            else None
        ),
    )

    def frame(event, direction):
        request = event["requestId"]
        if request not in media:
            return
        data = event["response"]
        if data.get("opcode") == 1 and direction == "rx":
            value = json.loads(data.get("payloadData") or "{}")
            if value.get("tx_format"):
                formats[request] = (value["tx_format"], value["rx_format"])
                calls[request] = value.get("call_id")
                messages.append(value)
        if data.get("opcode") == 2:
            value = base64.b64decode(data["payloadData"])
            assert value[0] == 1
            audio.setdefault((request, direction), bytearray()).extend(value[1:])

    cdp.on("Network.webSocketFrameReceived", lambda event: frame(event, "rx"))
    cdp.on("Network.webSocketFrameSent", lambda event: frame(event, "tx"))
    try:
        page.goto(base)
        page.evaluate(
            """async (userId) => {
          const ws=new WebSocket(location.origin.replace('http','ws')+'/control/ws');
          await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject;});
          let nextId=1000;const pending=new Map(),subscriptions=new Map();
          window.controlEvents=[];
          ws.onmessage=event=>{const message=JSON.parse(event.data);window.controlEvents.push(message);
            if(message.type==='result'&&pending.has(message.id)){
              const {resolve,reject}=pending.get(message.id);pending.delete(message.id);
              message.success?resolve(message.result):reject(Error(JSON.stringify(message.error)));
            }else if(message.type==='event')subscriptions.get(message.id)?.(message.event);};
          const call=(command,callback)=>new Promise((resolve,reject)=>{
            const id=++nextId;if(callback)subscriptions.set(id,callback);
            pending.set(id,{resolve,reject});ws.send(JSON.stringify({id,...command}));});
          const connection={connected:true,sendMessagePromise:command=>call(command),
            subscribeMessage:async(callback,command)=>{
              const id=nextId+1;await call(command,callback);
              return()=>{subscriptions.delete(id);void call({type:'unsubscribe_events',subscription:id});};},
            subscribeEvents:async(callback,event_type)=>connection.subscribeMessage(callback,{type:'subscribe_events',...(event_type?{event_type}:{})}),
            addEventListener(){},removeEventListener(){}};
          const hass=window.hass={language:'en',locale:{language:'en'},user:{id:userId,is_admin:true},states:{},
            config:{components:['voip_stack'],location_name:'Isolated shared account'},connection,
            callWS:command=>call(command),callService:(domain,service,service_data)=>call({type:'call_service',domain,service,service_data}),localize:key=>key};
          await import('/voip-stack/voip-stack-card.js');
          const card=window.card=document.createElement('voip-stack-card');
          card.setConfig({mode:'ha_softphone',device_id:'browser-casa',name:'Home reception'});
          card.hass=hass;document.getElementById('card').append(card);
          window.engine=(await import('/voip-stack/voip-stack-engine.js?v=dev')).voipStackEngine;
        }""",
            user_id,
        )
        print(json.dumps({"result": "ready"}), flush=True)
        for line in sys.stdin:
            command = json.loads(line)
            action = command["action"]
            try:
                if action == "wait-answer":
                    page.get_by_role("button", name="Answer", exact=True).wait_for(
                        timeout=8000
                    )
                    result = True
                elif action == "answer":
                    # The real button deliberately pulses. A user's center click
                    # does not require Playwright's animation-stability gate.
                    box = page.get_by_role(
                        "button", name="Answer", exact=True
                    ).bounding_box()
                    page.mouse.click(
                        box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                    )
                    result = True
                elif action == "hangup":
                    box = page.get_by_role(
                        "button", name="Hang up call", exact=True
                    ).bounding_box()
                    page.mouse.click(
                        box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                    )
                    result = True
                elif action == "screenshot":
                    page.screenshot(path=command["path"])
                    result = True
                elif action == "stats":
                    page.wait_for_timeout(10)
                    files = []
                    for (identity, direction), data in audio.items():
                        path = output / (
                            identity.replace(".", "_") + "-" + direction + ".pcm"
                        )
                        path.write_bytes(data)
                        files.append(
                            {
                                "request_id": identity,
                                "direction": direction,
                                "file": str(path),
                                "bytes": len(data),
                            }
                        )
                    result = {
                        "formats": formats,
                        "calls": calls,
                        "streams": files,
                        "messages": messages,
                        "errors": errors,
                        "media_connections": len(media),
                        "control_events": page.evaluate(
                            "()=>controlEvents.filter(message=>message.type==='event'||message.success===false)"
                        ),
                        "card": page.evaluate(
                            "()=>({state:card._softphoneSnapshot?.state,caller:card._softphoneSnapshot?.caller,callee:card._softphoneSnapshot?.callee,audioReady:engine._audioSession.ready,contextState:engine._audioSession.audioContext?.state,tracks:engine._audioSession.mediaStream?.getTracks().map(t=>t.readyState)||[],stats:engine._stats})"
                        ),
                    }
                elif action == "close":
                    page.evaluate("()=>card.remove()")
                    result = True
                else:
                    raise ValueError(action)
                print(json.dumps({"result": result}), flush=True)
                if action == "close":
                    break
            except Exception as error:
                print(json.dumps({"error": str(error)}), flush=True)
    except Exception as error:
        print(json.dumps({"error": str(error)}), flush=True)
        raise
    finally:
        browser.close()
