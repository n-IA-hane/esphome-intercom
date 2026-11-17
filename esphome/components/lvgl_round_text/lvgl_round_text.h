#pragma once

#include "esphome/core/component.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"
#include "esphome/components/lvgl/lvgl_esphome.h"
#include "round_text.h"
#include <string>

namespace esphome::lvgl_round_text {

class LvglRoundText : public Component {
 public:
  LvglRoundText() : text_(RoundText::Allocator{this, allocate_, release_}) {}
  void set_buffers_in_psram(bool enabled) { this->buffers_in_psram_ = enabled; }
  void setup() override {
    this->disable_loop();
    if (!this->text_.setup()) {
      ESP_LOGE("lvgl_round_text", "Cannot preallocate round text buffers");
      this->mark_failed();
    }
  }
  float get_setup_priority() const override { return setup_priority::PROCESSOR; }
  void dump_config() override {
    ESP_LOGCONFIG("lvgl_round_text", "Round text: reusable buffers, %s memory preferred",
                  this->buffers_in_psram_ ? "external" : "internal");
#ifdef USE_LVGL_ROUND_TEXT_DEBUG
    const auto &stats = this->text_.statistics();
    ESP_LOGCONFIG("lvgl_round_text", "Allocations: %u, copied bytes: %u, measurements: %u, label updates: %u",
                  static_cast<unsigned>(stats.allocations), static_cast<unsigned>(stats.copied_bytes),
                  static_cast<unsigned>(stats.measurements), static_cast<unsigned>(stats.label_updates));
#endif
  }
  void bind_thinking(lv_obj_t *const *labels) { this->text_.bind_thinking(labels); }
  void bind_reply(lv_obj_t *const *labels, lv_obj_t *counter) { this->text_.bind_reply(labels, counter); }
  template<typename Width> bool set_thinking(const std::string &text, const Width *chords,
                                            size_t count = RoundText::ROWS) {
    return this->text_.set_thinking(text.data(), text.size(), this->thinking_font_, chords, count);
  }
  template<typename Width> bool set_reply(const std::string &text, const Width *chords,
                                         size_t count = RoundText::ROWS) {
    return this->text_.set_reply(text.data(), text.size(), this->reply_font_, chords, count);
  }
  void set_thinking_font(const lv_font_t *font) { this->thinking_font_ = font; }
  void set_reply_font(const lv_font_t *font) { this->reply_font_ = font; }
  bool show_reply(size_t page) { return this->text_.show_reply(page); }
  size_t total_pages() const { return this->text_.total_pages(); }
  void clear() { this->text_.clear(); }

 protected:
  static void *allocate_(void *context, size_t bytes) {
    auto *self = static_cast<LvglRoundText *>(context);
    // ESPHome's allocator prefers PSRAM with NONE and falls back to internal
    // memory. Without PSRAM support the same contract allocates internally.
    RAMAllocator<uint8_t> allocator(self->buffers_in_psram_ ? RAMAllocator<uint8_t>::NONE
                                                           : RAMAllocator<uint8_t>::ALLOC_INTERNAL);
    return allocator.allocate(bytes);
  }
  static void release_(void *, void *pointer) {
    RAMAllocator<uint8_t>().deallocate(static_cast<uint8_t *>(pointer), 0);
  }
  bool buffers_in_psram_{true};
  const lv_font_t *thinking_font_{nullptr}, *reply_font_{nullptr};
  RoundText text_;
};

}  // namespace esphome::lvgl_round_text
