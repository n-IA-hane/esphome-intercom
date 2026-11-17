"""The native MWW microphone callback borrows capture bytes without a legacy shim."""

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "esphome/components/micro_wake_word"


def test_native_capture_callback_preserves_borrowing_and_consumer_reset_owner(tmp_path):
    source = (COMPONENT / "micro_wake_word.cpp").read_text()
    start = source.index("this->microphone_source_->add_data_callback(")
    end = source.index("\n  });", start) + len("\n  });")
    callback_registration = source[start:end]
    cpp = tmp_path / "capture-callback.cpp"
    cpp.write_text(r'''
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <functional>
#include <memory>
#include <new>
#include <span>
#include <vector>

static size_t allocations = 0;
void *operator new(size_t size) {
  ++allocations;
  if (void *ptr = std::malloc(size)) return ptr;
  throw std::bad_alloc();
}
// Keep the replacement deallocator at its ABI boundary, as in libstdc++; do not inline free into callers.
[[gnu::noinline]] void operator delete(void *ptr) noexcept { std::free(ptr); }
void operator delete(void *ptr, size_t) noexcept { ::operator delete(ptr); }
static unsigned event_bits = 0;
void xEventGroupSetBits(unsigned, unsigned bits) { event_bits |= bits; }

namespace esphome {
namespace microphone {
struct AudioCapture { std::span<const uint8_t> data; };
struct Microphone {
  std::function<void(const AudioCapture &)> callback;
  template<typename F> void add_data_callback(F callback) { this->callback = callback; }
};
}
namespace ring_buffer {
struct RingBuffer {
  const uint8_t *observed = nullptr;
  size_t observed_size = 0;
  unsigned writes = 0, resets = 0;
  bool accepts = true;
  size_t write_without_replacement(const uint8_t *data, size_t size, unsigned timeout, bool partial) {
    assert(timeout == 0 && !partial);
    observed = data; observed_size = size; ++writes;
    return accepts ? size : 0;
  }
  void reset() { ++resets; }
};
}
namespace micro_wake_word {
enum class State { STOPPED, RUNNING };
struct EventGroupBits {
  static constexpr unsigned WARNING_FULL_RING_BUFFER = 1, COMMAND_RESET_RING_BUFFER = 2;
};
struct MicroWakeWord {
  microphone::Microphone *microphone_source_;
  State state_ = State::STOPPED;
  std::weak_ptr<ring_buffer::RingBuffer> ring_buffer_{};
  unsigned event_group_ = 0;
  void bind() {
''' + callback_registration + r'''
  }
};
}
}
int main() {
  using namespace esphome;
  auto ring = std::make_shared<ring_buffer::RingBuffer>();
  microphone::Microphone mic;
  micro_wake_word::MicroWakeWord model{&mic};
  model.ring_buffer_ = ring;
  model.bind();
  const std::vector<uint8_t> bytes{0, 1, 127, 128, 254, 255};
  const microphone::AudioCapture capture{bytes};
  const size_t before = allocations;
  mic.callback(capture);
  assert(ring->writes == 0 && event_bits == 0);
  model.state_ = micro_wake_word::State::RUNNING;
  mic.callback(capture);
  assert(ring->writes == 1 && ring->observed == bytes.data() && ring->observed_size == bytes.size());
  assert(event_bits == 0);
  ring->accepts = false;
  mic.callback(capture);
  assert(ring->writes == 2 && ring->resets == 0);
  assert(event_bits == (micro_wake_word::EventGroupBits::WARNING_FULL_RING_BUFFER |
                        micro_wake_word::EventGroupBits::COMMAND_RESET_RING_BUFFER));
  ring.reset();
  event_bits = 0;
  mic.callback(capture);
  assert(event_bits == 0 && allocations == before);
  void *probe = ::operator new(1);
  assert(allocations == before + 1);
  ::operator delete(probe);
}
''')
    binary = tmp_path / "capture-callback"
    compiled = subprocess.run(
        ["g++", "-std=c++20", "-O1", "-Wall", "-Wextra", "-Werror",
         str(cpp), "-o", str(binary)],
        capture_output=True, text=True, timeout=30,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
