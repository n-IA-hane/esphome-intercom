"""Run the production queue controls and call-specific paging answer methods."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.js_runtime
FRONTEND = Path(__file__).resolve().parents[1] / "custom_components/voip_stack/frontend"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_queue_controls_and_paging_answer_use_existing_phone_owners():
    source = (FRONTEND / "voip-stack-card.js").read_text()
    methods = []
    for name in ("_setGroupSetting", "_setQueueAgentState", "_maybeAutoAnswer", "_tryAutoAnswer", "_checkAutoAnswerMedia"):
        start = source.find("  " + name + "(")
        if start < 0:
            start = source.index("  async " + name + "(")
        end = source.index("\n  }", start) + len("\n  }")
        methods.append(source[start:end])
    script = '''
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
const {availableQueueNames, queueDeclarationText, isPagingAutoAnswer} = await import(pathToFileURL(MODEL));
const _voip_log = {info() {}};
let permissionRequests = 0;
const voipStackEngine = {checkAutoAnswerMedia() { permissionRequests++; throw Error('Unexpected capture permission'); }};
const Card = class { METHODS };
const card = new Card();
const services = [], textChanges = [], answers = [];
let refreshes = 0;
Object.assign(card, {
 _hass: {async callService(domain, service, data) {services.push([domain, service, data]);}},
 _isHaSoftphoneMode: () => true,
 _getSoftphoneEndpointId: () => 'browser:desk',
 _getConfigDeviceId: () => 'esp-device',
 _loadSoftphoneState: async () => {refreshes++;},
 _setTextEntity: async (entity, value) => {textChanges.push([entity, value]);},
 _queuesTextEntityId: 'text.esp_voip_queues',
 _showError: message => {throw Error(message);},
 _render() {}, _isSoftphoneController: () => true,
 _otherPhoneOwnsBrowserMedia: () => false,
 _sessionCallId: () => card._softphoneSnapshot.call_id,
 _softphoneSupportsVideo: () => true,
 _answer: async options => answers.push(options),
 _lifecycleGeneration: 1, _autoAnswer: false,
});
await card._setGroupSetting('queues', 'New queue, 700');
assert.deepEqual(services[0], ['voip_stack','set_phone_queues',{phone:'browser:desk',queues:'New queue, 700'}]);
assert.equal(refreshes, 1);
await card._setQueueAgentState('support', 'logged_out');
assert.deepEqual(services[1], ['voip_stack','set_queue_agent_state',{phone:'browser:desk',queue:'support',state:'logged_out'}]);
assert.equal(card._routingSettingsBusy, false);
card._isHaSoftphoneMode = () => false;
await card._setGroupSetting('queues', '700');
assert.deepEqual(textChanges, []);
assert.deepEqual(services[2], ['voip_stack','set_phone_queues',{phone:'esp-device',queues:'700'}]);
await card._setGroupSetting('ring_group', 'New ring');
assert.deepEqual(services[3], ['voip_stack','set_phone_memberships',{phone:'esp-device',field:'ring_group',value:'New ring'}]);
assert.equal(queueDeclarationText(['Support','700']), 'Support, 700');
assert.deepEqual(availableQueueNames([{name:'Support',metadata:{virtual_endpoint:'queue'}},
 {name:'Ring',metadata:{group_type:'ring'}}, {name:'Disabled',enabled:false,metadata:{virtual_endpoint:'queue'}}]), ['Support']);

card._isHaSoftphoneMode = () => true;
card._softphoneSnapshot = {state:'ringing', direction:'incoming', call_id:'ordinary', audio_direction:'recvonly'};
card._maybeAutoAnswer(card._softphoneSnapshot);
assert.equal(answers.length,0,'A receive-only ordinary call must retain normal answer policy');
const paging = {state:'ringing', direction:'incoming', call_id:'page', announcement_id:'job',
 answer_mode:'auto', audio_direction:'recvonly', video_offered:true, send_video:true, video_camera_send_enabled:true};
assert.equal(isPagingAutoAnswer({...paging,answer_mode:'manual'}),false);
assert.equal(isPagingAutoAnswer({...paging,audio_direction:'sendrecv'}),false);
card._softphoneSnapshot = paging;
card._checkAutoAnswerMedia(paging);
card._maybeAutoAnswer(paging);
await Promise.resolve(); await Promise.resolve();
assert.deepEqual(answers,[{callId:'page',videoPermission:undefined,sendVideo:false}]);
assert.equal(permissionRequests,0,'Backend-authorized receive-only paging must not request microphone or camera');
assert.equal(card._autoAnswer,false,'Per-call paging never changes the persistent Auto Answer setting');
await card._tryAutoAnswer({callId:'stale-page'});
assert.equal(answers.length,1,'A stale call cannot be answered after selection changes');
console.log('queue and paging runtime contracts passed');
'''.replace("MODEL", json.dumps(str(FRONTEND / "voip-stack-card-model.js"))).replace("METHODS", "\n".join(methods))
    result = subprocess.run(["node", "--input-type=module", "-e", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
