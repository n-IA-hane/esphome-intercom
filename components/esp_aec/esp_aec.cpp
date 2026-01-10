#include "esp_aec.h"
#include "esphome/core/log.h"
#include <cstring>

namespace esphome {
namespace esp_aec {

static const char *TAG = "esp_aec";

void EspAec::setup() {
#ifdef USE_ESP_AEC
  this->aec_handle_ = aec_create(this->sample_rate_, this->filter_length_, 1, AEC_MODE_VOIP_HIGH_PERF);

  if (this->aec_handle_ != nullptr) {
    this->frame_size_ = aec_get_chunksize(this->aec_handle_);
    this->initialized_ = true;
  } else {
    this->mark_failed();
  }
#else
  this->initialized_ = false;
#endif
}

void EspAec::dump_config() {
  ESP_LOGCONFIG(TAG, "ESP AEC (ESP-SR):");
  ESP_LOGCONFIG(TAG, "  Sample Rate: %d Hz", this->sample_rate_);
  ESP_LOGCONFIG(TAG, "  Filter Length: %d", this->filter_length_);
  ESP_LOGCONFIG(TAG, "  Frame Size: %d samples", this->frame_size_);
  ESP_LOGCONFIG(TAG, "  Initialized: %s", this->initialized_ ? "YES" : "NO");
}

void EspAec::process(int16_t *mic_input, int16_t *speaker_ref, int16_t *output, size_t samples) {
#ifdef USE_ESP_AEC
  if (this->aec_handle_ == nullptr || !this->initialized_) {
    // Fallback: copy mic input directly to output
    memcpy(output, mic_input, samples * sizeof(int16_t));
    return;
  }

  // Process in frame_size chunks
  size_t processed = 0;
  while (processed < samples) {
    size_t chunk = std::min((size_t)this->frame_size_, samples - processed);

    if (chunk < (size_t)this->frame_size_) {
      // Last chunk is smaller than frame size, just copy
      memcpy(output + processed, mic_input + processed, chunk * sizeof(int16_t));
      break;
    }

    // Process AEC - removes echo from mic_input using speaker_ref as reference
    aec_process(this->aec_handle_, mic_input + processed, speaker_ref + processed, output + processed);

    processed += chunk;
  }
#else
  // No AEC available, passthrough
  memcpy(output, mic_input, samples * sizeof(int16_t));
#endif
}

}  // namespace esp_aec
}  // namespace esphome
