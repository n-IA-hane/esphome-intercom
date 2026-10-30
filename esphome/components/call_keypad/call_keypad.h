#pragma once

#include "esphome/core/component.h"
#include "esphome/core/helpers.h"
#include "esphome/components/lvgl/lvgl_esphome.h"
#include <cstdio>
#include <cstring>

namespace esphome::call_keypad {

// Labels are borrowed for the display's lifetime. The call and visibility
// owners deliver events; this presenter never observes SIP or polls state.
class CallKeypad : public Component {
 public:
  void set_labels(lv_obj_t *name, lv_obj_t *duration) { this->name_ = name; this->duration_ = duration; }
  void setup() override {
    this->disable_loop();
    this->timer_ = lv_timer_create([](lv_timer_t *timer) {
      static_cast<CallKeypad *>(lv_timer_get_user_data(timer))->update_duration_();
    }, 1000, this);
    if (this->timer_ == nullptr) { this->mark_failed(); return; }
    lv_timer_pause(this->timer_);
    std::strcpy(this->duration_text_, "00:00");
    lv_label_set_text_static(this->duration_, this->duration_text_);
  }
  float get_setup_priority() const override { return setup_priority::LATE - 1; }
  static void format_duration(char *out, size_t size, uint32_t seconds) {
    if (seconds >= 3600)
      std::snprintf(out, size, "%u:%02u:%02u", unsigned(seconds / 3600), unsigned((seconds / 60) % 60), unsigned(seconds % 60));
    else
      std::snprintf(out, size, "%02u:%02u", unsigned(seconds / 60), unsigned(seconds % 60));
  }
  void connected() { this->connected_ms_ = millis(); }
  void ended() {
    this->connected_ms_ = 0;
    if (this->timer_ != nullptr) lv_timer_pause(this->timer_);
    if (this->duration_ != nullptr && !lv_obj_has_flag(this->duration_, LV_OBJ_FLAG_HIDDEN))
      lv_obj_add_flag(this->duration_, LV_OBJ_FLAG_HIDDEN);
  }
  void sync(bool active, const char *peer) {
    if (this->timer_ == nullptr || this->is_failed()) return;
    if (!active) {
      this->ended_visibility_();
      return;
    }
    if (lv_obj_has_flag(this->duration_, LV_OBJ_FLAG_HIDDEN)) lv_obj_clear_flag(this->duration_, LV_OBJ_FLAG_HIDDEN);
    if (peer == nullptr) peer = "";
    if (std::strcmp(lv_label_get_text(this->name_), peer) != 0) lv_label_set_text(this->name_, peer);
    if (lv_timer_get_paused(this->timer_)) {
      lv_timer_resume(this->timer_);
      lv_timer_reset(this->timer_);
    }
    this->update_duration_();
  }

 protected:
  void ended_visibility_() {
    if (!lv_timer_get_paused(this->timer_)) lv_timer_pause(this->timer_);
    if (!lv_obj_has_flag(this->duration_, LV_OBJ_FLAG_HIDDEN)) lv_obj_add_flag(this->duration_, LV_OBJ_FLAG_HIDDEN);
  }
  void update_duration_() {
    const uint32_t seconds = (millis() - this->connected_ms_) / 1000;
    char next[sizeof(this->duration_text_)];
    this->format_duration(next, sizeof(next), seconds);
    if (std::strcmp(this->duration_text_, next) == 0) return;
    std::strcpy(this->duration_text_, next);
    // Static storage prevents reallocating the timer label on every second.
    lv_label_set_text_static(this->duration_, this->duration_text_);
  }
  lv_obj_t *name_{nullptr};
  lv_obj_t *duration_{nullptr};
  lv_timer_t *timer_{nullptr};
  uint32_t connected_ms_{0};
  char duration_text_[16]{};
};

}  // namespace esphome::call_keypad
