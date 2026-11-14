"""Camera acquisition keeps the existing sender owner, clocks and backpressure."""

import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
FRONT = ROOT / "custom_components/voip_stack/frontend"


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
const track={getSettings:()=>({width:640,height:360,frameRate:15}),stop(){this.stopped=true;}};
const context=vm.createContext({EventTarget,performance,console,Blob,URL,setTimeout,clearTimeout,
  CustomEvent:class extends Event {constructor(type,init){super(type);this.detail=init?.detail;}},
  WebSocket:{OPEN:1,CONNECTING:0},VideoFrame:Frame,VideoEncoder:class {},
  navigator:{mediaDevices:{getUserMedia:async()=>({getVideoTracks:()=>[track],getTracks:()=>[track]})}},
  document:{body:{appendChild(){}},createElement:tag=>tag==='video'?new Video():new Canvas()},
});
const model=new vm.SourceTextModule(fs.readFileSync(MODEL,'utf8'),{context});
await model.link(()=>{throw Error('Unexpected model import');});await model.evaluate();
const module=new vm.SourceTextModule(fs.readFileSync(SOURCE,'utf8')+'\nexport {createVideoElementFrameReader};',{
  context,initializeImportMeta(meta){meta.url='https://ha.test/video.js';},importModuleDynamically:async()=>model,
});
await module.link(()=>{throw Error('Unexpected static import');});await module.evaluate();
const reader=module.namespace.createVideoElementFrameReader({},15);
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
const starting=sender._replaceSender('vp8',3,'',false);
const rejected=assert.rejects(starting,/cancelled|superseded/);
await new Promise(resolve=>setImmediate(resolve));
assert.ok(sender._senderSetupReader);
await sender._cleanupSender();await rejected;
assert.ok(track.stopped);assert.ok(videos.at(-1).removed);assert.equal(sender._senderSetupReader,null);
'''
    script = script.replace("SOURCE", json.dumps(str(FRONT / "voip-stack-video.js")))
    script = script.replace("MODEL", json.dumps(str(FRONT / "voip-stack-video-model.js")))
    result = subprocess.run(["node", "--no-warnings", "--experimental-vm-modules",
                             "--input-type=module", "-e", script], capture_output=True,
                            text=True, timeout=12)
    assert result.returncode == 0, result.stdout + result.stderr
