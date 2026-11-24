"""The shared production video surface fits its actual card or dashboard slot."""

import json

import numpy as np
from PIL import Image
import pytest

from .test_frontend_popup_browser import BOOTSTRAP, popup_page as popup_page

pytestmark = [pytest.mark.js_runtime, pytest.mark.browser_real]


@pytest.fixture
def video_page(popup_page):
    # Match the standards-mode HA document, including percentage auto heights.
    popup_page.route("http://phone.test/video-geometry", lambda route: route.fulfill(
        content_type="text/html", body="<!doctype html><html><body></body></html>"))
    popup_page.goto("http://phone.test/video-geometry")
    popup_page.evaluate(BOOTSTRAP)
    assert popup_page.evaluate("document.compatMode") == "CSS1Compat"
    return popup_page


SHOW_VIDEO = r"""async ({mode,width,height,frameWidth,frameHeight,padded})=>{
 document.body.style.cssText='margin:0;background:#000;font-family:Arial;color:white';
 phone={...phone,kind:'sip_account',endpoint_id:'browser:desk',name:'Desk',available:true,availability:'available'};
 current={...current,state:'in_call',call_id:'pixel-call',endpoint_id:phone.endpoint_id,
  direction:'outgoing',role:'caller',caller:'Desk',callee:'Pattern',peer_name:'Pattern',
  can_control:true,can_attach_media:false,can_answer:false,parking:{enabled:true}};
 const ownerPhone={...phone,state:'in_call',calls:[{id:'pixel-call',state:'confirmed',direction:'outgoing',peer_name:'Pattern'}]};
 const send=hass.connection.sendMessagePromise;
 hass.connection.sendMessagePromise=async request=>request.type==='voip_stack/resolve_device'?phone:send(request);
 hass.connection.subscribeMessage=async(callback,request)=>{
   const item={callback,request,active:true};streams.push(item);
   queueMicrotask(()=>callback(request.type==='voip_stack/subscribe_phone_states'
    ?{kind:'snapshot',revision:1,phones:[ownerPhone]}:current));
   return()=>{item.active=false;};
 };
 hass.states['sensor.voip_phonebook']={state:'ready',attributes:{roster_json:'[]'}};
 await import('/voip-stack-card.js');
 const holder=window.holder=document.createElement('div');
 holder.style.cssText=`position:relative;margin:20px;width:${width}px;height:${height === null ? "auto" : `${height}px`};overflow:hidden;background:#000`;
 document.body.append(holder);
 if(mode==='dashboard'){
   await import('/voip-stack-dashboard-card.js');
   const dashboard=window.dashboard=document.createElement('voip-stack-dashboard-card');
   dashboard.style.cssText='display:block;width:100%;height:100%';
   dashboard.setConfig({device_id:phone.device_id});holder.append(dashboard);root.provideHass(dashboard);
   for(let i=0;i<40&&!dashboard._phoneView?._softphoneSnapshot?.call_id;i++)await new Promise(r=>setTimeout(r,10));
   window.card=dashboard._phoneView;
 }else{
   window.card=document.createElement('voip-stack-card');
   card.style.cssText='display:block;width:100%;height:100%';
   card.setConfig({mode:'ha_softphone',device_id:phone.device_id,name:'Desk'});
   holder.append(card);root.provideHass(card);
   for(let i=0;i<40&&!card._softphoneSnapshot?.call_id;i++)await new Promise(r=>setTimeout(r,10));
 }
 const {voipStackEngine:engine}=await import('/voip-stack-engine.js?v=dev');
 window.engine=engine;engine._endpointId=phone.endpoint_id;engine._deviceId=phone.device_id;engine._callId='pixel-call';
 engine.claimSoftphoneSession('pixel-call',phone.endpoint_id);engine._state='ACTIVE';
 const video=await engine._loadVideo();video._callId='pixel-call';video._active=true;video._canReceive=true;
 card._render();
 const source=document.createElement('canvas');source.width=frameWidth;source.height=frameHeight;
 const ctx=source.getContext('2d');ctx.fillStyle='#000';ctx.fillRect(0,0,frameWidth,frameHeight);
 const top=padded?frameHeight/4:0;
 ctx.fillStyle='#00c8ff';ctx.fillRect(0,top,frameWidth,frameHeight-top);
 ctx.fillStyle='#ed2040';ctx.fillRect(0,top,frameWidth,5);
 ctx.fillStyle='#2050ff';ctx.fillRect(0,frameHeight-5,frameWidth,5);
 ctx.fillStyle='#20ec50';ctx.fillRect(0,top,5,frameHeight-top);
 ctx.fillStyle='#ffe020';ctx.fillRect(frameWidth-5,top,5,frameHeight-top);
 ctx.strokeStyle='#fff';ctx.lineWidth=3;ctx.beginPath();ctx.arc(frameWidth/2,(top+frameHeight)/2,Math.min(frameWidth,frameHeight-top)/5,0,2*Math.PI);ctx.stroke();
 const bitmap=await createImageBitmap(source);
 video._receiver._queueDecodedFrame({bitmap,displayWidth:frameWidth,displayHeight:frameHeight,timestamp:1000,close:()=>bitmap.close()});
 return {mode,width,height,frameWidth,frameHeight,padded};
}"""

