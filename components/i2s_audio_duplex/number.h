#pragma once

#ifdef USE_ESP32

#include "esphome/components/number/number.h"
#include "esphome/core/component.h"
#include "i2s_audio_duplex.h"

namespace esphome {
namespace i2s_audio_duplex {

class MicGainNumber : public number::Number, public Component {
 public:
  void set_parent(I2SAudioDuplex *parent) { this->parent_ = parent; }

  void setup() override {
    if (this->parent_ != nullptr) {
      this->publish_state(this->parent_->get_mic_gain());
    }
  }

  void dump_config() override {
    ESP_LOGCONFIG("mic_gain", "Mic Gain Number");
  }

 protected:
  void control(float value) override {
    if (this->parent_ != nullptr) {
      this->parent_->set_mic_gain(value);
      this->publish_state(value);
    }
  }

  I2SAudioDuplex *parent_{nullptr};
};

class SpeakerVolumeNumber : public number::Number, public Component {
 public:
  void set_parent(I2SAudioDuplex *parent) { this->parent_ = parent; }

  void setup() override {
    if (this->parent_ != nullptr) {
      this->publish_state(this->parent_->get_speaker_volume());
    }
  }

  void dump_config() override {
    ESP_LOGCONFIG("speaker_volume", "Speaker Volume Number");
  }

 protected:
  void control(float value) override {
    if (this->parent_ != nullptr) {
      this->parent_->set_speaker_volume(value);
      this->publish_state(value);
    }
  }

  I2SAudioDuplex *parent_{nullptr};
};

}  // namespace i2s_audio_duplex
}  // namespace esphome

#endif  // USE_ESP32
