"""Composer dialog lifecycle against the HA persistent-dialog contract."""
from pathlib import Path
import json
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.js_runtime


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js is unavailable')
def test_composer_reopens_after_user_dismissal_and_ignores_child_overlays():
    source = Path(__file__).resolve().parents[1] / 'custom_components/voip_stack/frontend/voip-stack-composer.js'
    script = r'''
import fs from 'node:fs';
import assert from 'node:assert/strict';
class Element extends EventTarget {
  constructor() { super(); this.children = []; this.open = false; this.style = {}; }
  attachShadow() { return this.shadowRoot = new Element(); }
  setAttribute() {}
  setConfig(config) { this.config = config; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.children.push(child); }
}
globalThis.HTMLElement = Element;
globalThis.document = { createElement: () => new Element() };
const classes = new Map();
globalThis.customElements = { get: n => classes.get(n), define: (n, c) => classes.set(n, c) };
globalThis.engine = new EventTarget();
let source = fs.readFileSync(SOURCE, 'utf8')
 .replace(/^const version = .*;$/m, '')
 .replace(/^const \{ voipStackEngine \} = .*;$/m, 'const voipStackEngine = globalThis.engine;')
 .replace(/^const \{ voipStackTranslate \} = .*;$/m, 'const voipStackTranslate = (_, text) => text;');
await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const Composer = classes.get('voip-stack-composer');
const composer = new Composer();
const phone = composer._phone;
let closed = 0;
composer.addEventListener('dialog-closed', () => closed++);
// HA closes the inner overlay without writing its public `open` input.
// The owner must reset that input before the next showDialog().
for (const active of [false, true, false]) {
 phone.callActive = active;
 await composer.showDialog();
 assert.equal(composer._dialog.open, true);
 assert.equal(composer._open, true);
 const nested = new Event('closed');
 Object.defineProperty(nested, 'target', { value: composer._form });
 composer._dialog.dispatchEvent(nested);
 assert.equal(composer._open, true, 'A child selector must not close the composer');
 composer._dialog.dispatchEvent(new Event('closed'));
 assert.equal(composer._dialog.open, false);
 assert.equal(composer._open, false);
 assert.equal(composer._phone, phone, 'Closing the window preserves its media owner');
 assert.equal(phone.callActive, active);
 await composer.showDialog();
 assert.equal(composer._dialog.open, true);
 assert.equal(composer._open, true);
 composer.closeDialog();
 const before = closed;
 composer._dialog.dispatchEvent(new Event('closed'));
 assert.equal(closed, before, 'The hide animation must not report a second close');
}
assert.equal(closed, 6);
const devices = [{device_id:'native-phone',endpoint_type:'companion',mobile_device_id:'mobile'},
 {device_id:'browser-phone',endpoint_type:'browser',endpoint_id:'default'}];
const messages=[];
const auth = { external: { config: {}, sendMessage: async ({type}) => {
 messages.push(type);
 return type === 'config/get' ? {nativeCalls:1} : {deviceId:'mobile'};
}}};
const native = new Composer();
native.hass = {auth,callWS:async()=>({devices})};
await native.showDialog();
assert.deepEqual(messages,['config/get','call/context']);
assert.equal(native._selected,'native-phone');
assert.equal(native._phone.nativeCallContext.deviceId,'mobile');
assert.equal(native._phone.style.display,'');
for (const [external, expected] of [
 [undefined, 'No app connection'],
 [{sendMessage:async()=>({})}, 'does not report native calling support'],
 [{sendMessage:async({type})=>type==='config/get'?{nativeCalls:1}:{deviceId:null}}, 'has not provided'],
 [{sendMessage:async({type})=>type==='config/get'?{nativeCalls:1}:{deviceId:'other'}}, 'no matching calling phone'],
]) {
 const missing=new Composer();
 missing.hass={auth:{external},callWS:async()=>({devices:devices.filter(d=>d.endpoint_type==='companion')})};
 await missing.showDialog();
 assert.equal(missing._selected,'');
 assert.equal(missing._phone.style.display,'none');
 assert.ok(missing._error.textContent.includes(expected),missing._error.textContent);
}

'''.replace('SOURCE', json.dumps(str(source)))
    subprocess.run(['node', '--input-type=module', '-e', script], check=True, capture_output=True, text=True)
