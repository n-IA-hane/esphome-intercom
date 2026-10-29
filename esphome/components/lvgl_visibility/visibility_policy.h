#pragma once

#include "lvgl.h"
#include <algorithm>
#include <vector>

namespace esphome::lvgl_visibility {

// Presentation work follows LVGL's real screen and ancestor visibility.
// Bind the boot-created screens once; no timer or polling task is added.
class VisibilityPolicy {
 public:
  void set_on_change(void (*callback)(void *), void *context) {
    this->callback_ = callback;
    this->context_ = context;
  }
  void reserve_roots(size_t count) { this->roots_.reserve(count); }
  void add_root(lv_obj_t *root) {
    if (root == nullptr || std::find(this->roots_.begin(), this->roots_.end(), root) != this->roots_.end()) return;
    this->roots_.push_back(root);
    if (this->display_ != nullptr) this->bind_(root);
  }
  void attach(lv_display_t *display) {
    this->add_root(lv_display_get_layer_top(display));
    this->add_root(lv_display_get_layer_sys(display));
    this->add_root(lv_display_get_layer_bottom(display));
    size_t labels = 0, images = 0;
    for (auto *root : this->roots_) this->count_(root, labels, images);
    this->labels_.reserve(labels);
    this->images_.reserve(images);
    this->display_ = display;
    for (auto *root : this->roots_) this->bind_(root);
    // Initial visibility must include computed geometry, including script-only
    // containers that have no native animation to trigger a later update.
    for (auto *root : this->roots_) lv_obj_update_layout(root);
    lv_display_add_event_cb(display, layout_event_, LV_EVENT_UPDATE_LAYOUT_COMPLETED, this);
    this->reconcile();
    this->notify_();
  }
  void detach() {
    if (this->display_ != nullptr)
      lv_display_remove_event_cb_with_user_data(this->display_, layout_event_, this);
    for (auto *root : this->roots_) lv_obj_remove_event_cb_with_user_data(root, screen_event_, this);
    this->roots_.clear();
    this->labels_.clear();
    this->images_.clear();
    this->display_ = nullptr;
  }
  bool is_visible(const lv_obj_t *object) const {
    if (object == nullptr) return false;
    auto *screen = lv_obj_get_screen(object);
    auto *display = lv_obj_get_display(object);
    const bool active = screen == lv_display_get_screen_active(display) ||
                        screen == lv_display_get_layer_top(display) ||
                        screen == lv_display_get_layer_sys(display) ||
                        screen == lv_display_get_layer_bottom(display);
    return screen != this->unloading_ && screen != this->loading_ && active && lv_obj_is_visible(object);
  }
  void reconcile() {
    if (this->updating_) return;
    this->updating_ = true;
    bool changed = false;
    for (auto &binding : this->labels_) {
      const bool visible = this->is_visible(binding.object);
      const auto wanted = visible ? binding.mode : LV_LABEL_LONG_MODE_CLIP;
      if (lv_label_get_long_mode(binding.object) != wanted) {
        // CLIP also prevents a hidden set_text() from creating a new marquee.
        lv_label_set_long_mode(binding.object, wanted);
        changed = true;
      }
    }
#if LV_USE_ANIMIMG
    for (auto &image : this->images_) {
      auto *animation = lv_anim_get(image.object, nullptr);
      if (!this->is_visible(image.object)) {
        if (animation != nullptr && !lv_anim_is_paused(animation)) {
          lv_anim_pause(animation);
          image.paused = true;
          changed = true;
        }
      } else if (image.paused) {
        // Resolve the running copy again: another owner may have deleted or
        // replaced it while the page was inactive.
        if (animation != nullptr) lv_anim_resume(animation);
        image.paused = false;
        changed = true;
      }
    }
#endif
    this->updating_ = false;
    if (changed) this->notify_();
  }
  size_t label_count() const { return this->labels_.size(); }

