// Execute production catalog, dashboard and model code at controlled DOM/RPC boundaries.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';

const repo = new URL('../', import.meta.url).pathname;
const root = repo.replace(/\/$/, '') + '/custom_components/voip_stack/frontend/';
const model = await import(pathToFileURL(root+'voip-stack-dashboard-model.js'));
const {VoipEntityIds} = await import(pathToFileURL(root+'voip-stack-entity-ids.js'));
const cardModel = await import(pathToFileURL(root+'voip-stack-card-model.js'));
const source = fs.readFileSync(root+'voip-stack-dashboard-card.js', 'utf8');
const klass = source.slice(source.indexOf('class VoipStackDashboardCard'), source.indexOf('\nif (!customElements.get('));

class Element {
  constructor() {this.isConnected=true;}
  attachShadow() {this.shadowRoot={};}
}
class Observer {observe(){} disconnect(){}}
const phoneListeners=new Set();
const engine={configure(){}, subscribePhoneStates(cb){phoneListeners.add(cb);return()=>phoneListeners.delete(cb);},
  subscribeSoftphoneState(){return()=>{}}, dashboardSyncOwner(){}, active:false, callId:'', endpointId:'',
  mediaClientId:'fixture-only'};
const context=vm.createContext({HTMLElement:Element, ResizeObserver:Observer, VoipEntityIds,
  voipStackEngine:engine, ...model, ...cardModel,
  voipStackTranslate:(_hass,text)=>text, console});
vm.runInContext(klass+'\nthis.Card=VoipStackDashboardCard;', context);
const card=new context.Card();
// Render is an output sink, not the catalog's owner or invalidation mechanism.
card._render=()=>{};
const old={endpoint_id:'old',device_id:'old-device',name:'Old phone',kind:'sip_account',
  state:'idle',availability:'available',calls:[],softphone:true};
let catalog=[old], requests=0;
const readyCallbacks=new Set();
let hass={language:'en',entities:{},states:{'sensor.phonebook':{attributes:{roster_json:'[]'}}},
  connection:{addEventListener:(name,callback)=>{if(name==='ready')readyCallbacks.add(callback);},
    removeEventListener:(name,callback)=>{if(name==='ready')readyCallbacks.delete(callback);},
    sendMessagePromise:async request=>{
    assert.equal(request.type,'voip_stack/list_devices'); requests++;
    return {devices:catalog.map(x=>({...x})),entity_ids:{phonebook:'sensor.phonebook'}};
  }}, callWS:async()=>({endpoint_id:'old',device_id:'old-device',state:'idle'})};
const settle=async()=>{for(let i=0;i<8;i++)await Promise.resolve();};
card.setConfig({device_id:'old-device'});
card.hass=hass;
await settle();
for(const callback of phoneListeners) callback({kind:'snapshot',revision:0,phones:[old]});
await settle();
const before={requests,owner:card._owner()?.endpoint_id,devices:card._devices.length};
assert.deepEqual(before,{requests:1,owner:'old',devices:1});

const added={endpoint_id:'new',device_id:'new-device',name:'New phone',kind:'sip_account',
  state:'idle',availability:'available',calls:[],softphone:true};
catalog.push(added);
for(const callback of phoneListeners) callback({kind:'delta',revision:1,phones:[added]});
hass={...hass,entities:{'sensor.new_phone':{entity_id:'sensor.new_phone'}}};
card.hass=hass;
await settle();
card.setConfig({device_id:'new-device'});
await settle();
const refreshed={requests,owner:card._owner()?.endpoint_id,devices:card._devices.length,
  phoneInStream:card._store.phones.has('new'),selected:card._config.device_id};
assert.deepEqual(refreshed,{requests:2,owner:'new',devices:2,phoneInStream:true,selected:'new-device'});

// Rename and a changed native device identity reach the retained selection.
catalog=[old,{...added,name:'Renamed phone',device_id:'renamed-device'}];
for(const callback of phoneListeners) callback({kind:'delta',revision:2,phones:[catalog[1]]});
hass={...hass,entities:{}};card.hass=hass;
await settle();card.setConfig({device_id:'renamed-device'});
assert.equal(requests,3);assert.equal(card._owner().device_id,'renamed-device');
assert.equal(card._owner().name,'Renamed phone');

// Native removal retires the owner even when a phone stream update is late.
catalog=[old];hass={...hass,entities:{}};card.hass=hass;
await settle();
assert.equal(requests,4);assert.equal(card._owner(),null);assert.equal(card._ownerSelector,'');

