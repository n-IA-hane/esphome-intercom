"""Browser presentation tests with the real page engine and controlled HA I/O."""

from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONT = ROOT / "custom_components/voip_stack/frontend"
pytestmark = [pytest.mark.js_runtime, pytest.mark.browser_real]


BOOTSTRAP = """async () => {
          window.services=[]; window.streams=[]; window.contexts=0; window.mics=0;
          const Context=window.AudioContext;
          window.AudioContext=class extends Context {constructor(...args){super(...args);contexts++;}};
          navigator.mediaDevices={enumerateDevices:async()=>[],addEventListener(){},
            getUserMedia:async()=>{mics++;throw Error('No media requested by this presentation test');}};
          window.phone={softphone:true,device_id:'desk-device',endpoint_id:'browser:desk',name:'Desk',capabilities:['audio','video']};
          window.current={state:'idle',call_id:'',device_id:phone.device_id,endpoint_id:phone.endpoint_id,
            enabled:true,available:true,sequence:1,revision:1,capabilities:['audio','video']};
          const connection=new EventTarget();
          connection.subscribeMessage=async(callback,request)=>{
            const record={callback,request,active:true};streams.push(record);
            if(request.type==='voip_stack/subscribe_ha_softphone_state')queueMicrotask(()=>callback(current));
            if(request.type==='voip_stack/subscribe_phone_states')queueMicrotask(()=>callback({kind:'snapshot',revision:1,phones:[]}));
            return()=>{record.active=false;};
          };
          connection.sendMessagePromise=async request=>{
            if(request.type==='voip_stack/list_devices')return {devices:[phone],entity_ids:{}};
            if(request.type==='voip_stack/ha_softphone_state')return current;
            throw Error('Unexpected WS request '+request.type);
          };
          window.hass={connection,user:{id:'ordinary-user',is_admin:true},language:'en',states:{},
            config:{location_name:'Home',components:['voip_stack']},
            callWS:request=>connection.sendMessagePromise(request),
            callService:async(domain,service,data)=>{services.push({domain,service,data});}};
          window.push=patch=>{
            current={...current,...patch,sequence:current.sequence+1,revision:current.revision+1};
            for(const item of [...streams])if(item.active&&item.request.type==='voip_stack/subscribe_ha_softphone_state')item.callback(current);
          };
          class HA extends HTMLElement {
            constructor(){super();this.attachShadow({mode:'open'});this.consumers=[];this.updateComplete=Promise.resolve();}
            provideHass(element){this.consumers.push(element);element.hass=hass;}
          }
          customElements.define('home-assistant',HA);
          const root=window.root=document.createElement('home-assistant');document.body.append(root);
          // Only the HA host/dialog boundary is controlled. Its native <dialog>,
          // production popup, card, engine and engine subscription sharing are real.
          root.addEventListener('show-dialog',async event=>{
            const {dialogTag,dialogImport,dialogParams}=event.detail;await dialogImport();
            let dialog=root.shadowRoot.querySelector(dialogTag);
            if(!dialog){dialog=document.createElement(dialogTag);root.provideHass(dialog);root.shadowRoot.append(dialog);}
            dialog.showDialog(dialogParams);
          });
          await import('/voip-stack-popup.js');
        }"""

@pytest.fixture
def popup_page():
    from playwright.sync_api import sync_playwright

    assert shutil.which("chromium"), "The maintained Chromium browser is required"
    with sync_playwright() as runner:
        browser = runner.chromium.launch(executable_path=shutil.which("chromium"), headless=True)
        page = browser.new_page(viewport={"width": 420, "height": 800})
        page.set_default_timeout(2000)

        def serve(route):
            name = route.request.url.split("/")[-1].split("?")[0]
            path = FRONT / name
            route.fulfill(
                content_type="text/javascript" if path.is_file() else "text/html",
                body=path.read_text() if path.is_file() else "<html><body></body></html>",
            )

        page.route("http://phone.test/**", serve)
        page.goto("http://phone.test/")
        page.evaluate(BOOTSTRAP)
        page.wait_for_function("!!globalThis.__voipStackPopup")
        try:
            yield page
        finally:
            browser.close()


