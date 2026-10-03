"""Execute the experimental task owner's core selection and cleanup on the host."""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_inference_task_affinity_and_reuse(tmp_path):
    includes = tmp_path / "include"
    (includes / "freertos").mkdir(parents=True)
    (includes / "esphome/core").mkdir(parents=True)
    (includes / "freertos/FreeRTOS.h").write_text('''#pragma once
#include <cstdint>
using BaseType_t = int;
using UBaseType_t = unsigned;
using StackType_t = uint8_t;
using TaskHandle_t = void *;
using TaskFunction_t = void (*)(void *);
struct StaticTask_t {};
constexpr BaseType_t tskNO_AFFINITY = 0x7fffffff;
''')
    (includes / "freertos/task.h").write_text('''#pragma once
#include "FreeRTOS.h"
inline int selected_core=-2, creates=0, deletes=0;
inline bool fail_create=false, parked=true;
inline TaskHandle_t xTaskCreateStaticPinnedToCore(TaskFunction_t, const char *, uint32_t,
    void *, UBaseType_t, StackType_t *, StaticTask_t *, BaseType_t core) {
  selected_core=core; ++creates; return fail_create ? nullptr : reinterpret_cast<void *>(1);
}
inline void vTaskSuspend(TaskHandle_t) {}
enum eTaskState { eRunning, eSuspended };
inline eTaskState eTaskGetState(TaskHandle_t) { return parked ? eSuspended : eRunning; }
inline void vTaskDelete(TaskHandle_t) { ++deletes; }
''')
    (includes / "esphome/core/helpers.h").write_text('''#pragma once
#include <cstddef>
inline int allocations=0, frees=0;
namespace esphome {
template<class T> struct RAMAllocator {
  enum { ALLOC_EXTERNAL, ALLOC_INTERNAL };
  explicit RAMAllocator(int) {}
  T *allocate(size_t n) { ++allocations; return new T[n]; }
  void deallocate(T *p, size_t) { ++frees; delete[] p; }
};
}
''')
    harness = tmp_path / "test.cpp"
    harness.write_text('''#include <cassert>
#include "inference_task.h"
#include "esphome/core/helpers.h"
int main() {
  using esphome::micro_wake_word::InferenceTask;
  InferenceTask task;
  assert(task.create(nullptr,"mww",3072,nullptr,8,false));
  assert(selected_core==tskNO_AFFINITY && allocations==1);
  assert(!task.create(nullptr,"mww",3072,nullptr,8,false,1));
  assert(creates==1);
  parked=false;
  assert(!task.destroy() && task.is_created() && deletes==0);
  parked=true;
  assert(task.destroy() && !task.is_created() && frees==0);
  assert(task.create(nullptr,"mww",3072,nullptr,8,false,1));
  assert(selected_core==1 && allocations==1);
  assert(task.destroy());
  assert(task.create(nullptr,"mww",3072,nullptr,8,false,0));
  assert(selected_core==0 && allocations==1);
  assert(task.deallocate() && frees==1 && !task.is_created());
  assert(task.deallocate() && frees==1);
  fail_create=true;
  assert(!task.create(nullptr,"mww",3072,nullptr,8,false,1));
  assert(!task.is_created() && allocations==frees);
  fail_create=false;
  assert(task.create(nullptr,"mww",3072,nullptr,8,true,1));
  assert(selected_core==1);
  assert(task.deallocate() && allocations==frees);
}
''')
    component = ROOT / "esphome/components/micro_wake_word"
    executable = tmp_path / "test"
    subprocess.run([
        "g++", "-std=c++17", "-DUSE_ESP32", "-I", str(includes), "-I", str(component),
        str(harness), str(component / "inference_task.cpp"), "-o", str(executable),
    ], check=True, capture_output=True, text=True)
    subprocess.run([str(executable)], check=True)


def test_single_core_target_rejects_core_one(monkeypatch):
    import importlib.util
    import pytest
    import esphome.config_validation as cv

    spec = importlib.util.spec_from_file_location(
        "mww_affinity_config", ROOT / "esphome/components/micro_wake_word/__init__.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.esp32, "get_esp32_variant", lambda: module.esp32.VARIANT_ESP32S2)
    for core in (-1, 0):
        assert module._validate_task_core({"task_core": core})["task_core"] == core
    with pytest.raises(cv.Invalid, match="dual-core"):
        module._validate_task_core({"task_core": 1})
    monkeypatch.setattr(module.esp32, "get_esp32_variant", lambda: module.esp32.VARIANT_ESP32S3)
    assert module._validate_task_core({"task_core": 1})["task_core"] == 1
