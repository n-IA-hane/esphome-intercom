"""Real browser controls preserve keyboard focus and identify mailbox messages."""

from pathlib import Path
import re
import shutil

import pytest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "custom_components/voip_stack/frontend"
pytestmark = [pytest.mark.js_runtime, pytest.mark.browser_real]


@pytest.fixture
def card_page():
    playwright = pytest.importorskip("playwright.sync_api")
    if not shutil.which("chromium"):
        pytest.skip("Chromium is unavailable")
    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(executable_path=shutil.which("chromium"), headless=True)
        page = browser.new_page(viewport={"width": 360, "height": 800}, timezone_id="UTC")

        def serve(route):
            name = route.request.url.split("/")[-1].split("?")[0]
            path = FRONTEND / name
            route.fulfill(
                content_type="text/javascript" if path.is_file() else "text/html",
                body=path.read_text() if path.is_file() else "<html><body></body></html>",
            )

        page.route("http://card.test/**", serve)
        page.goto("http://card.test/")
        page.evaluate("""async () => {
          await import('/voip-stack-card.js');
          const card=window.card=document.createElement('voip-stack-card');
          card.style.cssText='display:block;width:340px';
          card.config={mode:'ha_softphone',device_id:'desk-device',name:'Ingresso'};
          card._activeDeviceInfo={device_id:'desk-device',endpoint_id:'desk',name:'Ingresso'};
          card._softphoneSnapshot={state:'idle',enabled:true,call_id:'',device_id:'desk-device'};
          card._voipStateEntityId='sensor.desk_state';
          card._destinationEntityId='sensor.desk_destination';
          card._hass={language:'it',user:{is_admin:true},config:{location_name:'Casa'},states:{
            'sensor.desk_state':{state:'idle'},'sensor.desk_destination':{state:'Cucina'}}};
          document.body.append(card);
        }""")
        try:
            yield page
        finally:
            browser.close()


