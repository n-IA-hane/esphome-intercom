"""Operator dashboard projections, real DOM controls and responsive layout."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "custom_components/voip_stack/frontend"
MODEL = FRONTEND / "voip-stack-dashboard-model.js"
pytestmark = pytest.mark.js_runtime


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is unavailable")
def test_dashboard_projection_accepts_restarted_snapshots_and_rejects_stale_deltas():
    script = f"""
import assert from 'node:assert/strict';
import {{DashboardPhoneStore,dashboardCanCall,dashboardPickupCandidate,dashboardSections,phonePresentation,distinctCalls,dashboardColumnCount}} from {json.dumps(MODEL.as_uri())};
const store=new DashboardPhoneStore();
const owner={{endpoint_id:'desk',device_id:'desk-device',name:'Desk',kind:'browser',state:'idle',availability:'available',calls:[]}};
const ringing={{endpoint_id:'kitchen',device_id:'kitchen-device',name:'Kitchen',state:'ringing',availability:'available',calls:[{{id:'ring',generation:3,state:'early',direction:'incoming',pickup_available:true,peer_name:'Door'}}]}};
assert.equal(store.apply({{kind:'delta',revision:1,phones:[owner]}}),false);
assert.equal(store.apply({{kind:'snapshot',revision:0,phones:[owner,ringing]}}),true);
assert.equal(store.apply({{kind:'delta',revision:5,phones:[{{...owner,state:'in_call'}}],removed:['kitchen']}}),true);
assert.equal(store.apply({{kind:'delta',revision:4,phones:[owner]}}),false);
assert.equal(store.phones.get('desk').state,'in_call');
assert.equal(store.apply({{kind:'snapshot',revision:0,phones:[owner,ringing]}}),true);
assert.equal(store.phones.get('desk').state,'idle');
assert.equal(store.apply({{kind:'delta',revision:0,removed:['desk']}}),false);
assert.equal(dashboardCanCall(owner,{{...ringing,state:'idle'}}),true);
assert.equal(dashboardCanCall({{...owner,state:'in_call'}},{{...ringing,state:'idle'}}),false);
assert.equal(dashboardCanCall(owner,ringing),false);
assert.equal(dashboardPickupCandidate(owner,ringing).id,'ring');
assert.equal(dashboardPickupCandidate({{...owner,state:'in_call'}},ringing),null);
assert.equal(dashboardPickupCandidate(owner,{{...ringing,calls:[...ringing.calls,{{...ringing.calls[0],id:'second'}}]}}),null);
assert.equal(distinctCalls({{calls:[...ringing.calls,{{...ringing.calls[0],leg_id:'other'}}]}}).length,1);
const sections=dashboardSections(store.phones,[{{id:'local',name:'Kitchen',metadata:{{endpoint_id:'kitchen'}}}},{{id:'desk-device',name:'Owner legacy'}},{{id:'owner-meta',name:'Owner',metadata:{{device_id:'desk-device'}}}},{{id:'owner-top',name:'Owner',endpoint_id:'desk'}},{{id:'support',name:'Support',number:'+12025550108'}}],'desk-device');
assert.equal(sections.owner.device_id,'desk-device');
assert.equal(sections.owner.owner,true);
assert.equal(sections.internals.some(x=>x.endpoint_id==='desk'),false);
assert.equal(store.phones.has('desk'),true);
assert.equal(sections.contacts.length,1);
assert.equal(sections.contacts[0].state,'unknown');
assert.equal(dashboardSections(new Map(),[{{id:'desk-device',name:'Owner'}}],'desk-device').contacts.length,0);
const swapped=dashboardSections(store.phones,[],'kitchen-device');
assert.equal(swapped.owner.endpoint_id,'kitchen');
assert.equal(swapped.internals.some(x=>x.endpoint_id==='desk'),true);
assert.equal(swapped.internals.some(x=>x.endpoint_id==='kitchen'),false);
const unconfigured=dashboardSections(new Map([['sip',{{endpoint_id:'sip',device_id:'',name:'SIP phone'}}]]),[],'');
assert.equal(unconfigured.owner,null);
assert.equal(unconfigured.internals.length,1);
assert.equal(phonePresentation({{state:'in_call',calls:[{{id:'one',held:true}},{{id:'two',state:'confirmed'}}]}}).attended,false);
assert.equal(phonePresentation({{state:'idle',availability:'offline'}}).visual,'offline');
assert.deepEqual([300,400,800,1200,1600].map(dashboardColumnCount),[1,2,3,4,5]);
assert.equal(store.apply({{kind:'error',error:'disconnected'}}),true);
assert.equal(store.ready,false);
assert.equal(store.apply({{kind:'reset'}}),true);
assert.equal(store.phones.size,0);
"""
    result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is unavailable")
def test_dashboard_semantic_icons_follow_actual_metadata_and_keep_availability_authoritative():
    script = f"""
