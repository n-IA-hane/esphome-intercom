"""Execute the shared contact renderer against independent SIP state inputs."""
from pathlib import Path
import os
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
VOIP = Path(os.environ.get("VOIP_STACK_REPO", ROOT.parent / "esphome-voip-stack"))


@pytest.mark.parametrize("presence", [False, True], ids=["dialog-only", "with-presence"])
def test_contact_renderer_separates_presence_from_call_activity(tmp_path, presence):
    from tests.native_profile import by_id, profile

    config = profile("full-experience/single-bus/spotpear-ball-v2-full-afe.yaml")
    body = by_id(config["phone_list_view"], "contacts_list_view")["render_row"]
    include = tmp_path / "esphome/core"
    include.mkdir(parents=True)
    (include / "defines.h").write_text("")
    source = tmp_path / "render.cpp"
    source.write_text(r'''
#include <cassert>
#include <string>
#include "sip_blf.h"
#include "phonebook.h"
using namespace esphome::voip_stack;
#define id(x) x
struct Row {
 std::string id,scope,name,detail,leading,trailing;unsigned color=0;unsigned action=0;bool blink=false;
 void set_id(std::string_view v){id=v;}void set_scope(std::string_view v){scope=v;} void set_name(std::string_view v){name=v;}
 void set_detail(std::string_view v){detail=v;}void set_leading(std::string_view v){leading=v;}
 void set_trailing(std::string_view v){trailing=v;}
};
struct Phone {
 ContactEntry contact;BlfStateInfo current;
 const BlfDialogCache *office=nullptr,*other=nullptr;
 unsigned get_contact_count(){return 1;}
 const ContactEntry *get_contact(unsigned){return &contact;}
 std::string_view get_contact_account_view(const ContactEntry&entry)const{
  return entry.account_id.empty()?std::string_view("office"):std::string_view(entry.account_id.data(),entry.account_id.size());
 }
 BlfStateInfo get_contact_blf(std::string_view id,std::string_view scope){
  if(office==nullptr)return current;
  BlfStateInfo value;if(id!="101")return value;
  if(scope=="office")office->snapshot(&value);else if(scope=="other")other->snapshot(&value);
  return value;
 }
}phone;
void render(unsigned index,Row&row){BODY}
Row output(BlfState activity,PresenceState presence,bool valid=true){
 phone.current.state=activity;phone.current.presence=presence;phone.current.available=valid;
 phone.current.ringing_dialogs=activity==BlfState::RINGING?1:0;
 Row row;render(0,row);return row;
}
int main(){
 phone.contact.name="Test phone";phone.contact.route="stable";phone.contact.kind=ContactKind::BROWSER;
 {
  // The external notifier supplies only RFC 4235, with no presence package.
  BlfDialogCache cache;
  auto document=[](unsigned version,bool full,const std::string&body){
   return std::string("<dialog-info xmlns='urn:ietf:params:xml:ns:dialog-info' entity='sip:101@external.example' version='")+
    std::to_string(version)+"' state='"+(full?"full":"partial")+"'>"+body+"</dialog-info>";
  };
  auto cached=[&](){phone.current={};cache.snapshot(&phone.current);Row result;render(0,result);return result;};
  auto actual=cached();assert(actual.detail=="Unknown"&&actual.color==0xA3A3A3);
  assert(cache.apply(document(0,true,""))==BlfDocumentResult::APPLIED);
  actual=cached();assert(phone.current.available&&phone.current.presence==PresenceState::UNKNOWN);
  assert(actual.detail=="Idle"&&actual.color==0x4ADE80);
  assert(cache.apply(document(1,false,"<dialog id='call' direction='recipient'><state>early</state></dialog>"))==BlfDocumentResult::APPLIED);
  actual=cached();assert(actual.detail=="Ringing: pickup"&&actual.action==1&&actual.blink);
  assert(cache.apply(document(2,false,"<dialog id='call'><state>confirmed</state></dialog>"))==BlfDocumentResult::APPLIED);
  actual=cached();assert(actual.detail=="In call"&&actual.color==0xF87171);
  assert(cache.apply(document(3,false,"<dialog id='call'><state>terminated</state></dialog>"))==BlfDocumentResult::APPLIED);
  actual=cached();assert(actual.detail=="Idle"&&actual.color==0x4ADE80);
  assert(cache.apply(document(5,false,""))==BlfDocumentResult::NEED_FULL);
  actual=cached();assert(!phone.current.available&&actual.detail=="Unknown"&&actual.color==0xA3A3A3);
  assert(cache.apply(document(6,true,""))==BlfDocumentResult::APPLIED);
  actual=cached();assert(actual.detail=="Idle"&&actual.color==0x4ADE80);
 }
 {
  BlfDialogCache office,other;
  assert(office.apply("<dialog-info xmlns='urn:ietf:params:xml:ns:dialog-info' entity='sip:101@office.example' version='0' state='full'><dialog id='a'><state>confirmed</state></dialog></dialog-info>")==BlfDocumentResult::APPLIED);
  assert(other.apply("<dialog-info xmlns='urn:ietf:params:xml:ns:dialog-info' entity='sip:101@other.example' version='0' state='full'><dialog id='b' direction='recipient'><state>early</state></dialog></dialog-info>")==BlfDocumentResult::APPLIED);
  phone.office=&office;phone.other=&other;phone.contact.id="101";phone.contact.name="Same Desk";
  phone.contact.account_id="office";Row first;render(0,first);
  phone.contact.account_id="other";Row second;render(0,second);
  assert(first.id==second.id&&first.name==second.name&&first.scope=="office"&&second.scope=="other");
  assert(first.detail=="In call"&&first.color==0xF87171&&first.action==0);
  assert(second.detail=="Ringing: pickup"&&second.color==0x38BDF8&&second.action==1);
  phone.office=phone.other=nullptr;phone.contact.id.clear();phone.contact.account_id.clear();
 }
 auto row=output(BlfState::IDLE,PresenceState::CLOSED);
 assert(row.detail=="Unavailable"&&row.color==0xA3A3A3);
 assert(row.leading=="\U000F07D0"&&row.trailing=="\U000F0DEF");
 row=output(BlfState::IDLE,PresenceState::OPEN);assert(row.detail=="Idle"&&row.color==0x4ADE80);
 row=output(BlfState::IDLE,PresenceState::UNKNOWN);assert(row.detail=="Idle"&&row.color==0x4ADE80);
 row=output(BlfState::UNKNOWN,PresenceState::CLOSED,false);assert(row.detail=="Unavailable");
 row=output(BlfState::ACTIVE,PresenceState::CLOSED);assert(row.detail=="In call"&&row.color==0xF87171);
 row=output(BlfState::RINGING,PresenceState::UNKNOWN);assert(row.detail=="Ringing: pickup"&&row.action==1&&row.blink&&row.color==0x38BDF8);
 row=output(BlfState::CALLING,PresenceState::OPEN);assert(row.detail=="Calling"&&row.action==0&&!row.blink&&row.color==0x38BDF8);
 phone.current.state=BlfState::RINGING;phone.current.ringing_dialogs=0;
 row=Row{};render(0,row);assert(row.detail=="Ringing"&&row.action==0);
 row=output(BlfState::UNKNOWN,PresenceState::OPEN,false);assert(row.detail=="Unknown");
 // HA groups and services intentionally have no endpoint subscription.
 // Their absence of BLF data must not become a visible phone status.
 phone.contact.auto_blf=false;
 for (auto kind : {ContactKind::GROUP,ContactKind::AUTOMATION}) {
  phone.contact.kind=kind;
  row=output(BlfState::UNKNOWN,PresenceState::UNKNOWN,false);
  assert(row.detail.empty()&&row.trailing.empty()&&!row.blink&&row.action==0);
  assert(!row.leading.empty()&&!row.name.empty());
  // An explicit, valid watch still owns the displayed call activity.
  row=output(BlfState::ACTIVE,PresenceState::OPEN);
  assert(row.detail=="In call"&&row.color==0xF87171);
 }
 phone.contact.kind=ContactKind::BROWSER;
 row=output(BlfState::UNKNOWN,PresenceState::UNKNOWN,false);assert(row.detail=="Unknown");
 phone.contact.kind=ContactKind::GROUP;phone.contact.auto_blf=true;
 row=output(BlfState::UNKNOWN,PresenceState::UNKNOWN,false);assert(row.detail=="Unknown");
 for (auto kind : {ContactKind::ESP,ContactKind::BROWSER,ContactKind::SIP_ACCOUNT}) {
  phone.contact.kind=kind;phone.contact.number="00101";
  row=output(BlfState::IDLE,PresenceState::OPEN);assert(row.detail=="00101"&&row.color==0x4ADE80);
  row=output(BlfState::ACTIVE,PresenceState::OPEN);assert(row.detail=="In call");
  row=output(BlfState::IDLE,PresenceState::CLOSED);assert(row.detail=="Unavailable");
  phone.contact.number.clear();
  row=output(BlfState::IDLE,PresenceState::OPEN);assert(row.detail=="Idle");
 }
 phone.contact.kind=ContactKind::CONTACT;phone.contact.auto_blf=false;phone.contact.number="+3900123456";
 row=output(BlfState::UNKNOWN,PresenceState::UNKNOWN,false);
 assert(row.detail=="+3900123456"&&row.trailing.empty()&&!row.blink&&row.action==0);
 row=output(BlfState::IDLE,PresenceState::OPEN);assert(row.detail=="+3900123456");
 row=output(BlfState::RINGING,PresenceState::OPEN);assert(row.detail=="Ringing: pickup");
 for (auto kind : {ContactKind::GROUP,ContactKind::AUTOMATION}) {
  phone.contact.kind=kind;phone.contact.number="700";
  row=output(BlfState::UNKNOWN,PresenceState::UNKNOWN,false);assert(row.detail.empty());
 }
}
'''.replace("BODY", str(body)))
    binary = tmp_path / "render"
    compiled = subprocess.run(["g++", "-std=c++17", "-DUSE_ESP32", "-DUSE_ESPHOME_VOIP_BLF", "-DUSE_ESPHOME_VOIP_CONTACT_METADATA",
        *(["-DUSE_ESPHOME_VOIP_PRESENCE"] if presence else []),
        "-I", str(tmp_path), "-I", str(VOIP / "esphome/components/voip_stack"), str(source),
        str(VOIP / "esphome/components/voip_stack/sip_blf.cpp"), "-o", str(binary)],
        capture_output=True, text=True)
    assert compiled.returncode == 0, compiled.stderr
    result = subprocess.run([binary], capture_output=True, text=True, timeout=5, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
