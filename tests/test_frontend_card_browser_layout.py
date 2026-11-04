"""Browser geometry checks for the production call controls."""

from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import shutil
from threading import Thread
from urllib.parse import urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "custom_components/voip_stack/frontend"
pytestmark = [pytest.mark.js_runtime, pytest.mark.browser_real]


@pytest.mark.skipif(shutil.which("chromium") is None, reason="Chromium is unavailable")
@pytest.mark.parametrize("video", [False, True], ids=["compact", "video-toolbar"])
def test_received_pcm_visibly_changes_hangup_without_moving_controls(video, tmp_path):
    """Exercise RX PCM, native playback/analyser and visible geometry together."""
    playwright = pytest.importorskip("playwright.sync_api")
    source_bytes = {path.name: path.read_bytes() for path in FRONTEND.glob("*.js")}

    class Assets(BaseHTTPRequestHandler):
        def do_GET(self):
            name = Path(urlsplit(self.path).path).name
            if name in source_bytes:
                body, content_type = source_bytes[name], "text/javascript"
            elif self.path == "/":
                body, content_type = b"<html><body></body></html>", "text/html"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    # Worklet module fetches use native Chromium networking, unlike the page
    # route fixtures used by presentation-only tests. Bind only loopback.
    server = ThreadingHTTPServer(("127.0.0.1", 0), Assets)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with playwright.sync_playwright() as runner:
            browser = runner.chromium.launch(
                executable_path=shutil.which("chromium"), headless=True,
                args=["--autoplay-policy=no-user-gesture-required"],
            )
            try:
                page = browser.new_page(viewport={"width": 420, "height": 700})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(f"http://127.0.0.1:{server.server_port}/")
                page.evaluate("""async video => {
                  // Only HA I/O and its icon boundary are controlled. No
                  // microphone, call, websocket or injected audio-level event.
                  Object.defineProperty(navigator, 'mediaDevices', {value: {
                    enumerateDevices: async () => [], addEventListener() {},
                    getUserMedia: async () => { throw Error('Unexpected microphone request'); }
                  }});
                  customElements.define('ha-icon', class extends HTMLElement {
                    constructor() { super(); this.attachShadow({mode:'open'}).innerHTML =
                      '<style>:host{display:inline-flex;width:var(--mdc-icon-size,24px);height:var(--mdc-icon-size,24px)}</style><svg viewBox="0 0 24 24"><path fill="currentColor" d="M3 14v-3c5-5 13-5 18 0v3h-5v-3c-3-1-5-1-8 0v3z"/></svg>'; }
                  });
                  await import('/voip-stack-card.js');
                  const e = window.engine = __voipStackEngine;
                  e._state = 'ACTIVE'; e._callId = 'pcm-call';
                  e._endpointId = 'desk'; e._deviceId = 'desk-device';
                  // Native graph runs silently, without opening a speaker.
                  e._createAudioContext = () => new AudioContext({sinkId:{type:'none'}});
                  await e._setupAudio({audio_mode:'full_duplex'}, {
                    tx_format:'16000:s16le:1:10', rx_format:'16000:s16le:1:10', audio_direction:'recvonly'
                  });
                  if (video) e._video = {callId:'pcm-call', visible:true, active:false, setCanvas(){}};
                  window.audioEvents = [];
                  e.addEventListener('audio-level', event => audioEvents.push(event.detail));
                  const c = window.card = document.createElement('voip-stack-card');
                  c.style.cssText='width:360px;height:360px;display:block';
                  c.config={mode:'ha_softphone',device_id:'desk-device',name:'Desk'};
                  c._activeDeviceInfo={device_id:'desk-device',endpoint_id:'desk',name:'Desk'};
                  c._softphoneStateLoaded=true;
                  c._softphoneSnapshot={state:'in_call',enabled:true,available:true,
                    endpoint_id:'desk',call_id:'pcm-call',device_id:'desk-device',media_owner:'other'};
                  c._hass={language:'en',user:{is_admin:true},config:{location_name:'Home'},states:{}};
                  document.body.append(c);
                  window.geometry = () => {
                    const button=c._els.hangupBtn;
                    const visual=button.querySelector(video ? '.hangup-icon' : '.hangup-pulse');
                    const samples=new Float32Array(e._playbackAnalyser.fftSize);
                    e._playbackAnalyser.getFloatTimeDomainData(samples);
                    return {button:button.getBoundingClientRect().toJSON(),
                      visual:visual.getBoundingClientRect().toJSON(), transform:getComputedStyle(visual).transform,
                      level:e.audioLevel,rms:Math.sqrt(samples.reduce((s,v)=>s+v*v,0)/samples.length),
                      video:c._els.card.classList.contains('video-active'),
                      reducedMotion:matchMedia('(prefers-reduced-motion:reduce)').matches};
                  };
                  window.feed = amplitude => {
                    clearInterval(window.feeder); let phase=0;
                    const write=()=>{
                      const data=new ArrayBuffer(321), view=new DataView(data); view.setUint8(0,1);
                      for(let i=0;i<160;i++)view.setInt16(1+2*i,
                        Math.round(amplitude*32767*Math.sin(phase++*2*Math.PI*440/16000)),true);
                      e._handleMessage({data});
                    };
                    for(let i=0;i<8;i++)write();
                    window.feeder=setInterval(write,10);
                  };
                  feed(0);
                }""", video)
                page.wait_for_timeout(200)
                quiet = page.evaluate("geometry()")
                page.screenshot(path=str(tmp_path / "quiet.png"))
                page.evaluate("feed(0.01)")  # Quiet PCM, about -43 dBFS RMS.
                page.wait_for_function("engine.audioLevel > 0.17")
                page.wait_for_timeout(160)
                speech = page.evaluate("geometry()")
                page.screenshot(path=str(tmp_path / "quiet-speech.png"))
                page.evaluate("feed(0.5)")
                page.wait_for_function("engine.audioLevel > 0.98")
                page.wait_for_timeout(160)
                loud = page.evaluate("geometry()")
                page.emulate_media(reduced_motion="reduce")
                # Emulation and style invalidation cross the browser process
                # boundary. Observe the actual preference and final CSS state.
                page.wait_for_function("geometry().reducedMotion && geometry().transform === 'none'")
                reduced = page.evaluate("geometry()")
                page.screenshot(path=str(tmp_path / "reduced-motion.png"))
                page.emulate_media(reduced_motion="no-preference")
                page.evaluate("feed(0)")
                page.wait_for_function("engine.audioLevel === 0")
                page.wait_for_timeout(160)
                silent = page.evaluate("geometry()")
                events = page.evaluate("audioEvents")
                stats = page.evaluate("engine.stats")
                page.evaluate("async()=>{clearInterval(feeder);card.remove();await engine._cleanupAudio('test-end');}")
                assert page.evaluate("engine._audioLevelFrame === 0 && engine._playbackAnalyser === null")
                receipt = {"browser": browser.version, "video": video, "quiet": quiet,
                           "speech": speech, "loud": loud, "reduced": reduced,
                           "silent": silent, "events": events, "stats": stats,
                           "source_sha256": {name: hashlib.sha256(source_bytes[name]).hexdigest()
                                             for name in ("voip-stack-engine.js", "voip-stack-card.js",
                                                          "voip-stack-card-view.js", "voip-stack-playback-processor.js")}}
                (tmp_path / "pcm-geometry.json").write_text(json.dumps(receipt, indent=2))
                assert not errors
                assert quiet["video"] is video
                assert quiet["level"] == 0
                assert .005 < speech["rms"] < .009
                assert stats["received"] > 40
                assert all(event["endpoint_id"] == "desk" and event["call_id"] == "pcm-call" for event in events)
                # A real waveform must produce a useful visible change at low
                # level, not merely a nonzero CSS value after a forged event.
                assert speech["visual"]["width"] - quiet["visual"]["width"] >= (2 if video else 6)
                for sample in (speech, loud, reduced, silent):
                    for field in ("x", "y", "width", "height"):
                        assert abs(sample["button"][field] - quiet["button"][field]) < .1
                    assert sample["button"]["width"] >= 44 and sample["button"]["height"] >= 44
                    for low, high in (("left", "right"), ("top", "bottom")):
                        assert sample["visual"][low] >= sample["button"][low] - .5
                        assert sample["visual"][high] <= sample["button"][high] + .5
                assert reduced["transform"] == "none"
                assert silent["level"] == 0
                assert abs(silent["visual"]["width"] - quiet["visual"]["width"]) < .1
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.skipif(shutil.which("chromium") is None, reason="Chromium is unavailable")
@pytest.mark.parametrize("mode", ["ha_softphone", "esp_mirror", "ha_transfer"])
@pytest.mark.parametrize("initial_keypad", [False, True])
def test_contacts_keypad_navigation_leaves_options(mode, initial_keypad, tmp_path):
    """Issue 142: a changed toggle label must match the visible panel."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(executable_path=shutil.which("chromium"), headless=True)
        try:
            page = browser.new_page(viewport={"width": 420, "height": 700})

            def serve(route):
                name = route.request.url.split("/")[-1].split("?")[0]
                path = FRONTEND / name
                route.fulfill(
                    content_type="text/javascript" if name.endswith(".js") else "text/html",
                    body=path.read_text() if name.endswith(".js") and path.is_file()
                    else "<html><body></body></html>",
                )

            page.route("http://card.test/**", serve)
            page.goto("http://card.test/")
            page.evaluate("""async ({mode}) => {
                await import('/voip-stack-card.js');
                const transfer=mode==='ha_transfer';
                const card=document.createElement('voip-stack-card');window.card=card;
                card.style.cssText='display:block;width:360px';
                card.config={mode:transfer?'ha_softphone':mode,device_id:'desk-device',name:'Desk'};
                card._activeDeviceInfo={device_id:'desk-device',endpoint_id:'desk',name:'Desk'};
                card._softphoneSnapshot={state:transfer?'in_call':'idle',enabled:true,
                    call_id:transfer?'current-call':'',device_id:'desk-device'};
                if(transfer)card._transferDraft={callId:'current-call',keypad:false,manual:''};
                card._startCallService='call';
                card._voipStateEntityId='sensor.desk_state';
                card._destinationEntityId='sensor.desk_destination';
                card._hass={language:'en',user:{is_admin:true},config:{location_name:'Home'},states:{
                    'sensor.desk_state':{state:'idle'},
                    'sensor.desk_destination':{state:'Kitchen'},
                }};
                document.body.append(card);
            }""", {"mode": mode})
            if initial_keypad:
                page.get_by_role("button", name="Keypad", exact=True).click()
            page.get_by_role("button", name="Options", exact=True).click()
            assert page.evaluate("card._settingsOpen") is True
            page.get_by_role("button", name="Contacts" if initial_keypad else "Keypad", exact=True).click()
            page.screenshot(path=str(tmp_path / "returned-from-options.png"))
            assert page.evaluate("card._settingsOpen") is False
            assert page.evaluate("card._els.card.classList.contains('settings-open')") is False
            visible = page.evaluate("""initial => {
                const element=initial ? card._els.destRow : card._els.keypadPanel;
                return !element.hidden && getComputedStyle(element).display !== 'none'
                    && element.getBoundingClientRect().height > 0;
            }""", initial_keypad)
            assert visible, "Selected contacts/keypad panel remains hidden by Options"
            page.evaluate("card.remove()")
        finally:
            browser.close()


@pytest.mark.skipif(shutil.which("chromium") is None, reason="Chromium is unavailable")
@pytest.mark.parametrize("width", [280, 360, 600])
@pytest.mark.parametrize("park_enabled", [False, True])
@pytest.mark.parametrize("keypad_open", [False, True])
@pytest.mark.parametrize("height", [280, 360])
def test_video_controls_do_not_overlap_timer_or_each_other(
    width, park_enabled, keypad_open, height, tmp_path
):
    """All actions share the bottom call bar without covering the timer or video."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(
            executable_path=shutil.which("chromium"), headless=True
        )
        try:
            page = browser.new_page(
                viewport={"width": width + 40, "height": 500}, has_touch=True
            )

            def serve(route):
                name = route.request.url.split("/")[-1].split("?")[0]
                path = FRONTEND / name
                if name.endswith(".js") and path.is_file():
                    route.fulfill(content_type="text/javascript", body=path.read_text())
                else:
                    route.fulfill(
                        content_type="text/html", body="<html><body></body></html>"
                    )

            page.route("http://card.test/**", serve)
            page.goto("http://card.test/")
            page.evaluate(
                """async ({width, park, keypad, height}) => {
                document.body.style.cssText='margin:10px;font-family:Arial';
                const host=document.createElement('div');
                host.style.cssText=`width:${width}px;height:${height}px`;
                document.body.append(host);
                await import('/voip-stack-card.js');
                const prototype=customElements.get('voip-stack-card').prototype;
                const owner={shadowRoot:host.attachShadow({mode:'open'}),
                    _attachEventHandlers(){
                        this._els.keypadBtn.onclick=()=>{
                            this._els.keypadPanel.hidden=!this._els.keypadPanel.hidden;
                        };
                    },
                    _measureLayout(){prototype._measureLayout.call(this);},
                    _layoutPhoneActivity(){prototype._layoutPhoneActivity.call(this);},
                    _observeLayout(){prototype._observeLayout.call(this);}};
                owner._resizeObserver=new ResizeObserver(()=>owner._measureLayout());
                const {buildMainCardSkeleton}=await import('/voip-stack-card-view.js');
                buildMainCardSkeleton.call(owner,'layout-test');
                const e=owner._els; window.elements=e;
                e.card.classList.add('video-active','keypad-available');
                for(const key of ['callBtn','answerBtn','declineBtn','placeholderBtn']) e[key].hidden=true;
                e.hangupBtn.hidden=false;
                e.hangupPeer.textContent='Office phone with a long display name';
                e.hangupDuration.textContent='01:23:45';
                e.parkBtn.hidden=!park;
                e.parkBtn.querySelector('.settings-label').textContent='Anruf parken';
                e.recordBtn.hidden=true;
                e.transferBtn.hidden=false;
                e.voicemailBtn.hidden=true;
                e.keypadPanel.hidden=true;e.keypadInput.hidden=true;
                // HA supplies these icon boxes; the fixture preserves their sizes.
                for(const icon of owner.shadowRoot.querySelectorAll('ha-icon'))
                    icon.style.cssText='display:inline-block;width:24px;height:24px';

            }""",
                {
                    "width": width,
                    "park": park_enabled,
                    "keypad": keypad_open,
                    "height": height,
                },
            )
            page.wait_for_function("elements.card.style.getPropertyValue('--voip-video-actions-space') !== ''")
            if keypad_open:
                page.get_by_role("button", name="Keypad", exact=True).click()
                page.wait_for_function("elements.card.style.getPropertyValue('--voip-video-keypad-height') !== ''")
            assert page.evaluate("elements.voicemailBtn.scrollWidth <= elements.voicemailBtn.clientWidth")
            boxes = page.evaluate("""() => {
                const e=elements;
                return Object.fromEntries(['card','hangupBtn','hangupDuration','keypadBtn',
                    'settingsBtn','parkBtn','recordBtn','voicemailBtn','transferBtn']
                    .filter(key=>!e[key].hidden)
                    .map(key=>[key,e[key].getBoundingClientRect().toJSON()]));
            }""")
            if keypad_open and height == 280:
                panel = page.evaluate("""() => ({
                    ...elements.keypadPanel.getBoundingClientRect().toJSON(),
                    scrollHeight: elements.keypadPanel.scrollHeight,
                    clientHeight: elements.keypadPanel.clientHeight,
                })""")
                assert panel["bottom"] <= boxes["hangupBtn"]["top"] + 1
                assert panel["scrollHeight"] > panel["clientHeight"]
                await_scroll = "elements.keypadPanel.scrollTop > 0"
                page.mouse.move(
                    panel["left"] + panel["width"] / 2,
                    panel["top"] + panel["height"] / 2,
                )
                page.mouse.wheel(0, 150)
                page.wait_for_function(await_scroll)
                assert page.evaluate("elements.card.scrollTop") == 0
                after = page.evaluate(
                    "elements.hangupBtn.getBoundingClientRect().toJSON()"
                )
                assert abs(after["top"] - boxes["hangupBtn"]["top"]) <= 1
                assert abs(after["bottom"] - boxes["hangupBtn"]["bottom"]) <= 1
                if width == 360 and park_enabled:
                    page.evaluate("elements.keypadPanel.scrollTop = 0")
                    cdp = page.context.new_cdp_session(page)
                    x = panel["left"] + panel["width"] / 2
                    y = panel["bottom"] - 10
                    cdp.send(
                        "Input.dispatchTouchEvent",
                        {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]},
                    )
                    for offset in (20, 40, 60, 80):
                        cdp.send(
                            "Input.dispatchTouchEvent",
                            {
                                "type": "touchMove",
                                "touchPoints": [{"x": x, "y": y - offset}],
                            },
                        )
                        page.wait_for_timeout(16)
                    cdp.send(
                        "Input.dispatchTouchEvent",
                        {"type": "touchEnd", "touchPoints": []},
                    )
                    page.wait_for_function(await_scroll)
                    cdp.detach()

            page.screenshot(path=str(tmp_path / "call-controls.png"))
            card = boxes.pop("card")
            hangup = boxes.pop("hangupBtn")
            assert abs(hangup["bottom"] - card["bottom"]) <= 1
            for name, box in boxes.items():
                assert box["width"] > 0 and box["height"] > 0, name
                assert (
                    card["left"] <= box["left"] < box["right"] <= card["right"] + 1
                ), name
                if name != "hangupDuration":
                    assert box["top"] >= hangup["top"] - 1, name
                    assert box["bottom"] <= hangup["bottom"] + 1, name
            items = list(boxes.items())
            for index, (name, box) in enumerate(items):
                for other_name, other in items[index + 1 :]:
                    overlap = min(box["right"], other["right"]) - max(
                        box["left"], other["left"]
                    )
                    vertical = min(box["bottom"], other["bottom"]) - max(
                        box["top"], other["top"]
                    )
                    assert overlap <= 0 or vertical <= 0, (name, other_name)
        finally:
            browser.close()


