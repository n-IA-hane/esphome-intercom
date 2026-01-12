#pragma once

#ifdef USE_ESP32

#include "esphome/core/component.h"
#include "esphome/core/automation.h"
#include "esphome/core/ring_buffer.h"

#ifdef USE_MICROPHONE
#include "esphome/components/microphone/microphone.h"
#endif
#ifdef USE_SPEAKER
#include "esphome/components/speaker/speaker.h"
#endif

#include <lwip/sockets.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

// Forward declare esp_aec if available
namespace esphome {
namespace esp_aec {
class EspAec;
}  // namespace esp_aec
}  // namespace esphome

// Forward declare i2s_audio_duplex (only if available)
#ifdef USE_I2S_AUDIO_DUPLEX
namespace esphome {
namespace i2s_audio_duplex {
class I2SAudioDuplex;
}  // namespace i2s_audio_duplex
}  // namespace esphome
#endif

namespace esphome {
namespace intercom_audio {

enum class StreamState : uint8_t {
  IDLE,
  STARTING,
  STREAMING,
  STOPPING,
};

class IntercomAudio : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

  // Configuration setters
#ifdef USE_MICROPHONE
  void set_microphone(microphone::Microphone *mic) { this->microphone_ = mic; }
#endif
#ifdef USE_SPEAKER
  void set_speaker(speaker::Speaker *spk) { this->speaker_ = spk; }
#endif
#ifdef USE_I2S_AUDIO_DUPLEX
  void set_duplex(i2s_audio_duplex::I2SAudioDuplex *duplex) { this->duplex_ = duplex; }
#endif
  void set_aec(esp_aec::EspAec *aec) { this->aec_ = aec; }

  void set_listen_port(uint16_t port) { this->listen_port_ = port; }

  // Lambda setters for dynamic IP/port (evaluated at start() time)
  void set_remote_ip_lambda(std::function<std::string()> &&f) { this->remote_ip_lambda_ = f; }
  void set_remote_port_lambda(std::function<uint16_t()> &&f) { this->remote_port_lambda_ = f; }

  // Get current remote IP/port (evaluates lambda)
  std::string get_remote_ip() {
    if (this->remote_ip_lambda_.has_value()) {
      return this->remote_ip_lambda_.value()();
    }
    return this->remote_ip_;
  }
  uint16_t get_remote_port() {
    if (this->remote_port_lambda_.has_value()) {
      return this->remote_port_lambda_.value()();
    }
    return this->remote_port_;
  }

  void set_buffer_size(size_t size) { this->buffer_size_ = size; }
  void set_prebuffer_size(size_t size) { this->prebuffer_size_ = size; }

  // Runtime control
  void start();
  void start(const std::string &remote_ip, uint16_t remote_port);
  void stop();
  bool is_streaming() const { return this->state_ == StreamState::STREAMING; }

  // State getters
  StreamState get_state() const { return this->state_; }
  uint32_t get_tx_packets() const { return this->tx_packets_; }
  uint32_t get_rx_packets() const { return this->rx_packets_; }
  size_t get_buffer_fill() const;

  // Get audio mode as string
  const char *get_mode_str() const {
#ifdef USE_I2S_AUDIO_DUPLEX
    if (this->duplex_ != nullptr) return "Full Duplex";
#endif
    bool has_mic = false;
    bool has_spk = false;
#ifdef USE_MICROPHONE
    has_mic = (this->microphone_ != nullptr);
#endif
#ifdef USE_SPEAKER
    has_spk = (this->speaker_ != nullptr);
#endif
    if (has_mic && has_spk) return "Full Duplex";
    if (has_mic) return "TX Only";
    if (has_spk) return "RX Only";
    return "None";
  }

  // Reset packet counters
  void reset_counters() {
    this->tx_packets_ = 0;
    this->rx_packets_ = 0;
    this->tx_drops_ = 0;
    this->rx_drops_ = 0;
  }

  // Drop counters (buffer overruns)
  uint32_t get_tx_drops() const { return this->tx_drops_; }
  uint32_t get_rx_drops() const { return this->rx_drops_; }

  // Volume control (delegates to speaker)
  void set_volume(float volume);
  float get_volume() const;

  // Mic gain control (for 32->16 bit conversion)
  void set_mic_gain(int gain) { this->mic_gain_ = gain; }
  int get_mic_gain() const { return this->mic_gain_; }

  // DC offset removal (for microphones with significant DC bias like SPH0645)
  void set_dc_offset_removal(bool enabled) { this->dc_offset_removal_ = enabled; }
  bool get_dc_offset_removal() const { return this->dc_offset_removal_; }