def test_italian_card_commands_match_dashboard_labels(card_page):
    from playwright.sync_api import expect

    page = card_page
    expect(page.get_by_role("button", name="Chiama", exact=True)).to_be_visible()
    assert page.locator("voip-stack-card").evaluate("el => el.shadowRoot.querySelectorAll('.video-call').length") == 0
    expect(page.get_by_role("button", name="Opzioni", exact=True)).to_be_visible()
    shared = page.evaluate("""async () => {
      const {dashboardTranslate}=await import('/voip-stack-dashboard-model.js');
      const {voipStackTranslate}=await import('/voip-stack-i18n.js');
      return ['Call','Answer','Decline','Hang up'].map(label=>[
        voipStackTranslate(card._hass,label),dashboardTranslate('it',label)]);
    }""")
    assert shared == [["Chiama", "Chiama"], ["Rispondi", "Rispondi"],
                      ["Rifiuta", "Rifiuta"], ["Riaggancia", "Riaggancia"]]
    page.evaluate("""() => {
      card._softphoneSnapshot={...card._softphoneSnapshot,state:'ringing',call_id:'offer',caller:'Portone'};
      card._render();
    }""")
    expect(page.get_by_role("button", name="Rispondi", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Rifiuta", exact=True)).to_be_visible()
    page.evaluate("""() => {
      card._softphoneSnapshot={...card._softphoneSnapshot,state:'in_call'};
      card._render();
    }""")
    expect(page.get_by_role("button", name="Riaggancia chiamata", exact=True)).to_be_visible()


@pytest.mark.parametrize("detail", ["version", "labels"])
def test_audio_call_details_keep_accessible_icons_and_version_in_options(card_page, detail, tmp_path):
    from playwright.sync_api import expect

    page = card_page
    page.evaluate("""() => {
      document.body.style.cssText='margin:12px;background:#142234;font-family:Arial';
      document.documentElement.style.cssText='--primary-text-color:#eef3fa;--secondary-text-color:#acb8ca;--ha-card-background:rgba(20,31,44,.6);--divider-color:rgba(190,210,230,.22)';
      card._softphoneStateLoaded=true;
      card._softphoneSnapshot={...card._softphoneSnapshot,state:'in_call',call_id:'audio-details',
        peer_name:'Cucina',connected_at:Date.now()/1000-12};card._render();
    }""")
    page.locator("voip-stack-card").screenshot(path=str(tmp_path / "audio-details.png"))
    expect(page.get_by_role("status")).to_contain_text("Cucina")
    expect(page.locator(".audio-call-summary")).to_have_text(re.compile(r"^\d+:\d{2}$"))
    expect(page.locator(".audio-call-summary .call-duration")).to_be_visible()
    if detail == "version":
        expect(page.locator(".version")).not_to_be_visible()
        page.get_by_role("button", name="Opzioni", exact=True).click()
        expect(page.locator(".version")).to_be_visible()
    else:
        expect(page.get_by_role("button", name="Tastierino", exact=True).locator(".settings-label")).to_be_hidden()
        expect(page.get_by_role("button", name="Opzioni", exact=True).locator(".settings-label")).to_be_hidden()
    page.evaluate("""() => {
      card.config.mode='esp_mirror';card._hass.states['sensor.desk_state'].state='in_call';
      card._mirroredCallId='physical-call';card._render();
    }""")
    expect(page.locator(".microphone-control")).not_to_be_visible()
    page.locator("voip-stack-card").screenshot(path=str(tmp_path / "mirror-details.png"))
    page.evaluate("card._hass.states['sensor.desk_state'].state='unavailable';card._render()")
    expect(page.locator(".audio-call-summary")).not_to_be_visible()


@pytest.mark.parametrize(("phase", "identity", "expected"), [
    ("calling", "Answer", "Chiamata a Answer..."),
    ("in_call", "Reception@Hotel", "In chiamata con Reception@Hotel"),
    ("ended", "*42 $& {name}", "Chiamata con *42 $& {name} terminata."),
])
def test_italian_call_messages_preserve_literal_counterparty_names(card_page, phase, identity, expected):
    from playwright.sync_api import expect

    page = card_page
    page.evaluate("""({phase,identity}) => {
      card._softphoneStateLoaded=true;
      card._softphoneSnapshot={...card._softphoneSnapshot,state:phase==='ended'?'idle':phase,
        call_id:'literal-name',direction:'outgoing',peer_name:identity,callee:identity};
      if(phase==='ended')card._captureEndReason('terminal','remote_hangup','remote',identity);
      card._render();
    }""", {"phase": phase, "identity": identity})
    expect(page.get_by_role("status")).to_have_text(expected)
    if phase == "in_call":
        expect(page.locator(".audio-call-summary")).to_be_hidden()


@pytest.mark.parametrize("mode", ["ha_softphone", "esp_mirror", "ha_transfer"])
def test_keypad_activation_retains_button_focus_and_tab_order(card_page, mode):
    from playwright.sync_api import expect

    page = card_page
    page.evaluate("""mode => {
      card.config.mode=mode==='esp_mirror' ? mode : 'ha_softphone';
      card._startCallService='call';
      if(mode==='ha_transfer') {
        card._softphoneSnapshot={...card._softphoneSnapshot,state:'in_call',call_id:'transfer-call'};
        card._transferDraft={callId:'transfer-call',keypad:false,manual:''};
      }
      card._render();
    }""", mode)
    page.get_by_role("button", name="Tastierino", exact=True).click()
    number = page.get_by_role("textbox", name="Numero, nome o URI SIP")
    expect(number).to_be_focused()
    digit = page.get_by_role("button", name="2", exact=True)
    digit.focus()
    digit.press("Space")
    expect(digit).to_be_focused()
    expect(page.get_by_role("textbox", name="Numero, nome o URI SIP")).to_have_value("2")
    page.keyboard.press("Tab")
    following = page.get_by_role("button", name="3", exact=True)
    expect(following).to_be_focused()
    following.press("Enter")
    expect(following).to_be_focused()
    expect(page.get_by_role("textbox", name="Numero, nome o URI SIP")).to_have_value("23")
    page.get_by_role("button", name="4", exact=True).click()
    assert page.evaluate("card.shadowRoot.activeElement !== card._els.keypadInput")
    number.fill("sip:cucina@example.test")
    assert page.evaluate("card._manualTarget()") == "sip:cucina@example.test"


def test_mailbox_actions_identify_caller_and_localized_date(card_page):
    from playwright.sync_api import expect

    page = card_page
    page.evaluate("""() => {
      card._softphoneSnapshot.voicemail={enabled:true,total:2};
      card._mailboxMessages=[
        {id:'first',caller:'Portone',created_at:1704110400,read:false},
        {id:'second',caller:'Portone',created_at:1704196800,read:true}];
      card._mailboxOpen=true;card._mailboxOffset=2;card._mailboxTotal=2;
      card._renderMailbox();card._render();
    }""")
    # Intl locale data may pad the day/month; retain independent fixed dates.
    for day in [1, 2]:
        date = rf"0?{day}\/0?1\/2024, 12:00:00"
        for action in ["Ascolta", "Elimina"]:
            name = re.compile(rf"^{action}: Portone, {date}$")
            expect(page.get_by_role("button", name=name)).to_be_visible()


def test_paging_options_save_existing_groups_once_and_clear_memberships(card_page):
    from playwright.sync_api import expect

    page = card_page
    page.evaluate("""() => {
      window.saves=[]; window.loads=0;
      card._softphoneSnapshot={...card._softphoneSnapshot,endpoint_id:'desk',paging_groups:['office']};
      card._rosterEntries=[
        {id:'paging:office',name:'Ufficio',enabled:true,metadata:{virtual_endpoint:'paging',paging_group_id:'office'}},
        {id:'paging:empty',name:'Ingresso',enabled:false,metadata:{virtual_endpoint:'paging',paging_group_id:'empty'}},
        {id:'queue:support',name:'Supporto',metadata:{group_type:'queue'}}];
      card._hass.services={voip_stack:{set_phone_memberships:{}}};
      card._hass.callService=async(domain,service,data)=>{
        saves.push({domain,service,data});
        card._softphoneSnapshot={...card._softphoneSnapshot,paging_groups:[...data.value]};
      };
      card._loadSoftphoneState=async()=>{loads++;};
      card._render();
    }""")
    page.get_by_role("button", name="Opzioni", exact=True).click()
    groups = page.get_by_role("listbox", name="Gruppi annunci", exact=True)
    expect(groups).to_be_visible()
    assert groups.locator("option").all_text_contents() == ["Ingresso (Non disponibile)", "Ufficio"]
    assert groups.evaluate("el=>Array.from(el.selectedOptions,o=>o.value)") == ["office"]
    groups.select_option(["office", "empty"])
    expect(groups).to_be_enabled()
    assert page.evaluate("saves") == [{"domain": "voip_stack", "service": "set_phone_memberships",
        "data": {"phone": "desk", "field": "paging_groups", "value": ["empty", "office"]}}]
    assert page.evaluate("loads") == 1
    page.get_by_role("button", name="Rimuovi da tutti i gruppi annunci", exact=True).click()
    expect(page.get_by_role("button", name="Rimuovi da tutti i gruppi annunci", exact=True)).to_be_disabled()
    assert page.evaluate("saves[1].data.value") == []
    assert page.evaluate("loads") == 2
    assert groups.evaluate("el=>el.selectedOptions.length") == 0


def test_paging_options_require_admin_and_supported_server_in_esp_mirror(card_page):
    from playwright.sync_api import expect

    page = card_page
    page.evaluate("""async () => {
      const {voipStackEngine}=await import('/voip-stack-engine.js');
      window.engine=voipStackEngine;
      card.config.mode='esp_mirror';
      card._hass.services={voip_stack:{set_phone_memberships:{}}};
      card._settingsOpen=true;
      card._render();
    }""")
    groups = page.get_by_role("listbox", name="Gruppi annunci", exact=True)
    expect(groups).to_be_hidden()
    page.evaluate("""() => {
      engine._phoneStates.set('esp:desk',{endpoint_id:'esp:desk',device_id:'desk-device',paging_groups:[]});
      engine._phoneStateRevision=1;engine._phoneStateFailed=false;
      card._rosterEntries=[{id:'paging:office',name:'Ufficio',metadata:{virtual_endpoint:'paging',paging_group_id:'office'}}];
      window.saves=[];
      card._hass.callService=async(domain,service,data)=>{saves.push(data);};
      card._render();
    }""")
    expect(groups).to_be_visible()
    groups.select_option(["office"])
    assert page.evaluate("saves") == [{"phone": "desk-device", "field": "paging_groups", "value": ["office"]}]
    page.evaluate("""() => {card._hass.user.is_admin=false;card._render();}""")
    expect(groups).to_be_hidden()
    page.evaluate("""async () => {await card._setGroupSetting('paging_groups',['office']);}""")
    assert page.evaluate("saves.length") == 1
    page.evaluate("""() => {engine._phoneStates.clear();}""")


def test_first_video_frame_moves_call_details_to_one_reachable_bar(card_page, tmp_path):
    from playwright.sync_api import expect

    page = card_page
    page.evaluate("""async () => {
      const {voipStackEngine: engine} = await import('/voip-stack-engine.js?v=dev');
      const id = 'video-toolbar';
      engine._endpointId = 'desk';
      engine._callId = id;
      engine.claimSoftphoneSession(id, 'desk');
      window.actions = [];
      card._hangup = () => actions.push('hangup');
      card._hass.callWS = async request => { actions.push(request.service); return {}; };
      const video = await engine._loadVideo();
      video._callId = id;
      video._active = true;
      video._canReceive = true;
      card.config.name = 'VoIP Stack';
      card._softphoneStateLoaded = true;
      card._softphoneSnapshot = {...card._softphoneSnapshot, state:'in_call', call_id:id,
        endpoint_id:'desk', direction:'outgoing', peer_name:'Portone', can_attach_media:false,
        parking:{enabled:true},
        connected_at:Date.now()/1000-12};
      card._render();
    }""")
    header = page.locator(".header")
    status = page.get_by_role("status")
    audio_time = page.locator(".audio-call-summary")
    expect(header).to_have_text("VoIP Stack")
    expect(header).to_be_visible()
    expect(status).to_have_text("In chiamata con Portone")
    expect(audio_time).to_have_text(re.compile(r"^\d+:\d{2}$"))

    hangup = page.get_by_role("button", name="Riaggancia chiamata", exact=True)
    def audio_timer_gap():
        clock_box = audio_time.locator(".call-duration").bounding_box()
        return hangup.bounding_box()["y"] - (clock_box["y"] + clock_box["height"])

    assert 0 <= audio_timer_gap() <= 16
    page.locator("voip-stack-card").screenshot(path=str(tmp_path / "audio-timer.png"))
    mute = page.locator(".microphone-control")
    mute.click()
    expect(mute).to_have_attribute("aria-pressed", "true")
    expect(mute.locator(".settings-label")).to_be_hidden()
    expect(audio_time).not_to_have_text(audio_time.inner_text())

    page.evaluate("""async () => {
      const {voipStackEngine: engine} = await import('/voip-stack-engine.js?v=dev');
      const source = document.createElement('canvas');
      source.width = 32; source.height = 24;
      const bitmap = await createImageBitmap(source);
      engine._video._receiver._queueDecodedFrame({bitmap, displayWidth:32, displayHeight:24,
        timestamp:1000, close:() => bitmap.close()});
    }""")
    expect(page.locator(".video-canvas")).to_be_visible()
    expect(header).to_be_hidden()
    expect(page.locator(".status")).to_be_hidden()
    expect(page.locator(".status-reason")).to_be_hidden()
    expect(audio_time).to_be_hidden()
    expect(page.get_by_text("Portone", exact=True).filter(visible=True)).to_have_count(1)
    expect(page.locator(".hangup-peer")).to_have_text("Portone")
    expect(page.locator(".hangup-duration")).to_be_visible()
    expect(hangup).to_have_count(1)
    assert hangup.bounding_box()["width"] == 48
    page.locator("voip-stack-card").screenshot(path=str(tmp_path / "video-toolbar.png"))
    video_time = page.locator(".hangup-duration")
    # These labels intentionally do not receive pointer events. Click their
    # screen positions, as a user would, without waiting for actionable labels.
    for label in (page.locator(".hangup-peer"), video_time):
        box = label.bounding_box()
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        assert page.evaluate("actions") == []
    expect(video_time).not_to_have_text(video_time.inner_text())
    handset = hangup.bounding_box()
    page.mouse.click(handset["x"] + handset["width"] + 2, handset["y"] + handset["height"] / 2)
    assert page.evaluate("actions") == []

    expect(mute).to_have_attribute("aria-pressed", "true")
    mute.click()
    expect(mute).to_have_attribute("aria-pressed", "false")
    keypad = page.locator("button[aria-controls=voip-keypad-panel]")
    keypad.click()
    expect(keypad).to_have_attribute("aria-expanded", "true")
    keypad.click()
    expect(keypad).to_have_attribute("aria-expanded", "false")
    options = page.locator("button[aria-controls=voip-settings-panel]")
    options.click()
    expect(options).to_have_attribute("aria-expanded", "true")
    options.click()
    expect(options).to_have_attribute("aria-expanded", "false")
    park = page.get_by_role("button", name="Parcheggia chiamata", exact=True)
    park.click()
    assert page.evaluate("actions") == ["park"]
    page.evaluate("card._softphoneSnapshot.video_active = true; card._render()")
    expect(park).to_be_disabled()
    box = park.bounding_box()
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    assert page.evaluate("actions") == ["park"]
    hangup.click()
    assert page.evaluate("actions") == ["park", "hangup"]

    page.evaluate("""async () => {
      const {voipStackEngine: engine} = await import('/voip-stack-engine.js?v=dev');
      engine._video._active = false;
      engine._video._emit();
    }""")
    expect(page.locator(".video-canvas")).to_be_hidden()
    expect(header).to_be_visible()
    expect(status).to_have_text("In chiamata con Portone")
    expect(audio_time).to_be_visible()
    expect(audio_time).to_have_text(re.compile(r"^\d+:\d{2}$"))
    assert 0 <= audio_timer_gap() <= 16


def test_destination_picker_migrates_contacts_and_dials_only_public_addresses(card_page):
    from playwright.sync_api import expect

    page = card_page
    page.evaluate("""async () => {
      const {voipStackEngine: engine} = await import('/voip-stack-engine.js?v=dev');
      const contacts = [
        {id:'record-one', name:'Desk @ West', metadata:{device_id:'registry-one'}},
        {id:'record-two', extension:426, metadata:{device_id:'registry-two'}},
        {id:'record-three', name:' ', number:'+390123'},
        {id:'record-four', sip_uri:'sip:door@example.test'},
        {id:'record-five'},
      ];
      card._sharedEntityIds = {phonebook:'sensor.roster'};
      window.updateRoster = entries => {
        card._hass.states['sensor.roster'] = {attributes:{roster_json:JSON.stringify(entries)}};
        card._loadSharedRoster();
        card._render();
      };
      window.contacts = contacts;
      window.calls = []; window.transfers = [];
      localStorage.setItem('voip_softphone_target_desk-device', 'registry-two');
      card._loadSoftphoneTargetPreference();
      updateRoster(contacts);
      engine.prepareAudioCall = async () => {};
      engine.startHaSoftphone = async destination => { calls.push(destination); };
      card._hass.callWS = async request => {
        if (request.service === 'transfer') transfers.push(request.service_data);
        return {response:{success:true}};
      };
    }""")
    picker = page.locator(".destination-select")
    expect(picker).to_have_value("426")
    assert picker.locator("option").evaluate_all("options => options.map(o => [o.value, o.text])") == [
        [address, address] for address in ("Desk @ West", "426", "+390123", "sip:door@example.test")
    ]
    assert page.evaluate("localStorage.getItem('voip_softphone_target_desk-device')") is None
    saved = "voip_softphone_destination_desk-device"
    assert page.evaluate("key => localStorage.getItem(key)", saved) == "426"

    picker.select_option("Desk @ West")
    await_start = "card._startHaSoftphoneCall({},card._callOperationId)"
    page.evaluate(await_start)
    assert page.evaluate("calls") == ["Desk @ West"]
    assert page.evaluate("key => localStorage.getItem(key)", saved) == "Desk @ West"
    page.evaluate("""() => {
      card._softphoneSnapshot = {...card._softphoneSnapshot, state:'in_call',
        call_id:'transfer-1', endpoint_id:'desk', peer_name:'Current call'};
      card._toggleTransfer();
    }""")
    picker.select_option("sip:door@example.test")
    page.evaluate("card._submitTransfer()")
    assert page.evaluate("transfers") == [
        {"device_id": "desk-device", "call_id": "transfer-1", "destination": "sip:door@example.test"}
    ]
    assert page.evaluate("key => localStorage.getItem(key)", saved) == "Desk @ West"

    page.evaluate("""() => {
      card._softphoneSnapshot = {...card._softphoneSnapshot, state:'idle', call_id:''};
      updateRoster(contacts.slice(1));
    }""")
    expect(picker).to_have_value("")
    expect(page.get_by_role("button", name="Chiama", exact=True)).to_be_disabled()
    assert page.evaluate(await_start + ".catch(error => error.message)") == "No endpoint available"
    assert page.evaluate("calls") == ["Desk @ West"]

    page.evaluate("card._softphoneKeypadOpen = true; card._softphoneManualTarget = ''")
    assert page.evaluate(await_start + ".catch(error => error.message)") == "No destination entered"
    page.evaluate("card._setManualTarget('*42');" + await_start)
    assert page.evaluate("calls") == ["Desk @ West", "*42"]