import assert from 'node:assert/strict';
import {{dashboardIconFor,dashboardTypeFor,dashboardSections,dashboardCanCall}} from {json.dumps(MODEL.as_uri())};
for(const [kind,icon] of [['browser','mdi:home-assistant'],['ha_softphone','mdi:home-assistant'],['esphome','mdi:deskphone'],['sip_account','mdi:phone-classic'],['contact','mdi:contacts']]){{
  for(const state of ['idle','ringing','in_call','unavailable']) assert.equal(dashboardIconFor({{kind,state}}),icon);
}}
assert.equal(dashboardIconFor({{metadata:{{virtual_endpoint:'automation'}}}}),'mdi:robot');
assert.equal(dashboardIconFor({{metadata:{{virtual_endpoint:'assist_pipeline'}}}}),'mdi:account-voice');
assert.equal(dashboardIconFor({{metadata:{{group_type:'ring'}}}}),'mdi:phone-ring');
assert.equal(dashboardIconFor({{metadata:{{group_type:'conference'}}}}),'mdi:account-group');
assert.equal(dashboardIconFor({{kind:'esphome',icon:'mdi:doorbell-video',metadata:{{icon:'mdi:robot'}}}}),'mdi:doorbell-video');
assert.equal(dashboardIconFor({{kind:'esphome',icon:'javascript:bad',metadata:{{icon:'mdi:doorbell'}}}}),'mdi:doorbell');
for(const icon of ['mdi:','mdi:bad icon','mdi:foo<script>','<svg>','https://icons.invalid/icon.svg'])
  assert.equal(dashboardIconFor({{kind:'esphome',icon}}),'mdi:deskphone');
assert.equal(dashboardTypeFor({{kind:'contact',metadata:{{virtual_endpoint:'automation'}}}}),'Automation');
const owner={{endpoint_id:'owner',device_id:'owner-device',kind:'browser',state:'idle',availability:'available'}};
const phones=new Map([['owner',owner],['esp',{{endpoint_id:'esp',device_id:'esp-device',kind:'esphome',state:'idle',availability:'offline'}}]]);
const roster=[{{id:'esp-device',metadata:{{device_id:'esp-device',icon:'mdi:doorbell'}}}},
  {{id:'gate',name:'Open gate',extension:'301',metadata:{{virtual_endpoint:'automation'}}}},
  {{id:'assist',name:'Assistant',extension:'302',metadata:{{virtual_endpoint:'assist_pipeline'}}}},
  {{id:'group',name:'House',metadata:{{group_type:'ring'}}}}];
