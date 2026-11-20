"""Camera acquisition keeps the existing sender owner, clocks and backpressure."""

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
FRONT = ROOT / "custom_components/voip_stack/frontend"
pytestmark = pytest.mark.js_runtime


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
@pytest.mark.parametrize("callback_mode", ["rvfc", "timer"])
def test_actual_fallback_reader_ends_pending_read_when_selected_track_ends(callback_mode):
    script = r'''
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const videos=[], timers=new Map();let timerId=0, pendingPlay=false;
class Video {
  constructor(){this.style={};this.readyState=4;this.videoWidth=640;this.videoHeight=360;videos.push(this);}
  setAttribute(){} play(){this.played=true;return pendingPlay?new Promise(()=>{}):Promise.resolve();}
  pause(){this.paused=true;} remove(){this.removed=(this.removed||0)+1;}
  requestVideoFrameCallback(callback){this.callback=callback;return 1;}
  cancelVideoFrameCallback(){this.callback=null;}
}
if(MODE==='timer')delete Video.prototype.requestVideoFrameCallback;
class Track extends EventTarget {
  constructor(kind){super();this.kind=kind;this.readyState='live';}
  end(){this.readyState='ended';this.dispatchEvent(new Event('ended'));}
  stop(){this.readyState='ended';}
}
const track=new Track('video'), audio=new Track('audio'), other=new Track('video');
const stream={getVideoTracks:()=>[other,track],getTracks:()=>[track,audio,other],get active(){return audio.readyState==='live';}};
const context=vm.createContext({EventTarget,Event,performance,console,URL,Blob,
  setTimeout:(cb)=>{timers.set(++timerId,cb);return timerId;},clearTimeout:id=>timers.delete(id),
  CustomEvent:class extends Event {constructor(type,init){super(type);this.detail=init?.detail;}},
  document:{body:{appendChild(){}},createElement:tag=>tag==='video'?new Video():{getContext:()=>({})}},
});
const {createFrontendLoader}=await import(LOADER);
const modules=createFrontendLoader(FRONTEND,context,'https://ha.test/voip-stack-video.js?v=reader',
  {exposeFrameReader:true});
const module=await modules.load('voip-stack-video.js');
const readerModule=await modules.load('voip-stack-video-sender.js');
const reader=readerModule.namespace.createVideoElementFrameReader(stream,15,track);
await reader.start();let ended=false;
const pending=reader.read().then(value=>{ended=value.done;return value;});
await assert.rejects(reader.read(),/already pending/);
other.end();await Promise.resolve();assert.equal(ended,false,'another video track is not this reader owner');
const late=videos[0].callback||[...timers.values()][0];
track.end();await Promise.resolve();await Promise.resolve();
assert.equal(stream.active,true,'a live audio track must not hide video EOF');
assert.equal(ended,true,'actual fallback adapter must resolve a pending read at selected-track EOF');
assert.equal((await pending).done,true);assert.equal((await reader.read()).done,true);
assert.ok(videos[0].removed&&videos[0].paused);assert.equal(timers.size,0);
assert.ok(!videos[0].callback);await reader.cancel();track.end();await reader.cancel();
late?.(0,{mediaTime:1});assert.equal(timers.size,0);assert.equal(videos[0].removed,1);

// stop() does not emit ended. The existing sender must cancel explicitly.
const stopped=new Track('video');const explicit=readerModule.namespace.createVideoElementFrameReader(stream,15,stopped);
await explicit.start();const stopping=explicit.read();stopped.stop();await explicit.cancel();
assert.equal((await stopping).done,true);assert.equal(videos.at(-1).removed,1);

// Check readyState too, including tracks which ended before listener setup.
const before=new Track('video');before.stop();
const already=readerModule.namespace.createVideoElementFrameReader(stream,15,before);
await assert.rejects(already.start(),/cancelled/);assert.equal(videos.at(-1).played,undefined);
assert.equal((await already.read()).done,true);
const between=new Track('video');const checked=readerModule.namespace.createVideoElementFrameReader(stream,15,between);
await checked.start();between.stop();assert.equal((await checked.read()).done,true);

pendingPlay=true;const startingTrack=new Track('video');
const startup=readerModule.namespace.createVideoElementFrameReader(stream,15,startingTrack);
const starting=startup.start();const rejected=assert.rejects(starting,/cancelled/);
startingTrack.end();await rejected;assert.equal(timers.size,0);
await startup.cancel();assert.equal(videos.at(-1).removed,1);

// An old source ending after cancellation cannot touch the replacement reader.
pendingPlay=false;const newTrack=new Track('video');
const replacement=readerModule.namespace.createVideoElementFrameReader(stream,15,newTrack);
await replacement.start();const replacementVideo=videos.at(-1);const replacementRead=replacement.read();
track.end();assert.ok(!replacementVideo.removed);assert.ok(replacementVideo.callback||timers.size);
newTrack.end();assert.equal((await replacementRead).done,true);assert.equal(timers.size,0);

// EOF from the actual adapter reaches the existing encoder-owner finally.
const ownerTrack=new Track('video');const ownerStream={getTracks:()=>[ownerTrack]};
const owned=readerModule.namespace.createVideoElementFrameReader(ownerStream,15,ownerTrack);await owned.start();
const sender=new module.namespace.VoipStackVideo();
Object.assign(sender,{_generation:7,_cameraEnabled:true,_cameraAllowed:true,_canSend:true});
Object.assign(sender._sender,{_senderGeneration:2,_cameraReader:owned,_cameraStream:ownerStream});
const encoder={state:'configured',close(){this.state='closed';}};sender._sender._encoder=encoder;
const encoding=sender._sender._encodeCamera(15,owned,encoder,7,2);ownerTrack.end();await encoding;
assert.equal(sender._canSend,false);assert.equal(sender._sender._cameraReader,null);
assert.equal(sender._sender._cameraStream,null);assert.equal(sender._sender._encoder,null);assert.equal(encoder.state,'closed');

// A superseded encode loop must leave its replacement's owner untouched.
const oldTrack=new Track('video');const oldReader=readerModule.namespace.createVideoElementFrameReader(stream,15,oldTrack);
await oldReader.start();const oldEncoder={state:'configured',close(){throw Error('old finally touched encoder');}};
sender._canSend=true;
Object.assign(sender._sender,{_cameraReader:oldReader,_encoder:oldEncoder,_cameraStream:stream});
const oldEncoding=sender._sender._encodeCamera(15,oldReader,oldEncoder,7,2);
sender._sender._senderGeneration=3;sender._sender._cameraReader=replacement;sender._sender._encoder=encoder;
oldTrack.end();await oldEncoding;assert.equal(sender._sender._cameraReader,replacement);
assert.equal(sender._sender._encoder,encoder);assert.equal(sender._canSend,true);
'''
    script = script.replace("FRONTEND", json.dumps(str(FRONT)))
    script = script.replace("LOADER", json.dumps(str(ROOT / "tests/support/frontend_module_loader.js")))
    script = script.replace("MODE", json.dumps(callback_mode))
    result = subprocess.run(["node", "--no-warnings", "--experimental-vm-modules",
                             "--input-type=module", "-e", script], capture_output=True,
                            text=True, timeout=12)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is required")
