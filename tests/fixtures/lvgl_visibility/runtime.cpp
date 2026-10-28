#include "lvgl.h"
#include "visibility_policy.h"
#include <array>
#include <cstdlib>
#include <iostream>
#include <string>

using esphome::lvgl_visibility::VisibilityPolicy;
static_assert(LVGL_VERSION_MAJOR == 9 && LVGL_VERSION_MINOR == 5,
              "This integration oracle is for real LVGL 9.5");

struct Scene {
  lv_display_t *display{};
  std::array<lv_obj_t *, 5> pages{};
  lv_obj_t *panel{}, *avatar{}, *marquee{}, *top_label{}, *generic{};
  VisibilityPolicy policy;
  std::array<uint8_t, 320> scanline{};
  std::array<uint16_t, 4> pixels_a{0xf800, 0xf800, 0xf800, 0xf800};
  std::array<uint16_t, 4> pixels_b{0x001f, 0x001f, 0x001f, 0x001f};
  lv_image_dsc_t frame_a{}, frame_b{};
  std::array<const void *, 2> frames{};
  lv_timer_t *countdown{};
  unsigned starts{}, stops{}, notifications{}, countdown_ticks{};
  unsigned avatar_executions{}, generic_executions{};
  lv_anim_exec_xcb_t avatar_original{}, generic_original{};
  bool script_running{false}, idle_phase{true};
  inline static Scene *current{};

