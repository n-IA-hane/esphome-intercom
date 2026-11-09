#pragma once

#include <cstdint>
#include <lvgl.h>

namespace esphome::lvgl_touch_diagnostic {

// The virtual device uses LVGL's normal pointer hit testing and event dispatch.
// Its existing LVGL read timer is paused in event mode except during a tap.
class TapInput {
 public:
  static constexpr uint32_t TAP_MS = 80;
  static constexpr uint32_t MIN_SWIPE_MS = 80, MAX_SWIPE_MS = 2000;
  enum class Result : uint8_t { ACCEPTED, UNAVAILABLE, BUSY, OUT_OF_BOUNDS, INVALID_DURATION };

  TapInput() = default;
  TapInput(const TapInput &) = delete;
  TapInput &operator=(const TapInput &) = delete;
  ~TapInput() { this->detach(); }
  bool attach(lv_display_t *display) {
    if (this->input_ != nullptr || display == nullptr)
      return false;
    this->input_ = lv_indev_create();
    if (this->input_ == nullptr)
      return false;
    this->display_ = display;
    lv_indev_set_type(this->input_, LV_INDEV_TYPE_POINTER);
    lv_indev_set_display(this->input_, display);
    lv_indev_set_user_data(this->input_, this);
    lv_indev_set_read_cb(this->input_, read_);
    lv_indev_set_mode(this->input_, LV_INDEV_MODE_EVENT);
    return true;
  }
  void detach() {
    if (this->input_ == nullptr)
      return;
    this->cancel();
    lv_indev_delete(this->input_);
    this->input_ = nullptr;
    this->display_ = nullptr;
  }
  Result tap(int32_t x, int32_t y) {
    return this->begin_(x, y, x, y, TAP_MS);
  }
  Result swipe(int32_t x1, int32_t y1, int32_t x2, int32_t y2, int32_t duration_ms) {
    if (duration_ms < static_cast<int32_t>(MIN_SWIPE_MS) || duration_ms > static_cast<int32_t>(MAX_SWIPE_MS))
      return Result::INVALID_DURATION;
    return this->begin_(x1, y1, x2, y2, static_cast<uint32_t>(duration_ms));
  }
  void cancel() {
    if (!this->pressed_)
      return;
    this->pressed_ = false;
    lv_indev_wait_release(this->input_);
    lv_indev_read(this->input_);
  }
  bool busy() const { return this->pressed_; }

 protected:
  Result begin_(int32_t x1, int32_t y1, int32_t x2, int32_t y2, uint32_t duration_ms) {
    if (this->input_ == nullptr)
      return Result::UNAVAILABLE;
    if (this->pressed_ || lv_indev_active() != nullptr || this->other_input_busy_() ||
        lv_display_get_screen_prev(this->display_) != nullptr ||
        lv_display_get_screen_loading(this->display_) != nullptr)
      return Result::BUSY;
    const auto width = lv_display_get_horizontal_resolution(this->display_);
    const auto height = lv_display_get_vertical_resolution(this->display_);
    if (x1 < 0 || y1 < 0 || x1 >= width || y1 >= height || x2 < 0 || y2 < 0 || x2 >= width || y2 >= height)
      return Result::OUT_OF_BOUNDS;
    this->point_ = this->start_ = {x1, y1};
    this->end_ = {x2, y2};
    this->duration_ms_ = duration_ms;
    this->screen_ = lv_display_get_screen_active(this->display_);
    this->rotation_ = lv_display_get_rotation(this->display_);
    this->started_ = lv_tick_get();
    this->pressed_ = true;
    lv_indev_read(this->input_);
    // LVGL also resumes this timer on a hit. Resume it even on empty space so
    // every accepted request reaches RELEASED; the release path pauses it.
    lv_timer_resume(lv_indev_get_read_timer(this->input_));
    return Result::ACCEPTED;
  }
  bool other_input_busy_() const {
    for (auto *input = lv_indev_get_next(nullptr); input != nullptr; input = lv_indev_get_next(input)) {
      if (input != this->input_ && lv_indev_get_display(input) == this->display_ &&
          (lv_indev_get_state(input) == LV_INDEV_STATE_PRESSED ||
           (lv_indev_get_type(input) == LV_INDEV_TYPE_POINTER && lv_indev_get_scroll_obj(input) != nullptr)))
        return true;
    }
    return false;
  }
  static void read_(lv_indev_t *input, lv_indev_data_t *data) {
    auto *self = static_cast<TapInput *>(lv_indev_get_user_data(input));
    const bool interrupted = self->pressed_ &&
        (self->other_input_busy_() || self->screen_ != lv_display_get_screen_active(self->display_) ||
         self->rotation_ != lv_display_get_rotation(self->display_));
    if (interrupted) {
      lv_indev_wait_release(input);
      self->pressed_ = false;
    } else if (self->pressed_) {
      const auto elapsed = lv_tick_elaps(self->started_);
      const auto position_ms = elapsed < self->duration_ms_ ? elapsed : self->duration_ms_;
      self->point_.x = self->start_.x + static_cast<int32_t>(
          static_cast<int64_t>(self->end_.x - self->start_.x) * position_ms / self->duration_ms_);
      self->point_.y = self->start_.y + static_cast<int32_t>(
          static_cast<int64_t>(self->end_.y - self->start_.y) * position_ms / self->duration_ms_);
      if (elapsed >= self->duration_ms_)
        self->pressed_ = false;
    }
    // Invert only LVGL's own display transform. ESPHome hardware rotation is
    // already represented by logical dimensions and leaves this rotation at0.
    const auto x = self->point_.x, y = self->point_.y;
    const auto width = lv_display_get_horizontal_resolution(self->display_);
    const auto height = lv_display_get_vertical_resolution(self->display_);
    switch (lv_display_get_rotation(self->display_)) {
      case LV_DISPLAY_ROTATION_90: data->point = {y, width - x - 1}; break;
      case LV_DISPLAY_ROTATION_180: data->point = {width - x - 1, height - y - 1}; break;
      case LV_DISPLAY_ROTATION_270: data->point = {height - y - 1, x}; break;
      default: data->point = self->point_; break;
    }
    data->state = self->pressed_ ? LV_INDEV_STATE_PRESSED : LV_INDEV_STATE_RELEASED;
  }

  lv_display_t *display_{nullptr};
  lv_indev_t *input_{nullptr};
  lv_obj_t *screen_{nullptr};
  lv_point_t point_{};
  lv_point_t start_{}, end_{};
  uint32_t started_{0};
  uint32_t duration_ms_{TAP_MS};
  lv_display_rotation_t rotation_{LV_DISPLAY_ROTATION_0};
  bool pressed_{false};
};

}  // namespace esphome::lvgl_touch_diagnostic