def enable(page):
    page.evaluate("""() => {
      const settings=document.createElement('voip-stack-popup-settings');
      document.body.append(settings);settings.hass=hass;window.settings=settings;
    }""")
    page.wait_for_function("settings._select.options.length===2")
    page.locator("voip-stack-popup-settings select").select_option("desk-device")
    page.get_by_label("Receive call popups on this device").check()
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_function("streams.some(s=>s.active&&s.request.type==='voip_stack/subscribe_ha_softphone_state')")


def incoming(page, call_id="incoming-A"):
    page.evaluate("call=>push({state:'ringing',call_id:call,direction:'incoming',caller:'Front door'})", call_id)
    page.wait_for_function("!!__voipStackPopup.dialog?._dialog.open")
    page.wait_for_function("!!__voipStackPopup.dialog?._phone?._softphoneSnapshot?.call_id")


def test_cold_without_card_persistence_and_shared_owner(popup_page):
    page = popup_page
    assert page.locator("voip-stack-card").count() == 0
    assert page.evaluate("streams.length") == 0
    assert page.locator("voip-stack-popup button:visible").count() == 0
    enable(page)
    assert page.evaluate("mics") == 0
    assert page.evaluate("contexts") == 1  # Existing engine ringtone, unlocked by opt-in.
    assert page.evaluate("JSON.parse(localStorage.voip_stack_popup_phone)") == {
        "device_id": "desk-device", "endpoint_id": "browser:desk"
    }
    incoming(page)
    page.evaluate("""() => {
      const card=document.createElement('voip-stack-card');
      card.setConfig({mode:'ha_softphone',device_id:phone.device_id});card.hass=hass;
      root.shadowRoot.append(card);window.visibleCard=card;
    }""")
    page.wait_for_function("!!visibleCard._softphoneSnapshot?.call_id")
    assert page.evaluate("streams.filter(s=>s.active&&s.request.type==='voip_stack/subscribe_ha_softphone_state').length") == 1
    assert page.evaluate("__voipStackEngine._ringtoneTimer!==null")
    assert page.evaluate("contexts") == 1
    assert page.evaluate("mics") == 0
    assert page.evaluate("visibleCard._sessionCallId()===__voipStackPopup.dialog._phone._sessionCallId()")
    assert page.evaluate("getComputedStyle(__voipStackPopup.dialog._phone._els.destRow).display") == "none"
    assert page.evaluate("getComputedStyle(__voipStackPopup.dialog._phone._els.callBtn).display") == "none"
    # Changing HA navigation and HA auth context does not create a user->phone map.
    page.evaluate("""() => {
      history.pushState({},'', '/config/devices');hass={...hass,user:{id:'another-user',is_admin:true}};
      for(const consumer of root.consumers)consumer.hass=hass;
    }""")
    assert page.evaluate("__voipStackPopup.binding.endpoint_id") == "browser:desk"
    assert page.evaluate("Object.keys(JSON.parse(localStorage.voip_stack_popup_phone)).sort()") == ["device_id", "endpoint_id"]
    assert page.evaluate("services") == []


def test_minimize_reopen_terminal_and_explicit_hangup(popup_page):
    page = popup_page
    enable(page)
    page.evaluate("""() => {
      const other=document.createElement('dialog');other.innerHTML='<input value="unsaved">';
      root.shadowRoot.append(other);other.showModal();window.other=other;
    }""")
    incoming(page)
    page.keyboard.press("Escape")
    assert page.evaluate("other.open && other.querySelector('input').value==='unsaved'")
    assert page.evaluate("!__voipStackPopup._mini.hidden")
    assert page.evaluate("__voipStackPopup.dialog._phone===null")
    assert page.evaluate("services") == []
    page.get_by_role("button", name="Reopen call").click()
    page.wait_for_function("__voipStackPopup.dialog._dialog.open")
    page.get_by_role("button", name="Minimize call").click()
    assert page.evaluate("!__voipStackPopup._mini.hidden")
    page.get_by_role("button", name="Hang up call", exact=True).click()
    page.wait_for_function("services.length===1")
    assert page.evaluate("services[0]") == {
        "domain": "voip_stack", "service": "decline",
        "data": {"device_id": "desk-device", "call_id": "incoming-A"},
    }
    page.evaluate("push({state:'idle',call_id:'incoming-A'})")
    assert page.evaluate("__voipStackPopup._mini.hidden && !__voipStackPopup.dialog._dialog.open")
    assert page.evaluate("__voipStackEngine._ringtoneTimer===null")
    assert page.evaluate("__voipStackPopup._timer===null || __voipStackPopup._timer===undefined")


