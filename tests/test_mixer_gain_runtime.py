"""Run the production source-buffer adapter with the pinned upstream GainRamp."""
from pathlib import Path
import hashlib
import os
import subprocess
import urllib.request

from tests.esphome_native import NATIVE_COMPONENTS

REVISION = '891fdfc531ad38d9e90b5e9088be60a66c82ed4c'
FILES = {
    'include/gain.h': '3e041d0344a0a4511eb18d3fadc4ee8ff2d681f315ebc846c45f0137fc5434b9',
    'src/gain.cpp': '68c197695678ae22dd4767c3a2ae3b312f1ed34ec3989a93b5a6eb703389f1d0',
    'src/compiler.h': '65a46977da2d707cf51c23863d5e941b938631ecbb34b947435abc8c0ded3d1d',
    'src/q31_utils.h': '847f3cb2afcd7c6d5ca789088fdcc13e6223c63844d837a6b411ebad13e30088',
}


def test_production_ducking_preserves_exposed_samples_and_ramp(tmp_path):
    dependency = Path(os.environ.get('AUDIO_LIBS_REPO', tmp_path / 'audio-libs'))
    for name, digest in FILES.items():
        target = dependency / name
        if not target.exists():
            url = f'https://raw.githubusercontent.com/esphome-libs/esp-audio-libs/{REVISION}/{name}'
            with urllib.request.urlopen(url, timeout=30) as response:
                data = response.read()
            assert hashlib.sha256(data).hexdigest() == digest, name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        assert hashlib.sha256(target.read_bytes()).hexdigest() == digest, name
    source = (NATIVE_COMPONENTS / 'mixer/speaker/mixer_speaker.cpp').read_text()
    start = source.index('size_t SourceSpeaker::process_data_from_source(')
    end = source.index('\nvoid SourceSpeaker::enter_stopping_state_', start)
    code = r'''
#include <gain.h>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <memory>
#include <vector>
using TickType_t = int;
namespace audio {
struct RingBufferAudioSource {
  std::vector<int16_t> pcm;
  bool exposed = false;
  size_t available() const { return exposed ? pcm.size() * 2 : 0; }
  size_t fill(int) { exposed = true; return pcm.size() * 2; }
  uint8_t *mutable_data() { return reinterpret_cast<uint8_t *>(pcm.data()); }
};
}
struct StreamInfo {
  uint32_t bytes_to_samples(size_t n) const { return n / 2; }
  int get_bits_per_sample() const { return 16; }
  uint32_t ms_to_samples(uint32_t ms) const { return ms * 16 * 2; }
};
struct SourceSpeaker {
  esp_audio_libs::gain::GainRamp ducking_ramp_;
  StreamInfo audio_stream_info_;
  size_t process_data_from_source(std::shared_ptr<audio::RingBufferAudioSource>&, TickType_t);
  void apply_ducking(uint8_t, uint32_t);
};
'''
    code += source[start:end] + r'''
int main() {
  SourceSpeaker speaker;
  auto source = std::make_shared<audio::RingBufferAudioSource>();
  source->pcm.assign(320, 10000);
  speaker.apply_ducking(20, 0);
  assert(speaker.process_data_from_source(source, 0) == 640);
  for (int16_t sample : source->pcm) assert(std::abs(sample - 1000) <= 1);
  // A partial consumer returns the same exposure. It must not duck it again.
  source->pcm.erase(source->pcm.begin(), source->pcm.begin() + 79);
  auto retained = source->pcm;
  assert(speaker.process_data_from_source(source, 0) == 0);
  assert(source->pcm == retained);
  // Restore over exactly 10 ms of stereo 16 kHz samples, including fragments.
  speaker.apply_ducking(0, 10);
  int previous = 0;
  unsigned total = 0;
  for (unsigned length : {31, 51, 101, 137}) {
    source->pcm.assign(length, 10000);
    source->exposed = false;
    assert(speaker.process_data_from_source(source, 0) == length * 2);
    for (int16_t sample : source->pcm) {
      assert(sample >= previous && sample <= 10000);
      previous = sample;
    }
    total += length;
  }
  assert(total == 320 && previous >= 9999);
  source->pcm.assign(8, 10000);
  source->exposed = false;
  speaker.process_data_from_source(source, 0);
  for (int16_t sample : source->pcm) assert(sample >= 9999);
}
'''
    cpp = tmp_path / 'gain-adapter.cpp'
    cpp.write_text(code)
    binary = tmp_path / 'gain-adapter'
    subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', '-Werror', '-O2',
                    '-I', str(dependency / 'include'), str(cpp),
                    str(dependency / 'src/gain.cpp'), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