 protected:
  struct Label { lv_obj_t *object; lv_label_long_mode_t mode; };
  struct Image { lv_obj_t *object; bool paused{false}; };
  void bind_(lv_obj_t *root) {
    this->collect_(root);
    lv_obj_add_event_cb(root, screen_event_, LV_EVENT_ALL, this);
  }
  static void count_(lv_obj_t *object, size_t &labels, size_t &images) {
    if (lv_obj_check_type(object, &lv_label_class)) {
      const auto mode = lv_label_get_long_mode(object);
      if (mode == LV_LABEL_LONG_MODE_SCROLL || mode == LV_LABEL_LONG_MODE_SCROLL_CIRCULAR) ++labels;
    }
#if LV_USE_ANIMIMG
    if (lv_obj_check_type(object, &lv_animimg_class)) ++images;
#endif
    for (uint32_t i = 0; i < lv_obj_get_child_count(object); ++i)
      count_(lv_obj_get_child(object, i), labels, images);
  }
  void collect_(lv_obj_t *object) {
    if (lv_obj_check_type(object, &lv_label_class)) {
      const auto mode = lv_label_get_long_mode(object);
      if (mode == LV_LABEL_LONG_MODE_SCROLL || mode == LV_LABEL_LONG_MODE_SCROLL_CIRCULAR)
        this->labels_.push_back({object, mode});
    }
#if LV_USE_ANIMIMG
    if (lv_obj_check_type(object, &lv_animimg_class)) this->images_.push_back({object});
#endif
    for (uint32_t i = 0; i < lv_obj_get_child_count(object); ++i) this->collect_(lv_obj_get_child(object, i));
  }
  void notify_() { if (this->callback_ != nullptr) this->callback_(this->context_); }
  static void layout_event_(lv_event_t *event) {
    auto *self = static_cast<VisibilityPolicy *>(lv_event_get_user_data(event));
    self->reconcile();
    self->notify_();
  }
  static void screen_event_(lv_event_t *event) {
    const auto code = lv_event_get_code(event);
    // ALL saves three descriptors per screen. Draw/layout events must never
    // trigger reconciliation from inside LVGL's rendering or invalidation.
    if (code != LV_EVENT_DELETE && code != LV_EVENT_SCREEN_UNLOAD_START &&
        code != LV_EVENT_SCREEN_LOAD_START && code != LV_EVENT_SCREEN_LOADED) return;
    auto *self = static_cast<VisibilityPolicy *>(lv_event_get_user_data(event));
    auto *screen = lv_event_get_target_obj(event);
    if (code == LV_EVENT_DELETE) {
      if (self->loading_ == screen) self->loading_ = nullptr;
      if (self->unloading_ == screen) self->unloading_ = nullptr;
      const auto belongs = [screen](const auto &entry) { return lv_obj_get_screen(entry.object) == screen; };
      self->labels_.erase(std::remove_if(self->labels_.begin(), self->labels_.end(), belongs), self->labels_.end());
      self->images_.erase(std::remove_if(self->images_.begin(), self->images_.end(),
          belongs), self->images_.end());
      self->roots_.erase(std::remove(self->roots_.begin(), self->roots_.end(), screen), self->roots_.end());
      return;
    }
    if (code == LV_EVENT_SCREEN_UNLOAD_START) self->unloading_ = screen;
    if (code == LV_EVENT_SCREEN_LOAD_START) self->loading_ = screen;
    if (code == LV_EVENT_SCREEN_LOADED) self->loading_ = self->unloading_ = nullptr;
    self->reconcile();
    self->notify_();
  }
  std::vector<lv_obj_t *> roots_;
  std::vector<Image> images_;
  std::vector<Label> labels_;
  lv_display_t *display_{nullptr};
  lv_obj_t *loading_{nullptr}, *unloading_{nullptr};
  void (*callback_)(void *){nullptr};
  void *context_{nullptr};
  bool updating_{false};
};

}  // namespace esphome::lvgl_visibility
