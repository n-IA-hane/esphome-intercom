#pragma once

#include "esphome/components/esp_audio_stack/esp_audio_stack.h"
#include "esphome/core/component.h"
#include <atomic>
#include <cstdint>
#include <string>
#include <freertos/FreeRTOS.h>
#include <freertos/stream_buffer.h>

namespace esphome::audio_stack_capture {

// ESP targets and the receiver use little endian. No pointers are serialized.
struct CaptureHeader {
  uint32_t kind;
  uint32_t sequence;
  uint32_t rate;
  uint16_t channels;
  uint16_t bits;
  uint32_t frames;
  uint32_t bytes;
  uint64_t timestamp_us;
};
static_assert(sizeof(CaptureHeader) == 32);

class AudioStackCapture : public Component {
 public:
  void set_audio_stack(esp_audio_stack::ESPAudioStack *stack) { this->stack_ = stack; }
  void set_host(const std::string &host) { this->host_ = host; }
  void set_port(uint16_t port) { this->port_ = port; }
  void set_duration_ms(uint32_t duration) { this->duration_ms_ = duration; }
  void set_buffer_bytes(size_t bytes) { this->buffer_bytes_ = bytes; }
  void set_deferred_transfer(bool deferred) { this->deferred_transfer_ = deferred; }
  void setup() override;
  bool capture(uint32_t duration_ms = 0, uint32_t mask = 0x1e);

 protected:
  static constexpr uint32_t ACTIVE = 1;
  static constexpr uint32_t WRITING = 2;
  static void raw_callback_(void *, uint32_t, const uint8_t *, size_t, uint32_t, uint8_t, uint8_t);
  static void mic_callback_(void *, const uint8_t *, size_t);
  static void played_callback_(void *, uint32_t, int64_t);
  static void task_(void *);
  void run_();
  void record_(uint32_t kind, const uint8_t *data, size_t bytes, uint32_t rate,
               uint16_t channels, uint16_t bits, uint32_t frames, uint64_t timestamp_us);
  bool receive_exact_(void *data, size_t bytes, TickType_t wait);

  esp_audio_stack::ESPAudioStack *stack_{nullptr};
  std::string host_;
  uint16_t port_{19091};
  uint32_t duration_ms_{10000};
  uint32_t capture_duration_ms_{10000};
  uint32_t capture_mask_{0x1e};
  size_t buffer_bytes_{128 * 1024};
  bool deferred_transfer_{false};
  uint8_t *storage_{nullptr};
  StaticStreamBuffer_t stream_state_{};
  StreamBufferHandle_t stream_{nullptr};
  std::atomic<bool> busy_{false};
  // One atomic gate closes entry while allowing an already accepted writer
  // to finish. It never blocks the audio task or holds a lock across a copy.
  std::atomic<uint32_t> gate_{0};
  std::atomic<bool> producer_conflict_{false};
  uint64_t deadline_us_{0};
  uint32_t sequence_{0};
  uint32_t dropped_{0};
  uint32_t max_callback_us_{0};
};

}  // namespace esphome::audio_stack_capture
