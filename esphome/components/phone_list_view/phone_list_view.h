#pragma once

#include "esphome/core/automation.h"
#include "esphome/core/component.h"
#include "esphome/components/lvgl/lvgl_esphome.h"
#include <algorithm>
#include <array>
#include <cstring>
#include <cstdint>
#include <string>
#include <functional>
#include <string_view>
#include <memory>
#include <cstdlib>
#include <new>
#include <type_traits>
#ifdef USE_ESP32
#include <esp_heap_caps.h>
#endif

namespace esphome::phone_list_view {

struct RowData {
  std::array<char, 161> id{};
  std::array<char, 129> name{};
  std::array<char, 129> detail{};
  std::array<char, 9> leading{};
  std::array<char, 9> trailing{};
  uint32_t color{0xCBD5E1};
  uint8_t action{0};
  bool enabled{true};
  bool blink{false};
  bool highlighted{false};

  template<size_t N> static void text(std::array<char, N> &out, std::string_view value) {
    size_t size = std::min(value.size(), N - 1);
    // Visible text may be clipped, but must remain valid UTF-8.
    if (size < value.size())
      while (size > 0 && (static_cast<uint8_t>(value[size]) & 0xC0) == 0x80) --size;
    if (size != 0) std::memcpy(out.data(), value.data(), size);
    out[size] = '\0';
  }
  void set_id(std::string_view value) {
    if (value.size() >= this->id.size()) { this->enabled = false; return; }
    text(this->id, value);
  }
  void set_name(std::string_view value) { text(this->name, value); }
  void set_detail(std::string_view value) { text(this->detail, value); }
  void set_leading(std::string_view value) { text(this->leading, value); }
  void set_trailing(std::string_view value) { text(this->trailing, value); }
};

class PhoneListView : public Component {
 public:
  void set_parent_widget(lv_obj_t *value) { this->parent_ = value; }
  void set_row_height(uint16_t value) { this->row_height_ = value; }
  uint16_t get_row_height() const { return this->row_height_; }
  void set_visible_rows(uint8_t value) { this->capacity_ = value; }
  void set_buffer_in_psram(bool value) { this->buffer_in_psram_ = value; }
  void set_empty_text(const std::string &value) { this->empty_text_ = value; }
  void set_count_callback(std::function<uint32_t()> callback) { this->count_ = std::move(callback); }
  void set_render_callback(std::function<void(uint32_t, RowData &)> callback) { this->render_ = std::move(callback); }
  void set_text_font(const lv_font_t *font) { this->text_font_ = font; }
  void set_icon_font(const lv_font_t *font) { this->icon_font_ = font; }
#if defined(USE_FONT) && defined(USE_LVGL_FONT)
  void set_text_font(const font::Font *font) { this->text_font_ = font->get_lv_font(); }
  void set_icon_font(const font::Font *font) { this->icon_font_ = font->get_lv_font(); }
#endif
  Trigger<std::string, uint8_t> *get_select_trigger() { return &this->selected_; }
  void setup() override;
  void refresh();
  void request_refresh() { if (!this->is_failed()) this->enable_loop_soon_any_context(); }
  void loop() override {
    this->disable_loop();
    this->refresh();
  }
  float get_setup_priority() const override { return setup_priority::LATE - 1; }

 protected:
  struct Slot {
    PhoneListView *owner{nullptr};
    lv_obj_t *button{nullptr}, *leading{nullptr}, *name{nullptr}, *detail{nullptr}, *trailing{nullptr};
    RowData row{};
    uint32_t generation{0}, pressed_generation{0};
  };
  static void row_event_(lv_event_t *event);
  static void parent_event_(lv_event_t *event);
  static void page_event_(lv_event_t *event);
  static void blink_(void *object, int32_t value);
  void stop_blink_();
  bool visible_() const;
  void paint_(Slot &slot, uint32_t index);
  lv_obj_t *parent_{nullptr}, *spacer_{nullptr}, *screen_{nullptr}, *empty_{nullptr};
  std::string empty_text_{"No entries"};
  const lv_font_t *text_font_{LV_FONT_DEFAULT}, *icon_font_{LV_FONT_DEFAULT};
  uint16_t row_height_{48};
  uint8_t capacity_{8};
  bool repainting_{false}, buffer_in_psram_{true};
  struct SlotDeleter {
    void operator()(Slot *value) const {
#ifdef USE_ESP32
      heap_caps_free(value);
#else
      std::free(value);
#endif
    }
  };
  std::unique_ptr<Slot[], SlotDeleter> slots_;
  std::function<uint32_t()> count_;
  std::function<void(uint32_t, RowData &)> render_;
  Trigger<std::string, uint8_t> selected_;
};

template<typename... Ts> class RefreshAction : public Action<Ts...>, public Parented<PhoneListView> {
 public:
  void play(const Ts &...x) override { this->parent_->refresh(); }
};

}  // namespace esphome::phone_list_view
