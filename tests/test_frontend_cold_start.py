"""Real module graph: optional editors must not delay an incoming-call card."""
from pathlib import Path
import shutil
from urllib.parse import urlsplit, parse_qs

import pytest

from tests.test_frontend_popup_browser import BOOTSTRAP

ROOT = Path(__file__).resolve().parents[1]
FRONT = ROOT / 'custom_components/voip_stack/frontend'
VERSION = 'cold-start-contract-1'
pytestmark = [pytest.mark.js_runtime, pytest.mark.browser_real]


@pytest.fixture
def cold_page():
    from playwright.sync_api import sync_playwright

    assert shutil.which('chromium'), 'Maintained Chromium required'
    with sync_playwright() as runner:
        browser = runner.chromium.launch(executable_path=shutil.which('chromium'), headless=True)
        page = browser.new_page()
        page.set_default_timeout(2000)
        yield page
        browser.close()


def prepare(page, held_name=None, failed_name=None, entry_name='voip-stack-card.js'):
    held, requests, errors = [], [], []
    page.on('pageerror', lambda error: errors.append(str(error)))

    def fulfill(route):
        path = FRONT / Path(urlsplit(route.request.url).path).name
        route.fulfill(content_type='text/javascript' if path.is_file() else 'text/html',
                      body=path.read_text() if path.is_file() else '<html><body></body></html>')

    def serve(route):
        parsed = urlsplit(route.request.url)
        name = Path(parsed.path).name
        if name.endswith('.js'):
            requests.append((name, parse_qs(parsed.query).get('v')))
        if name == failed_name:
            route.abort('failed')
        elif name == held_name:
            held.append(route)
        else:
            fulfill(route)

    page.route('http://phone.test/**', serve)
    page.goto('http://phone.test/?voip_answer=1&voip_endpoint=browser%3Adesk&voip_call_id=cold-A')
    # Reuse only the existing controlled HA/media I/O. Do not pre-import the
    # popup or engine: this must be the real cold production module graph.
    page.evaluate(BOOTSTRAP.replace("await import('/voip-stack-popup.js');", ''))
    page.evaluate("""({version,entry})=>{
      window.moduleLoaded=false;window.moduleError='';
      import('/'+entry+'?v='+version).then(()=>{moduleLoaded=true;}).catch(error=>{moduleError=String(error);});
    }""", {'version': VERSION, 'entry': entry_name})
    return held, requests, errors, fulfill


@pytest.mark.parametrize(('entry_name', 'held_name', 'siblings'), [
    ('voip-stack-card.js', 'voip-stack-entity-ids.js', [
        'voip-stack-i18n.js', 'voip-phonebook-card.js', 'voip-stack-engine.js',
        'voip-stack-card-model.js', 'voip-stack-card-view.js',
    ]),
    ('voip-stack-engine.js', 'ringtone.js', [
        'voip-stack-media-model.js', 'voip-stack-session-model.js',
        'voip-stack-media-devices.js',
    ]),
    ('voip-stack-popup.js', 'voip-stack-engine.js', [
        'voip-stack-card-model.js', 'voip-stack-i18n.js',
    ]),
])
def test_required_imports_start_together_but_keep_readiness_barrier(
    cold_page, entry_name, held_name, siblings,
):
    page = cold_page
    held, requests, errors, fulfill = prepare(
        page, held_name=held_name, entry_name=entry_name,
    )
    # This is an ordering barrier, not a wall-clock performance threshold:
    # independent responses must complete while the first response stays held.
    page.wait_for_function("""names=>names.every(name=>
      performance.getEntriesByType('resource').some(item=>
        new URL(item.name).pathname==='/'+name))""", arg=siblings)
    assert held
    assert not page.evaluate('moduleLoaded')
    assert page.evaluate("!customElements.get('voip-stack-card')")
    assert page.evaluate("!globalThis.__voipStackPopup")
    for route in held:
        fulfill(route)
    page.wait_for_function('moduleLoaded')
    assert page.evaluate('moduleError') == ''
    # Both entrypoints must share the already created engine and the same
    # versioned modules. Phonebook must be ready with the required card API.
    page.evaluate("""async version=>{
      window.firstEngine=__voipStackEngine;
      await Promise.all([
        import('/voip-stack-card.js?v='+version),
        import('/voip-stack-popup.js?v='+version),
      ]);
    }""", VERSION)
    assert page.evaluate('__voipStackEngine===firstEngine')
    assert page.evaluate("!!customElements.get('voip-stack-card') && !!customElements.get('voip-stack-phonebook-view')")
    assert page.evaluate('mics===0 && contexts===0 && services.length===0')
    assert all(version == [VERSION] for _, version in requests)
    names = [name for name, _ in requests]
    for name in [entry_name, held_name, *siblings]:
        assert names.count(name) == 1, name
    assert errors == []


