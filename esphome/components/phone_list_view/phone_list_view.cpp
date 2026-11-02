#include "phone_list_view.h"

namespace esphome::phone_list_view {

void PhoneListView::setup() {
  this->disable_loop();
  if (this->parent_ == nullptr || !this->count_ || !this->render_) { this->mark_failed(); return; }
  static_assert(std::is_trivially_destructible<Slot>::value, "Pool records must remain POD-like");
  const bool ordered = bool(this->group_) && bool(this->key_);
  const size_t pool_bytes = sizeof(Slot) * this->capacity_;
  const size_t bytes = pool_bytes + (ordered ? sizeof(Projection) : 0);
#ifdef USE_ESP32
  auto *pool = static_cast<Slot *>(heap_caps_malloc(bytes,
      MALLOC_CAP_8BIT | (this->buffer_in_psram_ ? MALLOC_CAP_SPIRAM : MALLOC_CAP_INTERNAL)));
#else
  auto *pool = static_cast<Slot *>(std::malloc(bytes));
#endif
  if (pool != nullptr)
    for (size_t i = 0; i < this->capacity_; ++i) new (&pool[i]) Slot();
  this->slots_.reset(pool);
  if (!this->slots_) { this->mark_failed(); return; }
  if (ordered) this->projection_ = new (reinterpret_cast<uint8_t *>(pool) + pool_bytes) Projection();
  this->screen_ = lv_obj_get_screen(this->parent_);
  lv_obj_remove_flag(this->parent_, LV_OBJ_FLAG_SCROLL_ELASTIC);
  this->spacer_ = lv_obj_create(this->parent_);
  lv_obj_remove_style_all(this->spacer_);
  lv_obj_set_width(this->spacer_, 1);
  lv_obj_remove_flag(this->spacer_, static_cast<lv_obj_flag_t>(LV_OBJ_FLAG_CLICKABLE | LV_OBJ_FLAG_SCROLLABLE));
  for (size_t i = 0; i < this->capacity_; ++i) {
    auto &slot = this->slots_[i];
    slot.owner = this;
    slot.button = lv_button_create(this->parent_);
    lv_obj_set_width(slot.button, LV_PCT(100));
    lv_obj_set_height(slot.button, this->row_height_ - 3);
    lv_obj_set_style_bg_color(slot.button, lv_color_hex(0x202020), 0);
    lv_obj_set_style_border_width(slot.button, 0, 0);
    lv_obj_set_style_shadow_width(slot.button, 0, 0);
    lv_obj_set_style_radius(slot.button, 8, 0);
    lv_obj_set_style_pad_all(slot.button, 7, 0);
    lv_obj_remove_flag(slot.button, static_cast<lv_obj_flag_t>(LV_OBJ_FLAG_SCROLLABLE | LV_OBJ_FLAG_SCROLL_ON_FOCUS));
    lv_obj_add_event_cb(slot.button, row_event_, LV_EVENT_PRESSED, &slot);
    lv_obj_add_event_cb(slot.button, row_event_, LV_EVENT_SHORT_CLICKED, &slot);
    slot.leading = lv_label_create(slot.button);
    slot.name = lv_label_create(slot.button);
    slot.detail = lv_label_create(slot.button);
    slot.trailing = lv_label_create(slot.button);
    lv_obj_set_style_text_font(slot.name, this->text_font_, 0);
    lv_obj_set_style_text_font(slot.detail, this->text_font_, 0);
    lv_obj_set_style_text_font(slot.leading, this->icon_font_, 0);
    lv_obj_set_style_text_font(slot.trailing, this->icon_font_, 0);
    const int line_height = lv_font_get_line_height(this->text_font_);
    const int vertical_padding = std::clamp((int(this->row_height_) - 3 - 2 * line_height - 3) / 2, 0, 7);
    lv_obj_set_style_pad_top(slot.button, vertical_padding, 0);
    lv_obj_set_style_pad_bottom(slot.button, vertical_padding, 0);
    lv_obj_set_height(slot.name, line_height);
    lv_obj_set_height(slot.detail, line_height);
    // Static buffers retain their full text. LVGL's DOT mode requires its own
    // mutable text allocation; a fixed one-line clip avoids row growth and
    // keeps name and metadata separate without allocation during scrolling.
    lv_label_set_long_mode(slot.name, LV_LABEL_LONG_CLIP);
    lv_label_set_long_mode(slot.detail, LV_LABEL_LONG_CLIP);
    lv_obj_set_style_text_color(slot.name, lv_color_hex(0xF1F5F9), 0);
    lv_obj_set_style_text_color(slot.detail, lv_color_hex(0xA3A3A3), 0);
  }
  // Creating a focusable LVGL child can synchronously update its parent's
  // layout. Publish the callbacks only after every pooled row exists.
  this->empty_ = lv_label_create(this->parent_);
  lv_obj_set_style_text_font(this->empty_, this->text_font_, 0);
  lv_obj_set_style_text_color(this->empty_, lv_color_hex(0xA3A3A3), 0);
  lv_label_set_text_static(this->empty_, this->empty_text_.c_str());
  lv_obj_align(this->empty_, LV_ALIGN_TOP_MID, 0, 12);
  lv_obj_add_event_cb(this->parent_, parent_event_, LV_EVENT_SCROLL, this);
  lv_obj_add_event_cb(this->parent_, parent_event_, LV_EVENT_SIZE_CHANGED, this);
  lv_obj_add_event_cb(this->screen_, page_event_, LV_EVENT_SCREEN_LOADED, this);
  lv_obj_add_event_cb(this->screen_, page_event_, LV_EVENT_SCREEN_UNLOADED, this);
  this->refresh();
}

bool PhoneListView::visible_() const {
  if (this->screen_ == nullptr || lv_display_get_screen_active(lv_obj_get_display(this->parent_)) != this->screen_) return false;
  for (auto *object = this->parent_; object != nullptr; object = lv_obj_get_parent(object))
    if (lv_obj_has_flag(object, LV_OBJ_FLAG_HIDDEN)) return false;
  return true;
}

int PhoneListView::compare_names_(std::string_view a, std::string_view b) {
  // Locale-independent UTF-8 byte order, with ASCII case folded without a copy.
  const auto fold = [](uint8_t ch) { return ch >= 'A' && ch <= 'Z' ? ch + ('a' - 'A') : ch; };
  for (size_t i = 0, end = std::min(a.size(), b.size()); i < end; ++i) {
    const auto left = fold(static_cast<uint8_t>(a[i])), right = fold(static_cast<uint8_t>(b[i]));
    if (left != right) return left < right ? -1 : 1;
  }
  return a.size() < b.size() ? -1 : a.size() > b.size() ? 1 : 0;
}

int32_t PhoneListView::rebuild_order_(uint32_t count, int32_t scroll) {
  // The visible pool retains the old identity even when the source replaced its
  // vector. Resolve this borrowed anchor before painting overwrites any slot.
  std::string_view anchor;
  const int32_t top = scroll / this->row_height_ * this->row_height_;
  for (size_t i = 0; i < this->capacity_; ++i) {
    const auto &slot = this->slots_[i];
    if (slot.generation != 0 && !lv_obj_has_flag(slot.button, LV_OBJ_FLAG_HIDDEN) &&
        lv_obj_get_style_y(slot.button, LV_PART_MAIN) == top) {
      anchor = slot.row.id.data();
      break;
    }
  }
  this->ordered_count_ = 0;
  if (count > MAX_ORDER_ROWS) return scroll;  // Keep every row visible if a generic source exceeds the bounded projection.
  for (uint32_t i = 0; i < count; ++i) {
    this->projection_->indices[i] = i;
    this->projection_->groups[i] = this->group_(i);
  }
  std::sort(this->projection_->indices.begin(), this->projection_->indices.begin() + count,
            [this](uint8_t a, uint8_t b) {
    if (this->projection_->groups[a] != this->projection_->groups[b])
      return this->projection_->groups[a] < this->projection_->groups[b];
    const auto left = this->key_(a), right = this->key_(b);
    const int names = compare_names_(left.name, right.name);
    return names != 0 ? names < 0 : left.id < right.id;
  });
  this->ordered_count_ = count;
  if (!anchor.empty())
    for (uint32_t i = 0; i < count; ++i)
      if (this->key_(this->projection_->indices[i]).id == anchor)
        return i * this->row_height_ + scroll % this->row_height_;
  return scroll;
}

void PhoneListView::refresh_() {
  if (this->slots_ == nullptr || this->repainting_) return;
  if (!this->visible_()) { this->stop_blink_(); return; }
  this->repainting_ = true;
  const uint32_t count = this->count_();
  int32_t scroll = std::max<int32_t>(0, lv_obj_get_scroll_y(this->parent_));
  const bool reorder = this->projection_ != nullptr && (this->order_dirty_ || count != this->ordered_count_);
  // A callback may request another update while painting. Consume only the
  // pending update that led here, so that request survives to the next loop.
  this->order_dirty_ = false;
  if (reorder) scroll = this->rebuild_order_(count, scroll);
  if (count == 0) lv_obj_remove_flag(this->empty_, LV_OBJ_FLAG_HIDDEN);
  else lv_obj_add_flag(this->empty_, LV_OBJ_FLAG_HIDDEN);
  lv_obj_set_height(this->spacer_, std::max<uint32_t>(1, count * this->row_height_));
  lv_obj_update_layout(this->parent_);
  const int32_t limit = std::max<int32_t>(0, int32_t(count * this->row_height_) - lv_obj_get_content_height(this->parent_));
  scroll = std::min(scroll, limit);
  if (lv_obj_get_scroll_y(this->parent_) != scroll) lv_obj_scroll_to_y(this->parent_, scroll, LV_ANIM_OFF);
  const uint32_t visible = (scroll % this->row_height_ + std::max<int32_t>(1, lv_obj_get_content_height(this->parent_)) +
                            this->row_height_ - 1) / this->row_height_;
  uint32_t first = scroll / this->row_height_;
  // Keep a row above only when the existing pool can still cover the viewport.
  if (first > 0 && visible < this->capacity_) --first;
  for (size_t i = 0; i < this->capacity_; ++i) {
    auto &slot = this->slots_[i];
    if (first + i >= count) {
      lv_anim_delete(slot.trailing, blink_);
      lv_obj_add_flag(slot.button, LV_OBJ_FLAG_HIDDEN);
    } else {
      this->paint_(slot, first + i);
    }
  }
  // A list can consume a drag even at its boundary. Restrict its directions
  // to actual content so an outward swipe reaches the screen's existing
  // navigation gesture, while drags inside the list still scroll normally.
  lv_obj_update_layout(this->parent_);
  lv_obj_set_scroll_dir(this->parent_, static_cast<lv_dir_t>(
      (lv_obj_get_scroll_top(this->parent_) > 0 ? LV_DIR_TOP : LV_DIR_NONE) |
      (lv_obj_get_scroll_bottom(this->parent_) > 0 ? LV_DIR_BOTTOM : LV_DIR_NONE)));
  this->repainting_ = false;
}

void PhoneListView::paint_(Slot &slot, uint32_t index) {
  RowData next;
  this->render_(this->ordered_count_ != 0 ? this->projection_->indices[index] : index, next);
  if (next.id[0] == '\0') next.enabled = false;
  const bool first_paint = slot.generation == 0;
  if (slot.row.id != next.id || slot.row.action != next.action) ++slot.generation;
  const bool changed = first_paint || slot.row.id != next.id || slot.row.name != next.name || slot.row.detail != next.detail ||
                       slot.row.leading != next.leading || slot.row.trailing != next.trailing;
  const bool color_changed = first_paint || slot.row.color != next.color;
  const bool highlight_changed = first_paint || slot.row.highlighted != next.highlighted;
  const bool enabled_changed = first_paint || slot.row.enabled != next.enabled;
  const bool blink_changed = slot.row.blink != next.blink || slot.row.color != next.color;
  slot.row = next;
  const int32_t row_y = index * this->row_height_;
  if (lv_obj_get_style_x(slot.button, LV_PART_MAIN) != 0 || lv_obj_get_style_y(slot.button, LV_PART_MAIN) != row_y)
    lv_obj_set_pos(slot.button, 0, row_y);
  lv_obj_clear_flag(slot.button, LV_OBJ_FLAG_HIDDEN);
  if (changed) {
    lv_label_set_text_static(slot.leading, slot.row.leading.data());
    lv_label_set_text_static(slot.name, slot.row.name.data());
    lv_label_set_text_static(slot.detail, slot.row.detail.data());
    lv_label_set_text_static(slot.trailing, slot.row.trailing.data());
  }
  const int32_t width = lv_obj_get_content_width(this->parent_);
  const int32_t text_width = std::max<int32_t>(24, width - 76);
  // LVGL refreshes style and label layout even when a setter repeats its value.
  // Reuse the row's existing snapshot and geometry instead of invalidating it.
  if (changed || lv_obj_get_style_width(slot.name, LV_PART_MAIN) != text_width) {
    lv_obj_set_width(slot.name, text_width);
    lv_obj_set_width(slot.detail, text_width);
    lv_obj_align(slot.leading, LV_ALIGN_LEFT_MID, 0, 0);
    lv_obj_align(slot.trailing, LV_ALIGN_RIGHT_MID, 0, 0);
    if (slot.row.detail[0] && 2 * lv_font_get_line_height(this->text_font_) <= this->row_height_ - 3) {
      lv_obj_align(slot.name, LV_ALIGN_TOP_LEFT, 30, 0);
      lv_obj_align(slot.detail, LV_ALIGN_BOTTOM_LEFT, 30, 0);
      lv_obj_clear_flag(slot.detail, LV_OBJ_FLAG_HIDDEN);
    } else {
      lv_obj_align(slot.name, LV_ALIGN_LEFT_MID, 30, 0);
      lv_obj_add_flag(slot.detail, LV_OBJ_FLAG_HIDDEN);
    }
  }
  if (color_changed) {
    lv_obj_set_style_text_color(slot.leading, lv_color_hex(slot.row.color), 0);
    lv_obj_set_style_text_color(slot.name, lv_color_hex(slot.row.color), 0);
    lv_obj_set_style_text_color(slot.detail, lv_color_hex(slot.row.color), 0);
    lv_obj_set_style_text_color(slot.trailing, lv_color_hex(slot.row.color), 0);
  }
  if (highlight_changed)
    lv_obj_set_style_bg_color(slot.button, lv_color_hex(slot.row.highlighted ? 0x303030 : 0x202020), 0);
  if (enabled_changed)
    lv_obj_set_style_opa(slot.button, slot.row.enabled ? LV_OPA_COVER : LV_OPA_50, 0);
  if (blink_changed || !slot.row.blink) {
    lv_anim_delete(slot.trailing, blink_);
    lv_obj_set_style_opa(slot.trailing, LV_OPA_COVER, 0);
  }
  if (slot.row.blink && lv_anim_get(slot.trailing, blink_) == nullptr) {
    lv_anim_t animation;
    lv_anim_init(&animation);
    lv_anim_set_var(&animation, slot.trailing);
    lv_anim_set_exec_cb(&animation, blink_);
    lv_anim_set_values(&animation, 80, LV_OPA_COVER);
    lv_anim_set_duration(&animation, 600);
    lv_anim_set_playback_duration(&animation, 600);
    lv_anim_set_repeat_count(&animation, LV_ANIM_REPEAT_INFINITE);
    lv_anim_start(&animation);
  }
}

void PhoneListView::stop_blink_() {
  if (!this->slots_) return;
  for (size_t i = 0; i < this->capacity_; ++i) lv_anim_delete(this->slots_[i].trailing, blink_);
}

void PhoneListView::blink_(void *object, int32_t value) { lv_obj_set_style_opa(static_cast<lv_obj_t *>(object), value, 0); }

void PhoneListView::row_event_(lv_event_t *event) {
  auto &slot = *static_cast<Slot *>(lv_event_get_user_data(event));
  if (lv_event_get_code(event) == LV_EVENT_PRESSED) slot.pressed_generation = slot.generation;
  else if (slot.row.enabled && slot.row.id[0] && slot.pressed_generation == slot.generation)
    slot.owner->selected_.trigger(slot.row.id.data(), slot.row.action);
}

void PhoneListView::parent_event_(lv_event_t *event) {
  static_cast<PhoneListView *>(lv_event_get_user_data(event))->refresh_();
}

void PhoneListView::page_event_(lv_event_t *event) {
  auto *self = static_cast<PhoneListView *>(lv_event_get_user_data(event));
  if (lv_event_get_code(event) == LV_EVENT_SCREEN_UNLOADED) self->stop_blink_();
  else self->refresh();
}

}  // namespace esphome::phone_list_view
