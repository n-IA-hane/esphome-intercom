"""Browser presentation tests with the real page engine and controlled HA I/O."""

from pathlib import Path
import shutil

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONT = ROOT / "custom_components/voip_stack/frontend"
pytestmark = [pytest.mark.js_runtime, pytest.mark.browser_real]


BOOTSTRAP = """async () => {
          window.services=[]; window.streams=[]; window.contexts=0; window.mics=0;
          window.popupBindings={}; window.bindingRequests=[];
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
            if(request.type==='voip_stack/ha_popup_binding'){bindingRequests.push(request);return popupBindings[request.binding_id]||null;}
            if(request.type==='voip_stack/set_ha_popup_binding'){bindingRequests.push(request);return popupBindings[request.binding_id]={...request};}
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
        context = browser.new_context(viewport={"width": 420, "height": 800})
        page = context.new_page()
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


def test_shared_phone_terminal_resyncs_passive_browser_without_reload(popup_page):
    """Real cards/engines, two page contexts, backend ACK-only endpoint stream."""
    first = popup_page
    second = first.context.new_page()

    def serve(route):
        path = FRONT / route.request.url.split('/')[-1].split('?')[0]
        route.fulfill(content_type='text/javascript' if path.is_file() else 'text/html',
                      body=path.read_text() if path.is_file() else '<html><body></body></html>')

    second.route('http://phone.test/**', serve)
    second.goto('http://phone.test/')
    second.evaluate(BOOTSTRAP)
    for page in (first, second):
        page.evaluate("""async()=>{
          const connection=hass.connection;
          // This is the actual server contract: the subscription acknowledges
          // registration, and a separate query supplies the initial snapshot.
          connection.subscribeMessage=async(callback,request)=>{
            const item={callback,request,active:true};streams.push(item);
            if(request.type==='voip_stack/subscribe_phone_states')queueMicrotask(()=>callback({kind:'snapshot',revision:1,phones:[]}));
            return()=>{item.active=false;};
          };
          const query=connection.sendMessagePromise;
          connection.sendMessagePromise=async request=>{
            if(request.type==='voip_stack/ha_softphone_state'&&request.media_client_id)
              return {...current,media_owner:'other'};
            return query(request);
          };
          await import('/voip-stack-card.js');
          const card=window.card=document.createElement('voip-stack-card');
          card.setConfig({mode:'ha_softphone',device_id:phone.device_id});card.hass=hass;
          root.shadowRoot.append(card);
        }""")
        page.wait_for_function("card._softphoneSnapshot?.state==='idle'")
        page.evaluate("push({state:'in_call',call_id:'shared-A',send_video:true,video_active:true})")
        page.wait_for_function("card._softphoneSnapshot.state==='in_call'")
    second.evaluate("hass.connection.dispatchEvent(new Event('disconnected'))")
    first.evaluate("push({state:'idle',call_id:'shared-A',terminal_reason:'local_hangup'})")
    first.wait_for_function("card._softphoneSnapshot.state==='idle'")
    second.evaluate("current={...current,state:'idle',call_id:'shared-A',terminal_reason:'local_hangup',revision:99}")
    assert second.evaluate("card._softphoneSnapshot.state") == 'in_call'
    second.evaluate("hass.connection.dispatchEvent(new Event('ready'))")
    second.wait_for_function("card._softphoneSnapshot.state==='idle'")
    assert 'In Call' not in second.evaluate("card._els.statusText.textContent")
    # HA restart may lose last-terminal history while retaining device identity.
    second.evaluate("push({state:'in_call',call_id:'shared-B'});hass.connection.dispatchEvent(new Event('disconnected'));current={...current,state:'idle',call_id:'',sequence:0,revision:0};hass.connection.dispatchEvent(new Event('ready'))")
    second.wait_for_function("card._softphoneSnapshot.state==='idle'&&card._softphoneSnapshot.call_id===''")
    for page in (first, second):
        assert page.evaluate('mics') == 0
        assert page.evaluate('services') == []
    second.close()


@pytest.mark.parametrize('answer_path', ['card-button', 'popup-button', 'url', 'automatic'])
def test_explicit_answer_camera_uses_real_acquisition_when_permission_query_unsupported(popup_page, answer_path):
    """Companion may support GUM but reject Permissions.query(camera)."""
    page = popup_page
    page.evaluate("""()=>{
      window.cameraRequests=0;window.cameraQueries=0;window.audioPreparations=0;
      Object.defineProperty(navigator.permissions,'query',{configurable:true,value:async({name})=>{
        if(name==='camera'){cameraQueries++;throw new TypeError('camera permission query unsupported');}
        return {state:'granted'};
      }});
      navigator.mediaDevices.getUserMedia=async options=>{
        if(!options.video||options.audio!==false)throw Error('Unexpected capture request');
        cameraRequests++;const track={stop(){},kind:'video'};
        return {getVideoTracks:()=>[track],getTracks:()=>[track]};
      };
      // The audio preparation contract is covered separately; keep this
      // camera decision test independent from real audio devices/HA services.
      __voipStackEngine.prepareAudioCall=async()=>{audioPreparations++;return true;};
    }""")
    if answer_path == 'popup-button':
        enable(page)
    else:
        page.evaluate("""async()=>{
          await import('/voip-stack-card.js');
          const card=window.cameraCard=document.createElement('voip-stack-card');
          card.setConfig({mode:'ha_softphone',device_id:phone.device_id});card.hass=hass;
          root.shadowRoot.append(card);
        }""")
    page.evaluate("""automatic=>push({state:'ringing',direction:'incoming',call_id:'camera-A',
      caller:'Dashboard',send_video:true,video_camera_send_enabled:true,video_offered:true,
      auto_answer:automatic})""", answer_path == 'automatic')
    if answer_path in ('card-button', 'popup-button'):
        # The production Answer pulse keeps changing its bounds. A human
        # clicks its stable centre; do the same with a trusted pointer event,
        # without disabling animation or forcing browser permission policy.
        button = page.get_by_role('button', name='Answer', exact=True)
        button.wait_for(state='visible')
        bounds = button.bounding_box()
        page.mouse.click(bounds['x'] + bounds['width'] / 2, bounds['y'] + bounds['height'] / 2)
    elif answer_path == 'url':
        page.evaluate("""()=>{
          // A notification is an explicit answer and may retry camera access
          // even when an earlier automatic page probe was denied.
          __voipStackEngine.recordAutoAnswerMedia('video',false);
          history.pushState({},'', '/?'+new URLSearchParams({voip_answer:'1',voip_endpoint:phone.endpoint_id,voip_call_id:'camera-A'}));
          dispatchEvent(new Event('location-changed'));
        }""")
    page.wait_for_function("services.some(x=>x.service==='answer')")
    assert page.evaluate("services.find(x=>x.service==='answer').data.send_video") is True
    assert page.evaluate('cameraRequests') == 1
    assert page.evaluate('cameraQueries') == (1 if answer_path == 'automatic' else 0)
    assert page.evaluate('audioPreparations') == 1


def setup_mailbox(page, outcome='success'):
    """Real card/engine and DTMF path; only HA/archive/media I/O are fixtures."""
    page.evaluate("""async outcome=>{
      await import('/voip-stack-card.js');
      window.savedMessages=[{id:'saved-1',caller:'Fixture',created_at:1,read:false}];
      window.digits=[];window.originations=[];window.cameraRequests=0;window.operationListener=null;
      const engine=__voipStackEngine;
      engine.prepareAudioCall=async()=>true;
      navigator.mediaDevices.getUserMedia=async options=>{
        if(!options.video||options.audio!==false)throw Error('Unexpected capture');
        cameraRequests++;const track={stop(){},kind:'video'};
        return {getVideoTracks:()=>[track],getTracks:()=>[track]};
      };
      current={...current,send_video:true,video_camera_send_enabled:true,
        voicemail:{enabled:true,available:true,new:1,old:0,total:1}};
      const originalQuery=hass.connection.sendMessagePromise;
      hass.connection.sendMessagePromise=async request=>{
        if(request.type==='call_service'&&request.service==='call'){
          originations.push(request.service_data);
          current={...current,state:'in_call',call_id:'mailbox-A',video_active:false};
          return {...current};
        }
        if(request.type==='voip_stack/voicemail/list')return {
          unread:savedMessages.length,total:savedMessages.length,messages:[...savedMessages],selections:{'saved-1':'01#'}};
        return originalQuery(request);
      };
      hass.connection.subscribeEvents=async callback=>{operationListener=callback;return()=>{operationListener=null;};};
      hass.callService=async(domain,service,data)=>{
        services.push({domain,service,data});
        if(service==='hangup')push({state:'idle',call_id:'mailbox-A'});
      };
      // Preserve the actual engine's sendDtmf/control/termination methods.
      engine.resumeSession=async(_info,device,payload)=>{
        engine._deviceId=device;engine._endpointId=phone.endpoint_id;engine._callId=payload.call_id;
        engine._state='IN_CALL';engine._ws={readyState:WebSocket.OPEN,close(){this.readyState=WebSocket.CLOSED;},send(text){
          const data=JSON.parse(text);if(data.type!=='dtmf')throw Error('Unexpected media control');
          digits.push(data.digit);
          if(digits.join('')==='01#2'&&outcome==='success'){
            savedMessages=[];
            // Actual backend order: archive committed, MWI snapshot, operation ACK.
            push({voicemail:{enabled:true,available:true,new:0,old:0,total:0}});
            operationListener?.({data:{call_id:'mailbox-A',mailbox_id:phone.endpoint_id,message_id:'saved-1',operation:'deleted'}});
          }
        }};
        return true;
      };
      const card=window.mailboxCard=document.createElement('voip-stack-card');
      card.setConfig({mode:'ha_softphone',device_id:phone.device_id});card.hass=hass;root.shadowRoot.append(card);
    }""", outcome)
    page.wait_for_function("mailboxCard._softphoneSnapshot?.voicemail?.total===1")
    page.get_by_role('button', name='Voicemail', exact=True).click()
    page.get_by_role('button', name='Delete', exact=True).wait_for(state='visible')


def test_mailbox_last_delete_hides_message_indicator_from_authoritative_zero(popup_page):
    page = popup_page
    setup_mailbox(page)
    page.get_by_role('button', name='Delete', exact=True).click()
    page.wait_for_function("!mailboxCard._mailboxBusy&&mailboxCard._softphoneSnapshot.state==='idle'")
    assert page.evaluate('digits.join("")') == '01#2'
    assert page.evaluate('mailboxCard._softphoneSnapshot.voicemail.total') == 0
    assert page.evaluate('mailboxCard._els.voicemailBtn.hidden') is True
    assert page.evaluate('mailboxCard._els.voicemailPanel.hidden') is True


def test_mailbox_dtmf_call_never_requests_camera_with_global_send_camera_enabled(popup_page):
    page = popup_page
    setup_mailbox(page)
    page.get_by_role('button', name='Delete', exact=True).click()
    page.wait_for_function("!mailboxCard._mailboxBusy&&mailboxCard._softphoneSnapshot.state==='idle'")
    assert page.evaluate('cameraRequests') == 0
    assert page.evaluate('originations[0].send_video') is False
    assert page.evaluate('mailboxCard._softphoneSnapshot.send_video') is True


def test_mailbox_audio_only_intent_leaves_regular_call_camera_preference(popup_page):
    page = popup_page
    setup_mailbox(page)
    page.evaluate("mailboxCard._startCall('Ordinary peer')")
    assert page.evaluate('originations[0].destination') == 'Ordinary peer'
    assert page.evaluate('originations[0].send_video') is True
    assert page.evaluate('cameraRequests') == 1
    assert page.evaluate('mailboxCard._softphoneSnapshot.send_video') is True


def test_mailbox_failed_delete_keeps_authoritative_message_count(popup_page):
    page = popup_page
    page.clock.install()
    setup_mailbox(page, 'failed')
    page.get_by_role('button', name='Delete', exact=True).click()
    page.wait_for_function("digits.join('')==='01#2'")
    page.clock.fast_forward(10001)
    page.wait_for_function('!mailboxCard._mailboxBusy')
    assert page.evaluate('mailboxCard._softphoneSnapshot.voicemail.total') == 1
    assert page.evaluate('mailboxCard._els.voicemailBtn.hidden') is False
    assert 'not confirmed' in page.evaluate('mailboxCard._errorMsg')


def test_mailbox_late_list_cannot_revive_deleted_messages(popup_page):
    page = popup_page
    setup_mailbox(page)
    page.evaluate("""()=>{
      const query=hass.callWS;
      hass.callWS=request=>request.type==='voip_stack/voicemail/list'
        ? new Promise(resolve=>{window.oldMailboxResult=resolve;}) : query(request);
      mailboxCard._mailboxBusy=true;
      window.oldMailboxRead=mailboxCard._loadMailbox();
      push({voicemail:{enabled:true,available:true,new:0,old:0,total:0}});
    }""")
    page.evaluate("""async()=>{oldMailboxResult({total:1,unread:1,messages:[{id:'old'}]});await oldMailboxRead;}""")
    assert page.evaluate('mailboxCard._mailboxMessages.length') == 0
    assert page.evaluate('mailboxCard._mailboxTotal') == 0
    assert page.evaluate('mailboxCard._mailboxOpen') is False


def test_popup_has_one_themed_glass_surface(popup_page):
    """Theme blur covers the full dialog, without a second inner card layer."""
    page = popup_page
    page.evaluate("""() => {
      document.documentElement.style.cssText=`--ha-card-background:transparent;
        --ha-card-backdrop-filter:blur(10px) saturate(1.25);
        --ha-card-border:0.5px solid rgba(234,235,238,.22);
        --ha-card-box-shadow:0 14px 38px rgba(0,0,0,.34);
        --ha-card-border-radius:18px;--primary-text-color:#eee`;
      window.themeCard=card=>{
        const style=document.createElement('style');
        style.textContent=`ha-card::before{content:'';position:absolute;inset:0;
          border-radius:inherit;backdrop-filter:var(--ha-card-backdrop-filter);
          background:rgba(2,6,11,.12);pointer-events:none;z-index:-1}`;
        card.shadowRoot.append(style);
      };
    }""")
    enable(page)
    incoming(page)
    page.evaluate("themeCard(__voipStackPopup.dialog._phone)")
    surfaces = page.evaluate("""() => {
      const d=__voipStackPopup.dialog,c=d._phone._els.card;
      const outer=getComputedStyle(d._dialog),inner=getComputedStyle(c);
      return {outerBlur:outer.backdropFilter,outerShadow:outer.boxShadow,
        innerBlur:inner.backdropFilter,innerFilter:inner.filter,
        innerBackground:inner.backgroundColor,innerShadow:inner.boxShadow,
        innerBorder:inner.borderTopWidth,pseudo:getComputedStyle(c,'::before').content};
    }""")
    assert surfaces["outerBlur"] == "blur(10px) saturate(1.25)"
    assert surfaces["outerShadow"] != "none"
    assert surfaces["innerBlur"] == surfaces["innerFilter"] == "none"
    assert surfaces["innerBackground"] == "rgba(0, 0, 0, 0)"
    assert surfaces["innerShadow"] == "none" and surfaces["innerBorder"] == "0px"
    assert surfaces["pseudo"] == "none"

    page.evaluate("""() => {
      const ordinary=document.createElement('voip-stack-card');window.ordinary=ordinary;
      ordinary.setConfig({mode:'ha_softphone',device_id:phone.device_id});ordinary.hass=hass;
      root.shadowRoot.append(ordinary);themeCard(ordinary);
    }""")
    assert page.evaluate("getComputedStyle(ordinary._els.card,'::before').content") == '\"\"'
    assert page.evaluate("getComputedStyle(ordinary._els.card,'::before').backdropFilter") == "blur(10px) saturate(1.25)"
    page.keyboard.press("Escape")
    mini = page.evaluate("""() => {
      const style=getComputedStyle(__voipStackPopup._mini);
      return {hidden:__voipStackPopup._mini.hidden,blur:style.backdropFilter,shadow:style.boxShadow};
    }""")
    assert not mini["hidden"] and mini["blur"] == surfaces["outerBlur"]
    assert mini["shadow"] == surfaces["outerShadow"]
    assert page.evaluate("services") == []


def test_cold_without_card_persistence_and_shared_owner(popup_page):
    page = popup_page
    assert page.locator("voip-stack-card").count() == 0
    assert page.evaluate("streams.length") == 0
    assert page.locator("voip-stack-popup button:visible").count() == 0
    enable(page)
    assert page.evaluate("mics") == 0
    assert page.evaluate("contexts") == 1  # Existing engine ringtone, unlocked by opt-in.
    saved = page.evaluate("JSON.parse(localStorage.voip_stack_popup_phone)")
    binding_id = saved.pop("binding_id")
    assert binding_id.startswith("browser-")
    assert saved == {"device_id": "desk-device", "endpoint_id": "browser:desk", "enabled": True}
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
    # Navigation/auth context does not replace the account or browser binding ID.
    page.evaluate("""() => {
      history.pushState({},'', '/config/devices');hass={...hass,user:{id:'another-user',is_admin:true}};
      for(const consumer of root.consumers)consumer.hass=hass;
    }""")
    assert page.evaluate("__voipStackPopup.binding.endpoint_id") == "browser:desk"
    assert page.evaluate("Object.keys(JSON.parse(localStorage.voip_stack_popup_phone)).sort()") == ["binding_id", "device_id", "enabled", "endpoint_id"]
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
    saved = page.evaluate("__voipStackPopup.binding")
    assert saved.pop("binding_id").startswith("browser-")
    assert saved == {"device_id": "desk-device", "endpoint_id": "browser:desk", "enabled": True}
    incoming(page, "incoming-after-reopen")
    assert page.evaluate("__voipStackPopup.state.call_id") == "incoming-after-reopen"
    assert page.evaluate("mics") == 0


def test_optout_keeps_phone_and_survives_restart_pageshow_and_reload(popup_page):
    page = popup_page
    enable(page)
    page.evaluate("__voipStackPopup.bindPhone(phone,false)")
    expected = {"device_id": "desk-device", "endpoint_id": "browser:desk", "enabled": False,
        "binding_id": page.evaluate("__voipStackPopup.binding.binding_id")}
    assert page.evaluate("JSON.parse(localStorage.voip_stack_popup_phone)") == expected
    page.evaluate("hass.connection.dispatchEvent(new Event('ready'));window.dispatchEvent(new PageTransitionEvent('pageshow'))")
    page.wait_for_timeout(40)
    page.evaluate("push({state:'ringing',direction:'incoming',call_id:'disabled-call'})")
    assert page.evaluate("!__voipStackPopup.dialog?._dialog.open")
    assert page.evaluate("!__voipStackPopup._unsubscribe")
    assert page.evaluate("services") == []
    page.reload()
    page.evaluate(BOOTSTRAP)
    page.wait_for_function("!!globalThis.__voipStackPopup")
    assert page.evaluate("__voipStackPopup.binding") == expected
    assert page.evaluate("streams.length") == 0
    page.evaluate("""() => {
      const settings=document.createElement('voip-stack-popup-settings');
      root.shadowRoot.append(settings);settings.hass=hass;window.settings=settings;
    }""")
    page.wait_for_function("settings._select.options.length===2")
    assert page.evaluate("settings._select.value") == "desk-device"
    assert page.evaluate("settings._enabled.checked") is False


def test_other_tab_optout_wins_over_pending_reconnect_resolution(popup_page):
    page = popup_page
    enable(page)
    other = page.context.new_page()
    def serve(route):
        name = route.request.url.split('/')[-1].split('?')[0]
        path = FRONT / name
        route.fulfill(content_type='text/javascript' if path.is_file() else 'text/html',
                      body=path.read_text() if path.is_file() else '<html><body></body></html>')
    other.route('http://phone.test/**', serve)
    other.goto('http://phone.test/')
    other.evaluate(BOOTSTRAP)
    other.wait_for_function("!!__voipStackPopup._unsubscribe")
    page.evaluate("""() => {
      window.oldPreference=localStorage.voip_stack_popup_phone;
      const original=hass.callWS;window.lateResolves=[];
      hass.callWS=request=>request.type==='voip_stack/list_devices'
        ? new Promise(resolve=>{lateResolves.push(resolve);}) : original(request);
      hass.connection.dispatchEvent(new Event('ready'));
    }""")
    page.wait_for_function("lateResolves.length>0")
    other.evaluate("__voipStackPopup.bindPhone(phone,false)")
    page.wait_for_function("__voipStackPopup.binding?.enabled===false")
    page.evaluate("""() => {
      for(const resolve of lateResolves.splice(0))resolve({devices:[phone]});
      window.dispatchEvent(new StorageEvent('storage', {key:'voip_stack_popup_phone',
        newValue:oldPreference,storageArea:localStorage}));
      push({state:'ringing',direction:'incoming',call_id:'late-ring'});
      window.dispatchEvent(new PageTransitionEvent('pageshow'));
    }""")
    page.wait_for_timeout(50)
    assert page.evaluate("__voipStackPopup.binding.enabled") is False
    assert page.evaluate("!__voipStackPopup._unsubscribe && !__voipStackPopup.dialog?._dialog.open")
    assert page.evaluate("services") == []
    other.close()


def test_disabled_popup_does_not_block_scoped_notification_on_mounted_card(popup_page):
    page = popup_page
    page.evaluate("""async () => {
      await import('/voip-stack-card.js');
      await __voipStackPopup.bindPhone(phone,false);
      const card=document.createElement('voip-stack-card');
      card.setConfig({mode:'ha_softphone',device_id:phone.device_id});card.hass=hass;
      root.shadowRoot.append(card);window.visibleCard=card;
      window.prepares=0;
      __voipStackEngine.prepareAudioCall=()=>{prepares++;return new Promise(resolve=>{window.allow=resolve;});};
      window.link=id=>{history.pushState({},'', '/?voip_answer=1&voip_endpoint=browser%3Adesk&voip_call_id='+id);
        window.dispatchEvent(new CustomEvent('location-changed'));};
    }""")
    page.wait_for_function("visibleCard._softphoneSnapshot?.endpoint_id===phone.endpoint_id")
    page.evaluate("push({state:'ringing',direction:'incoming',call_id:'mounted-A'});link('stale-A')")
    assert page.evaluate("prepares") == 0
    page.evaluate("link('mounted-A')")
    page.wait_for_function("prepares===1 && !!allow")
    assert page.evaluate("services") == []  # No backend answer before media preparation.
    page.evaluate("allow()")
    page.wait_for_function("services.some(s=>s.service==='answer')")
    assert page.evaluate("services.filter(s=>s.service==='answer')[0].data.call_id") == "mounted-A"
    page.evaluate("push({state:'idle',call_id:'mounted-A'});link('mounted-A')")
    assert page.evaluate("services.filter(s=>s.service==='answer').length") == 1
    page.wait_for_function("!__voipStackEngine.mediaIntent && !__voipStackEngine.mediaCleanupPending")
    page.evaluate("""() => {
      __voipStackEngine.prepareAudioCall=async()=>{prepares++;};
      push({state:'ringing',direction:'incoming',call_id:'mounted-B'});link('mounted-B');
    }""")
    page.wait_for_function("services.filter(s=>s.service==='answer').length===2")
    assert page.evaluate("services.filter(s=>s.service==='answer')[1].data.call_id") == "mounted-B"
    assert page.evaluate("__voipStackPopup.binding.enabled") is False


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


def test_sip_popup_binding_is_server_declared_and_stable_without_media(popup_page):
    page = popup_page
    assert page.evaluate("bindingRequests") == []
    page.evaluate("phone={...phone,softphone:false,kind:'sip_account',ha_client_supported:true}")
    enable(page)
    binding = page.evaluate("JSON.parse(localStorage.getItem('voip_stack_popup_phone'))")
    assert binding["binding_id"].startswith("browser-")
    assert page.evaluate("popupBindings[__voipStackPopup.binding.binding_id].enabled") is True
    writes = page.evaluate("bindingRequests.filter(r=>r.type==='voip_stack/set_ha_popup_binding').length")
    page.evaluate("hass.connection.dispatchEvent(new Event('ready'))")
    page.wait_for_function("__voipStackPopup._bindingServerSynced!==null")
    assert page.evaluate("__voipStackPopup.binding.binding_id") == binding["binding_id"]
    assert page.evaluate("bindingRequests.filter(r=>r.type==='voip_stack/set_ha_popup_binding').length") == writes
    assert page.evaluate("mics") == 0


def test_legacy_disabled_preference_never_inherits_server_optin(popup_page):
    page = popup_page
    page.evaluate("""() => {
      localStorage.setItem('voip_stack_popup_phone',JSON.stringify({device_id:phone.device_id,
        endpoint_id:phone.endpoint_id,enabled:false}));
      __voipStackPopup.syncPreference(true);
    }""")
    page.wait_for_function("__voipStackPopup._bindingServerSynced!==null")
    assert page.evaluate("__voipStackPopup.binding.enabled") is False
    assert page.evaluate("popupBindings[__voipStackPopup.binding.binding_id].enabled") is False
    assert page.evaluate("!__voipStackPopup._unsubscribe && mics===0")


def test_server_popup_optout_is_respected_on_reconnect(popup_page):
    page = popup_page
    enable(page)
    page.evaluate("""() => {
      popupBindings[__voipStackPopup.binding.binding_id].enabled=false;
      hass.connection.dispatchEvent(new Event('ready'));
    }""")
    page.wait_for_function("__voipStackPopup.binding.enabled===false")
    assert page.evaluate("!__voipStackPopup._unsubscribe")
    assert page.evaluate("popupBindings[__voipStackPopup.binding.binding_id].enabled") is False


def test_sip_call_is_shared_observation_without_browser_capture(popup_page):
    page = popup_page
    page.evaluate("""async () => {
      phone={...phone,softphone:false,kind:'sip_account',ha_client_supported:true};
      await import('/voip-stack-card.js');
      const card=window.observerCard=document.createElement('voip-stack-card');
      card.setConfig({mode:'ha_softphone',device_id:phone.device_id});card.hass=hass;
      root.shadowRoot.append(card);
    }""")
    page.wait_for_function("observerCard._softphoneSnapshot?.endpoint_id===phone.endpoint_id")
    page.evaluate("push({state:'in_call',direction:'outgoing',call_id:'native-call',can_attach_media:false,media_is_local:false,media_owner:'other'})")
    page.wait_for_function("observerCard._softphoneSnapshot?.call_id==='native-call'")
    assert page.evaluate("mics===0 && !__voipStackEngine.active")
    assert page.get_by_role("button", name="Hang up call", exact=True).is_visible()