GEOMETRY = r"""() => {
 const rect=element=>element.getBoundingClientRect().toJSON();
 const e=card._els,style=getComputedStyle(e.videoCanvas);
 return {slot:rect(holder),host:rect(card),card:rect(e.card),canvas:rect(e.videoCanvas),
   objectFit:style.objectFit,objectPosition:style.objectPosition,
   intrinsic:{width:e.videoCanvas.width,height:e.videoCanvas.height},hangup:rect(e.hangupBtn),
   toolbar:rect(e.hangupBtn.parentElement),
   actions:[...e.runtimeControls.querySelectorAll('button')].filter(b=>!b.hidden&&b.getBoundingClientRect().width)
     .map(b=>({label:b.getAttribute('aria-label'),rect:rect(b)})),mics,services};
}"""

def show_video(page, presenter, slot, frame, *, padded=False):
    page.set_viewport_size({"width": 720, "height": 720})
    page.evaluate(SHOW_VIDEO, {"mode": presenter, "width": slot[0], "height": slot[1],
        "frameWidth": frame[0], "frameHeight": frame[1], "padded": padded})
    page.wait_for_function("card?._els.card.classList.contains('video-active') && engine._video.visible")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    return page.evaluate(GEOMETRY)


def media_pixels(page, path):
    page.locator("voip-stack-card canvas.video-canvas").screenshot(path=str(path),
        style=".video-shade,.card > :not(.video-canvas){visibility:hidden!important}")
    pixels = np.asarray(Image.open(path).convert("RGB"))
    # Saturated frame colors exclude the native HA card's grey border.
    colored = (pixels.max(axis=2).astype(int) - pixels.min(axis=2).astype(int) > 85) & (pixels.max(axis=2) > 100)
    ys, xs = np.where(colored)
    assert len(xs), "The production receiver did not paint its decoded frame"
    return pixels, {"x": int(xs.min()), "y": int(ys.min()),
        "width": int(xs.max() - xs.min() + 1), "height": int(ys.max() - ys.min() + 1)}


@pytest.mark.parametrize("presenter", ["card", "dashboard"])
@pytest.mark.parametrize("slot", [(240, 240), (500, 220), (500, 440)])
@pytest.mark.parametrize("frame", [(640, 360), (320, 240), (240, 425), (240, 240)])
def test_decoded_video_centers_without_cropping_inside_its_visible_slot(video_page, tmp_path, presenter, slot, frame):
    page = video_page
    geometry = show_video(page, presenter, slot, frame)
    page.screenshot(path=str(tmp_path / "controls.png"))
    pixels, content = media_pixels(page, tmp_path / "media.png")
    (tmp_path / "geometry.json").write_text(json.dumps({"geometry": geometry, "content": content}, indent=2))
    viewport, canvas = geometry["slot"], geometry["canvas"]
    assert geometry["card"]["height"] <= viewport["height"] + 1
    assert canvas["right"] <= viewport["right"] + 1 and canvas["bottom"] <= viewport["bottom"] + 1
    assert geometry["objectFit"] == "contain" and geometry["objectPosition"] == "50% 50%"
    assert geometry["intrinsic"] == {"width": frame[0], "height": frame[1]}
    assert abs(canvas["x"] + content["x"] + content["width"] / 2 - (viewport["x"] + viewport["width"] / 2)) <= 1.5
    # The actual toolbar occludes the bottom of the card. Center the complete
    # decoded frame in the region above it, including on narrow HA slots.
    toolbar = geometry["toolbar"]
    assert abs(canvas["bottom"] - toolbar["top"]) <= 1.5
    frame_top = canvas["y"] + content["y"]
    frame_bottom = frame_top + content["height"]
    assert abs((frame_top - canvas["top"]) - (toolbar["top"] - frame_bottom)) <= 2
    assert abs((frame_top + frame_bottom) / 2 - (canvas["top"] + toolbar["top"]) / 2) <= 1.5
    scale = min(canvas["width"] / frame[0], canvas["height"] / frame[1])
    assert abs(content["width"] - frame[0] * scale) <= 2
    assert abs(content["height"] - frame[1] * scale) <= 2
    # The white source circle is an independent check against aspect stretching.
    center = pixels[int(content["y"] + content["height"] * .2):int(content["y"] + content["height"] * .8),
                    int(content["x"] + content["width"] * .2):int(content["x"] + content["width"] * .8)]
    ys, xs = np.where(center.min(axis=2) > 200)
    assert len(xs) and abs((xs.max() - xs.min()) - (ys.max() - ys.min())) <= 2
    hangup = geometry["hangup"]
    assert hangup["width"] == 48 and hangup["left"] >= viewport["left"]
    assert hangup["bottom"] <= viewport["bottom"] + 1
    assert all(action["rect"]["left"] >= hangup["right"] for action in geometry["actions"])
    assert geometry["mics"] == 0


