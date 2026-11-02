"""Page ownership remains distinct from copied storage and transient card views."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "custom_components/voip_stack/frontend/voip-stack-engine.js"
pytestmark = pytest.mark.js_runtime


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
@pytest.mark.parametrize("scenario", ["clone", "legacy_reload", "setup_without_view", "answer_without_view", "answer_during_setup"])
def test_page_owned_media_lifecycle(scenario):
    # Import the actual engine and its dependencies. Only browser host APIs and
    # the asynchronous HA subscription boundary are controlled in this Node test.
    script = r'''
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const storage = new Map();
const local = new Map();
const adapter = values => ({
  getItem:key=>values.get(key)||null,
  setItem:(key,value)=>values.set(key,String(value)),
  removeItem:key=>values.delete(key),
});
globalThis.sessionStorage = adapter(storage);
globalThis.localStorage = adapter(local);
Object.defineProperty(globalThis, 'navigator', {value:{}, configurable:true});
globalThis.window = {location:{protocol:'https:',host:'ha.example'},
  addEventListener(){},setTimeout,clearTimeout,setInterval,clearInterval};
const imported = await import(pathToFileURL(ENGINE));
const Engine = imported.voipStackEngine.constructor;
const navigation = {type:'navigate'};
Object.defineProperty(globalThis, 'performance', {
  value:{getEntriesByType:()=>[navigation]}, configurable:true,
});
const nextDocument = () => {delete globalThis.__voipStackMediaClientId; return new Engine();};
const turn = () => new Promise(resolve=>setImmediate(resolve));
if (SCENARIO === 'clone') {
  const original=nextDocument();
  original.claimSoftphoneSession('call-A','desk');
  const copiedId=storage.get('voip_stack_media_client_id');
  const clone=nextDocument();
  assert.notEqual(clone.mediaClientId,copiedId,'Copied sessionStorage is not a media lease');
  assert.equal(new Engine().mediaClientId,clone.mediaClientId,'SPA/card instances share the document owner');
  navigation.type='reload';
  assert.equal(nextDocument().mediaClientId,clone.mediaClientId,'Real reload retains only this window identity');
} else if (SCENARIO === 'legacy_reload') {
  navigation.type='reload';
  const ids=[];
  for(let page=0;page<2;page++) {
    storage.set('voip_stack_media_client_id','legacy-shared-identifier-123456');
    ids.push(nextDocument().mediaClientId);
  }
  assert.notEqual(ids[0],ids[1],'Legacy copied identities migrate independently');
  assert.ok(ids.every(id=>id.startsWith('media2-')));
} else {
  const engine=nextDocument();
  let callback, unsubscribes=0, resumeCount=0;
  engine._busConnection={subscribeMessage:async listener=>{
    callback=listener;return()=>{unsubscribes++;};
  }};
  const detach=engine.subscribeSoftphoneState(()=>{}, {device_id:'desk-device'});
  await turn();
  callback({endpoint_id:'desk',device_id:'desk-device',state:'idle',call_id:''});
  if(SCENARIO==='setup_without_view') {
    const intent={};
    assert.ok(engine.tryAcquireMediaIntent('desk',intent));
    detach();
    assert.equal(unsubscribes,0,'Removing a view must retain an in-flight setup subscription');
    engine.releaseMediaIntent(intent);
  } else {
    local.set('voip_microphone_anti_alias_desk-device','false');
    const intent=SCENARIO==='answer_during_setup'?{}:null;
    if(intent) assert.ok(engine.tryAcquireMediaIntent('desk',intent));
    if(!intent) engine.claimSoftphoneSession('call-A','desk');
    engine.resumeSession=async(owner,deviceId,state)=>{
      assert.equal(deviceId,'desk-device');assert.equal(state.call_id,'call-A');
      assert.equal(owner.microphone_anti_alias,false);resumeCount++;
    };
    detach();
    assert.equal(unsubscribes,0,'Removing a ringing view must retain its call subscription');
    callback({endpoint_id:'desk',device_id:'desk-device',state:'in_call',call_id:'call-A'});
    await turn();
    if(intent) {
      assert.equal(resumeCount,0,'Setup intent must serialize media attachment');
      engine.claimSoftphoneSession('call-A','desk');
      engine.releaseMediaIntent(intent);
      await turn();
    }
    assert.equal(resumeCount,1,'Remote answer without any view must attach through the page owner');
    callback({endpoint_id:'desk',device_id:'desk-device',state:'idle',call_id:'old-call'});
    await turn();
    assert.equal(engine.softphoneCallIdFor('desk'),'call-A','Stale terminal state must not release a different call');
    callback({endpoint_id:'desk',device_id:'desk-device',state:'idle',call_id:'call-A'});
    await turn();
    assert.equal(engine.softphoneCallIdFor('desk'),'');
  }
  assert.equal(unsubscribes,1,'The final call/setup release unsubscribes exactly once');
  assert.equal(engine._softphoneScopeSubscriptions.size,0);
}
'''
    script = f"const ENGINE={json.dumps(str(ENGINE))}; const SCENARIO={json.dumps(scenario)};\n" + script
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
@pytest.mark.parametrize("terminal_order", ["softphone_first", "call_event_first"])
def test_unmounted_terminal_releases_all_subscriptions_in_either_order(terminal_order):
    """Observe unsubscribe callbacks independently of the engine's owner maps."""
    script = r'''
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const adapter=()=>({getItem:()=>null,setItem(){},removeItem(){}});
globalThis.sessionStorage=adapter();globalThis.localStorage=adapter();
Object.defineProperty(globalThis,'navigator',{value:{},configurable:true});
globalThis.window={location:{protocol:'https:',host:'ha.example'},addEventListener(){},
  setTimeout,clearTimeout,setInterval,clearInterval};
const imported=await import(pathToFileURL(ENGINE));
const engine=new imported.voipStackEngine.constructor();
const turn=()=>new Promise(resolve=>setImmediate(resolve));
const active=new Map(),callbacks=new Map(),releases=new Map();
const connection={subscribeMessage:async(callback,request)=>{
  const type=request.type;
  assert.equal(active.has(type),false,'A scope must have only one subscription');
  active.set(type,callback);callbacks.set(type,callback);
  return()=>{active.delete(type);releases.set(type,(releases.get(type)||0)+1);};
}};
engine.configure({connection});
const detachState=engine.subscribeSoftphoneState(()=>{}, {endpoint_id:'desk'});
const detachEvents=engine.subscribeCallEvents(()=>{});
engine.claimSoftphoneSession('call-A','desk');
await turn();
detachState();detachEvents();
assert.equal(active.size,2,'The page retains both subscriptions while its call rings without a view');
const state=callbacks.get('voip_stack/subscribe_ha_softphone_state');
const event=callbacks.get('voip_stack/subscribe_call_events');
const terminalState=()=>state({endpoint_id:'desk',state:'idle',call_id:'call-A'});
const terminalEvent=()=>event({event_type:'voip_stack.call_event',
  data:{endpoint_id:'desk',state:'idle',type:'ended',call_id:'call-A'}});
const ordered=ORDER==='softphone_first'?[terminalState,terminalEvent]:[terminalEvent,terminalState];
ordered[0]();await turn();
assert.equal(engine.softphoneCallIdFor('desk'),'');
assert.equal(active.size,0,'The first authoritative terminal observation releases every call subscription');
ordered[1]();await turn();
assert.equal(active.size,0,'An already queued terminal observation cannot recreate subscriptions');
assert.equal(engine._softphoneScopeSubscriptions.size,0);
assert.equal(engine._busUnsub,null);
for(const type of callbacks.keys())assert.equal(releases.get(type),1,'Each subscription is released exactly once');
'''
    result = subprocess.run(
        ["node", "--input-type=module", "-e",
         f"const ENGINE={json.dumps(str(ENGINE))}; const ORDER={json.dumps(terminal_order)};\n" + script],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
@pytest.mark.parametrize("cached_call_id", ["", "previous-call"])
def test_pre_call_idle_does_not_terminate_a_new_unmounted_dialog(cached_call_id):
    """Replay the independent review witness, plus unidentified state negatives."""
    script = r'''
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const adapter=()=>({getItem:()=>null,setItem(){},removeItem(){}});
globalThis.sessionStorage=adapter();globalThis.localStorage=adapter();
Object.defineProperty(globalThis,'navigator',{value:{},configurable:true});
globalThis.window={location:{protocol:'https:',host:'ha.example'},addEventListener(){},
  setTimeout,clearTimeout,setInterval,clearInterval};
const imported=await import(pathToFileURL(ENGINE));
const engine=new imported.voipStackEngine.constructor();
const turn=()=>new Promise(resolve=>setImmediate(resolve));
let callback,resumes=0,unsubscribes=0;
engine._busConnection={subscribeMessage:async listener=>{callback=listener;return()=>{unsubscribes++;};}};
const detach=engine.subscribeSoftphoneState(()=>{}, {device_id:'desk-device'});
await turn();
callback({endpoint_id:'desk',device_id:'desk-device',state:'idle',call_id:CACHED});
const intent={};assert.equal(engine.tryAcquireMediaIntent('desk',intent),true);
detach();
assert.equal(engine.claimSoftphoneSession('call-after-idle','desk'),true);
engine.resumeSession=async()=>{resumes++;};
assert.equal(engine.releaseMediaIntent(intent),true);
await turn();
assert.equal(engine.softphoneCallIdFor('desk'),'call-after-idle',
  'A cached pre-call idle snapshot cannot release the newly claimed dialog');
assert.equal(unsubscribes,0);
for(const state of [undefined,'unknown','idle']) {
  callback({endpoint_id:'desk',device_id:'desk-device',state,
    call_id:state==='idle'?'':'call-after-idle'});
  await turn();
  assert.equal(engine.softphoneCallIdFor('desk'),'call-after-idle',
    'Teardown requires a recognized terminal state with the owned Call-ID');
}
callback({endpoint_id:'desk',device_id:'desk-device',state:'in_call',
  call_id:'call-after-idle',media_owner:'self'});
await turn();
assert.equal(resumes,1,'Later authoritative answer attaches once without any view');
callback({endpoint_id:'desk',device_id:'desk-device',state:'idle',call_id:'call-after-idle'});
await turn();
assert.equal(engine.softphoneCallIdFor('desk'),'');
assert.equal(unsubscribes,1,'Matching terminal state releases the single retained scope');
assert.equal(engine._softphoneScopeSubscriptions.size,0);
'''
    result = subprocess.run(
        ["node", "--input-type=module", "-e",
         f"const ENGINE={json.dumps(str(ENGINE))}; const CACHED={json.dumps(cached_call_id)};\n" + script],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
@pytest.mark.parametrize("mode", ["self", "other", "ended", "rejected", "failure_inspection", "typed_cancellation"])
def test_same_call_replacement_owns_its_async_audio_generation(mode):
    """Real attach/setup/close methods, with controlled host API completions."""
    script = r'''
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const adapter=()=>({getItem:()=>null,setItem(){},removeItem(){}});
globalThis.sessionStorage=adapter();globalThis.localStorage=adapter();
Object.defineProperty(globalThis,'navigator',{value:{},configurable:true});
const timer=(fn,ms)=>{const value=setTimeout(fn,ms);value.unref();return value;};
let releasePreflight,rejectPreflight,releaseCurrentWorklet;
const oldPreflight=new Promise((resolve,reject)=>{releasePreflight=resolve;rejectPreflight=reject;});
const newWorklet=new Promise(resolve=>{releaseCurrentWorklet=resolve;});
const contexts=[];
class AudioContext {
 constructor(){this.id=contexts.length+1;contexts.push(this);this.state='running';this.sampleRate=48000;this.destination={};
  this.audioWorklet={addModule:()=>this.id===1?newWorklet:Promise.resolve()};}
 async close(){this.closed=true;this.state='closed';}
}
globalThis.window={location:{protocol:'https:',host:'ha.example'},addEventListener(){},AudioContext,
 setTimeout:timer,clearTimeout,setInterval,clearInterval};
class Worklet {constructor(context){this.context=context;this.port={postMessage(){}};}connect(){return this;}disconnect(){}}
globalThis.AudioWorkletNode=Worklet;
const session={state:'in_call',call_id:'same-call',endpoint_id:'desk',device_id:'desk-device',
 media_owner:'self',tx_format:'16000:s16le:1:10',rx_format:'48000:s16le:1:10',audio_direction:'recvonly'};
const sockets=[];let failOldSockets=MODE==='failure_inspection';
class Socket {
 static OPEN=1;static CONNECTING=0;
 constructor(url){this.url=url;this.readyState=0;sockets.push(this);setImmediate(()=>{
  if(failOldSockets){this.onerror?.();return;}
  this.readyState=1;this.onopen?.();this.onmessage?.({data:JSON.stringify(session)});
 });}
 close(){this.readyState=3;this.onclose?.();}send(){}
}
globalThis.WebSocket=Socket;
const imported=await import(pathToFileURL(ENGINE));
const engine=new imported.voipStackEngine.constructor();
const turn=()=>new Promise(resolve=>setImmediate(resolve));
let preflights=0,hangups=0;const errors=[];
engine.addEventListener('error',event=>errors.push(event.detail));
engine._hass={callWS:async request=>{
 if(request.type==='auth/sign_path')return {path:request.path};
 if(request.type==='voip_stack/ha_softphone_state'){
  preflights++;
  if(MODE==='failure_inspection'&&preflights===2)failOldSockets=false;
  return preflights===(MODE==='failure_inspection'?2:1)?oldPreflight:session;
 }
 throw Error('Unexpected control request '+request.type);
},callService:async()=>{hangups++;}};
if(MODE==='typed_cancellation') {
  engine._callId='same-call';
  const setup=engine._setupAudio({microphone_anti_alias:false},session);
  const cancellation=setup.then(()=>null,error=>error);
  while(!contexts.length)await turn();
  await engine.close('cancel-old-setup');releaseCurrentWorklet();
  const retiredError=await cancellation;
  assert.equal(retiredError.constructor.name,'AudioSetupSupersededError');
  engine.claimSoftphoneSession('new-call','desk');
  engine.resumeSession=async()=>{throw retiredError;};
  engine._onSoftphoneState({...session,call_id:'new-call'});
  await turn();
  assert.deepEqual(errors,[],'A typed retired setup is cancellation, not an error on the replacement');
  assert.equal(engine.softphoneCallIdFor('desk'),'new-call');
  assert.equal(hangups,0);
  engine.releaseSoftphoneSession('new-call','desk');
} else {
  engine.claimSoftphoneSession('same-call','desk');
  const oldAttach=engine.resumeSession({endpoint_id:'desk',microphone_anti_alias:false},'desk-device',session);
  while(preflights<(MODE==='failure_inspection'?2:1))await turn();
  await engine.close('view-cycle-teardown');
  const newAttach=engine.resumeSession({endpoint_id:'desk',microphone_anti_alias:true},'desk-device',session);
  while(contexts.length<1)await turn();
  const currentKey=engine._sessionAttachKey,currentPending=engine._sessionAttachPromise;
  const duplicate=engine.resumeSession({endpoint_id:'desk',microphone_anti_alias:true},'desk-device',session);
  assert.equal(engine._sessionAttachPromise,currentPending,'Live same-call views deduplicate one attach');
  if(MODE==='rejected')rejectPreflight(new Error('retired preflight transport failure'));
  else releasePreflight(MODE==='ended'?{...session,state:'idle'}:
    ['other','failure_inspection'].includes(MODE)?{...session,media_owner:'other'}:session);
  await oldAttach;
  assert.equal(engine._sessionAttachKey,currentKey,'Retired finally cannot clear the current attempt');
  assert.equal(engine._sessionAttachPromise,currentPending);
  assert.equal(engine.softphoneCallIdFor('desk'),'same-call','Retired preflight cannot release current ownership');
  assert.equal(contexts.length,1,'Retired completion cannot allocate/publish a second AudioContext');
  releaseCurrentWorklet();await Promise.all([newAttach,duplicate]);
  assert.equal(engine._microphoneAntiAlias,true,'Replacement preference wins');
  assert.equal(engine._audioContext,contexts[0],'Replacement publishes only its own resources');
  assert.equal(contexts[0].closed,undefined);
  assert.equal(engine.callId,'same-call');
  assert.equal(sockets.filter(socket=>socket.readyState===1).length,1);
  assert.equal(engine._sessionAttachPromise,null);
  assert.equal(engine._sessionAttachKey,'');
  assert.deepEqual(errors,[]);assert.equal(hangups,0);
  await engine.close('test-cleanup');engine.releaseSoftphoneSession('same-call','desk');
  assert.ok(contexts.every(context=>context.closed));
  assert.ok(sockets.every(socket=>socket.readyState===3));
}
'''
    result = subprocess.run(
        ["node", "--input-type=module", "-e",
         f"const ENGINE={json.dumps(str(ENGINE))}; const MODE={json.dumps(mode)};\n" + script],
        cwd=ROOT, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr or result.stdout