def test_saved_phone_bootstraps_after_reopen_without_a_card(popup_page):
    page = popup_page
    enable(page)
    page.reload()
    page.evaluate(BOOTSTRAP)
    page.wait_for_function("streams.some(s=>s.active&&s.request.type==='voip_stack/subscribe_ha_softphone_state')")
    assert page.locator("voip-stack-card").count() == 0
    assert page.locator("voip-stack-popup button:visible").count() == 0
    assert page.evaluate("__voipStackPopup.binding") == {"device_id": "desk-device", "endpoint_id": "browser:desk"}
    incoming(page, "incoming-after-reopen")
    assert page.evaluate("__voipStackPopup.state.call_id") == "incoming-after-reopen"
    assert page.evaluate("mics") == 0


def test_rx_animation_has_fixed_hitbox_and_rejects_stale_call(popup_page):
    page = popup_page
    enable(page)
    incoming(page)
    page.evaluate("push({state:'in_call',call_id:'incoming-A',connected_at:Date.now()/1000-12,media_owner:'other'})")
    page.wait_for_function("!__voipStackPopup.dialog._phone._els.hangupBtn.hidden")
    result = page.evaluate("""() => {
      const card=__voipStackPopup.dialog._phone,button=card._els.hangupBtn;
      const rect=button.getBoundingClientRect();window.initialRect=[rect.x,rect.y,rect.width,rect.height];
      window.initialPulse=card.shadowRoot.querySelector('.hangup-pulse').getBoundingClientRect().width;
      const level=(call,value)=>__voipStackEngine.dispatchEvent(new CustomEvent('audio-level',
        {detail:{endpoint_id:phone.endpoint_id,call_id:call,level:value}}));window.level=level;
      level('old-call',1);
      return {scale:card._els.card.style.getPropertyValue('--voip-hangup-scale'),rect:initialRect};
    }""")
    assert result["scale"] == "1.0000"
    assert min(result["rect"][2:]) >= 44
    page.evaluate("level('incoming-A',1)")
    page.wait_for_timeout(130)
    assert page.evaluate("__voipStackPopup.dialog._phone._els.card.style.getPropertyValue('--voip-hangup-scale')") == "1.2400"
    assert page.evaluate("__voipStackPopup.dialog._phone.shadowRoot.querySelector('.hangup-pulse').getBoundingClientRect().width > initialPulse*1.06")
    assert page.evaluate("""() => {
      const b=__voipStackPopup.dialog._phone._els.hangupBtn,r=b.getBoundingClientRect();
      return JSON.stringify([r.x,r.y,r.width,r.height])===JSON.stringify(initialRect) && getComputedStyle(b).transform==='none';
    }""")
    page.emulate_media(reduced_motion="reduce")
    assert page.evaluate("getComputedStyle(__voipStackPopup.dialog._phone.shadowRoot.querySelector('.hangup-pulse')).transform") == "none"
    page.evaluate("level('incoming-A',0)")
    assert page.evaluate("__voipStackPopup.dialog._phone._els.card.style.getPropertyValue('--voip-hangup-scale')") == "1.0000"
    page.get_by_role("button", name="Minimize call").click()
    assert page.evaluate("__voipStackPopup._duration.textContent") in {"00:12", "00:13"}
    page.evaluate("push({state:'idle',call_id:'incoming-A'})")
    assert page.evaluate("__voipStackPopup._timer===null")


