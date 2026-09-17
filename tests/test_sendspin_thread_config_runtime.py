"""Sendspin worker configuration preserves the caller's pthread settings."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_worker_stack_capabilities_and_configuration_lifetime(tmp_path):
    (tmp_path / "esp_heap_caps.h").write_text(
        "#pragma once\n"
        "#define MALLOC_CAP_SPIRAM 1024\n"
        "#define MALLOC_CAP_8BIT 4\n"
        "#define MALLOC_CAP_INTERNAL 2048\n"
    )
    (tmp_path / "esp_pthread.h").write_text(r'''
#pragma once
#include <cstddef>
#include "esp_heap_caps.h"
constexpr int ESP_OK = 0;
constexpr int ESP_ERR_INVALID_ARG = 1;
constexpr int ESP_ERR_NOT_FOUND = 2;
struct esp_pthread_cfg_t {
  size_t stack_size = 3072;
  int prio = 5;
  const char *thread_name = "pthread";
  unsigned stack_alloc_caps = 0;
};
inline esp_pthread_cfg_t current;
inline bool present = false;
inline bool reject_configuration = false;
inline esp_pthread_cfg_t esp_pthread_get_default_config() { return {}; }
inline int esp_pthread_get_cfg(esp_pthread_cfg_t *out) {
  if (!present) return ESP_ERR_NOT_FOUND;
  *out = current;
  return ESP_OK;
}
inline int esp_pthread_set_cfg(const esp_pthread_cfg_t *value) {
  if (reject_configuration || value->stack_size < 768 ||
      (value->stack_alloc_caps && !(value->stack_alloc_caps & MALLOC_CAP_8BIT))) {
    return ESP_ERR_INVALID_ARG;
  }
  current = *value;
  if (!current.stack_alloc_caps) {
    current.stack_alloc_caps = MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT;
  }
  present = true;
  return ESP_OK;
}
''')
    source = tmp_path / "thread_config.cpp"
    source.write_text(r'''
#include "thread_config.h"
#include <cassert>
#include <cstring>
int main() {
  {
    sendspin::PlatformThreadConfig worker("Sendspin", 6192, 6, true);
    assert(worker.valid());
    assert(current.stack_size == 6192 && current.prio == 6);
    assert(current.stack_alloc_caps == (MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    assert(std::strcmp(current.thread_name, "Sendspin") == 0);
  }
  assert(current.stack_size == 3072 && current.prio == 5);
  esp_pthread_cfg_t caller;
  caller.stack_size = 9000;
  caller.prio = 4;
  caller.thread_name = "caller";
  assert(esp_pthread_set_cfg(&caller) == ESP_OK);
  {
    sendspin::PlatformThreadConfig worker("SsArt", 4096, 2, false);
    assert(worker.valid());
    assert(current.stack_size == 4096 && current.prio == 2);
    assert(current.stack_alloc_caps == (MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
  }
  assert(current.stack_size == 9000 && current.prio == 4);
  assert(std::strcmp(current.thread_name, "caller") == 0);
  reject_configuration = true;
  {
    sendspin::PlatformThreadConfig worker("Sendspin", 6192, 6, true);
    assert(!worker.valid());
  }
  assert(current.stack_size == 9000 && current.prio == 4);
}
''')
    binary = tmp_path / "thread_config"
    subprocess.run(
        ["g++", "-std=c++17", "-DESP_PLATFORM", "-I", str(tmp_path),
         "-I", str(ROOT / "esphome/components/sendspin_compat/voip_sendspin_compat"),
         str(source), "-o", str(binary)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["bash", "-c", 'ulimit -c 0; exec "$1"', "bash", str(binary)],
        check=True, capture_output=True, text=True,
    )
