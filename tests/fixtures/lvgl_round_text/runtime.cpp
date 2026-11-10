#include "round_text.h"
#include "lvgl_round_text.h"
#include "legacy_wrap.h"
#include <algorithm>
#include <cassert>
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <map>
#include <random>
#include <string>
#include <vector>

using esphome::lvgl_round_text::RoundText;
static_assert(LVGL_VERSION_MAJOR == 9 && LVGL_VERSION_MINOR == 5);

struct AllocationProbe {
  size_t calls{0}, fail_at{SIZE_MAX};
  std::map<void *, size_t> live;
  ~AllocationProbe() { assert(this->live.empty()); }
  static void *allocate(void *context, size_t bytes) {
    auto &self = *static_cast<AllocationProbe *>(context);
    if (self.calls++ == self.fail_at) return nullptr;
    void *result = std::malloc(bytes);
    if (result != nullptr) self.live[result] = bytes;
    return result;
  }
  static void release(void *context, void *pointer) {
    auto &self = *static_cast<AllocationProbe *>(context);
    assert(self.live.erase(pointer) == 1); std::free(pointer);
  }
  RoundText::Allocator allocator() { return {this, allocate, release}; }
  size_t bytes() const { size_t result=0; for (const auto &item : this->live) result += item.second; return result; }
};
struct Scene {
  AllocationProbe allocations;
  lv_display_t *display{};
  std::array<lv_obj_t *, 10> thinking{}, reply{};
  lv_obj_t *counter{};
  std::array<uint16_t, 320> scanline{};
  RoundText text{allocations.allocator()};
  const lv_font_t *font{&lv_font_montserrat_14};
  std::array<int16_t, 10> chords{70, 130, 170, 200, 220, 220, 200, 170, 130, 70};
  Scene() {
    lv_init(); display = lv_display_create(240, 240); lv_display_set_default(display);
    lv_display_set_buffers(display, scanline.data(), nullptr, sizeof(scanline), LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_flush_cb(display, [](lv_display_t *d, const lv_area_t *, uint8_t *) { lv_display_flush_ready(d); });
    auto *screen = lv_display_get_screen_active(display);
    for (size_t i=0; i<10; ++i) {
      thinking[i] = lv_label_create(screen); reply[i] = lv_label_create(screen);
      lv_obj_set_style_text_font(thinking[i], font, LV_PART_MAIN);
      lv_obj_set_style_text_font(reply[i], font, LV_PART_MAIN);
      lv_label_set_long_mode(thinking[i], LV_LABEL_LONG_MODE_CLIP);
      lv_label_set_long_mode(reply[i], LV_LABEL_LONG_MODE_CLIP);
    }
    counter=lv_label_create(screen);
    assert(text.setup()); text.bind_thinking(thinking.data()); text.bind_reply(reply.data(), counter);
  }
  ~Scene() { text.clear(); lv_display_delete(display); lv_deinit(); }
  std::vector<std::string> visible() const {
    std::vector<std::string> result;
    for (auto *row:reply) result.emplace_back(lv_label_get_text(row));
    result.emplace_back(lv_label_get_text(counter)); return result;
  }
  void compare(const std::string &input) { this->compare_widths(input, this->chords); }
  template<typename Widths> void compare_widths(const std::string &input, const Widths &widths) {
    const auto expected = legacy_wrap(input, widths, font, false);
    assert(text.set_reply(input.data(), input.size(), font, widths.data(), widths.size()));
    assert(text.line_count() == expected.rows.size());
    const size_t pages = std::max(size_t{1}, expected.rows.size()/10);
    assert(text.total_pages() == pages);
    for (size_t index=0; index<expected.rows.size(); ++index) {
      const auto line=text.reply_line(index);
      assert(std::string(text.reply_data()+line.offset, line.length) == expected.rows[index]);
    }
    for (size_t page=0; page<pages+1; ++page) {
      assert(text.show_reply(page));
      for (size_t row=0; row<10; ++row) {
        const size_t index=(page%pages)*10+row;
        const std::string expected_line=index<expected.rows.size()?expected.rows[index]:"";
        assert(expected_line == lv_label_get_text(reply[row]));
      }
      const std::string expected_counter=pages>1?std::to_string(page%pages+1)+"/"+std::to_string(pages):"";
      assert(expected_counter == lv_label_get_text(counter));
    }
    const auto thought=legacy_wrap(input, widths, font, true);
    assert(text.set_thinking(input.data(), input.size(), font, widths.data(), widths.size()));
    auto *dynamic=lv_label_create(lv_display_get_screen_active(display));
    for (size_t row=0; row<10; ++row) {
      lv_label_set_text(dynamic, thought.rows[row].c_str());
      assert(std::strcmp(lv_label_get_text(thinking[row]), lv_label_get_text(dynamic)) == 0);
    }
    lv_obj_delete(dynamic);
  }
};

static void differential() {
  Scene scene;
  std::vector<std::string> examples{"", " ", "\n\r\t", "...", " hello\nworld\r\t again ",
      "Caffè già più: perché? Ελληνικά 中文 UTF-8", "مرحبا بالعالم", std::string(700, 'W')};
  std::string oversized;
  for (size_t i=0; i<1200; ++i) oversized += "word"+std::to_string(i)+" ";
  examples.push_back(oversized);
  for (const auto &input:examples) scene.compare(input);
  std::mt19937 random(1979);
  const std::array<std::string,7> words{"I", "ill", "WWWWWWWW", "caffè", "κόσμος", "中文", "longer_word"};
  const std::array<std::string,4> separators{" ", "\n", "\r", "\t"};
  for (unsigned sample=0; sample<150; ++sample) {
    std::string input;
    for (unsigned i=0, length=random()%100; i<length; ++i) input += separators[random()%4]+words[random()%words.size()];
    scene.compare(input);
  }
  scene.chords={0,0,0,0,0,0,0,0,0,0}; scene.compare("all rows unavailable");
  scene.chords={0,0,0,25,25,0,0,25,0,0}; scene.compare(oversized);
  scene.compare_widths(oversized, std::vector<int>{0,50,100,150,150,100,50,0});
  scene.compare_widths(oversized, std::vector<int>{0,50,100,150,180,200,200,180,150,100,50,0});
  scene.text.clear(); scene.compare("same reply after clear"); scene.text.clear(); scene.compare("same reply after clear");
}
static void pointer_and_cache() {
  Scene scene;
  scene.compare("first answer with familiar words");
  assert(scene.text.show_reply(0));
  lv_obj_update_layout(scene.reply[0]);
  const int32_t previous_width=lv_obj_get_self_width(scene.reply[0]);
  auto *before=lv_label_get_text(scene.reply[0]);
  const auto updates=scene.text.statistics().label_updates;
  const std::string changed="other answer with familiar words";
  assert(scene.text.set_reply(changed.data(), changed.size(), scene.font, scene.chords.data()));
  assert(scene.text.show_reply(0));
  assert(lv_label_get_text(scene.reply[0]) == before);
  assert(scene.text.statistics().label_updates > updates);
  lv_obj_update_layout(scene.reply[0]);
  lv_point_t measured;
  lv_text_get_size(&measured, lv_label_get_text(scene.reply[0]), scene.font, 0, 0, INT32_MAX, LV_TEXT_FLAG_NONE);
  assert(lv_obj_get_self_width(scene.reply[0]) == measured.x);
  assert(previous_width != measured.x);  // Independent LVGL cache invalidation.
  const auto stable=scene.text.statistics();
  for (unsigned i=0; i<100; ++i) assert(scene.text.show_reply(0));
  assert(scene.text.statistics().allocations == stable.allocations);
  assert(scene.text.statistics().copied_bytes == stable.copied_bytes);
  assert(scene.text.statistics().measurements == stable.measurements);
  assert(scene.text.statistics().label_updates == stable.label_updates);
  scene.text.clear(); scene.compare(changed); scene.text.clear();
  const auto cleared=scene.text.statistics().label_updates;
  scene.text.clear(); assert(scene.text.statistics().label_updates == cleared);
  const size_t warmed=scene.allocations.calls;
  for (unsigned i=0; i<30; ++i) { scene.compare(changed); scene.text.clear(); }
  assert(scene.allocations.calls == warmed);
  const size_t retained=scene.allocations.bytes();
  scene.compare(std::string(10000, 'W')); assert(scene.allocations.bytes() > retained);
  scene.text.clear(); assert(scene.allocations.bytes() == retained);
  for (auto *label:scene.reply) assert(std::strcmp(lv_label_get_text(label), "") == 0);
  scene.compare(changed);
}
static void failed_allocation() {
  std::string large;
  for (size_t i=0; i<600; ++i) large += std::string(60, char('a'+i%20))+" ";
  for (int chord : {20, 6000}) {
    bool reached_success=false;
    for (size_t failure=0; failure<20; ++failure) {
      Scene scene; scene.compare("preserve the previous valid reply");
      const auto visible=scene.visible();
      const std::string prior=scene.text.reply_data();
      const size_t lines=scene.text.line_count();
      const size_t previous_bytes=scene.allocations.bytes();
      scene.chords.fill(chord);
      scene.allocations.fail_at=scene.allocations.calls+failure;
      const bool result=scene.text.set_reply(large.data(), large.size(), scene.font, scene.chords.data());
      if (result) { reached_success=true; break; }
      assert(scene.visible() == visible);
      assert(prior == scene.text.reply_data()); assert(scene.text.line_count() == lines);
      assert(scene.allocations.bytes() == previous_bytes);
      scene.allocations.fail_at=SIZE_MAX;
      assert(scene.text.set_reply(large.data(), large.size(), scene.font, scene.chords.data()));
      assert(scene.text.show_reply(0));
    }
    assert(reached_success);
  }
  // A later page can need larger visible storage than page zero. Failure must
  // leave all ten rows and the page counter on the previous valid page.
  Scene scene; scene.chords.fill(40);
  std::string paged;
  for (unsigned i=0; i<10; ++i) paged += "rowword ";
  for (unsigned i=0; i<10; ++i) paged += std::string(600, 'W')+" ";
  assert(scene.text.set_reply(paged.data(), paged.size(), scene.font, scene.chords.data()));
  assert(scene.text.show_reply(0));
  const auto previous=scene.visible();
  const size_t page_bytes=scene.allocations.bytes();
  for (unsigned failure=0; failure<10; ++failure) {
    scene.allocations.fail_at=scene.allocations.calls+failure;
    assert(!scene.text.show_reply(1)); assert(scene.visible() == previous);
    assert(scene.allocations.bytes() == page_bytes);
  }
  scene.allocations.fail_at=SIZE_MAX; assert(scene.text.show_reply(1));
  // Thinking uses the same all-row transaction, independently of the reply.
  scene.text.clear(); scene.compare("previous thought");
  std::array<std::string, 10> old_thinking;
  for (size_t i=0; i<10; ++i) old_thinking[i]=lv_label_get_text(scene.thinking[i]);
  std::string thought;
  for (unsigned i=0; i<8; ++i) thought += std::string(600, 'W')+" ";
  const size_t thinking_bytes=scene.allocations.bytes();
  for (unsigned failure=0; failure<9; ++failure) {
    scene.allocations.fail_at=scene.allocations.calls+failure;
    assert(!scene.text.set_thinking(thought.data(), thought.size(), scene.font, scene.chords.data()));
    for (size_t i=0; i<10; ++i) assert(old_thinking[i] == lv_label_get_text(scene.thinking[i]));
    assert(scene.allocations.bytes() == thinking_bytes);
  }
}
static void work() {
  Scene scene; std::string input;
  for (size_t i=0; i<60; ++i) input += "long_word_"+std::to_string(i)+" ";
  const auto baseline=legacy_wrap(input, scene.chords, scene.font, false);
  scene.compare(input); scene.text.clear();
  assert(scene.text.set_reply(input.data(), input.size(), scene.font, scene.chords.data()));
  assert(scene.text.show_reply(0));
  const auto before=scene.text.statistics(); const size_t allocations=scene.allocations.calls;
  constexpr unsigned repeats=100;
  auto start=std::chrono::steady_clock::now();
  for (unsigned i=0; i<repeats; ++i) { assert(scene.text.set_reply(input.data(), input.size(), scene.font, scene.chords.data())); assert(scene.text.show_reply(0)); }
  auto optimized=std::chrono::steady_clock::now()-start;
  start=std::chrono::steady_clock::now();
  size_t old_bytes=0, old_measures=0;
  for (unsigned i=0; i<repeats; ++i) { auto old=legacy_wrap(input, scene.chords, scene.font, false); old_bytes += old.candidate_bytes; old_measures += old.measurements; }
  auto legacy=std::chrono::steady_clock::now()-start;
  const auto after=scene.text.statistics();
  assert(scene.allocations.calls == allocations);
  std::cout << "METRICS allocations=" << scene.allocations.calls-allocations
            << " copied_bytes=" << after.copied_bytes-before.copied_bytes
            << " candidate_bytes_legacy=" << old_bytes
            << " measurements=" << after.measurements-before.measurements
            << " measurements_legacy=" << old_measures
            << " elapsed_ns=" << std::chrono::duration_cast<std::chrono::nanoseconds>(optimized).count()
            << " legacy_elapsed_ns=" << std::chrono::duration_cast<std::chrono::nanoseconds>(legacy).count() << '\n';
  assert(baseline.rows.size() == scene.text.line_count());
}
static void wrapper() {
  Scene scene;
  esphome::lvgl_round_text::LvglRoundText owner;
  owner.set_buffers_in_psram(true); owner.setup();
  assert(!owner.loop_enabled && !owner.failed);
  owner.bind_thinking(scene.thinking.data()); owner.bind_reply(scene.reply.data(), scene.counter);
  owner.set_thinking_font(scene.font); owner.set_reply_font(scene.font);
  assert(owner.set_thinking("a request", scene.chords.data()));
  const std::array<int, 10> legacy_widths{70,130,170,200,220,220,200,170,130,70};
  assert(owner.set_reply("legacy and current share the same owner", legacy_widths.data(), legacy_widths.size()));
  assert(owner.total_pages() == 1 && owner.show_reply(0));
  owner.dump_config(); owner.clear();
}
static void setup_failure() {
  for (size_t failure=0; failure<24; ++failure) {
    AllocationProbe allocations;
    RoundText text(allocations.allocator());
    allocations.fail_at=failure;
    assert(!text.setup());
    assert(allocations.live.empty());
    allocations.fail_at=SIZE_MAX;
    assert(text.setup());
  }
}
static void oversized_empty_clear() {
  Scene scene; scene.chords.fill(40);
  std::string input="tiny "+std::string(10000, 'W')+" ";
  for (unsigned i=0; i<9; ++i) input += "tiny ";
  assert(scene.text.set_reply(input.data(), input.size(), scene.font, scene.chords.data()));
  assert(scene.text.total_pages() == 2 && scene.text.show_reply(0));
  assert(std::strlen(lv_label_get_text(scene.reply[1])) == 10000);
  assert(scene.text.show_reply(1));
  assert(std::strcmp(lv_label_get_text(scene.reply[1]), "") == 0);
  const char *empty_borrowed=lv_label_get_text(scene.reply[1]);
  scene.text.clear();
  assert(lv_label_get_text(scene.reply[1]) != empty_borrowed);
  lv_obj_update_layout(scene.reply[1]);
  assert(lv_obj_get_self_width(scene.reply[1]) == 0);
  const std::string shorter="tiny";
  assert(scene.text.set_reply(shorter.data(), shorter.size(), scene.font, scene.chords.data()));
  assert(scene.text.show_reply(0)); scene.text.clear();
  lv_obj_update_layout(scene.reply[1]); assert(lv_obj_get_self_width(scene.reply[1]) == 0);
  const auto updates=scene.text.statistics().label_updates;
  scene.text.clear(); assert(scene.text.statistics().label_updates == updates);
}
int main(int argc, char **argv) {
  assert(argc == 2); const std::string scenario=argv[1];
  if (scenario == "differential") differential();
  else if (scenario == "pointer_cache") pointer_and_cache();
  else if (scenario == "allocation_failure") failed_allocation();
  else if (scenario == "work") work();
  else if (scenario == "wrapper") wrapper();
  else if (scenario == "setup_failure") setup_failure();
  else if (scenario == "oversized_empty_clear") oversized_empty_clear();
  else assert(false);
  std::cout << "PASS real_lvgl_9_5 " << scenario << '\n';
  return 0;
}
