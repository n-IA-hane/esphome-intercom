#pragma once

#include "lvgl.h"
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <new>
#include <new>

namespace esphome::lvgl_round_text {

// One owner for reusable text storage. LVGL's static setter borrows these bytes.
// A replacement allocation remains staged until every row can be committed.
class RoundText {
 public:
  static constexpr size_t ROWS = 10;
  struct Allocator {
    void *context{nullptr};
    void *(*allocate)(void *, size_t){[](void *, size_t bytes) { return std::malloc(bytes); }};
    void (*release)(void *, void *){[](void *, void *pointer) { std::free(pointer); }};
  };
  struct Line { uint32_t offset{0}, length{0}; };
#ifdef USE_LVGL_ROUND_TEXT_DEBUG
  struct Statistics { size_t allocations{0}, copied_bytes{0}, measurements{0}, label_updates{0}; };
  const Statistics &statistics() const { return this->statistics_; }
#endif

  RoundText() = default;
  explicit RoundText(Allocator allocator) : allocator_(allocator) {}
  RoundText(const RoundText &) = delete;
  RoundText &operator=(const RoundText &) = delete;
  // Widget lifetime belongs to LVGL. Call clear() while widgets still exist
  // before destroying a host fixture; firmware components have static lifetime.
  ~RoundText() { this->release_all_(); }

  bool setup() {
    if (this->ready_) return true;
    if (!this->reserve_initial_(this->arena_, 2048, 4096) ||
        !this->reserve_initial_(this->indices_, 64 * sizeof(Line), 128 * sizeof(Line)) ||
        !this->reserve_initial_(this->scratch_, 128, 1024) ||
        !this->reserve_initial_(this->counter_, 64, 64)) {
      this->release_all_(); return false;
    }
    for (auto &buffer : this->thinking_) {
      if (!this->reserve_initial_(buffer, 64, 256)) { this->release_all_(); return false; }
    }
    for (auto &buffer : this->reply_) {
      if (!this->reserve_initial_(buffer, 64, 256)) { this->release_all_(); return false; }
    }
    this->ready_ = true;
    return true;
  }
  void bind_thinking(lv_obj_t *const *labels) {
    for (size_t i = 0; i < ROWS; ++i) this->thinking_labels_[i] = labels[i];
  }
  void bind_reply(lv_obj_t *const *labels, lv_obj_t *counter) {
    for (size_t i = 0; i < ROWS; ++i) this->reply_labels_[i] = labels[i];
    this->counter_label_ = counter;
  }
  size_t line_count() const { return this->line_count_; }
  size_t total_pages() const { return this->line_count_ == 0 ? 1 : this->line_count_ / ROWS; }
  const char *reply_data() const { return this->arena_.active; }
  Line reply_line(size_t index) const {
    return index < this->line_count_ ? reinterpret_cast<const Line *>(this->indices_.active)[index] : Line{};
  }

  template<typename Width> bool set_thinking(const char *text, size_t length, const lv_font_t *font,
                                            const Width *chords, size_t chord_count = ROWS) {
    if (!this->ready_ || font == nullptr || chord_count == 0 || !this->bound_(this->thinking_labels_)) return false;
    size_t normalized = 0, words = 0;
    this->count_words_(text, length, normalized, words);
    if (length == 3 && std::memcmp(text, "...", 3) == 0) normalized = words = 0;
    Prepared scratch;
    if (!this->prepare_(scratch, this->scratch_, normalized + 1)) return false;
    this->normalize_(text, length, scratch.target, normalized);
    std::array<Line, ROWS> rows{};
    size_t cursor = 0;
    const size_t first = chord_count / 2 >= 3 ? chord_count / 2 - 3 : 0;
    for (size_t row = first; row < ROWS && row < chord_count && cursor < normalized; ++row)
      if (chords[row] > 0) rows[row] = this->next_line_(scratch.target, normalized, cursor, font, chords[row]);
    if (!this->render_(this->thinking_labels_, this->thinking_, scratch.target, rows, true)) return false;
    scratch.target[0] = '\0'; this->commit_(scratch);
    return true;
  }

