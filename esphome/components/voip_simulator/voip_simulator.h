#pragma once

#include <atomic>
#include <mutex>
#include <string>
#include <thread>

#include "esphome/core/component.h"

namespace esphome {
namespace voip_simulator {

class VoipSimulator : public Component {
 public:
  void set_source_profile(const std::string &source_profile) { this->source_profile_ = source_profile; }
  void set_device_profile(const std::string &device_profile) { this->device_profile_ = device_profile; }
  void set_socket_path(const std::string &socket_path) { this->socket_path_ = socket_path; }
  void set_speaker_output_path(const std::string &speaker_output_path) { this->speaker_output_path_ = speaker_output_path; }
  void set_microphone_input_path(const std::string &microphone_input_path) { this->microphone_input_path_ = microphone_input_path; }
  void set_framebuffer_path(const std::string &framebuffer_path) { this->framebuffer_path_ = framebuffer_path; }
  void setup() override;
  void loop() override;
  void dump_config() override;
  void on_shutdown() override;

  struct EndpointState {
    std::string state{"idle"};
    std::string caller;
    std::string destination;
    std::string selected;
    std::string last_reason;
    int visible_contacts{0};
  };

  struct SimulatorState {
    EndpointState esp;
    EndpointState caller;
    EndpointState callee;
    EndpointState second;
    EndpointState softphone;
    std::string bridge_state{"idle"};
    std::string bridge_left;
    std::string bridge_right;
    bool audio_tx_ready{false};
    bool audio_rx_ready{false};
    std::string audio_owner{"none"};
    int audio_tx_frames{0};
    int audio_rx_frames{0};
    int browser_tx_ready_latency_ms{-1};
    int sip_last_status{0};
    std::string sip_decline_reason;
    std::string sip_auth_reason;
    std::string led_color;
    std::string led_effect;
    std::string media_state{"idle"};
    std::string voip_state{"idle"};
    std::string voip_caller;
    std::string voice_assistant_state{"idle"};
    std::string voice_assistant_phase{"idle"};
    std::string wake_word;
    int voice_assistant_events{0};
    int aec_frames{0};
    int aec_last_processing_us{0};
    int aec_max_processing_us{0};
    int afe_frames{0};
    int afe_last_latency_us{0};
    int afe_max_latency_us{0};
    std::string display_page{"idle"};
    std::string display_status{"idle"};
    std::string touch_last;
    bool backlight_on{true};
    bool mic_muted{false};
    bool speaker_muted{false};
    std::string card_mode;
    std::string card_controlled_device;
    std::string card_rendered_state{"idle"};
    std::string card_source;
    bool backend_browser_audio{false};
    int phonebook_revision{0};
    bool phonebook_duplicate_ids{false};
    int ha_visible_contacts{0};
    bool opt_esp_dnd{false};
    bool opt_esp_auto_answer{false};
    bool opt_caller_sip_bridge{false};
    bool ha_answer_pending{false};
    uint64_t now_ms{0};
    bool shutdown{false};
  };

 protected:
  void reset_state_();
  void server_main_();
  std::string handle_request_(const std::string &line);
  std::string dispatch_(const std::string &method, const std::string &params);
  std::string snapshot_json_() const;
  void write_framebuffer_() const;
  void write_audio_marker_(const std::string &label) const;
  void press_button_(const std::string &button);
  void inject_event_(const std::string &params);
  void ha_call_(const std::string &target, const std::string &caller);
  void esp_call_(const std::string &source, const std::string &destination, const std::string &route);
  void sip_invite_(const std::string &caller, const std::string &callee, const std::string &call_id);

  std::string source_profile_;
  std::string device_profile_{"generic"};
  std::string socket_path_{"test_captures/simulator/voip-sim.sock"};
  std::string speaker_output_path_{"test_captures/simulator/audio/speaker_output.pcm"};
  std::string microphone_input_path_{"tests/simulator/audio/mic_input.pcm"};
  std::string framebuffer_path_{"test_captures/simulator/framebuffer.png"};
  mutable std::mutex mutex_;
  SimulatorState state_;
  std::thread server_thread_;
  std::atomic<bool> running_{false};
};

}  // namespace voip_simulator
}  // namespace esphome
