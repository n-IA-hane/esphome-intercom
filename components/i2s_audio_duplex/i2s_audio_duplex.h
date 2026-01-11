#pragma once

#ifdef USE_ESP32

#include "esphome/core/component.h"
#include "esphome/core/ring_buffer.h"

#include <driver/i2s_std.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <atomic>
#include <functional>
#include <vector>

// Forward declare AEC
namespace esphome {
namespace esp_aec {
class EspAec;
}  // namespace esp_aec
}  // namespace esphome

namespace esphome {
namespace i2s_audio_duplex {

// Callback type for mic data: receives raw PCM samples (pointer + length, zero-copy)
using MicDataCallback = std::function<void(const uint8_t *data, size_t len)>;

class I2SAudioDuplex : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::HARDWARE; }

  // Pin setters
  void set_lrclk_pin(int pin) { this->lrclk_pin_ = pin; }
  void set_bclk_pin(int pin) { this->bclk_pin_ = pin; }
  void set_mclk_pin(int pin) { this->mclk_pin_ = pin; }
  void set_din_pin(int pin) { this->din_pin_ = pin; }
  void set_dout_pin(int pin) { this->dout_pin_ = pin; }
  void set_sample_rate(uint32_t rate) { this->sample_rate_ = rate; }

  // AEC setter
  void set_aec(esp_aec::EspAec *aec);
  void set_aec_enabled(bool enabled) { this->aec_enabled_ = enabled; }
  bool is_aec_enabled() const { return this->aec_enabled_; }

  // Volume control (0.0 - 1.0)
  void set_mic_gain(float gain) { this->mic_gain_ = gain; }
  float get_mic_gain() const { return this->mic_gain_; }
  void set_speaker_volume(float volume) { this->speaker_volume_ = volume; }
  float get_speaker_volume() const { return this->speaker_volume_; }

  // Microphone interface
  void add_mic_data_callback(MicDataCallback callback) { this->mic_callbacks_.push_back(callback); }
  void start_mic();
  void stop_mic();
  bool is_mic_running() const { return this->mic_running_; }

  // Speaker interface
  size_t play(const uint8_t *data, size_t len, TickType_t ticks_to_wait = portMAX_DELAY);
  void start_speaker();
  void stop_speaker();
  bool is_speaker_running() const { return this->speaker_running_; }

  // Full duplex control
  void start();  // Start both mic and speaker
  void stop();   // Stop both

  bool is_running() const { return this->duplex_running_; }

 protected:
  bool init_i2s_duplex_();
  void deinit_i2s_();

  static void audio_task(void *param);
  void audio_task_();

  // Pin configuration
  int lrclk_pin_{-1};
  int bclk_pin_{-1};
  int mclk_pin_{-1};
  int din_pin_{-1};   // Mic data in
  int dout_pin_{-1};  // Speaker data out

  uint32_t sample_rate_{16000};

  // I2S handles - BOTH created from single channel for duplex
  i2s_chan_handle_t tx_handle_{nullptr};
  i2s_chan_handle_t rx_handle_{nullptr};

  // State
  std::atomic<bool> duplex_running_{false};
  bool mic_running_{false};
  bool speaker_running_{false};
  TaskHandle_t audio_task_handle_{nullptr};

  // Mic data callbacks
  std::vector<MicDataCallback> mic_callbacks_;

  // Speaker ring buffer
  std::unique_ptr<RingBuffer> speaker_buffer_;

  // AEC support
  esp_aec::EspAec *aec_{nullptr};
  bool aec_enabled_{true};  // Runtime toggle
  std::unique_ptr<RingBuffer> speaker_ref_buffer_;  // Reference for AEC
  uint32_t aec_frame_count_{0};  // Debug counter, reset on start()

  // Volume control
  float mic_gain_{1.0f};       // 0.0 - 2.0 (1.0 = unity gain)
  float speaker_volume_{1.0f}; // 0.0 - 1.0
};

}  // namespace i2s_audio_duplex
}  // namespace esphome

#endif  // USE_ESP32