@pytest.mark.parametrize("presenter", ["card", "dashboard"])
def test_video_auto_height_keeps_the_existing_280px_fallback(video_page, presenter):
    geometry = show_video(video_page, presenter, (500, None), (320, 240))
    assert 280 <= geometry["card"]["height"] <= 282
    assert geometry["slot"]["height"] >= geometry["card"]["height"]


@pytest.mark.parametrize("presenter", ["card", "dashboard"])
def test_encoded_black_padding_is_preserved_instead_of_cropped_to_fake_centering(video_page, tmp_path, presenter):
    page = video_page
    geometry = show_video(page, presenter, (500, 440), (320, 240), padded=True)
    pixels, content = media_pixels(page, tmp_path / "encoded-padding.png")
    source = page.evaluate("() => [...card._els.videoCanvas.getContext('2d').getImageData(160,20,1,1).data]")
    assert source[:3] == [0, 0, 0]
    assert geometry["intrinsic"] == {"width": 320, "height": 240}
    assert content["y"] + content["height"] / 2 > pixels.shape[0] / 2 + 40


@pytest.mark.parametrize("presenter", ["card", "dashboard"])
@pytest.mark.parametrize("slot", [(320, 216), (320, 240), (500, 440)])
def test_small_video_slot_keeps_only_the_left_handset_as_hangup(video_page, presenter, slot, tmp_path):
    page = video_page
    page.evaluate("current={...current,send_video:true,video_camera_send_enabled:true}")
    geometry = show_video(page, presenter, slot, (640, 360))
    page.evaluate("""() => {
      window.hangups=0; window.parks=[]; card._hangup=()=>hangups++;
      const send=hass.connection.sendMessagePromise;
      hass.connection.sendMessagePromise=async request=>{
        if(request.type==='call_service'&&request.domain==='voip_stack'&&request.service==='park'){
          parks.push(request);return {};
        }
        return send(request);
      };
    }""")
    phone = page.locator("voip-stack-card")
    controls = page.locator("voip-stack-dashboard-card") if presenter == "dashboard" else phone
    # The informational labels ignore pointer events; exercise actual screen
    # clicks rather than waiting for the labels themselves to become targets.
    for label in (phone.locator(".hangup-peer"), phone.locator(".hangup-duration")):
        box = label.bounding_box()
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        assert page.evaluate("hangups") == 0
    for index, label in enumerate(("Options", "Options", "Keypad", "Contacts", "Park call")):
        controls.get_by_role("button", name=label, exact=True).click()
        if label == "Options":
            assert page.evaluate("card._settingsOpen") is (index == 0)
            if index == 0:
                checkbox = controls.get_by_role("checkbox", name="Send Camera", exact=True)
                assert checkbox.is_checked()
                def inspect():
                    return page.evaluate("""() => {
                      const rect=e=>e?.getBoundingClientRect().toJSON();
                      const deep=(x,y)=>{let e=document.elementFromPoint(x,y),path=[];while(e){path.push({tag:e.localName,id:e.id,class:e.className});const next=e.shadowRoot?.elementFromPoint(x,y);if(!next||next===e)break;e=next;}return path;};
                      const e=card._els,b=e.videoCameraCheckbox,r=b.getBoundingClientRect(),label=b.labels?.[0],l=label?.getBoundingClientRect();
                      return {slot:rect(holder),card:rect(e.card),settings:rect(e.settingsPanel),checkbox:rect(b),label:rect(label),checked:b.checked,
                        checkboxHit:deep(r.x+r.width/2,r.y+r.height/2),labelHit:l?deep(l.x+l.width/2,l.y+l.height/2):[],
                        dashboard:typeof dashboard==='undefined'?null:{header:rect(dashboard.shadowRoot.querySelector('.dashboard-header')),phoneHost:rect(dashboard._els.phoneHost),headerHeight:dashboard.shadowRoot.querySelector('.dashboard-header').clientHeight,phoneHeight:dashboard._els.phoneHost.clientHeight,scrollHeight:dashboard._els.card.scrollHeight,clientHeight:dashboard._els.card.clientHeight},
                        cardDOM:card.shadowRoot.innerHTML,dashboardDOM:typeof dashboard==='undefined'?null:dashboard.shadowRoot.innerHTML};
                    }""")
                before = inspect()
                print(json.dumps({k:v for k,v in before.items() if not k.endswith('DOM')}))
                (tmp_path / 'options-before.json').write_text(json.dumps(before, indent=2))
                page.screenshot(path=str(tmp_path / 'options-before.png'))
                # Wheel over this surface as a user would. A control visible
                # only through a clipped strip is not a reachable option row.
                area = before["slot"]
                page.mouse.move(area["x"] + area["width"] / 2, area["bottom"] - 24)
                for _ in range(8):
                    state = inspect()
                    box, label_box = state["checkbox"], state["label"]
                    if all(area["top"] <= item["top"] and item["bottom"] <= area["bottom"]
                           for item in (box, label_box)):
                        break
                    page.mouse.wheel(0, 100)
                    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
                try:
                    checkbox.uncheck(timeout=1000)
                    state = inspect()
                    assert all(area["top"] <= item["top"] and item["bottom"] <= area["bottom"]
                               for item in (state["checkbox"], state["label"]))
                finally:
                    (tmp_path / 'options-after.json').write_text(json.dumps(inspect(), indent=2))
                    page.screenshot(path=str(tmp_path / 'options-after.png'))
                assert not checkbox.is_checked()
                assert page.evaluate("services.filter(item=>item.service==='set_send_video'&&item.data?.send_video===false).length") == 1
        if label in ("Keypad", "Contacts"):
            assert page.evaluate("card._keypadOpen()") is (label == "Keypad")
        assert page.evaluate("hangups") == 0
        assert page.evaluate("services.filter(item=>item.service==='hangup').length") == 0
    assert page.evaluate("parks.length") == 1
    assert page.evaluate("parks[0].service_data") == {"device_id": "desk-device"}
    phone.get_by_role("button", name="Hang up call", exact=True).click()
    assert page.evaluate("hangups") == 1
    assert geometry["hangup"]["width"] == 48