// Independent consumers of the same native catalog share one request.
let complete, sharedRequests=0;
const sharedConnection={sendMessagePromise:()=>{sharedRequests++;return new Promise(resolve=>complete=resolve);}};
const sharedHass={connection:sharedConnection,entities:{},states:{}};
const first=new VoipEntityIds(),second=new VoipEntityIds();
first.update(sharedHass,()=>{});second.update(sharedHass,()=>{});
assert.equal(sharedRequests,1);
complete({devices:[old],entity_ids:{phonebook:'sensor.shared'}});
await Promise.all([first._pending,second._pending]);
assert.equal(first.phonebook,'sensor.shared');assert.equal(second.devices[0].endpoint_id,'old');

// A shared phonebook disappearance invalidates once for all retained consumers.
const presentHass={...sharedHass,states:{'sensor.shared':{}}};
first.update(presentHass,()=>{});second.update(presentHass,()=>{});
first.update(sharedHass,()=>{});second.update(sharedHass,()=>{});
assert.equal(sharedRequests,2);
complete({devices:[added],entity_ids:{phonebook:null}});
await Promise.all([first._pending,second._pending]);
assert.equal(first.devices[0].endpoint_id,'new');assert.equal(second.devices[0].endpoint_id,'new');

// Invalidation during a shared pending request cannot publish its late result.
first.refresh(sharedHass,()=>{});second.update(sharedHass,()=>{});
const superseded=[first._pending,second._pending],finishSuperseded=complete;
assert.equal(sharedRequests,3);
first.refresh(sharedHass,()=>{});
assert.equal(sharedRequests,4);
complete({devices:[added],entity_ids:{phonebook:'sensor.latest'}});await first._pending;
finishSuperseded({devices:[old],entity_ids:{phonebook:'sensor.stale'}});await Promise.all(superseded);
if(second._pending)await second._pending;
assert.equal(first.phonebook,'sensor.latest');assert.equal(second.phonebook,'sensor.latest');
assert.equal(second.devices[0].endpoint_id,'new');assert.equal(sharedRequests,4);

// A newer native registry response wins when RPCs finish out of order.
let late, current;
const racingConnection={sendMessagePromise:()=>new Promise(resolve=>{
  if(!late)late=resolve;else current=resolve;
})};
const racing=new VoipEntityIds();
const oldHass={connection:racingConnection,entities:{},states:{}};
racing.update(oldHass,()=>{});const obsoletePending=racing._pending;
racing.update({...oldHass,entities:{}},()=>{});
current({devices:[added],entity_ids:{phonebook:'sensor.current'}});await racing._pending;
late({devices:[old],entity_ids:{phonebook:'sensor.obsolete'}});await obsoletePending;
assert.equal(racing.phonebook,'sensor.current');assert.equal(racing.devices[0].endpoint_id,'new');

// Connection replacement does not wait for the previous server's RPC.
let oldFinish, newFinish;
const previous={sendMessagePromise:()=>new Promise(resolve=>oldFinish=resolve)};
const replacement={sendMessagePromise:()=>new Promise(resolve=>newFinish=resolve)};
const resolver=new VoipEntityIds();
resolver.update({connection:previous,entities:{},states:{}},()=>{});const previousPending=resolver._pending;
resolver.update({connection:replacement,entities:{},states:{}},()=>{});
newFinish({devices:[added],entity_ids:{phonebook:'sensor.new_server'}});await resolver._pending;
oldFinish({devices:[old],entity_ids:{phonebook:'sensor.old_server'}});await previousPending;
assert.equal(resolver.phonebook,'sensor.new_server');assert.equal(resolver.devices[0].endpoint_id,'new');

// A failed refresh recovers on the same connection without a polling timer.
let fail=true,retries=0;
const retryConnection={sendMessagePromise:async()=>{
 retries++;if(fail)throw new Error('Temporary lookup failure');return {devices:[added]};
}};
const retryHass={connection:retryConnection,entities:{},states:{}};
const retry=new VoipEntityIds();retry.update(retryHass,()=>{});await retry._pending;
assert.equal(retry.error,'Temporary lookup failure');fail=false;
retry.update(retryHass,()=>{});await retry._pending;
assert.equal(retry.error,'');assert.equal(retry.devices[0].endpoint_id,'new');assert.equal(retries,2);
retry.refresh();await retry._pending;assert.equal(retries,3);

// Reconnect refresh follows the dashboard's real event handler.
for(const callback of readyCallbacks)callback();await settle();assert.equal(requests,5);

console.log(JSON.stringify({before,after_native_registry_update_and_reconfigure:refreshed,
  rename_and_device_identity:true,remove_selected:true,shared_request:true,
  late_registry_and_connection_responses:true,shared_disappearance_and_inflight_invalidation:true,
  failed_refresh_recovery:true,reconnect:true}));