@pytest.mark.parametrize('held_name', ['voip-stack-card-editor.js', 'voip-stack-dashboard-card-editor.js'])
def test_uri_answer_does_not_wait_for_optional_editor(cold_page, held_name):
    page = cold_page
    held, requests, errors, fulfill = prepare(page, held_name)
    # Before the fix, either held editor keeps this actual custom element
    # undefined. No stopwatch performance assumption or mocked card class.
    page.wait_for_function("!!customElements.get('voip-stack-card') && moduleLoaded")
    assert page.evaluate("moduleError") == ''
    assert not any(name == 'voip-stack-card-editor.js' for name, _ in requests)
    page.evaluate("""()=>{
      window.firstEngine=__voipStackEngine;window.prepares=0;
      __voipStackEngine.prepareAudioCall=async()=>{prepares++;return true;};
      current={...current,state:'ringing',direction:'incoming',call_id:'cold-A',
        caller:'Door',auto_answer:false,send_video:false,video_offered:false};
      const card=window.coldCard=document.createElement('voip-stack-card');
      card.setConfig({mode:'ha_softphone',device_id:phone.device_id});card.hass=hass;
      root.shadowRoot.append(card);
    }""")
    page.wait_for_function("services.some(item=>item.service==='answer')")
    assert page.evaluate("services.filter(item=>item.service==='answer').map(item=>item.data.call_id)") == ['cold-A']
    assert page.evaluate('prepares') == 1
    assert page.evaluate("!new URLSearchParams(location.search).has('voip_answer')")
    # Explicit editor opening uses HA's supported async contract. Its promise
    # may wait, while the ringing-card/answer path has already completed.
    page.evaluate("""()=>{
      window.editorReady=false;
      customElements.get('voip-stack-card').getConfigElement().then(editor=>{
        window.loadedEditor=editor;editorReady=true;
      });
    }""")
    page.wait_for_timeout(50)
    assert held, 'The optional module request must be genuinely held'
    if held_name == 'voip-stack-card-editor.js':
        assert page.evaluate('editorReady') is False
    else:
        assert page.evaluate("!customElements.get('voip-stack-dashboard-card')")
    for route in held:
        fulfill(route)
    page.wait_for_function("editorReady && !!customElements.get('voip-stack-dashboard-card')")
    assert page.evaluate("loadedEditor.localName") == 'voip-stack-card-editor'
    assert page.evaluate("customElements.get('voip-stack-dashboard-card').getConfigElement().localName") == 'voip-stack-dashboard-card-editor'
    assert page.evaluate("__voipStackEngine===firstEngine")
    assert page.evaluate("!!customElements.get('voip-stack-phonebook-view')")
    assert all(version == [VERSION] for _, version in requests)
    assert errors == []


def test_dashboard_import_failure_does_not_retire_base_card(cold_page):
    page = cold_page
    console = []
    page.on('console', lambda message: console.append(message.text) if message.type == 'error' else None)
    _, _, errors, _ = prepare(page, failed_name='voip-stack-dashboard-card.js')
    page.wait_for_function("!!customElements.get('voip-stack-card') && moduleLoaded")
    page.wait_for_timeout(100)
    assert page.evaluate('moduleError') == ''
    assert any('dashboard card' in error.lower() for error in console)
    assert errors == []


def test_required_import_failure_still_rejects_card_module(cold_page):
    page = cold_page
    prepare(page, failed_name='voip-stack-card-model.js')
    page.wait_for_function('!!moduleError')
    assert page.evaluate("!customElements.get('voip-stack-card') && !moduleLoaded")