@pytest.mark.parametrize("presenter", ["card", "dashboard"])
def test_video_region_follows_measured_toolbar_and_slot_resize(video_page, tmp_path, presenter):
    page = video_page
    show_video(page, presenter, (500, 440), (640, 360))
    # A theme can change the actual bar height without resizing the card.
    page.evaluate("card._els.hangupBtn.parentElement.style.height='73px'")
    page.wait_for_function("Math.abs(card._els.videoCanvas.getBoundingClientRect().bottom - card._els.hangupBtn.parentElement.getBoundingClientRect().top)<1.5")
    for width, height in ((500, 440), (240, 240), (500, 220)):
        page.evaluate("([width,height])=>{holder.style.width=`${width}px`;holder.style.height=`${height}px`}", [width, height])
        page.wait_for_function("([width,height])=>Math.abs(card.getBoundingClientRect().width-width)<3 && Math.abs(card.getBoundingClientRect().height-height)<3", arg=[width, height])
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        geometry = page.evaluate(GEOMETRY)
        _, content = media_pixels(page, tmp_path / f"resized-{width}x{height}.png")
        canvas, toolbar = geometry["canvas"], geometry["toolbar"]
        assert toolbar["height"] == 73
        assert abs(canvas["bottom"] - toolbar["top"]) <= 1.5
        assert abs(canvas["top"] + content["y"] + content["height"] / 2 - (canvas["top"] + toolbar["top"]) / 2) <= 1.5
        assert geometry["hangup"]["width"] == 48
        assert geometry["card"]["height"] <= geometry["slot"]["height"] + 1
        assert geometry["mics"] == 0
