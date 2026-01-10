#pragma once

#include "esphome/core/component.h"
#include "esphome/core/log.h"
#include "esphome/core/defines.h"

#ifdef USE_ESP_AEC
// ESP-SR AEC library (C interface requires extern "C")
extern "C" {
#include <esp_aec.h>
}
#endif

namespace esphome {
namespace esp_aec {

class EspAec : public Component {
 public:
  EspAec() = default;
  void setup() override;
  void loop() override {}  // Empty - no business logic
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::LATE; }  // -200.0 - very late, after WiFi

  // Configuration
  void set_sample_rate(uint32_t rate) { this->sample_rate_ = rate; }
  void set_filter_length(int length) { this->filter_length_ = length; }

  // Processing interface (called by i2s_audio_udp)
  // Input: mic_input = microphone data, speaker_ref = speaker playback reference
  // Output: processed audio with echo removed
  void process(int16_t *mic_input, int16_t *speaker_ref, int16_t *output, size_t samples);
  int get_frame_size() const { return this->frame_size_; }
  bool is_initialized() const { return this->initialized_; }

 protected:
  uint32_t sample_rate_{16000};
  int filter_length_{4};  // Recommended: 4 for ESP32-S3
  int frame_size_{0};
  bool initialized_{false};

#ifdef USE_ESP_AEC
  aec_handle_t *aec_handle_{nullptr};
#endif
};

}  // namespace esp_aec
}  // namespace esphome