@pytest.mark.parametrize("minimized", [False, True])
def test_foreground_answer_link_reuses_existing_answer_once(popup_page, minimized):
    page = popup_page
    enable(page)
    incoming(page)
    if minimized:
        page.get_by_role("button", name="Minimize call").click()
    # Only the microphone preparation boundary is controlled. The real card
    # Answer, engine intent/Call-ID ownership and HA command are exercised.
    page.evaluate("""() => {
      window.prepares=0;__voipStackEngine.prepareAudioCall=async()=>{prepares++;};
      history.pushState({},'', '/?voip_answer=1&voip_endpoint=browser%3Adesk&voip_call_id=incoming-A');
      history.replaceState({dialog:'voip-stack-phone-dialog'},'');
      window.dispatchEvent(new CustomEvent('location-changed'));
    }""")
    page.wait_for_function("services.some(item=>item.service==='answer')")
    assert page.evaluate("services.filter(item=>item.service==='answer').length") == 1
    data = page.evaluate("services.find(item=>item.service==='answer').data")
    assert data["device_id"] == "desk-device"
    assert data["call_id"] == "incoming-A"
    assert data["media_client_id"] == page.evaluate("__voipStackEngine.mediaClientId")
    page.evaluate("window.dispatchEvent(new PageTransitionEvent('pageshow'))")
    page.wait_for_timeout(60)
    assert page.evaluate("prepares") == 1
    assert page.evaluate("services.filter(item=>item.service==='answer').length") == 1
    assert page.evaluate("new URL(location.href).searchParams.has('voip_answer')") is False
    assert page.evaluate("history.state.dialog") == "voip-stack-phone-dialog"


def test_answer_link_rejects_wrong_scope_and_changed_call_during_permission(popup_page):
    page = popup_page
    enable(page)
    incoming(page)
    page.evaluate("""() => {
      window.prepares=0;window.allow=null;
      __voipStackEngine.prepareAudioCall=()=>{prepares++;return new Promise(resolve=>{window.allow=resolve;});};
      window.link=query=>{history.pushState({},'', '/?'+query);window.dispatchEvent(new CustomEvent('location-changed'));};
      link('voip_answer=1&voip_endpoint=browser%3Adesk&voip_call_id=old-call');
      link('voip_answer=1&voip_endpoint=another&voip_call_id=incoming-A');
      link('voip_answer=1&voip_endpoint=browser%3Adesk');
      push({state:'ringing',call_id:'incoming-A'});
    }""")
    assert page.evaluate("prepares") == 0
    assert page.evaluate("services") == []
    page.evaluate("link('voip_answer=1&voip_endpoint=browser%3Adesk&voip_call_id=incoming-A')")
    page.wait_for_function("prepares===1 && !!allow")
    page.evaluate("push({state:'ringing',call_id:'incoming-B'});allow()")
    page.wait_for_timeout(80)
    assert page.evaluate("services") == []
    assert page.evaluate("__voipStackPopup.binding.endpoint_id") == "browser:desk"