  template<typename Width> bool set_reply(const char *text, size_t length, const lv_font_t *font,
                                         const Width *chords, size_t chord_count = ROWS) {
    if (!this->ready_ || font == nullptr || chord_count == 0 ||
        !this->bound_(this->reply_labels_) || this->counter_label_ == nullptr)
      return false;
    size_t normalized = 0, words = 0;
    this->count_words_(text, length, normalized, words);
    if (normalized >= UINT32_MAX || normalized > std::numeric_limits<size_t>::max() - alignof(Line) ||
        words > (std::numeric_limits<size_t>::max() / sizeof(Line) - 9) / 2)
      return false;
    const size_t staged_offset = (normalized + alignof(Line)) & ~(alignof(Line) - 1);
    const size_t staged_capacity = words * 2 + 9;
    if (staged_capacity > (std::numeric_limits<size_t>::max() - staged_offset) / sizeof(Line)) return false;
    Prepared scratch;
    if (!this->prepare_(scratch, this->scratch_, staged_offset + staged_capacity * sizeof(Line))) return false;
    this->normalize_(text, length, scratch.target, normalized);
    // Stage intervals in the existing transaction scratch. A single wrap does
    // the expensive font measurements; commit copies only validated spans.
    auto *staged = reinterpret_cast<Line *>(scratch.target + staged_offset);
    std::array<Line, ROWS> first_page{};
    const size_t count = this->wrap_reply_(scratch.target, normalized, words, font, chords, chord_count,
        [&first_page, staged](size_t index, Line line) {
          new (staged + index) Line(line);
          if (index < ROWS) first_page[index] = line;
        });
    Prepared arena, indices;
    if (!this->prepare_(arena, this->arena_, normalized + 1) ||
        !this->prepare_(indices, this->indices_, count * sizeof(Line))) return false;
    std::array<Prepared, ROWS> visible;
    for (size_t i = 0; i < ROWS; ++i) {
      const size_t previous = std::strlen(lv_label_get_text(this->reply_labels_[i])) + 1;
      const size_t required = first_page[i].length + 1 > previous ? first_page[i].length + 1 : previous;
      if (!this->prepare_(visible[i], this->reply_[i], required)) return false;
    }
    // All component allocations succeeded. Growing a visible cache must rebind
    // the unchanged previous text before releasing its previous backing bytes.
    for (size_t i = 0; i < ROWS; ++i) this->retain_visible_(visible[i], this->reply_labels_[i]);
    this->copy_(arena.target, scratch.target, normalized + 1);
    auto *lines = reinterpret_cast<Line *>(indices.target);
    for (size_t i = 0; i < count; ++i) new (lines + i) Line(staged[i]);
#ifdef USE_LVGL_ROUND_TEXT_DEBUG
    this->statistics_.copied_bytes += count * sizeof(Line);
#endif
    this->commit_(arena); this->commit_(indices);
    this->line_count_ = count;
    scratch.target[0] = '\0';  // Scratch has no retained reply representation.
    this->commit_(scratch);
    return true;
  }

  bool show_reply(size_t page) {
    if (!this->ready_ || !this->bound_(this->reply_labels_) || this->counter_label_ == nullptr) return false;
    const size_t pages = this->total_pages(), current = page % pages;
    std::array<Line, ROWS> rows{};
    for (size_t i = 0; i < ROWS; ++i) rows[i] = this->reply_line(current * ROWS + i);
    char counter[48]{};
    if (pages > 1) std::snprintf(counter, sizeof(counter), "%zu/%zu", current + 1, pages);
    // Counter capacity is fixed and preallocated; its maximum text is bounded
    // by two size_t values plus slash and NUL, below 48 on supported targets.
    if (!this->render_(this->reply_labels_, this->reply_, this->arena_.active, rows, false)) return false;
    if (std::strcmp(lv_label_get_text(this->counter_label_), counter) != 0) {
      this->copy_(this->counter_.active, counter, std::strlen(counter) + 1);
      lv_label_set_text_static(this->counter_label_, this->counter_.active);
      this->label_updated_();
    }
    return true;
  }

  void clear() {
    // Detach every borrowed pointer before freeing temporary oversized storage.
    for (size_t i = 0; i < ROWS; ++i) {
      this->clear_label_(this->thinking_labels_[i], this->thinking_[i]);
      this->clear_label_(this->reply_labels_[i], this->reply_[i]);
      this->clear_overflow_(this->thinking_[i]); this->clear_overflow_(this->reply_[i]);
    }
    this->clear_label_(this->counter_label_, this->counter_);
    this->clear_overflow_(this->arena_); this->clear_overflow_(this->indices_);
    this->clear_overflow_(this->scratch_); this->clear_overflow_(this->counter_);
    if (this->arena_.active != nullptr) this->arena_.active[0] = '\0';
    this->line_count_ = 0;
  }

