#pragma once

#include <cstddef>
#include <cstdint>
#include <cstring>

namespace esphome::esp_screenshot {

// The caller holds its publication mutex. A reader keeps its slot immutable
// even after a newer image is published. The two slots never grow at runtime.
class SnapshotSlots {
public:
  int begin_write() {
    if (this->writing_ >= 0)
      return -1;
    for (int index = 0; index < 2; ++index) {
      if (index != this->published_ && this->readers_[index] == 0) {
        this->writing_ = index;
        return index;
      }
    }
    return -1;
  }
  bool publish(int index) {
    if (index < 0 || index != this->writing_)
      return false;
    this->published_ = index;
    this->writing_ = -1;
    return true;
  }
  void cancel_write() { this->writing_ = -1; }
  int acquire() {
    if (this->published_ < 0)
      return -1;
    ++this->readers_[this->published_];
    return this->published_;
  }
  void release(int index) {
    if (index >= 0 && index < 2 && this->readers_[index] != 0)
      --this->readers_[index];
  }

protected:
  int published_{-1};
  int writing_{-1};
  unsigned readers_[2]{};
};

// Assembles a full composed LVGL refresh from temporary RGB565 rectangles.
// Missing rectangles are rejected instead of publishing stale or black pixels.
class FrameAssembler {
public:
  void reset(uint8_t *pixels, uint8_t *coverage, unsigned width,
             unsigned height) {
    this->pixels_ = pixels;
    this->coverage_ = coverage;
    this->width_ = width;
    this->height_ = height;
    this->covered_ = 0;
    std::memset(coverage, 0, (size_t(width) * height + 7) / 8);
  }
  bool add(int x, int y, unsigned width, unsigned height, const uint8_t *pixels,
           size_t stride) {
    if (pixels == nullptr || x < 0 || y < 0 || unsigned(x) > this->width_ ||
        unsigned(y) > this->height_ || width > this->width_ - unsigned(x) ||
        height > this->height_ - unsigned(y) || stride < width * 2U)
      return false;
    for (unsigned row = 0; row < height; ++row) {
      const size_t start = size_t(y + row) * this->width_ + unsigned(x);
      std::memcpy(this->pixels_ + start * 2, pixels + row * stride, width * 2U);
      for (unsigned column = 0; column < width; ++column) {
        const size_t bit = start + column;
        const auto mask = uint8_t(1U << (bit & 7U));
        if ((this->coverage_[bit >> 3U] & mask) == 0) {
          this->coverage_[bit >> 3U] |= mask;
          ++this->covered_;
        }
      }
    }
    return true;
  }
  bool complete() const {
    return this->covered_ == size_t(this->width_) * this->height_;
  }

protected:
  uint8_t *pixels_{nullptr};
  uint8_t *coverage_{nullptr};
  unsigned width_{0}, height_{0};
  size_t covered_{0};
};

} // namespace esphome::esp_screenshot
