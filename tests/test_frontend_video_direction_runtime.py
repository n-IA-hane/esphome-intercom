"""Direction-only SDP updates preserve unaffected browser codec owners."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.js_runtime
ROOT = Path(__file__).resolve().parents[1]
VIDEO = ROOT / 'custom_components/voip_stack/frontend/voip-stack-video.js'


@pytest.mark.skipif(shutil.which('node') is None, reason='Node.js unavailable')
def test_direction_update_rebuilds_only_changed_video_path():
    script = r'''
import assert from 'node:assert/strict';
import {pathToFileURL} from 'node:url';
globalThis.CustomEvent=class extends Event {constructor(type,options={}){super(type);this.detail=options.detail;}};
globalThis.VideoDecoder=class {};
globalThis.WebSocket={OPEN:1};
const {VoipStackVideo:Video}=await import(pathToFileURL(VIDEO));
const video=new Video();const socket={readyState:1};const call='direction-call';
const format={codec:'vp8',encoding:'VP8',clock_rate:90000,payload_type:104,format:'pt=104:VP8/90000;rtp-profile=RTP/AVP;direction=sendrecv'};
const both={can_receive:true,can_send:true,send:format,receive:format};
video._ws=socket;video._callId=call;video._active=true;video._generation=7;
video._negotiated=both;video._cameraAllowed=true;video._cameraEnabled=true;
video._canReceive=true;video._canSend=true;
const originalDecoder={state:'configured',close(){this.state='closed';}},originalEncoder={state:'configured'};
video._receiver._decoder=originalDecoder;video._sender._encoder=originalEncoder;
video._receiver._receiverHasFrame=true;
let receiverDisposals=0,senderDisposals=0,decoderCreates=0,senderCreates=0;
const cleanupReceiver=video._receiver._cleanupReceiver.bind(video._receiver);
video._receiver._cleanupReceiver=()=>{receiverDisposals++;cleanupReceiver();};
video._sender._cleanupSender=async()=>{senderDisposals++;video._sender._encoder=null;video._canSend=false;};
video._receiver._setupDecoder=async()=>{decoderCreates++;video._receiver._decoder={state:'configured',close(){this.state='closed';}};};
// _ensureSender remains production code: existing configured encoders are reused.
video._sender._replaceSender=async()=>{senderCreates++;video._sender._encoder={state:'configured'};};
const muted={...format,format:format.format.replace('direction=sendrecv','direction=recvonly')};
await video._applyMediaUpdate({...both,can_send:false,send:muted,receive:muted},socket,call);
assert.equal(receiverDisposals,0,'Disabling local TX must preserve remote decoder and last frame');
assert.equal(decoderCreates,0);assert.equal(video._receiver._decoder,originalDecoder);
assert.equal(video.visible,true);assert.equal(senderDisposals,1);
await video._applyMediaUpdate(both,socket,call);await Promise.resolve();
assert.equal(senderCreates,1,'Reenabled local camera creates one sender');
assert.equal(receiverDisposals,0);assert.equal(decoderCreates,0);
const retainedEncoder=video._sender._encoder;
await video._applyMediaUpdate({...both,can_receive:false},socket,call);
assert.equal(video._sender._encoder,retainedEncoder,'Remote camera pause must preserve local TX');
assert.equal(senderDisposals,1);assert.equal(senderCreates,1);
assert.equal(receiverDisposals,1);assert.equal(video.visible,false);
await video._applyMediaUpdate(both,socket,call);await Promise.resolve();
assert.equal(decoderCreates,1,'Remote camera resume creates one decoder');
assert.equal(senderDisposals,1);assert.equal(senderCreates,1);
assert.equal(video._generation,7,'Direction changes do not invalidate unaffected callbacks');
assert.equal(video._ws,socket);assert.equal(video._callId,call);
await video._applyMediaUpdate(both,socket,call);await Promise.resolve();
assert.equal(decoderCreates,1);assert.equal(senderCreates,1,'Duplicate SDP update is a media no-op');
// Codec changes retain the existing full generation rebuild.
await video._applyMediaUpdate({...both,receive:{...format,codec:'avc1.42E01F',encoding:'H264'}},socket,call);
await Promise.resolve();
assert.equal(video._generation,8);assert.equal(receiverDisposals,2);assert.equal(senderDisposals,2);
// A close/replacement during sender cleanup must suppress the old update.
const stale=new Video();stale._ws=socket;stale._callId=call;stale._active=true;
stale._negotiated=both;stale._canReceive=true;stale._receiver._decoder={state:'configured'};
let releaseCleanup;
stale._sender._cleanupSender=()=>new Promise(resolve=>{releaseCleanup=resolve;});
const pending=stale._applyMediaUpdate({...both,can_send:false},socket,call);
stale._generation++;stale._ws=null;stale._callId='';releaseCleanup();await pending;
assert.equal(stale._negotiated,both);assert.equal(stale._ws,null);
'''.replace('VIDEO', json.dumps(str(VIDEO)))
    result = subprocess.run(['node','--input-type=module','-e',script],cwd=ROOT,capture_output=True,text=True)
    assert result.returncode == 0, result.stderr or result.stdout