def test_fallback_reader_clock_geometry_backpressure_and_pending_start_cleanup():
    script = r'''
import fs from 'node:fs';
import vm from 'node:vm';
import assert from 'node:assert/strict';
const videos=[], drawings=[];
let pendingPlay=false;
class Video {
  constructor(){this.style={};this.readyState=4;this.videoWidth=360;this.videoHeight=640;this.currentTime=0;videos.push(this);}
  setAttribute(){}
  play(){return pendingPlay?new Promise(()=>{}):Promise.resolve();}
  pause(){this.paused=true;}
  remove(){this.removed=true;}
  requestVideoFrameCallback(callback){this.callback=callback;return 1;}
  cancelVideoFrameCallback(){this.callback=null;}
  frame(time){const cb=this.callback;this.callback=null;cb(0,{mediaTime:time});}
}
class Canvas {
  getContext(){return {fillRect(){},drawImage(...args){drawings.push(args);}};}
}
class Frame {
  constructor(source,{timestamp}){this.timestamp=timestamp;this.width=source.width;this.height=source.height;}
  close(){this.closed=true;}
}
const track=new EventTarget();Object.assign(track,{
  readyState:'live',getSettings:()=>({width:640,height:360,frameRate:15}),
  stop(){this.stopped=true;this.readyState='ended';},
});
const context=vm.createContext({EventTarget,performance,console,Blob,URL,setTimeout,clearTimeout,
  CustomEvent:class extends Event {constructor(type,init){super(type);this.detail=init?.detail;}},
  WebSocket:{OPEN:1,CONNECTING:0},VideoFrame:Frame,VideoEncoder:class {},
  navigator:{mediaDevices:{getUserMedia:async()=>({getVideoTracks:()=>[track],getTracks:()=>[track]})}},
  document:{body:{appendChild(){}},createElement:tag=>tag==='video'?new Video():new Canvas()},
});
const {createFrontendLoader}=await import(LOADER);
const modules=createFrontendLoader(FRONTEND,context,'https://ha.test/voip-stack-video.js?v=reader',
  {exposeFrameReader:true});
const module=await modules.load('voip-stack-video.js');
const readerModule=await modules.load('voip-stack-video-sender.js');
const reader=readerModule.namespace.createVideoElementFrameReader({},15,track);
await reader.start();
const video=videos.at(-1);
assert.equal(video.callback,undefined,'no frame callback before demand');
reader.setTarget({width:640,height:360});
let next=reader.read();video.frame(0);let result=await next;
assert.equal(result.value.timestamp,0);result.value.close();
assert.equal(video.callback,null,'completed read must not keep capture callbacks running');
next=reader.read();video.frame(.1);result=await next;
assert.equal(result.value.timestamp,100000);
const draw=drawings.at(-1);assert.equal(draw[3]/draw[4],360/640,'fit preserves source aspect');
assert.equal(result.value.width,640);assert.equal(result.value.height,360);result.value.close();
next=reader.read();video.frame(0);result=await next;
assert.ok(result.value.timestamp>100000,'clock discontinuity cannot reverse timestamps');result.value.close();
next=reader.read();await reader.cancel();assert.equal((await next).done,true);
assert.ok(video.removed&&video.paused);assert.equal(video.srcObject,null);
assert.equal((await reader.read()).done,true);

// Closing the existing sender must cancel a video.play() that never settles.
pendingPlay=true;
const sender=new module.namespace.VoipStackVideo();
sender._generation=3;sender._cameraEnabled=true;sender._cameraAllowed=true;
sender._negotiated={can_send:true,can_receive:false,send:{codec:'vp8',encoding:'VP8',clock_rate:90000,payload_type:104}};
const starting=sender._sender._replaceSender('vp8',3,'',false);
const rejected=assert.rejects(starting,/cancelled|superseded/);
await new Promise(resolve=>setImmediate(resolve));
assert.ok(sender._sender._senderSetupReader);
await sender._sender._cleanupSender();await rejected;
assert.ok(track.stopped);assert.ok(videos.at(-1).removed);assert.equal(sender._sender._senderSetupReader,null);
'''
    script = script.replace("FRONTEND", json.dumps(str(FRONT)))
    script = script.replace("LOADER", json.dumps(str(ROOT / "tests/support/frontend_module_loader.js")))
    result = subprocess.run(["node", "--no-warnings", "--experimental-vm-modules",
                             "--input-type=module", "-e", script], capture_output=True,
                            text=True, timeout=12)
    assert result.returncode == 0, result.stdout + result.stderr