@pytest.mark.parametrize("hidden_mode", ["popup", "dashboard"])
def test_video_hangup_bounds_follow_visible_runtime_controls(popup_page, hidden_mode):
    """Exercise the production builder and layout owner in a real native dialog."""
    page = popup_page
    page.set_viewport_size({"width": 1280, "height": 900})
    page.evaluate("""async () => {
      await import('/voip-stack-card.js');
      const {buildMainCardSkeleton}=await import('/voip-stack-card-view.js');
      const Card=customElements.get('voip-stack-card');
      const dialog=document.createElement('voip-stack-phone-dialog');
      root.shadowRoot.append(dialog);dialog._title.textContent='Test';
      // The static layout fixture has no phone/media lifecycle. Its template,
      // native dialog, CSS and measured layout methods are production code.
      const view=document.createElement('div');view.attachShadow({mode:'open'});
      view._attachEventHandlers=()=>{};view._observeLayout=()=>{};
      view._layoutPhoneActivity=Card.prototype._layoutPhoneActivity;
      view._measureLayout=Card.prototype._measureLayout;
      buildMainCardSkeleton.call(view,'layout-fixture');dialog._view.append(view);
      const e=view._els;e.card.classList.add('video-active');
      for(const item of [e.header,e.destRow,e.answerBtn,e.declineBtn,e.callBtn,
          e.placeholderBtn,e.settingsPanel,e.keypadPanel,e.voicemailPanel])item.hidden=true;
      for(const button of e.runtimeControls.querySelectorAll('button'))button.hidden=true;
      e.settingsBtn.hidden=false;e.keypadBtn.hidden=false;e.hangupBtn.hidden=false;
      e.hangupPeer.textContent='Waveshare P4 Touch';e.hangupDuration.textContent='00:25';
      e.videoCanvas.hidden=false;e.videoShade.hidden=false;
      dialog._dialog.showModal();
      const box=node=>{const b=node.getBoundingClientRect();
        return {left:b.left,right:b.right,top:b.top,bottom:b.bottom,width:b.width,height:b.height};};
      const textBoxes=node=>{const r=document.createRange();r.selectNodeContents(node);
        return [...r.getClientRects()].filter(b=>b.width>0&&b.height>0)
          .map(b=>({left:b.left,right:b.right,top:b.top,bottom:b.bottom}));};
      window.layoutFixture={view,e,dialog,measure:()=>view._measureLayout(),
        inspect:()=>({card:box(e.card),button:box(e.hangupBtn),controls:box(e.runtimeControls),
          paddingRight:parseFloat(getComputedStyle(e.hangupBtn).paddingRight),
          copy:textBoxes(e.hangupBtn.querySelector('.hangup-copy')),
          duration:textBoxes(e.hangupDuration)})};
    }""")

    def assert_readable(state):
        card, button = state["card"], state["button"]
        assert card["left"] > 100  # A hidden control's viewport origin is not card-relative.
        assert button["width"] >= 44 and button["height"] >= 44
        assert button["left"] >= card["left"] - 1
        assert button["right"] <= card["right"] + 1
        for key in ("copy", "duration"):
            assert state[key]
            for rect in state[key]:
                assert rect["left"] >= max(card["left"], button["left"]) - 1
                assert rect["right"] <= min(card["right"], button["right"]) + 1
                assert rect["top"] >= button["top"] - 1
                assert rect["bottom"] <= button["bottom"] + 1

    # A visible toolbar still needs its own space, including after it reappears.
    page.evaluate("layoutFixture.measure()")
    visible = page.evaluate("layoutFixture.inspect()")
    assert_readable(visible)
    assert visible["controls"]["width"] > 0 and visible["paddingRight"] > 0
    assert max(rect["right"] for rect in visible["duration"]) <= visible["controls"]["left"]

    page.evaluate("""mode=>{
      if(mode==='popup')layoutFixture.view.setAttribute('data-receive-popup','');
      else layoutFixture.e.card.classList.add('dashboard-inline-options');
      layoutFixture.measure();
    }""", hidden_mode)
    hidden = page.evaluate("layoutFixture.inspect()")
    assert hidden["controls"]["width"] == 0 and hidden["controls"]["height"] == 0
    assert_readable(hidden)
    assert hidden["paddingRight"] == 0

    page.evaluate("""()=>{
      layoutFixture.view.removeAttribute('data-receive-popup');
      layoutFixture.e.card.classList.remove('dashboard-inline-options');
      layoutFixture.measure();
    }""")
    restored = page.evaluate("layoutFixture.inspect()")
    assert_readable(restored)
    assert restored["paddingRight"] > 0
    assert max(rect["right"] for rect in restored["duration"]) <= restored["controls"]["left"]


def test_popup_labels_follow_existing_italian_catalogue(popup_page):
    page = popup_page
    enable(page)
    page.evaluate("""() => {
      hass={...hass,language:'it-IT'};
      for(const element of root.consumers)element.hass=hass;
      settings.hass=hass;
    }""")
    assert page.get_by_role("heading", name="Popup chiamate").count() == 1
    assert page.get_by_role("button", name="Salva", exact=True).count() == 1
    assert page.get_by_label("Ricevi popup chiamate su questo dispositivo").count() == 1
    incoming(page)
    page.get_by_role("button", name="Minimizza chiamata").click()
    assert page.get_by_role("button", name="Riapri chiamata").count() == 1
    assert page.get_by_role("button", name="Riaggancia chiamata").count() == 1
