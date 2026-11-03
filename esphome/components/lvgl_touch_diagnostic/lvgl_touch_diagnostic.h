#pragma once

#include "esphome/core/automation.h"
#include "esphome/core/component.h"
#include "esphome/core/log.h"
#include "esphome/components/lvgl/lvgl_esphome.h"
#include "tap_input.h"

namespace esphome::lvgl_touch_diagnostic {

class LvglTouchDiagnostic : public Component {
 public:
  void set_lvgl(lvgl::LvglComponent *parent) { this->parent_ = parent; }
  float get_setup_priority() const override { return setup_priority::LATE; }
  void setup() override {
    this->disable_loop();
    if (this->parent_ == nullptr || !this->parent_->is_ready() || !this->input_.attach(this->parent_->get_disp()))
      this->mark_failed();
  }
  void on_shutdown() override { this->input_.cancel(); }
  void tap(int32_t x, int32_t y) {
    if (this->is_failed() || this->parent_ == nullptr || !this->parent_->is_ready() || this->parent_->is_paused()) {
      ESP_LOGW("lvgl_touch", "Tap rejected: display unavailable or paused");
      return;
    }
    auto result = this->input_.tap(x, y);
    ESP_LOGI("lvgl_touch", "Tap x=%ld y=%ld result=%u", static_cast<long>(x), static_cast<long>(y),
             static_cast<unsigned>(result));
  }

 protected:
  lvgl::LvglComponent *parent_{nullptr};
  TapInput input_;
};

template<typename... Ts> class TapAction : public Action<Ts...> {
 public:
  void set_parent(LvglTouchDiagnostic *parent) { this->parent_ = parent; }
  TEMPLATABLE_VALUE(int32_t, x)
  TEMPLATABLE_VALUE(int32_t, y)
  void play(Ts... args) override { this->parent_->tap(this->x_.value(args...), this->y_.value(args...)); }

 protected:
  LvglTouchDiagnostic *parent_;
};

}  // namespace esphome::lvgl_touch_diagnostic
