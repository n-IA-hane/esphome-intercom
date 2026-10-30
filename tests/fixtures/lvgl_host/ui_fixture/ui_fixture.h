#pragma once

#include "esphome/core/component.h"
#include "esphome/core/automation.h"
#ifdef USE_MEDIA_PLAYER
#include "esphome/components/media_player/media_player.h"
#endif
#ifdef USE_SWITCH
#include "esphome/components/switch/switch.h"
#endif
#include "lvgl_pointer_probe.h"
#include <string_view>

namespace esphome::wifi {
struct FixtureNetwork {
  bool connected{true};
  bool is_connected() const { return connected; }
  int wifi_rssi() const { return -40; }
};
inline FixtureNetwork fixture_network;
inline FixtureNetwork *global_wifi_component = &fixture_network;
}  // namespace esphome::wifi

namespace esphome::ui_fixture {

inline uint32_t voip_action_writes{0};
inline uint32_t dial_requests{0};
inline std::string last_dial_target;
class UiPhone {
 public:
  bool idle{true};
  bool ringing{false};
  bool calling{false};
  bool in_call{false};
  std::string peer_name{"Casa"};
  std::string selected_name{"Other contact"};
  std::string get_active_peer_name() const { return this->peer_name; }
  std::string get_current_destination() const { return this->selected_name; }
  bool is_idle() const { return this->idle; }
  bool is_active() const { return !this->idle; }
  bool is_ringing() const { return this->ringing; }
  bool is_calling() const { return this->calling; }
  bool is_in_call() const { return this->in_call; }
  bool can_send_dtmf() const { return this->in_call; }
  bool send_dtmf(char) {
    if (!this->can_send_dtmf()) return false;
    ++voip_action_writes;
    return true;
  }
  void call_toggle() { ++voip_action_writes; }
  bool call_mailbox() { ++voip_action_writes; return true; }
#ifdef USE_SWITCH
  void set_conference_ring_switch(switch_::Switch *setting) { this->conference_ring_switch_ = setting; }
  switch_::Switch *get_conference_ring_switch() const { return this->conference_ring_switch_; }
#endif
  bool get_conference_ring() const {
#ifdef USE_SWITCH
    if (this->conference_ring_switch_ != nullptr) return this->conference_ring_switch_->state;
#endif
    return this->conference_ring_;
  }
  void set_conference_ring(bool value) {
#ifdef USE_SWITCH
    if (this->conference_ring_switch_ != nullptr) {
      if (value) this->conference_ring_switch_->turn_on();
      else this->conference_ring_switch_->turn_off();
      return;
    }
#endif
    this->conference_ring_ = value;
  }
 protected:
#ifdef USE_SWITCH
  switch_::Switch *conference_ring_switch_{nullptr};
#endif
  bool conference_ring_{false};
};

// The UI consumes renderer readiness, not decoder or RTP internals.
class UiVideo {
 public:
  bool remote_frame{false};
  bool has_remote_frame() const { return this->remote_frame; }
};

class UiRuntime {
 public:
  bool timer_ringing{false};
  bool no_ha{false};
  bool is_activity_active(std::string_view name) const {
    return (name == "timer_ringing" && timer_ringing) || (name == "no_ha" && no_ha);
  }
};

template<typename... Ts> class UiDialAction : public Action<Ts...> {
 public:
  TEMPLATABLE_VALUE(std::string, target)
  void play(const Ts &...x) override {
    ++dial_requests;
    last_dial_target = this->target_.value(x...);
  }
};
template<typename... Ts> class UiVoipAction : public Action<Ts...> {
 public:
  void play(const Ts &...) override { ++voip_action_writes; }
};

// A data owner for exercising the public media-player UI contract. It has no
// audio, SIP, HA or transport implementation and never opens a device.
#ifdef USE_MEDIA_PLAYER
class UiPlayer : public Component, public media_player::MediaPlayer {
 public:
  uint32_t writes{0};
  bool accept_writes{true};
  media_player::MediaPlayerTraits get_traits() override { return {}; }
  void setup() override {
    this->state = media_player::MEDIA_PLAYER_STATE_IDLE;
    this->volume = 1.0f;
    this->publish_state();
  }

 protected:
  void control(const media_player::MediaPlayerCall &call) override {
    ++this->writes;
    if (this->accept_writes && call.get_volume().has_value())
      this->volume = *call.get_volume();
    this->publish_state();
  }
};

#endif  // USE_MEDIA_PLAYER

}  // namespace esphome::ui_fixture
