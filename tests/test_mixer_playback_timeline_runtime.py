"""Mixer completion credits follow the samples contributed by each source."""

from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def test_source_credit_matches_independent_sample_timeline(tmp_path):
    source = tmp_path / "timeline.cpp"
    source.write_text(r'''
#include "playback_timeline.h"
#include <cassert>
#include <deque>
#include <random>
using esphome::mixer_speaker::PlaybackTimeline;
int main() {
  PlaybackTimeline timeline;
  timeline.append(0, 100);
  timeline.append(200, 100);
  assert(timeline.consume(100).frames == 100);
  assert(timeline.consume(100).frames == 0);
  assert(timeline.consume(100).frames == 100);
  assert(timeline.pending_frames() == 0);
  timeline.append(20, 10);
  auto completion = timeline.consume(50);
  assert(completion.frames == 10 && completion.trailing_frames == 20);
  for (unsigned i = 0; i < 32; ++i) {
    assert(timeline.can_append(i * 2, 1));
    timeline.append(i * 2, 1);
  }
  assert(!timeline.can_append(64, 1));
  assert(timeline.consume(64).frames == 32);
  assert(timeline.pending_frames() == 0);

  std::deque<bool> wire;
  std::mt19937 random(42);
  auto complete = [&](unsigned count) {
    unsigned owned = 0, trailing = 0;
    for (unsigned i = 0; i < count; ++i) {
      bool contributed = wire.front();
      wire.pop_front();
      if (contributed) {
        ++owned;
        trailing = count - i - 1;
      }
    }
    auto actual = timeline.consume(count);
    assert(actual.frames == owned);
    if (owned) assert(actual.trailing_frames == trailing);
  };
  for (unsigned step = 0; step < 10000; ++step) {
    unsigned count = 1 + random() % 31;
    bool contributes = random() % 3 != 0;
    if ((random() % 2 || !timeline.can_append(wire.size(), count)) && !wire.empty()) {
      complete(std::min<unsigned>(count, wire.size()));
    } else if (!contributes || timeline.can_append(wire.size(), count)) {
      if (contributes) timeline.append(wire.size(), count);
      for (unsigned i = 0; i < count; ++i) wire.push_back(contributes);
    }
  }
  while (!wire.empty()) complete(std::min<unsigned>(17, wire.size()));
  assert(timeline.pending_frames() == 0);
  timeline.clear();
  timeline.append(0, 7);
  assert(timeline.consume(7).frames == 7);
}
''')
    binary = tmp_path / "timeline"
    subprocess.run(
        ["g++", "-std=c++17", "-I",
         str(ROOT / "esphome/components/mixer/speaker"),
         str(source), "-o", str(binary)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        ["bash", "-c", 'ulimit -c 0; exec "$1"', "bash", str(binary)],
        check=True, capture_output=True, text=True,
    )
