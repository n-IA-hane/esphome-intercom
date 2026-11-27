// Execute production owner binding/synchronization with deferred media work.
// Rendering and the public shared-engine boundary are controlled, not DOM/media.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {pathToFileURL} from 'node:url';

const repo = process.argv[2] || new URL('../', import.meta.url).pathname.replace(/\/$/, '');
const root = repo + '/custom_components/voip_stack/frontend/';
const model = await import(pathToFileURL(root + 'voip-stack-dashboard-model.js'));
const cardModel = await import(pathToFileURL(root + 'voip-stack-card-model.js'));
const {VoipEntityIds} = await import(pathToFileURL(root + 'voip-stack-entity-ids.js'));
const source = fs.readFileSync(root + 'voip-stack-dashboard-card.js', 'utf8');
const klass = source.slice(source.indexOf('class VoipStackDashboardCard'), source.indexOf('\nif (!customElements.get('));
class Element {
  constructor() { this.isConnected = true; }
  attachShadow() { this.shadowRoot = {}; }
}
class Observer { observe() {} disconnect() {} }
const settle = async () => { for (let index = 0; index < 16; index++) await Promise.resolve(); };
const phone = identity => ({endpoint_id: identity, device_id: 'retained-device',
  name: identity, kind: 'sip_account', softphone: true, calls: [], availability: 'available'});
const snapshot = (identity, sequence = 1) => ({endpoint_id: identity,
  device_id: 'retained-device', state: 'in_call', call_id: `${identity}-call`,
  generation: 1, sequence, revision: sequence});

async function harness() {
  const work = [], ownerPushes = [], phonePushes = [];
  const engine = {
    configure() {},
    subscribePhoneStates(callback) { phonePushes.push(callback); return () => {}; },
    subscribeSoftphoneState(callback) { ownerPushes.push(callback); return () => {}; },
    dashboardSyncOwner(owner, state) {
      return new Promise((resolve, reject) => work.push({owner: owner.endpoint_id, state, resolve, reject}));
    },
    active: false, callId: '', endpointId: '', mediaClientId: 'test-fixture',
  };
  const context = vm.createContext({HTMLElement: Element, ResizeObserver: Observer,
    VoipEntityIds, voipStackEngine: engine, ...model, ...cardModel,
    voipStackTranslate: (_hass, text) => text, console});
  vm.runInContext(klass + '\nthis.Card = VoipStackDashboardCard;', context);
  const card = new context.Card();
  card._render = () => {};
  let current = 'account-A';
  let catalog = [phone(current)];
  let hass = {language: 'en', states: {}, entities: {},
    connection: {sendMessagePromise: async () => ({devices: catalog})},
    callWS: async () => snapshot(current)};
  card.setConfig({device_id: 'retained-device'});
  card.hass = hass;
  await settle();
  assert.deepEqual(work.map(item => item.owner), ['account-A']);
  const result = {card, work, engine, ownerPushes, phonePushes,
    async rebind(identity) {
      current = identity;
      catalog = [phone(identity)];
      hass = {...hass, entities: {}};
      card.hass = hass;
      await settle();
      assert.equal(card._owner().endpoint_id, identity);
      assert.equal(card._ownerSnapshot.endpoint_id, identity);
    },
    push(state) { ownerPushes.at(-1)(state); },
    streamRebind(identity) {
      current = identity;
      phonePushes.at(-1)({kind: 'snapshot', revision: 1, phones: [phone(identity)]});
    },
    async finish() {
      for (const item of work) item.resolve();
      await settle();
      assert.equal(card._syncPending, false);
    },
  };
  return result;
}

for (const outcome of ['reject', 'resolve']) {
  const h = await harness();
  const epoch = h.card._epoch;
  await h.rebind('account-B');
  assert.equal(h.card._epoch, epoch, 'device identity and connection did not change');
  assert.equal(h.work.length, 1, 'new media synchronization waits for the previous operation');
  h.work[0][outcome](outcome === 'reject' ? new Error('Retired A attach failed') : undefined);
  await settle();
  assert.equal(h.card._error, '', 'retired synchronization cannot publish an error on B');
  assert.deepEqual(h.work.map(item => item.owner), ['account-A', 'account-B']);
  assert.equal(h.work[1].state.endpoint_id, 'account-B');
  await h.finish();
}

// Returning to A is a distinct binding even while the first A attempt is pending.
{
  const h = await harness();
  await h.rebind('account-B');
  await h.rebind('account-A');
  h.work[0].reject(new Error('Retired first A attach failed'));
  await settle();
  assert.equal(h.card._error, '');
  assert.deepEqual(h.work.map(item => item.owner), ['account-A', 'account-A']);
  await h.finish();
}

// A newer snapshot in the same binding also survives an older failed attempt.
{
  const h = await harness();
  h.push(snapshot('account-A', 2));
  h.work[0].reject(new Error('Older A state failed'));
  await settle();
  assert.equal(h.card._error, '');
  assert.deepEqual(h.work.map(item => item.state.sequence), [1, 2]);
  await h.finish();
}

// The current error is visible, including when the failed engine attempt changed
// its state. Completion itself must not retry the same snapshot indefinitely.
{
  const h = await harness();
  h.engine.active = true;
  h.engine.callId = 'account-A-call';
  h.engine.endpointId = 'account-A';
  h.card._engineListener();
  h.work[0].reject(new Error('Current A attach failed'));
  await settle();
  assert.equal(h.card._error, 'Current A attach failed');
  assert.equal(h.work.length, 1, 'current failure must not immediately retry');
  assert.equal(h.card._syncPending, false);
  h.push(snapshot('account-A', 2));
  await settle();
  assert.equal(h.work.length, 2, 'a newer authoritative snapshot can trigger new work');
  await h.finish();
}

// An owner may change after work is queued but before the microtask starts it.
{
  const h = await harness();
  h.work[0].resolve();
  await settle();
  h.push(snapshot('account-A', 2));
  h.streamRebind('account-B');
  await settle();
  assert.deepEqual(h.work.map(item => item.owner), ['account-A', 'account-B']);
  assert.equal(h.work[1].state.endpoint_id, 'account-B');
  await h.finish();
}

console.log(JSON.stringify({same_device_rebind_late_reject: true,
  same_device_rebind_late_success: true, same_device_return_to_A: true,
  new_snapshot_during_pending_sync: true, current_error_without_retry_loop: true,
  retired_queued_work_not_started: true}));