 protected:
  struct Buffer {
    char *base{nullptr}, *overflow{nullptr}, *active{nullptr};
    size_t capacity{0}, overflow_capacity{0}, maximum{0};
  };
  struct Prepared {
    RoundText *owner{nullptr};
    Buffer *buffer{nullptr};
    char *target{nullptr};
    size_t capacity{0};
    bool allocated{false}, overflow{false};
    ~Prepared() { if (this->allocated) this->owner->free_(this->target); }
    Prepared() = default;
    Prepared(const Prepared &) = delete;
    Prepared &operator=(const Prepared &) = delete;
  };
  void *allocate_(size_t bytes) {
#ifdef USE_LVGL_ROUND_TEXT_DEBUG
    ++this->statistics_.allocations;
#endif
    return this->allocator_.allocate(this->allocator_.context, bytes);
  }
  void free_(void *pointer) { if (pointer != nullptr) this->allocator_.release(this->allocator_.context, pointer); }
  void copy_(void *destination, const void *source, size_t bytes) {
    std::memcpy(destination, source, bytes);
#ifdef USE_LVGL_ROUND_TEXT_DEBUG
    this->statistics_.copied_bytes += bytes;
#endif
  }
  void label_updated_() {
#ifdef USE_LVGL_ROUND_TEXT_DEBUG
    ++this->statistics_.label_updates;
#endif
  }
  void clear_label_(lv_obj_t *label, const Buffer &buffer) {
    if (label == nullptr) return;
    const char *current = lv_label_get_text(label);
    // Empty contents still require a rebind while LVGL borrows our storage.
    // Afterwards the same clear is a no-op and owner destruction is safe.
    if (current[0] != '\0' || current == buffer.base || current == buffer.overflow) {
      lv_label_set_text_static(label, "");
      this->label_updated_();
    }
  }
  bool prepare_(Prepared &plan, Buffer &buffer, size_t required) {
    plan.owner = this; plan.buffer = &buffer; plan.overflow = required > buffer.maximum;
    const size_t existing = plan.overflow ? buffer.overflow_capacity : buffer.capacity;
    plan.target = plan.overflow ? buffer.overflow : buffer.base;
    plan.capacity = existing;
    if (plan.target != nullptr && existing >= required) return true;
    size_t capacity = existing == 0 ? 1 : existing;
    while (capacity < required) {
      if (capacity > std::numeric_limits<size_t>::max() / 2) { capacity = required; break; }
      capacity *= 2;
    }
    if (!plan.overflow && capacity > buffer.maximum) capacity = buffer.maximum;
    plan.target = static_cast<char *>(this->allocate_(capacity));
    if (plan.target == nullptr) return false;
    plan.capacity = capacity; plan.allocated = true;
    return true;
  }
  void commit_(Prepared &plan) {
    auto &buffer = *plan.buffer;
    if (plan.allocated) {
      if (plan.overflow) {
        this->free_(buffer.overflow); buffer.overflow = plan.target; buffer.overflow_capacity = plan.capacity;
      } else {
        this->free_(buffer.base); buffer.base = plan.target; buffer.capacity = plan.capacity;
      }
    }
    if (!plan.overflow) {
      this->free_(buffer.overflow); buffer.overflow = nullptr; buffer.overflow_capacity = 0;
    }
    buffer.active = plan.target;
    plan.allocated = false;
  }
  bool reserve_initial_(Buffer &buffer, size_t initial, size_t maximum) {
    buffer.maximum = maximum;
    Prepared plan;
    if (!this->prepare_(plan, buffer, initial)) return false;
    if (plan.allocated) plan.target[0] = '\0';
    this->commit_(plan);
    return true;
  }
  void retain_visible_(Prepared &plan, lv_obj_t *label) {
    if (plan.target != plan.buffer->active) {
      const char *previous = lv_label_get_text(label);
      // An unbound empty label and a retained oversized row are both supported.
      const size_t length = std::strlen(previous);
      if (length + 1 <= plan.capacity) this->copy_(plan.target, previous, length + 1);
      else plan.target[0] = '\0';
      lv_label_set_text_static(label, plan.target);
      this->label_updated_();
    }
    this->commit_(plan);
  }
  void release_(Buffer &buffer) { this->free_(buffer.base); this->free_(buffer.overflow); buffer = Buffer{}; }
  void release_all_() {
    this->release_(this->arena_); this->release_(this->indices_); this->release_(this->scratch_);
    for (auto &buffer : this->thinking_) this->release_(buffer);
    for (auto &buffer : this->reply_) this->release_(buffer);
    this->release_(this->counter_);
  }
  void clear_overflow_(Buffer &buffer) {
    this->free_(buffer.overflow); buffer.overflow = nullptr; buffer.overflow_capacity = 0; buffer.active = buffer.base;
  }
  static bool bound_(const std::array<lv_obj_t *, ROWS> &labels) {
    for (auto *label : labels) if (label == nullptr) return false;
    return true;
  }
  static bool separator_(char value) { return value == ' ' || value == '\n' || value == '\r' || value == '\t'; }
  static void count_words_(const char *text, size_t length, size_t &normalized, size_t &words) {
    size_t position = 0;
    while (position < length) {
      while (position < length && separator_(text[position])) ++position;
      const size_t start = position;
      while (position < length && !separator_(text[position])) ++position;
      if (position > start) { normalized += position - start + (words != 0); ++words; }
    }
  }
  void normalize_(const char *text, size_t length, char *destination, size_t normalized) {
    size_t position = 0, output = 0;
    while (position < length && output < normalized) {
      while (position < length && separator_(text[position])) ++position;
      const size_t start = position;
      while (position < length && !separator_(text[position])) ++position;
      if (position > start) {
        if (output != 0) destination[output++] = ' ';
        this->copy_(destination + output, text + start, position - start);
        output += position - start;
      }
    }
    destination[output] = '\0';
  }
  Line next_line_(char *text, size_t length, size_t &cursor, const lv_font_t *font, int chord) {
    const size_t start = cursor;
    size_t end = start;
    const int average = lv_font_get_line_height(font) * 2 / 3;
    while (cursor < length) {
      size_t word_end = cursor;
      while (word_end < length && text[word_end] != ' ') ++word_end;
      if (static_cast<int>((word_end - start) * average) > chord) {
        const char saved = text[word_end]; text[word_end] = '\0';
        lv_point_t size;
        lv_text_get_size(&size, text + start, font, 0, 0, INT32_MAX, LV_TEXT_FLAG_NONE);
        text[word_end] = saved;
#ifdef USE_LVGL_ROUND_TEXT_DEBUG
        ++this->statistics_.measurements;
#endif
        if (size.x > chord) {
          if (cursor == start) { end = word_end; cursor = word_end < length ? word_end + 1 : length; }
          break;
        }
      }
      end = word_end; cursor = word_end < length ? word_end + 1 : length;
    }
    return Line{static_cast<uint32_t>(start), static_cast<uint32_t>(end - start)};
  }
  template<typename Width, typename Sink> size_t wrap_reply_(char *text, size_t length, size_t words,
                    const lv_font_t *font, const Width *chords, size_t chord_count, Sink sink) {
    size_t cursor = 0, safety = 0, count = 0;
    while (cursor < length && safety++ < words * 2) {
      const int chord = chords[count % chord_count];
      Line line{};
      if (chord > 0) line = this->next_line_(text, length, cursor, font, chord);
      sink(count++, line);
    }
    while (count != 0 && count % ROWS != 0) sink(count++, Line{});
    return count;
  }
  bool render_(const std::array<lv_obj_t *, ROWS> &labels, std::array<Buffer, ROWS> &buffers,
               const char *text, const std::array<Line, ROWS> &rows, bool thinking) {
    std::array<Prepared, ROWS> plans;
    std::array<bool, ROWS> changed{};
    for (size_t i = 0; i < ROWS; ++i) {
      const auto line = rows[i];
      const char *previous = lv_label_get_text(labels[i]);
      // Compare before changing a buffer borrowed by LVGL. Looking afterwards
      // would compare the same pointer with its freshly overwritten bytes.
      changed[i] = std::strlen(previous) != line.length || std::memcmp(previous, text + line.offset, line.length) != 0;
      if (changed[i] && !this->prepare_(plans[i], buffers[i], line.length + 1)) return false;
    }
    for (size_t i = 0; i < ROWS; ++i) {
      if (!changed[i]) continue;
      const auto line = rows[i];
      this->copy_(plans[i].target, text + line.offset, line.length);
      plans[i].target[line.length] = '\0';
#if LV_USE_ARABIC_PERSIAN_CHARS
      if (thinking) lv_label_set_text(labels[i], plans[i].target);
      else lv_label_set_text_static(labels[i], plans[i].target);
#else
      (void) thinking;
      lv_label_set_text_static(labels[i], plans[i].target);
#endif
      this->commit_(plans[i]); this->label_updated_();
    }
    return true;
  }

  Allocator allocator_{};
  Buffer arena_{}, indices_{}, scratch_{}, counter_{};
  std::array<Buffer, ROWS> thinking_{}, reply_{};
  std::array<lv_obj_t *, ROWS> thinking_labels_{}, reply_labels_{};
  lv_obj_t *counter_label_{nullptr};
  size_t line_count_{0};
  bool ready_{false};
#ifdef USE_LVGL_ROUND_TEXT_DEBUG
  Statistics statistics_{};
#endif
};

}  // namespace esphome::lvgl_round_text