  explicit Scene(bool native_widgets = true, bool initial_layout = true,
                 bool special_layer = false, bool generic_animation = false) {
    current = this;
    lv_init();
    display = lv_display_create(160, 96);
    lv_display_set_default(display);
    lv_display_set_color_format(display, LV_COLOR_FORMAT_RGB565);
    lv_display_set_buffers(display, scanline.data(), nullptr, scanline.size(),
                           LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_flush_cb(display, [](lv_display_t *d, const lv_area_t *, uint8_t *) {
      lv_display_flush_ready(d);
    });
    pages[0] = lv_display_get_screen_active(display);
    for (size_t i = 1; i < pages.size(); ++i) pages[i] = lv_obj_create(nullptr);
    panel = lv_obj_create(pages[0]);
    lv_obj_remove_style_all(panel);
    lv_obj_set_size(panel, 150, 90);
    if (native_widgets || generic_animation) {
      frame_a.header.magic = frame_b.header.magic = LV_IMAGE_HEADER_MAGIC;
      frame_a.header.cf = frame_b.header.cf = LV_COLOR_FORMAT_RGB565;
      frame_a.header.w = frame_b.header.w = 2;
      frame_a.header.h = frame_b.header.h = 2;
      frame_a.header.stride = frame_b.header.stride = 4;
      frame_a.data_size = frame_b.data_size = 8;
      frame_a.data = reinterpret_cast<const uint8_t *>(pixels_a.data());
      frame_b.data = reinterpret_cast<const uint8_t *>(pixels_b.data());
      frames = {&frame_a, &frame_b};
    }
    if (native_widgets) {
      avatar = lv_animimg_create(panel);
      lv_obj_set_size(avatar, 2, 2);
      lv_image_set_src(avatar, &frame_a);
      lv_animimg_set_src(avatar, frames.data(), frames.size());
      lv_animimg_set_duration(avatar, 120);
      lv_animimg_set_repeat_count(avatar, LV_ANIM_REPEAT_INFINITE);
      avatar_original = lv_animimg_get_anim(avatar)->exec_cb;
      lv_anim_set_exec_cb(lv_animimg_get_anim(avatar), &Scene::frame);
      marquee = lv_label_create(panel);
      lv_obj_set_size(marquee, 60, 20);
      lv_obj_set_pos(marquee, 0, 30);
      lv_label_set_text(marquee, "A long caller name whose scrolling is real LVGL work");
      lv_label_set_long_mode(marquee, LV_LABEL_LONG_MODE_SCROLL_CIRCULAR);
    }
    if (special_layer) {
      top_label = lv_label_create(lv_display_get_layer_top(display));
      lv_obj_set_size(top_label, 60, 20);
      lv_label_set_text(top_label, "Top layer text that must continue while another page is active");
      lv_label_set_long_mode(top_label, LV_LABEL_LONG_MODE_SCROLL_CIRCULAR);
    }
    if (generic_animation) {
      generic = lv_animimg_create(panel);
      lv_obj_set_pos(generic, 10, 0);
      lv_obj_set_size(generic, 2, 2);
      lv_image_set_src(generic, &frame_a);
      lv_animimg_set_src(generic, frames.data(), frames.size());
      lv_animimg_set_duration(generic, 120);
      lv_animimg_set_repeat_count(generic, LV_ANIM_REPEAT_INFINITE);
      generic_original = lv_animimg_get_anim(generic)->exec_cb;
      lv_anim_set_exec_cb(lv_animimg_get_anim(generic), &Scene::frame);
    }
    countdown = lv_timer_create([](lv_timer_t *t) {
      ++static_cast<Scene *>(lv_timer_get_user_data(t))->countdown_ticks;
    }, 100, this);
    if (initial_layout) lv_obj_update_layout(pages[0]);
    if (generic) lv_animimg_start(generic);
    for (auto *page : pages) policy.add_root(page);
    policy.set_on_change(&Scene::changed, this);
    policy.attach(display);
    pump(64);
  }
  ~Scene() {
    policy.detach();
    lv_timer_delete(countdown);
    lv_display_delete(display);
    lv_deinit();
    current = nullptr;
  }
  static void frame(void *object, int32_t value) {
    auto &s = *current;
    if (object == s.avatar) {
      ++s.avatar_executions;
      s.avatar_original(object, value);
    } else if (object == s.generic) {
      ++s.generic_executions;
      s.generic_original(object, value);
    } else {
      std::abort();
    }
  }
  static bool inactive(lv_obj_t *object) {
    auto *animation = lv_anim_get(object, nullptr);
    return animation == nullptr || lv_anim_is_paused(animation);
  }
  static void changed(void *data) {
    auto &s = *static_cast<Scene *>(data);
    ++s.notifications;
    // Representative script-owner callback: visibility is supplied by the
    // production policy. The oracle never implements its own visibility rule.
    const bool run = s.idle_phase && s.policy.is_visible(s.panel);
    if (run && !s.script_running) {
      ++s.starts;
      s.script_running = true;
      if (s.avatar) lv_animimg_start(s.avatar);
    } else if (!run && s.script_running) {
      ++s.stops;
      s.script_running = false;
    }
    // Idle-script ownership includes its own native avatar. Other generic
    // animations below have no script owner and must be paused/resumed by
    // the production policy without restarting them.
    if (!run && s.avatar) lv_animimg_delete(s.avatar);
  }
  void pump(unsigned ms) {
    for (unsigned elapsed = 0; elapsed < ms; elapsed += 4) {
      lv_tick_inc(4);
      lv_timer_handler();
    }
  }
  void show(size_t index) {
    lv_screen_load(pages[index]);
    pump(64);
  }
  void require(bool condition, const char *message) const {
    if (condition) return;
    std::cerr << "FAIL " << message << " starts=" << starts << " stops=" << stops
              << " script_running=" << script_running << " callbacks=" << notifications
              << " native_animations=" << lv_anim_count_running()
              << " countdown_ticks=" << countdown_ticks
              << " real_visible=" << lv_obj_is_visible(panel)
              << " policy_visible=" << policy.is_visible(panel) << '\n';
    std::exit(2);
  }
};

static void screen_matrix() {
  Scene s;
  s.pump(180);
  s.require(s.script_running && s.starts == 1, "visible idle script did not start exactly once");
  s.require(lv_anim_get(s.avatar, nullptr) != nullptr, "visible native avatar is not advancing");
  s.require(lv_anim_get(s.marquee, nullptr) != nullptr, "visible marquee is not animating");
  for (size_t index = 1; index < s.pages.size(); ++index) {
    auto ticks = s.countdown_ticks;
    auto starts = s.starts;
    s.show(index);
    const auto executions = s.avatar_executions;
    s.pump(240);
    s.require(!s.script_running, "offscreen idle script kept running");
    s.require(Scene::inactive(s.avatar) && s.avatar_executions == executions,
              "offscreen native avatar executed frames");
    s.require(lv_label_get_long_mode(s.marquee) == LV_LABEL_LONG_MODE_CLIP,
              "offscreen marquee can restart when its text changes");
    for (unsigned n = 0; n < 5; ++n) {
      lv_label_set_text_fmt(s.marquee, "Changed while hidden: %u, still a very long caller", n);
      s.pump(32);
    }
    s.require(lv_anim_get(s.marquee, nullptr) == nullptr, "hidden set_text recreated marquee work");
    s.require(s.countdown_ticks > ticks, "functional countdown was stopped with cosmetic work");
    s.show(0);
    s.pump(100);
    s.require(s.script_running && s.starts == starts + 1,
              "return to idle did not restart only the desired script");
    s.require(lv_anim_get(s.avatar, nullptr) != nullptr, "return did not start native avatar");
    s.require(lv_label_get_long_mode(s.marquee) == LV_LABEL_LONG_MODE_SCROLL_CIRCULAR,
              "return changed the declared marquee mode");
    auto same_page_starts = s.starts;
    s.show(0);
    s.require(s.starts == same_page_starts, "same-page request restarted an active script");
  }
}

static void ancestor_hidden() {
  Scene s;
  lv_obj_add_flag(s.panel, LV_OBJ_FLAG_HIDDEN);
  s.pump(64);
  s.require(!lv_obj_is_visible(s.panel), "LVGL did not hide the ancestor");
  s.require(!s.script_running, "ancestor HIDDEN did not stop its script owner");
  s.require(Scene::inactive(s.avatar), "hidden ancestor avatar kept running");
  s.require(lv_anim_get(s.marquee, nullptr) == nullptr, "hidden ancestor marquee kept running");
  lv_obj_remove_flag(s.panel, LV_OBJ_FLAG_HIDDEN);
  s.pump(64);
  s.require(s.script_running && s.starts == 2, "visible ancestor did not restore its script");
}

static void script_only_hidden() {
  Scene s(false);
  s.require(s.script_running, "script-only scene did not start");
  lv_obj_add_flag(s.panel, LV_OBJ_FLAG_HIDDEN);
  s.pump(64);
  s.require(!lv_obj_is_visible(s.panel), "script-only ancestor did not become hidden");
  s.require(!s.script_running && s.stops == 1,
            "visibility notification depends on having a marquee or native animimg");
}

static void bootstrap_layout() {
  Scene s(false, false);
  s.require(lv_obj_is_visible(s.panel), "bootstrap layout did not make the owner visible");
  s.require(s.script_running && s.starts == 1,
            "first completed layout did not notify a script-only owner");
}

static void transition_and_phase() {
  Scene s;
  auto count = s.starts;
  lv_screen_load_anim(s.pages[4], LV_SCR_LOAD_ANIM_MOVE_LEFT, 120, 0, false);
  s.pump(8);
  s.require(!s.script_running, "unloading animation still runs the idle script");
  s.require(Scene::inactive(s.avatar), "unloading native avatar remains active");
  s.pump(160);
  s.idle_phase = false;
  s.show(0);
  s.require(!s.script_running && s.starts == count,
            "return while another phase is selected restarted idle animation");
  s.require(Scene::inactive(s.avatar),
            "policy autostarted an avatar without its script owner");
  s.idle_phase = true;
  s.show(2);
  s.show(0);
  s.require(s.script_running && s.starts == count + 1,
            "return to the idle phase lost the script restart");
}

static void top_layer() {
  Scene s(false, true, true);
  s.require(lv_obj_is_visible(s.top_label), "top-layer label is not geometrically visible");
  s.require(s.policy.is_visible(s.top_label), "policy treats the top layer as an inactive page");
  s.require(lv_anim_get(s.top_label, nullptr) != nullptr, "visible top-layer marquee was disabled");
  s.show(4);
  s.require(s.policy.is_visible(s.top_label), "top-layer visibility changed with the active page");
  s.require(lv_anim_get(s.top_label, nullptr) != nullptr, "top-layer marquee stopped on another page");
  lv_obj_add_flag(s.top_label, LV_OBJ_FLAG_HIDDEN);
  s.pump(64);
  s.require(lv_anim_get(s.top_label, nullptr) == nullptr, "hidden top-layer marquee kept running");
  lv_obj_remove_flag(s.top_label, LV_OBJ_FLAG_HIDDEN);
  s.pump(64);
  s.require(lv_label_get_long_mode(s.top_label) == LV_LABEL_LONG_MODE_SCROLL_CIRCULAR,
            "top-layer marquee mode was not restored");
}

static void generic_pause(bool owner_paused) {
  Scene s(false, true, false, true);
  s.pump(40);
  auto *original = lv_anim_get(s.generic, nullptr);
  s.require(original != nullptr && s.generic_executions > 1,
            "generic native animation has no actual frame executions");
  if (owner_paused) lv_anim_pause(original);
  auto ticks = s.countdown_ticks;
  s.show(4);
  auto frames = s.generic_executions;
  auto *image = lv_image_get_src(s.generic);
  s.pump(260);
  s.require(s.generic_executions == frames && lv_image_get_src(s.generic) == image,
            "offscreen generic animation advanced its frame");
  s.require(s.countdown_ticks > ticks, "functional timer was suspended by cosmetic policy");
  s.show(0);
  s.require(lv_anim_get(s.generic, nullptr) == original,
            "return allocated/restarted a different generic animation");
  s.pump(40);
  if (owner_paused)
    s.require(s.generic_executions == frames && lv_anim_is_paused(original),
              "policy overrode an animation paused by its own owner");
  else
    s.require(s.generic_executions > frames && !lv_anim_is_paused(original),
              "generic non-idle animation did not resume on return");
}

int main(int argc, char **argv) {
  const std::string name = argc > 1 ? argv[1] : "matrix";
  if (name == "matrix") screen_matrix();
  else if (name == "hidden") ancestor_hidden();
  else if (name == "script_only") script_only_hidden();
  else if (name == "bootstrap") bootstrap_layout();
  else if (name == "transition") transition_and_phase();
  else if (name == "top") top_layer();
  else if (name == "generic") generic_pause(false);
  else if (name == "owner_paused") generic_pause(true);
  else return 3;
  std::cout << "PASS real_lvgl_9_5 " << name << '\n';
}