@pytest.mark.skipif(shutil.which("chromium") is None, reason="Chromium is unavailable")
@pytest.mark.parametrize("width", [280, 500])
@pytest.mark.parametrize("endpoint_known", [False, True])
def test_esp_mirror_keeps_call_status_without_duplicate_activity(
    width, endpoint_known, tmp_path
):
    """Render the real mirror card using ESP entities and shared call identities."""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(
            executable_path=shutil.which("chromium"), headless=True
        )
        try:
            page = browser.new_page(viewport={"width": width + 40, "height": 550})

            def serve(route):
                name = route.request.url.split("/")[-1].split("?")[0]
                path = FRONTEND / name
                route.fulfill(
                    content_type="text/javascript" if name.endswith(".js") else "text/html",
                    body=path.read_text() if name.endswith(".js") and path.is_file()
                    else "<html><body></body></html>",
                )

            page.route("http://card.test/**", serve)
            page.goto("http://card.test/")
            page.evaluate("""async ({width, endpointKnown}) => {
                document.body.style.cssText='margin:20px;background:#161d1a;color:#ddd;font-family:Arial';
                await import('/voip-stack-card.js');
                const card=document.createElement('voip-stack-card');window.card=card;
                card.style.cssText=`display:block;width:${width}px;--ha-card-background:#25352d;--primary-text-color:#eee;--secondary-text-color:#bbb;--primary-color:#75b596`;
                card.config={mode:'esp_mirror',device_id:'ws3-device',name:'Waveshare S3'};
                card._activeDeviceInfo={device_id:'ws3-device',name:'Waveshare S3',
                    ...(endpointKnown?{endpoint_id:'ws3'}:{})};
                card._voipStateEntityId='sensor.ws3_state';
                card._destinationEntityId='sensor.ws3_destination';
                card._callerEntityId='sensor.ws3_caller';
                card._hass={language:'en',config:{location_name:'Home'},states:{
                    'sensor.ws3_state':{state:'remote_ringing'},
                    'sensor.ws3_destination':{state:'Kitchen'},
                    'sensor.ws3_caller':{state:''},
                }};
                window.activity={parked:['701'],phones:[
                    {endpoint_id:'ws3',device_id:'ws3-device',name:'Waveshare S3',state:'calling',call_ids:['shared-call']},
                    {endpoint_id:'kitchen',device_id:'kitchen-device',name:'Kitchen',state:'ringing',call_ids:['shared-call']},
                    {endpoint_id:'office',device_id:'office-device',name:'Office',state:'ringing',call_ids:['unrelated-call']},
                ]};
                card._onCallEvent({event_type:'voip_stack.activity',data:activity});
                document.body.append(card);
                window.setState=state=>{card._hass.states['sensor.ws3_state']={state};card._render();};
                window.rows=()=>[...card._els.phoneActivity.children].map(e=>e.textContent);
            }""", {"width": width, "endpointKnown": endpoint_known})
            page.wait_for_function("card._els.card.style.getPropertyValue('--voip-button-size') !== ''")
            page.screenshot(path=str(tmp_path / "esp-mirror-ringing.png"), full_page=True)
            assert page.evaluate("card._els.statusText.textContent") == "Kitchen is ringing..."
            assert page.evaluate("rows()") == ["Office: Ringing"]
            assert page.evaluate("card._els.parkingNotice.textContent") == "Parked in slot: 701"
            assert page.evaluate("card._softphoneSnapshot") is None
            for state in ("calling", "connecting", "ringing", "incoming", "answering", "in_call", "terminating"):
                page.evaluate("state=>setState(state)", state)
                assert page.evaluate("rows()") == ["Office: Ringing"], state

            # Call identity, not the displayed contact or a cached bridge event,
            # excludes activity. Another phone with the same name stays visible.
            page.evaluate("""()=>{activity.phones[2].name='Kitchen';
                card._hass.states['sensor.ws3_destination']={state:'Unrelated selection'};
                setState('remote_ringing');}""")
            assert page.evaluate("rows()") == ["Kitchen: Ringing"]
            page.evaluate("setState('idle')")
            assert page.evaluate("rows()") == ["Kitchen: Ringing", "Kitchen: Ringing"]

            # If the endpoint has no current dialog in the projection, do not
            # hide a similarly named phone that is ringing on a different call.
            page.evaluate("""()=>{activity.phones=activity.phones.slice(1);
                setState('remote_ringing');}""")
            assert page.evaluate("rows()") == ["Kitchen: Ringing", "Kitchen: Ringing"]
        finally:
            browser.close()


