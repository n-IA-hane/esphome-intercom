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
def test_audio_call_details_keep_runtime_labels_and_version_in_options(card_page, detail, tmp_path):
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
    expect(page.locator(".audio-call-summary .call-peer")).to_have_text("Cucina")
    expect(page.locator(".audio-call-summary .call-peer")).to_be_visible()
    expect(page.locator(".audio-call-summary .call-duration")).to_be_visible()
    if detail == "version":
        expect(page.locator(".version")).not_to_be_visible()
        page.get_by_role("button", name="Opzioni", exact=True).click()
        expect(page.locator(".version")).to_be_visible()
    else:
        expect(page.get_by_role("button", name="Tastierino", exact=True).locator(".settings-label")).to_be_visible()
        expect(page.get_by_role("button", name="Opzioni", exact=True).locator(".settings-label")).to_be_visible()
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
        expect(page.locator(".audio-call-summary .call-peer")).to_have_text(identity)


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
    number = page.get_by_role("textbox", name="Number, name or SIP URI")
    expect(number).to_be_focused()
    digit = page.get_by_role("button", name="2", exact=True)
    digit.focus()
    digit.press("Space")
    expect(digit).to_be_focused()
    expect(page.get_by_role("textbox", name="Number, name or SIP URI")).to_have_value("2")
    page.keyboard.press("Tab")
    following = page.get_by_role("button", name="3", exact=True)
    expect(following).to_be_focused()
    following.press("Enter")
    expect(following).to_be_focused()
    expect(page.get_by_role("textbox", name="Number, name or SIP URI")).to_have_value("23")
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
