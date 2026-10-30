#pragma once

#include "esphome/core/automation.h"
#include "esphome/core/component.h"
#include "esphome/core/time.h"
#include "esphome/components/lvgl/lvgl_esphome.h"
#include <algorithm>
#include <array>
#include <cstring>
#include <cstdio>
#include <functional>
#include <memory>
#include <cstdlib>
#include <new>
#include <type_traits>
#ifdef USE_ESP32
#include <esp_heap_caps.h>
#endif

namespace esphome::phone_list_view {

// IMAP INTERNALDATE carries its own UTC offset. Reuse ESPHome's configured
// timezone rather than presenting the server's UTC clock as local time.
inline std::string message_time(const std::string &value) {
  unsigned day, year, hour, minute, second, zone;
  char month[4]{}, sign;
  int consumed = 0;
  if (std::sscanf(value.c_str(), "%u-%3[A-Za-z]-%u %u:%u:%u %c%4u%n",
      &day, month, &year, &hour, &minute, &second, &sign, &zone, &consumed) != 8 ||
      consumed != static_cast<int>(value.size()) || year < 1970 || year > 2099 ||
      hour > 23 || minute > 59 || second > 59 || zone / 100 > 23 || zone % 100 > 59 ||
      (sign != '+' && sign != '-')) return value;
  unsigned number = 0;
  const char *months[] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};
  for (unsigned i = 0; i < 12; ++i) if (std::strcmp(month, months[i]) == 0) number = i + 1;
  if (number == 0 || day < 1 || day > days_in_month(number, year)) return value;
  ESPTime time{};
  time.year = year; time.month = number; time.day_of_month = day;
  time.hour = hour; time.minute = minute; time.second = second;
  time.recalc_timestamp_utc(false);
  const int offset = static_cast<int>(zone / 100 * 3600 + zone % 100 * 60);
  auto local = ESPTime::from_epoch_local(time.timestamp + (sign == '+' ? -offset : offset));
  char output[24];
  local.strftime(output, sizeof(output), "%d %b %H:%M");
  return output;
}

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

  template<size_t N> static void text(std::array<char, N> &out, const std::string &value) {
    size_t size = std::min(value.size(), N - 1);
    // Visible text may be clipped, but must remain valid UTF-8.
    if (size < value.size())
      while (size > 0 && (static_cast<uint8_t>(value[size]) & 0xC0) == 0x80) --size;
    std::memcpy(out.data(), value.data(), size);
    out[size] = '\0';
  }
  void set_id(const std::string &value) {
    if (value.size() >= this->id.size()) { this->enabled = false; return; }
    text(this->id, value);
  }
  void set_name(const std::string &value) { text(this->name, value); }
  void set_detail(const std::string &value) { text(this->detail, value); }
  void set_leading(const std::string &value) { text(this->leading, value); }
  void set_trailing(const std::string &value) { text(this->trailing, value); }
};

class PhoneListView : public Component {
 public:
  void set_parent_widget(lv_obj_t *value) { this->parent_ = value; }
  void set_row_height(uint16_t value) { this->row_height_ = value; }
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