@pytest.mark.skipif(shutil.which("chromium") is None, reason="Chromium is unavailable")
@pytest.mark.parametrize("width", [280, 500])
@pytest.mark.parametrize("ringing", [False, True])
def test_activity_lists_leave_call_buttons_unchanged(width, ringing, tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(executable_path=shutil.which("chromium"), headless=True)
        try:
            page = browser.new_page(viewport={"width": width + 40, "height": 650})
            def serve(route):
                name = route.request.url.split("/")[-1].split("?")[0]
                path = FRONTEND / name
                route.fulfill(content_type="text/javascript" if name.endswith(".js") else "text/html",
                              body=path.read_text() if name.endswith(".js") and path.is_file() else "<html><body></body></html>")
            page.route("http://card.test/**", serve)
            page.goto("http://card.test/")
            page.evaluate('''async ({width,ringing})=>{
                const host=document.createElement('div');host.style.cssText=`width:${width}px;height:440px`;
                document.body.append(host);await import('/voip-stack-card.js');
                const p=customElements.get('voip-stack-card').prototype;
                const owner={shadowRoot:host.attachShadow({mode:'open'}),_attachEventHandlers(){},
                    _getSoftphoneEndpointId(){return 'mine'},_getConfigDeviceId(){return 'my-device'},_t:v=>v,
                    _isHaSoftphoneMode(){return true},
                    _getEspState(){return p._getEspState.call(this)},
                    _sessionCallId(){return p._sessionCallId.call(this)},
                    _measureLayout(){p._measureLayout.call(this)},_observeLayout(){p._observeLayout.call(this)},
                    _layoutPhoneActivity(){p._layoutPhoneActivity.call(this)}};
                owner._resizeObserver=new ResizeObserver(()=>owner._measureLayout());
                const {buildMainCardSkeleton}=await import('/voip-stack-card-view.js');
                buildMainCardSkeleton.call(owner,'test');window.owner=owner;window.elements=owner._els;
                const e=elements;e.statusText.textContent='Ready';e.callBtn.hidden=ringing;e.answerBtn.hidden=!ringing;e.declineBtn.hidden=!ringing;
                e.hangupBtn.hidden=true;e.placeholderBtn.hidden=true;e.keypadPanel.hidden=true;e.keypadInput.hidden=true;
                for(const button of owner.shadowRoot.querySelectorAll('button'))button.style.animation='none';
                window.renderActivity=value=>{owner._phoneActivity=value;p._renderPhoneActivity.call(owner,false)};
                window.controls=()=>Object.fromEntries(['callBtn','answerBtn','declineBtn','keypadBtn','settingsBtn']
                    .filter(k=>!e[k].hidden).map(k=>[k,e[k].getBoundingClientRect().toJSON()]));
                renderActivity({parked:[],phones:[]});
            }''', {"width": width, "ringing": ringing})
            page.wait_for_function("elements.card.style.getPropertyValue('--voip-button-size') !== ''")
            page.evaluate("async()=>{await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));await Promise.all(elements.card.getAnimations({subtree:true}).map(a=>a.finished.catch(()=>{})));}")
            before = page.evaluate("controls()")
            page.evaluate('''renderActivity({parked:['701','702'],phones:[
                {endpoint_id:'mine',device_id:'my-device',name:'This phone',state:'ringing'},
                {endpoint_id:'kitchen',name:'Kitchen',state:'ringing'},
                {endpoint_id:'office',name:'Office phone with a long name',state:'busy'}]})''')
            after = page.evaluate("controls()")
            for key, box in before.items():
                for field in ("x", "y", "width", "height"):
                    assert abs(box[field] - after[key][field]) < .5, (key, field)
            rows = page.evaluate("[...owner.shadowRoot.querySelectorAll('.phone-activity li')].map(e=>e.getBoundingClientRect().toJSON())")
            assert len(rows) == 4
            for row in rows:
                for button in after.values():
                    assert min(row['right'], button['right']) <= max(row['left'], button['left']) or min(row['bottom'], button['bottom']) <= max(row['top'], button['top'])
            assert page.evaluate("getComputedStyle(elements.phoneActivity.firstElementChild).flexDirection") == "row-reverse"
            assert page.evaluate("getComputedStyle(elements.phoneActivity.firstElementChild.firstElementChild).direction") == "ltr"
            # Lists are bounded by the call controls, never the diagnostics/footer.
            assert page.evaluate("""()=>{const buttons=[elements.callBtn,elements.answerBtn,elements.declineBtn]
                .filter(e=>!e.hidden).map(e=>e.getBoundingClientRect());
                return [elements.parkingNotice,elements.phoneActivity].every(e=>{
                    const r=e.getBoundingClientRect();return r.top>=Math.min(...buttons.map(b=>b.top))-.5 &&
                    r.bottom<=Math.max(...buttons.map(b=>b.bottom))+.5;});}""")
            page.screenshot(path=str(tmp_path / "activity.png"), full_page=True)
            page.evaluate("""()=>{
                const p=customElements.get('voip-stack-card').prototype;
                owner._isHaSoftphoneMode=()=>true;
                owner._availableDevices=[{device_id:'kitchen-device',endpoint_id:'kitchen',name:'Kitchen'},
                    {device_id:'office-device',endpoint_id:'office',name:'Office phone'}];
                owner._softphoneTargets=()=>owner._availableDevices;
                owner._getSoftphoneTargetDevice=()=>owner._availableDevices[1];
                owner._activeSoftphonePeerDevice=()=>p._activeSoftphonePeerDevice.call(owner);
            }""")
            for call_state in ("remote_ringing", "calling", "connecting", "ringing", "incoming", "answering", "in_call", "terminating"):
                page.evaluate("""state=>{
                    const incoming=['ringing','incoming','answering'].includes(state);
                    owner._softphoneSnapshot={state,call_id:'current-call',direction:incoming?'incoming':'outgoing',
                        target_device_id:'kitchen-device',caller:incoming?'Kitchen':'This phone',
                        callee:incoming?'This phone':'Kitchen'};
                    elements.statusText.textContent='Kitchen is ringing...';
                    renderActivity(owner._phoneActivity);
                }""", call_state)
                if call_state == "remote_ringing":
                    page.screenshot(path=str(tmp_path / "activity-remote-ringing.png"), full_page=True)
                assert 'Kitchen' not in page.evaluate("elements.phoneActivity.textContent"), call_state
                assert 'Office phone' in page.evaluate("elements.phoneActivity.textContent")
            page.evaluate("""()=>{owner._softphoneSnapshot={state:'idle'};
                renderActivity(owner._phoneActivity);}""")
            assert 'Kitchen' in page.evaluate("elements.phoneActivity.textContent")
            page.evaluate("renderActivity({parked:[],phones:[]})")
            assert page.evaluate("elements.parkingNotice.hidden && elements.phoneActivity.hidden")
            assert page.evaluate("controls()") == before
        finally:
            browser.close()
