"""An unanswered call retains its SIP owner only until terminal completion."""
import importlib.util
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
VOIP = ROOT.parent / "esphome-voip-stack"


def test_offline_peer_cancel_timeout_releases_transport_for_redial(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(VOIP / "tests"))
    spec = importlib.util.spec_from_file_location("contact_wire", VOIP / "tests/test_refer_wire_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    helpers = (VOIP / "tests/fixtures/refer_wire_main.cpp").read_text().split("void auth_case(")[0]
    source = module.wire_source() + "\n" + helpers + r'''
int main(){
 SipTransport s;Peer remote(false);setup(s,remote);s.call_id_.clear();s.media_active_=false;
 assert(s.send_invite_unlocked_("offline","esp","esp","peer","peer"));
 auto invite=remote.drain();assert(invite.size()==1&&invite[0].rfind("INVITE ",0)==0);
 // A registrar/proxy can acknowledge receipt while the destination is offline.
 // RFC3261 section9.1 permits CANCEL after that provisional response.
 remote.send_to(s,response(invite[0],100));remote.drain();
 assert(s.send_cancel_unlocked_("offline"));
 auto cancel=remote.drain();assert(cancel.size()==1&&cancel[0].rfind("CANCEL ",0)==0);
 assert(header_value(cancel[0],"Call-ID")==header_value(invite[0],"Call-ID"));
 assert(via_branch(header_value(cancel[0],"Via"))==via_branch(header_value(invite[0],"Via")));
 assert(s.terminal_transaction_pending_locked_());
 clock_ms=s.pending_cancel_.deadline_ms+1;s.pump_udp_retransmits_unlocked_();
 assert(!s.terminal_transaction_pending_locked_()&&s.call_id_.empty());
 bool terminal=false;for(const auto&e:s.delivered)terminal|=e.type==SipSignalType::TERMINAL_COMPLETE;
 assert(terminal&&"terminal completion must release the application owner");
 remote.drain();
 assert(s.send_invite_unlocked_("redial","esp","esp","peer","peer"));
 auto retry=remote.drain();assert(retry.size()==1&&retry[0].rfind("INVITE ",0)==0);
 assert(header_value(retry[0],"Call-ID")!=header_value(invite[0],"Call-ID"));
 cleanup(s);
}
'''
    cpp = tmp_path / "cancel.cpp"
    cpp.write_text(source)
    binary = tmp_path / "cancel"
    result = subprocess.run(["g++", "-std=c++17", "-pthread", "-I", str(VOIP),
                             str(cpp), "-o", str(binary)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
