#pragma once

#include "esphome/core/automation.h"
#include "esphome/core/component.h"
#include "esphome/components/lvgl/lvgl_esphome.h"
#include "visibility_policy.h"

namespace esphome::lvgl_visibility {

class LvglVisibility : public Component {
 public:
  void set_initializer(void (*initializer)(VisibilityPolicy &)) { this->initializer_ = initializer; }
  void setup() override {
    this->disable_loop();
    this->policy_.set_on_change([](void *context) {
      static_cast<LvglVisibility *>(context)->changed_.trigger();
    }, this);
    if (this->initializer_ != nullptr) this->initializer_(this->policy_);
  }
  float get_setup_priority() const override { return setup_priority::LATE; }
  bool is_visible(lv_obj_t *object) const { return this->policy_.is_visible(object); }
  void reconcile() { this->policy_.reconcile(); }
  Trigger<> *get_change_trigger() { return &this->changed_; }

 protected:
  void (*initializer_)(VisibilityPolicy &){nullptr};
  VisibilityPolicy policy_;
  Trigger<> changed_;
};

}  // namespace esphome::lvgl_visibility
