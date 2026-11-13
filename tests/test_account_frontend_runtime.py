"""Real frontend methods accept canonical accounts and preserve media ownership."""

import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
FRONT = ROOT / "custom_components/voip_stack/frontend"
pytestmark = pytest.mark.js_runtime


def editor_contract(editor=None):
    return r'''
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";
import { pathToFileURL } from "url";
const model = await import(pathToFileURL(MODEL));
const dashboard = await import(pathToFileURL(DASHBOARD));
let source = fs.readFileSync(EDITOR, "utf8").replace(/import.meta.url/g, JSON.stringify("file:///editor.js"))
  .replace(/await import\(`\.\/voip-stack-i18n\.js[^`]+`\)/, "globalThis.i18n")
  .replace(/await import\(`\.\/voip-stack-card-model\.js[^`]+`\)/, "globalThis.model");
const types = new Map();
const context = vm.createContext({ URL, HTMLElement:class {}, model, i18n:{voipStackTranslate:(_h,t)=>t},
  customElements:{get:key=>types.get(key),define:(key,value)=>types.set(key,value)} });
const loaded = new vm.SourceTextModule(source,{context});
await loaded.link(()=>{throw new Error("Unexpected static import");});await loaded.evaluate();
const editor = types.get("voip-stack-card-editor").prototype;
const account = {kind:"sip_account",device_id:"device-sip",endpoint_id:"sip:desk",name:"Desk",availability:"offline"};
assert.equal(editor._isSoftphoneDevice(account), true, "An unbound authorized SIP account must be selectable");
assert.equal(editor._isSoftphoneDevice({kind:"esphome",device_id:"esp"}), false);
const selected = dashboard.ownerDeviceInfo([account],new Map(),"device-sip");
assert.equal(selected.endpoint_id,"sip:desk");assert.equal(selected.softphone,true);
assert.deepEqual(model.normaliseCardConfig({mode:"ha_softphone",device_id:"device-sip",endpoint_id:"old"}).config,
  {mode:"ha_softphone",device_id:"device-sip"});
'''.replace('MODEL', json.dumps(str(FRONT / 'voip-stack-card-model.js'))).replace('DASHBOARD', json.dumps(str(FRONT / 'voip-stack-dashboard-model.js'))).replace('EDITOR', json.dumps(str(editor or FRONT / 'voip-stack-card-editor.js')))


def test_unbound_sip_account_is_selectable_without_changing_saved_selector():
    subprocess.run(["node", "--experimental-vm-modules", "--input-type=module", "-e", editor_contract()], cwd=ROOT, check=True)


def test_snapshot_order_uses_generation_before_per_call_sequence():
    script = '''
import assert from "assert/strict";
import {pathToFileURL} from "url";
const {softphoneSnapshotSupersedes:accept} = await import(pathToFileURL(MODEL));
const current={call_id:"reused-id",state:"in_call",generation:5,sequence:8,revision:9};
assert.equal(accept(current,{...current,generation:4,sequence:100}),false);
assert.equal(accept(current,{...current,generation:6,sequence:1,revision:1}),true);
assert.equal(accept(current,{...current,generation:5,sequence:7}),false);
assert.equal(accept(current,{...current,state:"idle",generation:4,sequence:100}),false);
'''.replace('MODEL', json.dumps(str(FRONT / 'voip-stack-card-model.js')))
    subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True)
