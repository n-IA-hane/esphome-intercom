#pragma once

#include "esphome/core/automation.h"
#include "esphome/core/component.h"
#include "esphome/components/lvgl/lvgl_esphome.h"
#include "visibility_policy.h"

namespace esphome::lvgl_visibility {

class LvglVisibility : public Component {
 public:
  void set_lvgl(lvgl::LvglComponent *parent) { this->parent_ = parent; }
  void add_page(lvgl::LvPageType *page) { this->pages_.push_back(page); }
  void setup() override {
    this->disable_loop();
    this->policy_.set_on_change([](void *context) {
      static_cast<LvglVisibility *>(context)->changed_.trigger();
    }, this);
    for (auto *page : this->pages_) this->policy_.add_root(page->obj);
    if (this->pages_.empty()) this->policy_.add_root(this->parent_->get_screen_active());
    this->policy_.attach(this->parent_->get_disp());
  }
  float get_setup_priority() const override { return setup_priority::LATE; }
  bool is_visible(lv_obj_t *object) const { return this->policy_.is_visible(object); }
  void reconcile() { this->policy_.reconcile(); }
  Trigger<> *get_change_trigger() { return &this->changed_; }

 protected:
  lvgl::LvglComponent *parent_{nullptr};
  std::vector<lvgl::LvPageType *> pages_;
  VisibilityPolicy policy_;
  Trigger<> changed_;
};

}  // namespace esphome::lvgl_visibility