  // AEC control
  void set_aec_enabled(bool enabled) { this->aec_enabled_ = enabled; }
  bool is_aec_enabled() const { return this->aec_enabled_; }

  // Triggers for automations
  Trigger<> *get_start_trigger() { return &this->start_trigger_; }
  Trigger<> *get_stop_trigger() { return &this->stop_trigger_; }

 protected:
  // Audio task - runs on dedicated core
  static void audio_task(void *param);
  void audio_task_();

  // Microphone callback
  void on_microphone_data_(const std::vector<uint8_t> &data);

  // UDP helpers
  bool setup_sockets_();
  void close_sockets_();
  bool send_audio_(const uint8_t *data, size_t bytes);
  size_t receive_audio_(int16_t *buffer, size_t max_samples);

  // Components - either duplex OR separate mic/speaker
#ifdef USE_I2S_AUDIO_DUPLEX
  i2s_audio_duplex::I2SAudioDuplex *duplex_{nullptr};  // Full duplex mode
#endif
#ifdef USE_MICROPHONE
  microphone::Microphone *microphone_{nullptr};  // Separate mode
#endif
#ifdef USE_SPEAKER
  speaker::Speaker *speaker_{nullptr};           // Separate mode
#endif
  esp_aec::EspAec *aec_{nullptr};

  // Network config
  uint16_t listen_port_{12346};
  std::string remote_ip_;
  uint16_t remote_port_{12346};
  optional<std::function<std::string()>> remote_ip_lambda_;
  optional<std::function<uint16_t()>> remote_port_lambda_;

  // Buffer config
  size_t buffer_size_{8192};
  size_t prebuffer_size_{2048};

  // Mic gain for 32->16 bit conversion (default 4x)
  int mic_gain_{4};

  // DC offset removal for mics with significant DC bias (e.g., SPH0645)
  bool dc_offset_removal_{false};

  // State
  StreamState state_{StreamState::IDLE};
  bool task_running_{false};
  TaskHandle_t audio_task_handle_{nullptr};

  // Sockets
  int rx_socket_{-1};
  int tx_socket_{-1};
  struct sockaddr_in remote_addr_;

  // Jitter buffer
  std::unique_ptr<RingBuffer> rx_buffer_;

  // AEC speaker reference buffer (stores what we play for echo cancellation)
  std::unique_ptr<RingBuffer> speaker_ref_buffer_;
  bool aec_enabled_{false};

  // Pre-allocated AEC processing buffers (to avoid stack allocation in callback)
  std::vector<int16_t> aec_converted_data_;
  std::vector<int16_t> aec_speaker_ref_;
  std::vector<int16_t> aec_output_;

  // AEC processing task (separate from mic callback to avoid stack overflow)
  std::unique_ptr<RingBuffer> mic_input_buffer_;  // Raw mic data for AEC processing
  TaskHandle_t aec_task_handle_{nullptr};
  bool aec_task_running_{false};
  static void aec_task(void *param);
  void aec_task_();

  // Metrics
  uint32_t tx_packets_{0};
  uint32_t rx_packets_{0};
  uint32_t tx_drops_{0};  // Buffer overruns on TX
  uint32_t rx_drops_{0};  // Buffer overruns on RX

  // Automations
  Trigger<> start_trigger_;
  Trigger<> stop_trigger_;
};

// Actions
template<typename... Ts>
class StartAction : public Action<Ts...>, public Parented<IntercomAudio> {
 public:
  void set_remote_ip(std::function<std::string(Ts...)> func) { this->remote_ip_ = func; }
  void set_remote_port(std::function<uint16_t(Ts...)> func) { this->remote_port_ = func; }

  void play(Ts... x) override {
    if (this->remote_ip_.has_value() && this->remote_port_.has_value()) {
      this->parent_->start(this->remote_ip_.value()(x...), this->remote_port_.value()(x...));
    } else if (this->remote_ip_.has_value()) {
      this->parent_->start(this->remote_ip_.value()(x...), this->parent_->get_remote_port());
    } else {
      this->parent_->start();
    }
  }

 protected:
  optional<std::function<std::string(Ts...)>> remote_ip_;
  optional<std::function<uint16_t(Ts...)>> remote_port_;
};

template<typename... Ts>
class StopAction : public Action<Ts...>, public Parented<IntercomAudio> {
 public:
  void play(Ts... x) override { this->parent_->stop(); }
};

template<typename... Ts>
class ResetCountersAction : public Action<Ts...>, public Parented<IntercomAudio> {
 public:
  void play(Ts... x) override { this->parent_->reset_counters(); }
};

}  // namespace intercom_audio
}  // namespace esphome

#endif  // USE_ESP32
