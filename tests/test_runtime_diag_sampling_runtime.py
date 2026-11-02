"""Exercise production task sampling and log chunks across changing task sets."""

import json
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("trace", [0, 1])
def test_snapshot_task_identity_and_chunk_round_trip(tmp_path, trace):
    header = ROOT / "esphome/components/runtime_diag/runtime_diag.h"
    source = header.with_suffix(".cpp").read_text()
    helpers = source.split("namespace {", 1)[1].split("}  // namespace\n", 1)[0]
    snapshot = source.split("std::string RuntimeDiag::build_snapshot_(", 1)[1]
    snapshot = "std::string RuntimeDiag::build_snapshot_(" + snapshot.rsplit("}  // namespace runtime_diag", 1)[0]
    capture = source.split("void RuntimeDiag::capture(", 1)[1].split("void RuntimeDiag::capture_heap(", 1)[0]
    capture = "void RuntimeDiag::capture(" + capture
    for name, body in {
        "freertos/FreeRTOS.h": "#pragma once\n#include <cstdint>\nusing UBaseType_t=unsigned;using BaseType_t=int;\n",
        "freertos/task.h": "#pragma once\n",
        "esphome/core/component.h": "#pragma once\nnamespace esphome {struct Component {virtual void setup(){};virtual void loop(){};virtual void dump_config(){};};}\n",
    }.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    cpp = tmp_path / "sample.cpp"
    cpp.write_text(r'''
#include <algorithm>
#include <cassert>
#include <cstdarg>
#include <cstdio>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <sstream>
#include <string>
#include <utility>
#include <vector>
enum eTaskState {eRunning,eReady,eBlocked,eSuspended,eDeleted,eInvalid};
struct TaskStatus_t {
 unsigned xTaskNumber; const char *pcTaskName; eTaskState eCurrentState;
 int xCoreID; unsigned uxCurrentPriority,uxBasePriority,usStackHighWaterMark,ulRunTimeCounter;
};
struct multi_heap_info_t {unsigned total_allocated_bytes=0,allocated_blocks=0;};
constexpr unsigned MALLOC_CAP_INTERNAL=1,MALLOC_CAP_DMA=2;
void heap_caps_get_info(multi_heap_info_t*,unsigned){}
unsigned heap_caps_get_free_size(unsigned){return 100;}
unsigned heap_caps_get_largest_free_block(unsigned){return 100;}
unsigned heap_caps_get_minimum_free_size(unsigned){return 100;}
static std::vector<TaskStatus_t> input;
static unsigned total=0;static int64_t now=1000;
unsigned millis(){return now/1000;}
int64_t esp_timer_get_time(){return now;}
unsigned uxTaskGetSystemState(TaskStatus_t *out,unsigned capacity,uint32_t *runtime){
 assert(input.size()<=capacity);std::copy(input.begin(),input.end(),out);*runtime=total;return input.size();
}
static std::string joined;
void record(const char*,const char *format,...){
 char buffer[512];va_list args;va_start(args,format);vsnprintf(buffer,sizeof(buffer),format,args);va_end(args);
 const std::string line=buffer;joined+=line.substr(line.find("] ")+2);
}
#define ESP_LOGI(...) record(__VA_ARGS__)
#define configTASKLIST_INCLUDE_COREID 0
''' + f'#include "{header}"\n' + r'''
namespace esphome::runtime_diag {
static const char *const TAG="runtime_diag";
namespace {
''' + helpers + "}\n" + snapshot + capture + r'''
void RuntimeDiag::setup(){} void RuntimeDiag::loop(){} void RuntimeDiag::dump_config(){}
}
struct Probe:esphome::runtime_diag::RuntimeDiag {
 std::string sample(){return build_snapshot_("probe");}
};
TaskStatus_t task(unsigned id,unsigned runtime){return {id,"task",eReady,0,1,1,512,runtime};}
int main(){
 Probe probe;
 input={task(1,10),task(2,20)};total=100;
 std::cout<<probe.sample()<<'\n';
 now=2000;input={task(2,30),task(1,14),task(3,5)};total=200;
 std::cout<<probe.sample()<<'\n';
 now=3000;input={task(1,17)};total=300;
 std::cout<<probe.sample()<<'\n';
 now=4000;input={task(3,20),task(1,20)};total=400;
 std::cout<<probe.sample()<<'\n';
 now=5000;input.clear();total=500;std::cout<<probe.sample()<<'\n';
 now=6000;input={task(1,50)};total=600;std::cout<<probe.sample()<<'\n';
 now=7000;input.clear();for(unsigned i=0;i<48;++i)input.push_back(task(i+100,10));total=700;
 std::cout<<probe.sample()<<'\n';
 now=8000;std::reverse(input.begin(),input.end());for(auto &t:input)t.ulRunTimeCounter+=3;total=800;
 std::cout<<probe.sample()<<'\n';
 std::string reason(900,'x');reason+="\"\\\t\n";
 probe.capture(reason.c_str());std::cout<<joined<<'\n';
}
''')
    binary = tmp_path / "sample"
    result = subprocess.run(
        ["g++", "-std=c++20", "-Wall", "-Wextra", "-Wno-unused-function",
         f"-DconfigUSE_TRACE_FACILITY={trace}", f"-I{tmp_path}", str(cpp), "-o", str(binary)],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stdout + result.stderr
    samples = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(samples) == 9
    assert samples[-1]["reason"] == "x" * 900 + '"\\\t\n'
    if not trace:
        assert all(sample["tasks"] == "trace_facility_disabled" for sample in samples)
        return
    assert [task["delta"] for task in samples[1]["tasks"]] == [10, 4, 0]
    assert [task["delta"] for task in samples[2]["tasks"]] == [3]
    assert [task["delta"] for task in samples[3]["tasks"]] == [0, 3]
    assert samples[4]["tasks"] == []
    assert samples[5]["tasks"][0]["delta"] == 0
    assert len(samples[7]["tasks"]) == 48
    assert all(task["delta"] == 3 for task in samples[7]["tasks"])
