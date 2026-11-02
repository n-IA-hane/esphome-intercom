"""Controlled completion order on the actual worker handler, separate from codec proof."""

import json
from pathlib import Path
import subprocess

import pytest

WORKER = (
    Path(__file__).resolve().parents[1]
    / "custom_components/voip_stack/frontend/voip-stack-video-worker.js"
)


@pytest.mark.js_runtime
@pytest.mark.parametrize("completion", ["resolve", "reject"])
def test_old_capability_result_cannot_replace_or_close_new_decoder(completion):
    script = r"""
import fs from 'node:fs';import vm from 'node:vm';
const pending=new Map(),messages=[],created=[];
class Decoder {
 constructor(){this.state='unconfigured';created.push(this);}
 static isConfigSupported(config){return new Promise((resolve,reject)=>pending.set(config.codec,{resolve,reject,config}));}
 configure(config){this.state='configured';this.codec=config.codec;}
 close(){this.state='closed';}
 addEventListener(){}
 decode(){this.decoded=(this.decoded||0)+1;}
}
const self={postMessage:m=>messages.push(m),close(){}};
const context=vm.createContext({self,VideoDecoder:Decoder,EncodedVideoChunk:class{},Uint8Array,Number,Set,Map,clearTimeout,setTimeout});
vm.runInContext(fs.readFileSync(process.argv[2],'utf8'),context);
const configure=(requestId,generation,codec)=>self.onmessage({data:{type:'configure_decoder',requestId,generation,config:{codec}}});
const old=configure(1,1,'old');const current=configure(2,2,'current');
pending.get('current').resolve({supported:true,config:{codec:'current'}});await current;
if(process.argv[3]==='reject')pending.get('old').reject(new Error('old capability failure'));
else pending.get('old').resolve({supported:true,config:{codec:'old'}});
await old;
await self.onmessage({data:{type:'decode',generation:2,keyFrame:true,timestamp:0,buffer:new ArrayBuffer(1),length:1}});
console.log(JSON.stringify({created:created.map(d=>({codec:d.codec,state:d.state,decoded:d.decoded||0})),messages}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-", str(WORKER), completion],
        input=script,
        capture_output=True,
        text=True,
        check=True,
    )
    evidence = json.loads(result.stdout)
    assert evidence["created"] == [
        {"codec": "current", "state": "configured", "decoded": 1}
    ], evidence
