#pragma once
#include <algorithm>
#include <array>
#include <atomic>
#include <cstdint>

namespace esphome::mixer_speaker {

// Timing metadata only. The mixer's lock serializes reservations and hardware
// completions. PCM remains in the existing source/output buffers.
class PlaybackTimeline {
 public:
  struct Completion { uint32_t frames{0}; uint32_t trailing_frames{0}; };
  bool can_append(uint32_t pipeline_frames, uint32_t frames) const {
    return frames == 0 || (pipeline_frames >= this->distance_ &&
           (this->size_ < CAPACITY || pipeline_frames == this->distance_));
  }
  void append(uint32_t pipeline_frames, uint32_t frames) {
    if (frames == 0) return;
    const uint32_t gap = pipeline_frames - this->distance_;
    if (this->size_ != 0 && gap == 0) {
      this->spans_[(this->head_ + this->size_ - 1) % CAPACITY].frames += frames;
    } else {
      this->spans_[(this->head_ + this->size_) % CAPACITY] = {gap, frames};
      ++this->size_;
    }
    this->distance_ = pipeline_frames + frames;
    this->pending_.fetch_add(frames, std::memory_order_release);
  }
  Completion consume(uint32_t frames) {
    Completion result;
    uint32_t remaining = frames;
    this->distance_ -= std::min(this->distance_, frames);
    while (remaining != 0 && this->size_ != 0) {
      auto &span = this->spans_[this->head_];
      const uint32_t skip = std::min(span.gap, remaining);
      span.gap -= skip;
      remaining -= skip;
      if (remaining == 0) break;
      const uint32_t played = std::min(span.frames, remaining);
      span.frames -= played;
      remaining -= played;
      result.frames += played;
      if (played != 0) result.trailing_frames = remaining;
      if (span.frames == 0) {
        this->head_ = (this->head_ + 1) % CAPACITY;
        --this->size_;
      }
    }
    this->pending_.fetch_sub(result.frames, std::memory_order_release);
    return result;
  }
  uint32_t pending_frames() const { return this->pending_.load(std::memory_order_acquire); }
  void clear() {
    this->head_ = 0;
    this->size_ = 0;
    this->distance_ = 0;
    this->pending_.store(0, std::memory_order_release);
  }
 private:
  struct Span { uint32_t gap; uint32_t frames; };
  static constexpr uint8_t CAPACITY = 32;
  std::array<Span, CAPACITY> spans_{};
  uint8_t head_{0}, size_{0};
  uint32_t distance_{0};
  std::atomic<uint32_t> pending_{0};
};

}  // namespace esphome::mixer_speaker
