"""Negotiate a standard 20 ms SIP offer against the resolved WS3 FULL profile."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tests import native_profile

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / 'yamls/full-experience/single-bus/waveshare-s3-full-afe.yaml'
NATIVE = Path(os.environ.get('VOIP_STACK_REPO', ROOT.parent / 'esphome-voip-stack'))


@pytest.fixture(scope='module')
def ws3_sdp_binary(tmp_path_factory):
    resolved = native_profile.profile(PROFILE)
    audio = resolved['voip_stack']['audio']
    assert audio['tx']['sample_rate'] == 16000
    assert audio['rx']['sample_rate'] == 48000
    assert audio['rx']['frame_ms'] == 10

    fixture_path = NATIVE / 'tests/test_sdp_receive_payloads.py'
    spec = importlib.util.spec_from_file_location('native_sdp_payload_fixture', fixture_path)
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    sys.path.insert(0, str(NATIVE / 'tests'))
    try:
        source = fixture.sdp_fixture_source()
    finally:
        sys.path.remove(str(NATIVE / 'tests'))

    initializers = []
    for direction in ('tx', 'rx'):
        formats = [audio[direction], *audio.get(direction + '_formats', [])]
        assert len(formats) <= 8
        initializers.append(f's.offer_{direction}_formats_.count=0;')
        for fmt in formats:
            assert fmt['pcm_format'] == 's16le' and fmt.get('codec', 'pcm') == 'pcm'
            initializers.append(
                f's.offer_{direction}_formats_.formats[s.offer_{direction}_formats_.count++]='
                f'AudioFormat{{{fmt["sample_rate"]},PcmFormat::S16LE,{fmt["channels"]},{fmt["frame_ms"]}}};')
    source += '\nvoid configure(SipTransport &s){\n' + '\n'.join(initializers) + '\n}\n'
    source += r'''
int main(int argc,char **argv){
 assert(argc==2);SipTransport s;configure(s);
 const std::string scenario=argv[1];
 const std::string base="v=0\r\no=- 1 1 IN IP4 192.0.2.2\r\ns=standard SIP peer\r\n"
                        "c=IN IP4 192.0.2.2\r\nt=0 0\r\n";
 if(scenario=="ha-10"){
  assert(s.learn_remote_rtp_from_sdp_(base+
      "m=audio 40002 RTP/AVP 126 118\r\na=rtpmap:126 L16/48000/1\r\n"
      "a=rtpmap:118 L16/16000/1\r\na=ptime:10\r\na=sendrecv\r\n",0));
  assert(s.tx.sample_rate==16000&&s.rx.sample_rate==48000);
  assert(s.tx.frame_ms==10&&s.rx.frame_ms==10);
  return 0;
 }
 const std::string offer=base+"m=audio 40002 RTP/AVP 118\r\n"
     "a=rtpmap:118 L16/16000/1\r\na=ptime:20\r\na="+scenario+"\r\n";
 assert(s.learn_remote_rtp_from_sdp_(offer,0)&&"shipped FULL must accept standard L16/16000 mono at 20 ms");
 const bool tx=scenario!="sendonly",rx=scenario!="recvonly";
 assert(s.tx.is_valid()==tx&&s.rx.is_valid()==rx);
 if(tx){assert(s.tx.sample_rate==16000&&s.tx.channels==1&&s.tx.frame_ms==20);assert(s.tx_pt==118);}
 if(rx){assert(s.rx.sample_rate==16000&&s.rx.channels==1&&s.rx.frame_ms==20);assert(s.rx_pt==118);}
 const std::string answer=s.build_sdp_answer_();
 const std::string direction=tx?(rx?"sendrecv":"sendonly"):"recvonly";
 assert(answer.find("a="+direction+"\r\n")!=std::string::npos);
 assert(answer.find("a=ptime:20\r\n")!=std::string::npos);
 assert(answer.find("L16/16000/1")!=std::string::npos);
}
'''
    directory = tmp_path_factory.mktemp('ws3-real-sdp')
    cpp = directory / 'ws3.cpp'
    cpp.write_text(source)
    binary = directory / 'ws3'
    result = subprocess.run(['g++', '-std=c++17', '-I', str(NATIVE), str(cpp), '-o', str(binary)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return binary


@pytest.mark.parametrize('scenario', ['sendrecv', 'sendonly', 'recvonly', 'ha-10'])
def test_ws3_full_standard_sip_packet_time(ws3_sdp_binary, scenario):
    result = subprocess.run([str(ws3_sdp_binary), scenario], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