const sections=dashboardSections(phones,roster,'owner-device');
assert.equal(sections.contacts.length,3);
assert.equal(dashboardIconFor(sections.internals[0]),'mdi:doorbell');
assert.equal(sections.internals[0].availability,'offline');
assert.equal(phones.get('esp').metadata,undefined);
for(const contact of sections.contacts){{
  assert.equal(contact.kind,'contact');assert.equal(contact.state,'unknown');
  assert.equal(contact.availability,'unknown');assert.equal(dashboardCanCall(owner,contact),true);
}}
const gate=sections.contacts.find(phone=>phone.id==='gate');
assert.equal(gate.extension,'301');assert.equal(gate.metadata.virtual_endpoint,'automation');
"""
    result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


ENGINE_STUB = """
const engine=new EventTarget();
Object.assign(engine,{actions:[],phoneSubscribers:new Set(),ownerSubscribers:new Set(),active:false,callId:'',endpointId:'',closed:0,mediaClientId:'dashboard-test-client-1234',
 configure(){},
 subscribePhoneStates(cb){this.phoneSubscribers.add(cb);if(window.fixture)queueMicrotask(()=>cb(window.fixture));return()=>this.phoneSubscribers.delete(cb)},
 subscribeSoftphoneState(cb,selector){this.ownerSubscribers.add(cb);this.ownerSelectors=(this.ownerSelectors||[]).concat(selector);return()=>this.ownerSubscribers.delete(cb)},
 async dashboardStartCall(owner,target){this.actions.push(['call',owner.device_id,target.name])},
 async dashboardAnswer(owner,snapshot){this.actions.push(['answer',owner.device_id,snapshot.call_id])},
 async dashboardTerminate(action,owner,snapshot){this.actions.push([action,owner.device_id,snapshot.call_id])},
 async dashboardPickup(owner,target,candidate){this.actions.push(['pickup',owner.device_id,target.device_id,candidate.id,candidate.generation])},
 async dashboardSyncOwner(){},close(){this.closed++},
 push(event){for(const cb of this.phoneSubscribers)cb(event)},pushOwner(event){for(const cb of this.ownerSubscribers)cb(event)}
});window.engine=engine;export const voipStackEngine=engine;
"""

FIXTURE = {
    "kind": "snapshot", "revision": 0, "phones": [
        {"endpoint_id": "reception", "device_id": "reception-device", "name": "Reception", "extension": "100", "kind": "esphome", "state": "idle", "availability": "available", "calls": [], "mailbox": {"waiting": True, "new": 2}},
        {"endpoint_id": "office", "device_id": "office-device", "name": "Office", "extension": "101", "kind": "sip_account", "state": "in_call", "availability": "available", "calls": [{"id": "support-call", "state": "confirmed", "direction": "outgoing", "peer_name": "Support"}, {"id": "held-call", "state": "confirmed", "held": True, "peer_name": "Customer"}]},
        {"endpoint_id": "kitchen", "device_id": "kitchen-device", "name": "Kitchen", "extension": "102", "kind": "esphome", "state": "ringing", "availability": "available", "calls": [{"id": "door-call", "generation": 7, "state": "early", "direction": "incoming", "peer_name": "Front Door", "pickup_available": True}]},
        {"endpoint_id": "living", "device_id": "living-device", "name": "Living Room", "extension": "103", "kind": "esphome", "state": "in_call", "availability": "available", "calls": [{"id": "living-workshop", "state": "confirmed", "peer_name": "Workshop", "peer_endpoint_id": "workshop"}]},
        {"endpoint_id": "workshop", "device_id": "workshop-device", "name": "Workshop", "extension": "104", "kind": "esphome", "state": "in_call", "availability": "available", "calls": [{"id": "living-workshop", "state": "confirmed", "peer_name": "Living Room", "peer_endpoint_id": "living"}]},
        {"endpoint_id": "bedroom", "device_id": "bedroom-device", "name": "Bedroom", "extension": "105", "kind": "browser", "state": "idle", "availability": "available", "dnd": True, "calls": []},
        {"endpoint_id": "garage", "device_id": "garage-device", "name": "Garage", "extension": "106", "kind": "esphome", "state": "unavailable", "availability": "offline", "calls": []},
        {"endpoint_id": "studio", "device_id": "studio-device", "name": "Studio", "extension": "107", "kind": "browser", "state": "ringing", "availability": "available", "calls": [{"id": "mario-call", "generation": 2, "state": "early", "direction": "incoming", "peer_name": "Mario Rossi", "pickup_available": True}]},
    ],
}


@pytest.fixture
def dashboard_page():
    if shutil.which("chromium") is None:
        pytest.skip("Chromium is unavailable")
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runner:
        browser = runner.chromium.launch(executable_path=shutil.which("chromium"), headless=True)
        page = browser.new_page(viewport={"width": 1480, "height": 1100}, has_touch=True)

        def serve(route):
            name = route.request.url.split("/")[-1].split("?")[0]
            path = FRONTEND / name
            if name == "voip-stack-engine.js":
                route.fulfill(content_type="text/javascript", body=ENGINE_STUB)
            elif name.endswith(".js") and path.is_file():
                route.fulfill(content_type="text/javascript", body=path.read_text())
            else:
                route.fulfill(content_type="text/html", body="<html><body></body></html>")

        page.route("http://dashboard.test/**", serve)
        page.goto("http://dashboard.test/")
        page.evaluate("""async fixture=>{
          window.fixture=fixture;
          document.body.style.cssText='margin:20px;background:linear-gradient(120deg,#0a1b2e,#263b51);font-family:Arial';
          document.documentElement.style.cssText='--primary-text-color:#eef3fa;--secondary-text-color:#acb8ca;--ha-card-background:rgba(20,31,44,.6);--divider-color:rgba(190,210,230,.22)';
          await import('/voip-stack-dashboard-card.js');
          window.ownerState={endpoint_id:'studio',device_id:'studio-device',state:'ringing',call_id:'mario-call',sequence:1,revision:1,caller:'+1 202-555-0184'};
          const devices=fixture.phones.filter(p=>['browser','esphome'].includes(p.kind)).map(p=>({...p,softphone:p.kind==='browser'}));
          const roster=[{id:'support',name:'Support desk',number:'+1 202-555-0108'},{id:'mobile',name:'Mobile contact',number:'+1 202-555-0142'},
            {id:'gate',name:'Open gate',extension:'301',metadata:{virtual_endpoint:'automation'}}];
          window.hass={language:'en',states:{'sensor.renamed_phonebook':{attributes:{roster_json:JSON.stringify({contacts:roster})}}},entities:{},
            connection:{sendMessagePromise:async()=>({devices,entity_ids:{phonebook:'sensor.renamed_phonebook'}})},
            callWS:async request=>window.ownerState};
          const card=document.createElement('voip-stack-dashboard-card');window.card=card;
          card.setConfig({type:'custom:voip-stack-dashboard-card',device_id:'studio-device'});
          card.style.cssText='display:block;width:1370px;height:920px';document.body.append(card);card.hass=hass;
        }""", FIXTURE)
        page.wait_for_function("card.shadowRoot.querySelectorAll('.tile').length===10")
        yield page
        browser.close()


@pytest.mark.browser_real
def test_dashboard_real_dom_header_details_and_shared_command_routing(dashboard_page):
    page = dashboard_page
    assert page.locator(".tile").count() == 10
    assert page.get_by_text("In call with Support", exact=True).is_visible()
    assert page.get_by_text("Customer on hold", exact=True).is_visible()
    assert page.locator(".transfer-line:visible").count() == 0
    assert page.locator(".owner-badge:visible").count() == 0
    assert page.locator(".owner-name").inner_text() == "Studio · 107"
    assert page.locator(".tile[data-key='endpoint:studio']").count() == 0
    assert page.get_by_role("button", name="Pick up: Kitchen", exact=True).is_disabled()
    page.get_by_role("button", name="Answer", exact=True).click()
    assert page.evaluate("engine.actions.at(-1)") == ["answer", "studio-device", "mario-call"]
    page.get_by_role("button", name="Decline", exact=True).click()
    assert page.evaluate("engine.actions.at(-1)") == ["decline", "studio-device", "mario-call"]
    page.get_by_role("button", name="Office, 101. SIP phone. In call with Support. Call details", exact=True).click()
    assert page.get_by_role("dialog").is_visible()
    assert page.locator(".details-row").count() == 2
    page.get_by_role("button", name="Close", exact=True).click()
    assert page.evaluate("engine.ownerSelectors") == [{"device_id": "studio-device"}]
    page.evaluate("card.remove()")
    assert page.evaluate("engine.closed") == 0
    assert page.evaluate("engine.phoneSubscribers.size") == 0
    assert page.evaluate("engine.ownerSubscribers.size") == 0


@pytest.mark.browser_real
def test_dashboard_delta_preserves_untouched_tile_dom(dashboard_page):
    page = dashboard_page
    page.evaluate("""() => {
      const tile = card.shadowRoot.querySelector('.tile[data-key="endpoint:reception"] .tile-name');
      window.untouchedLabel = tile.firstChild;
      const kitchen = fixture.phones.find(phone => phone.endpoint_id === 'kitchen');
      engine.push({kind:'delta',revision:1,phones:[{...kitchen,name:'Kitchen updated'}],removed:[]});
    }""")
    assert page.get_by_text("Kitchen updated", exact=True).is_visible()
    assert page.evaluate("""() => card.shadowRoot.querySelector(
      '.tile[data-key="endpoint:reception"] .tile-name').firstChild === window.untouchedLabel""")


@pytest.mark.browser_real
def test_dashboard_endpoint_icons_availability_and_automation_routing(dashboard_page):
    page = dashboard_page
    expected = {"reception": "mdi:deskphone", "office": "mdi:phone-classic", "bedroom": "mdi:home-assistant", "kitchen": "mdi:deskphone", "garage": "mdi:deskphone"}
    for endpoint, icon in expected.items():
        assert page.locator(f".tile[data-key='endpoint:{endpoint}'] .tile-icon > ha-icon").first.get_attribute("icon") == icon
    assert page.locator(".identity-icon ha-icon").get_attribute("icon") == "mdi:home-assistant"
    # HA renders the icon in live qualification; this fixture has no ha-icon definition.
    assert page.locator(".tile[data-key='endpoint:kitchen'] .ringing-bell").evaluate("node=>!node.hidden")
    assert page.locator(".tile[data-key='endpoint:reception'] .availability-icon").get_attribute("icon") == "mdi:check-circle"
    assert page.locator(".tile[data-key='endpoint:garage'] .availability-icon").get_attribute("icon") == "mdi:close-circle-outline"
    assert page.locator(".tile[data-key='endpoint:garage'] .tile-main").get_attribute("aria-label") == "Garage, 106. ESPHome phone. Offline. Call details"
    gate = page.locator(".tile[data-key='contact:gate']")
    assert gate.locator(".tile-icon > ha-icon").first.get_attribute("icon") == "mdi:robot"
    assert gate.locator(".availability-icon").is_hidden()
    assert gate.locator(".tile-main").get_attribute("aria-label") == "Open gate, 301. Automation. Unknown. Call details"
    assert page.locator(".tile[data-key='contact:mobile'] .availability-icon").is_hidden()
    page.evaluate("""()=>{const owner={...fixture.phones.find(x=>x.endpoint_id==='studio'),state:'idle',calls:[]};engine.push({kind:'delta',revision:1,phones:[owner],removed:[]})}""")
    gate.locator(".tile-main").click()
    assert page.evaluate("engine.actions.at(-1)") == ["call", "studio-device", "Open gate"]
    assert page.evaluate("card._tileData.get('contact:gate').metadata.virtual_endpoint") == "automation"


@pytest.mark.browser_real
def test_dashboard_call_pickup_and_revision_changes_keep_identity(dashboard_page):
    page = dashboard_page
    page.evaluate("""()=>{const owner={...fixture.phones.find(x=>x.endpoint_id==='studio'),state:'idle',calls:[]};engine.push({kind:'delta',revision:1,phones:[owner],removed:[]})}""")
    page.get_by_role("button", name="Reception, 100. ESPHome phone. Available. Voicemail: 2. Call", exact=True).click()
    assert page.evaluate("engine.actions.at(-1)") == ["call", "studio-device", "Reception"]
    page.get_by_role("button", name="Pick up: Kitchen", exact=True).click()
    assert page.evaluate("engine.actions.at(-1)") == ["pickup", "studio-device", "kitchen-device", "door-call", 7]
    page.evaluate("engine.push({kind:'error',error:'Connection interrupted'})")
    assert page.get_by_text("Connection interrupted", exact=True).is_visible()
    assert page.get_by_role("button", name="Pick up: Kitchen", exact=True).is_disabled()


@pytest.mark.browser_real
@pytest.mark.parametrize("width,columns", [(1370, 4), (1550, 5), (800, 3), (400, 2), (300, 1)])
def test_dashboard_responsive_grid_and_scroll(dashboard_page, width, columns, tmp_path):
    page = dashboard_page
    page.set_viewport_size({"width": max(width + 40, 420), "height": 1100})
    page.evaluate("""width=>{card.style.width=width+'px';card.style.height='700px'}""", width)
    page.wait_for_function("expected=>getComputedStyle(card.shadowRoot.querySelector('ha-card')).getPropertyValue('--dash-columns').trim()===String(expected)", arg=columns)
    assert page.evaluate("card.shadowRoot.querySelector('ha-card').scrollWidth<=card.clientWidth")
    assert page.evaluate("card.shadowRoot.querySelector('.scroller').scrollWidth<=card.shadowRoot.querySelector('.scroller').clientWidth")
    page.screenshot(path=str(tmp_path / f"dashboard-{width}.png"))


@pytest.mark.browser_real
def test_dashboard_mobile_keeps_name_and_voicemail_without_empty_badge_rows(dashboard_page):
    page = dashboard_page
    page.set_viewport_size({"width": 430, "height": 932})
    page.evaluate("document.body.style.margin='8px';card.style.width='414px';card.style.height='850px'")
    page.wait_for_function("card.classList.contains('narrow')")
    reception = page.locator(".tile[data-key='endpoint:reception']")
    assert reception.locator(".tile-name").inner_text() == "Reception"
    assert reception.locator(".tile-name").evaluate("""node=>{
      const range=document.createRange();range.selectNodeContents(node);
      return range.getClientRects().length===1;
    }""")
    assert reception.locator(".mail-badge").is_visible()
    assert reception.locator(".mail-badge").inner_text() == "2"
    assert reception.locator(".tile-badges").evaluate("node=>node.getBoundingClientRect().top>=node.parentElement.querySelector('.tile-name-copy').getBoundingClientRect().bottom")
    assert page.locator(".tile[data-key='contact:mobile'] .tile-badges").evaluate("node=>getComputedStyle(node).display==='none'")
    assert page.evaluate("""()=>{
      const scroller=card.shadowRoot.querySelector('.scroller');
      return card.scrollWidth<=414&&scroller.scrollWidth<=scroller.clientWidth&&
        [...card.shadowRoot.querySelectorAll('.tile')].every(node=>node.scrollWidth<=node.clientWidth);
    }""")
    output = ROOT / "test_captures/voip-dashboard-20261027/mobile-layout-regression.png"
    page.screenshot(path=str(output), full_page=True)


@pytest.mark.browser_real
def test_dashboard_editor_selects_owner_only_and_retains_missing_config(dashboard_page):
    page = dashboard_page
    page.evaluate("""()=>{const editor=customElements.get('voip-stack-dashboard-card').getConfigElement();window.editor=editor;editor.setConfig({device_id:'studio-device'});editor.hass=hass;document.body.append(editor)}""")
    page.wait_for_function("editor.shadowRoot.querySelector('select').options.length>1")
    assert page.locator("voip-stack-dashboard-card-editor select").count() == 1
    assert page.locator("voip-stack-dashboard-card-editor select option[value='office-device']").count() == 0
    page.evaluate("editor.setConfig({device_id:'missing-device'})")
    assert page.locator("voip-stack-dashboard-card-editor select").input_value() == "missing-device"


@pytest.mark.browser_real
def test_dashboard_language_change_and_unknown_contact_do_not_invent_state(dashboard_page):
    page = dashboard_page
    page.locator(".tile[data-key='contact:mobile'] .tile-main").click()
    assert page.get_by_role("dialog").get_by_text("Unknown", exact=True).is_visible()
    page.get_by_role("button", name="Close", exact=True).click()
    page.evaluate("hass={...hass,language:'it'};card.hass=hass")
    assert page.get_by_text("Interni (7)", exact=True).is_visible()
    assert page.get_by_role("button", name="Rispondi", exact=True).is_visible()
    assert page.locator(".tile").count() == 10


@pytest.mark.browser_real
def test_dashboard_late_initial_owner_read_cannot_replace_newer_push(dashboard_page):
    page = dashboard_page
    page.evaluate("""()=>{
      card.remove();
      window.engine.ownerSelectors=[];
      window.lateRead=null;
      hass.callWS=()=>new Promise(resolve=>{window.lateRead=resolve});
      document.body.append(card);
      engine.pushOwner({endpoint_id:'studio',device_id:'studio-device',state:'ringing',call_id:'new-call',sequence:4,revision:7});
      window.lateRead({endpoint_id:'studio',device_id:'studio-device',state:'ringing',call_id:'old-call',sequence:1,revision:1});
    }""")
    page.wait_for_function("card._ownerSnapshot.call_id==='new-call'")
    assert page.evaluate("card._ownerSnapshot.call_id") == "new-call"


@pytest.mark.browser_real
def test_dashboard_media_conflict_keeps_other_owner_call_and_shows_error(dashboard_page):
    page = dashboard_page
    page.evaluate("""()=>{
      engine.active=true;engine.callId='other-call';engine.endpointId='another-phone';
      engine.dashboardStartCall=async()=>{throw new Error('This browser is already handling another phone call.')};
      const owner={...fixture.phones.find(p=>p.endpoint_id==='studio'),state:'idle',calls:[]};
      engine.push({kind:'delta',revision:1,phones:[owner],removed:[]});
    }""")
    page.get_by_role("button", name="Reception, 100. ESPHome phone. Available. Voicemail: 2. Call", exact=True).click()
    assert page.get_by_text("This browser is already handling another phone call.", exact=True).is_visible()
    assert page.evaluate("[engine.active,engine.callId,engine.endpointId,engine.closed]") == [True, "other-call", "another-phone", 0]
    assert page.evaluate("card._store.phones.get('studio').state") == "idle"


@pytest.mark.browser_real
def test_dashboard_semantic_colors_remain_distinct_under_muted_ha_theme(dashboard_page):
    page = dashboard_page
    page.evaluate("document.documentElement.style.setProperty('--info-color','#777');document.documentElement.style.setProperty('--error-color','#777')")
    ringing = page.locator(".tile[data-key='endpoint:kitchen']")
    busy = page.locator(".tile[data-key='endpoint:office']")
    assert ringing.evaluate("node=>getComputedStyle(node).borderTopColor") == "rgb(22, 140, 255)"
    assert busy.evaluate("node=>getComputedStyle(node).borderTopColor") == "rgb(238, 50, 73)"
    page.evaluate("card.style.setProperty('--voip-dashboard-ringing-color','#8b2cf5')")
    # The tile transitions its border color; observe the final computed value.
    page.wait_for_function("getComputedStyle(card.shadowRoot.querySelector('.tile[data-key=\"endpoint:kitchen\"]')).borderTopColor==='rgb(139, 44, 245)'")


@pytest.mark.browser_real
def test_dashboard_owner_change_moves_only_the_grid_exclusion(dashboard_page):
    page = dashboard_page
    page.evaluate("card.setConfig({type:'custom:voip-stack-dashboard-card',device_id:'reception-device'})")
    assert page.locator(".owner-name").inner_text() == "Reception · 100"
    assert page.get_by_text("Extensions (7)", exact=True).is_visible()
    assert page.locator(".tile[data-key='endpoint:reception']").count() == 0
    assert page.locator(".tile[data-key='endpoint:studio']").count() == 1
    assert page.locator(".owner-badge:visible").count() == 0
    assert page.locator(".tile").count() == 10
    assert page.evaluate("card._store.phones.size") == 8


@pytest.mark.browser_real
def test_dashboard_header_opens_hidden_owner_multiple_calls_and_controls(dashboard_page):
    page = dashboard_page
    page.evaluate("""()=>{
      const owner=fixture.phones.find(phone=>phone.endpoint_id==='studio');
      engine.push({kind:'delta',revision:1,phones:[{...owner,calls:[...owner.calls,
        {id:'owner-held-call',generation:5,state:'confirmed',held:true,peer_name:'Customer'}]}],removed:[]});
    }""")
    assert page.locator(".tile[data-key='endpoint:studio']").count() == 0
    page.locator(".owner-call .more-calls").click()
    assert page.get_by_role("dialog").is_visible()
    assert page.locator(".details-title").inner_text() == "Studio"
    assert page.locator(".details-row").count() == 2
    held = page.locator(".details-row").filter(has=page.get_by_text("Customer", exact=True))
    held.get_by_role("button", name="Hang up", exact=True).click()
    assert page.evaluate("engine.actions.at(-1)") == ["hangup", "studio-device", "owner-held-call"]
    assert page.evaluate("card._tileData.get('endpoint:studio').owner") is True
