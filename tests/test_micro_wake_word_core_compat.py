"""Borrow PCM and call each core's owner API without allocation or legacy shims."""

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/micro_wake_word"


def test_core_api_adapters_preserve_bytes_without_allocating(tmp_path):
    source = tmp_path / "compat.cpp"
    source.write_text(r"""
#include "core_compat.h"
#include <cassert>
#include <cstdlib>
#include <cstring>
#include <new>
#include <span>
#include <type_traits>

static size_t allocations = 0;
void *operator new(size_t size) {
  ++allocations;
  if (void *p = std::malloc(size)) return p;
  throw std::bad_alloc();
}
void operator delete(void *p) noexcept { std::free(p); }
void operator delete(void *p, size_t) noexcept { std::free(p); }

// These signatures match the two supported core interfaces. The actual core
// headers and link are checked by their separate firmware builds.
struct AudioCapture { std::span<const uint8_t> data; };
struct LegacySource {
  unsigned calls{0}; uint32_t timeout{0}; bool shift{true};
  size_t fill(uint32_t ticks, bool pre_shift) {
    ++calls; timeout = ticks; shift = pre_shift; return 128;
  }
};
struct CurrentSource {
  unsigned calls{0}, deprecated_calls{0}; uint32_t timeout{0};
  size_t fill(uint32_t ticks) { ++calls; timeout = ticks; return 128; }
  size_t fill(uint32_t, bool) { ++deprecated_calls; return 0; }
};
struct LegacyStatus {
  const char *name{nullptr}; uint32_t duration{0};
  void status_momentary_error(const char *value, uint32_t length) { name=value; duration=length; }
};
struct CurrentStatus {
  unsigned calls{0}, deprecated_calls{0}; uint32_t duration{0};
  void status_momentary_error(uint32_t length) { ++calls; duration=length; }
  void status_momentary_error(const char *, uint32_t) { ++deprecated_calls; }
};

int main() {
  using namespace esphome::micro_wake_word;
  const std::vector<uint8_t> original{0, 1, 127, 128, 254, 255};
  const AudioCapture capture{original};
  const uint8_t *received = nullptr; size_t bytes = 0;
  auto callback = [&](const auto &input) {
    const auto &data = compat::audio_bytes(input);
    received = data.data(); bytes = data.size();
    assert(std::memcmp(received, original.data(), original.size()) == 0);
  };
  static_assert(std::is_invocable_v<decltype(callback), const std::vector<uint8_t> &>);
  static_assert(std::is_invocable_v<decltype(callback), const AudioCapture &>);
  const size_t before = allocations;
  callback(original);
  assert(received == original.data() && bytes == original.size());
  callback(capture);
  assert(received == original.data() && bytes == original.size());
  LegacySource legacy; CurrentSource current;
  assert(compat::fill(legacy, uint32_t{50}, 0) == 128);
  assert(legacy.calls == 1 && legacy.timeout == 50 && !legacy.shift);
  assert(compat::fill(current, uint32_t{50}, 0) == 128);
  assert(current.calls == 1 && current.timeout == 50 && current.deprecated_calls == 0);
  LegacyStatus old_status; CurrentStatus new_status;
  compat::momentary_error(old_status, "frontend_alloc", 1000, 0);
  compat::momentary_error(new_status, "frontend_alloc", 1000, 0);
  assert(std::strcmp(old_status.name, "frontend_alloc") == 0 && old_status.duration == 1000);
  assert(new_status.calls == 1 && new_status.duration == 1000 && new_status.deprecated_calls == 0);
  assert(allocations == before);
}
""")
    executable = tmp_path / "core-compat"
    compiled = subprocess.run(
        ["g++", "-std=c++20", "-O0", "-Wall", "-Wextra", "-Werror",
         "-I" + str(COMPONENT), str(source), "-o", str(executable)],
        capture_output=True, text=True, timeout=30,
    )
    assert compiled.returncode == 0, compiled.stderr
    run = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
    assert run.returncode == 0, run.stderr
