"""Provisional browser audio must not manufacture an answered call."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.js_runtime
ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / 'custom_components/voip_stack/frontend/voip-stack-engine.js'


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js unavailable')
@pytest.mark.parametrize('entrypoint', ['resume', 'owned', 'unmounted'])
def test_selected_183_attaches_existing_audio_without_answer(entrypoint):
    script = r'''
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const storage=()=>{const v=new Map();return {getItem:k=>v.get(k)||null,setItem:(k,x)=>v.set(k,String(x)),removeItem:k=>v.delete(k)}};
globalThis.sessionStorage=storage();globalThis.localStorage=storage();
globalThis.CustomEvent=class extends Event {constructor(type,options={}){super(type);this.detail=options.detail;}};
globalThis.window={location:{protocol:'https:',host:'ha.example'},addEventListener(){},setTimeout,clearTimeout,setInterval,clearInterval};
Object.defineProperty(globalThis,'navigator',{value:{mediaDevices:{}},configurable:true});
const {voipStackEngine:engine}=await import(pathToFileURL(ENGINE));
const endpoint='desk';const call='early-browser';
const owner={endpoint_id:endpoint,device_id:'desk-browser',kind:'browser',ha_client_supported:true};
const state={...owner,call_id:call,state:'remote_ringing',direction:'outgoing',early_media:true,
 can_attach_media:true,media_owner:'self',audio_direction:'recvonly'};
const {hasAttachableCallMedia,isEstablishedCallState}=await import(pathToFileURL(ENGINE.replace('voip-stack-engine.js','voip-stack-card-model.js')));
assert.equal(isEstablishedCallState(state.state),false);
for(const denied of [{...state,early_media:false},{...state,direction:'incoming'},
 {...state,can_attach_media:false},{...state,state:'idle'}]) assert.equal(hasAttachableCallMedia(denied),false);
engine.claimSoftphoneSession(call,endpoint);engine._state='REMOTE_RINGING';
let attached=0;
engine._setupAudioOrAbort=async()=>{attached++;return true;};engine._ensureVideo=async()=>{};
if(ENTRYPOINT==='resume') await engine.resumeSession(owner,owner.device_id,state);
if(ENTRYPOINT==='owned') await engine.syncOwnedSoftphoneState(owner,state);
if(ENTRYPOINT==='unmounted'){engine._syncUnmountedSoftphoneState(state);await new Promise(resolve=>setTimeout(resolve,0));}
assert.equal(attached,1,'Selected provisional SDP must open the existing browser audio path before any 200');
assert.equal(engine._state,'REMOTE_RINGING','Media negotiation must not falsely answer the call');
assert.equal(engine.softphoneCallIdFor(endpoint),call);
engine.releaseSoftphoneSession(call,endpoint);
'''.replace('ENGINE', json.dumps(str(ENGINE))).replace('ENTRYPOINT', json.dumps(entrypoint))
    result = subprocess.run(['node','--input-type=module','-e',script],cwd=ROOT,capture_output=True,text=True)
    assert result.returncode == 0, result.stderr or result.stdout
