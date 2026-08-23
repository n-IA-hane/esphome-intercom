#pragma once

#include "esphome/core/defines.h"

#ifdef USE_ESP32

#include "esphome/components/audio/audio.h"
#include "esphome/components/media_source/media_source.h"
#include "esphome/core/component.h"

#include <micro_decoder/decoder_source.h>
#include <micro_decoder/types.h>

#include <atomic>
#include <memory>
#include <string>

namespace esphome::audio_http {

// MediaSource reports state and writes audio to its listener. DecoderListener
// receives decoded audio and state changes from the managed decoder.
class AudioHTTPMediaSource final : public Component,
                                   public media_source::MediaSource,
                                   public micro_decoder::DecoderListener {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;

  void set_buffer_size(size_t buffer_size) { this->buffer_size_ = buffer_size; }
  void set_task_stack_in_psram(bool task_stack_in_psram) { this->decoder_task_stack_in_psram_ = task_stack_in_psram; }
  void set_persistent_ring_buffer(bool persistent) { this->persistent_ring_buffer_ = persistent; }

  bool play_uri(const std::string &uri) override;
  void handle_command(media_source::MediaSourceCommand command) override;
  bool can_handle(const std::string &uri) const override;

  size_t on_audio_write(const uint8_t *data, size_t length, uint32_t timeout_ms) override;
  void on_stream_info(const micro_decoder::AudioStreamInfo &info) override;
  void on_state_change(micro_decoder::DecoderState state) override;

 protected:
  std::unique_ptr<micro_decoder::DecoderSource> decoder_;
  audio::AudioStreamInfo stream_info_;

  size_t buffer_size_{50000};
  std::atomic<bool> pause_{false};
  bool decoder_task_stack_in_psram_{false};
  bool persistent_ring_buffer_{false};
};

}  // namespace esphome::audio_http

#endif  // USE_ESP32

