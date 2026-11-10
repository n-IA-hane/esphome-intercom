#include "tap_input.h"
#include <array>
#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

using esphome::lvgl_touch_diagnostic::TapInput;
static bool count_allocations;
static size_t allocations, allocated_bytes;
extern "C" void *__real_malloc(size_t);
extern "C" void *__real_calloc(size_t, size_t);
extern "C" void *__real_realloc(void *, size_t);
extern "C" void *__wrap_malloc(size_t n) {
  if (count_allocations) { ++allocations; allocated_bytes += n; }
  return __real_malloc(n);
}
extern "C" void *__wrap_calloc(size_t n, size_t size) {
  if (count_allocations) { ++allocations; allocated_bytes += n * size; }
  return __real_calloc(n, size);
}
extern "C" void *__wrap_realloc(void *p, size_t n) {
  if (count_allocations) { ++allocations; allocated_bytes += n; }
  return __real_realloc(p, n);
}

struct Scene {
  lv_display_t *display;
  lv_obj_t *button;
  TapInput input;
  std::vector<uint8_t> pixels;
  std::array<lv_event_code_t, 64> events{};
  unsigned event_count{}, flushes{};
  lv_point_t observed{};
  lv_indev_t *event_input{};
  bool physical_pressed{};
  lv_indev_t *physical{};
  Scene(int width=1280, int height=800) : pixels(width * 4 * 2) {
    lv_init();
    display = lv_display_create(width, height);
    lv_display_set_user_data(display, this);
    lv_display_set_buffers(display, pixels.data(), nullptr, pixels.size(), LV_DISPLAY_RENDER_MODE_PARTIAL);
    lv_display_set_flush_cb(display, [](lv_display_t *d, const lv_area_t *, uint8_t *) {
      ++static_cast<Scene *>(lv_display_get_user_data(d))->flushes;
      lv_display_flush_ready(d);
    });
    lv_obj_remove_flag(lv_display_get_screen_active(display), LV_OBJ_FLAG_SCROLLABLE);
    button = lv_button_create(lv_display_get_screen_active(display));
    lv_obj_remove_style_all(button);
    lv_obj_set_pos(button, 40, 60);
    lv_obj_set_size(button, 100, 50);
    lv_obj_set_style_bg_opa(button, LV_OPA_COVER, 0);
    lv_obj_set_style_bg_color(button, lv_color_hex(0xAA2222), 0);
    lv_obj_set_style_bg_color(button, lv_color_hex(0x2222AA), LV_STATE_PRESSED);
    lv_obj_add_event_cb(button, [](lv_event_t *e) {
      auto &s = *static_cast<Scene *>(lv_event_get_user_data(e));
      auto code = lv_event_get_code(e);
      if (code != LV_EVENT_PRESSED && code != LV_EVENT_RELEASED && code != LV_EVENT_SHORT_CLICKED &&
          code != LV_EVENT_CLICKED && code != LV_EVENT_PRESS_LOST) return;
      assert(s.event_count < s.events.size());
      s.events[s.event_count++] = code;
      s.event_input = lv_indev_active();
      assert(s.event_input != nullptr);
      lv_indev_get_point(s.event_input, &s.observed);
    }, LV_EVENT_ALL, this);
    pump(64);
    allocations = allocated_bytes = 0;
    count_allocations = true;
    assert(input.attach(display));
    count_allocations = false;
  }
  ~Scene() {
    input.detach();
    if (physical) lv_indev_delete(physical);
    lv_display_delete(display);
    lv_deinit();
  }
  void pump(unsigned milliseconds) {
    for (unsigned i=0; i<milliseconds; i+=4) { lv_tick_inc(4); lv_timer_handler(); }
  }
  unsigned count(lv_event_code_t code) const {
    unsigned result=0; for (unsigned i=0;i<event_count;++i) if (events[i]==code) ++result; return result;
  }
  void physical_read(bool pressed) {
    if (!physical) {
      physical = lv_indev_create();
      lv_indev_set_type(physical, LV_INDEV_TYPE_POINTER);
      lv_indev_set_display(physical, display);
      lv_indev_set_user_data(physical, this);
      lv_indev_set_mode(physical, LV_INDEV_MODE_EVENT);
      lv_indev_set_read_cb(physical, [](lv_indev_t *i, lv_indev_data_t *data) {
        auto *s=static_cast<Scene *>(lv_indev_get_user_data(i));
        data->point={300,300};
        data->state=s->physical_pressed ? LV_INDEV_STATE_PRESSED : LV_INDEV_STATE_RELEASED;
      });
    }
    physical_pressed=pressed;
    lv_indev_read(physical);
  }
  void tap_button() {
    assert(input.tap(80,80)==TapInput::Result::ACCEPTED);
    assert(count(LV_EVENT_PRESSED)==1);
    assert(lv_obj_has_state(button, LV_STATE_PRESSED));
    assert(count(LV_EVENT_CLICKED)==0);
    unsigned before=flushes;
    pump(40);
    assert(flushes>before && input.busy());
    pump(80);
    assert(!input.busy() && !lv_obj_has_state(button, LV_STATE_PRESSED));
    assert(event_count==4);
    assert((events[0]==LV_EVENT_PRESSED && events[1]==LV_EVENT_RELEASED &&
            events[2]==LV_EVENT_SHORT_CLICKED && events[3]==LV_EVENT_CLICKED));
    assert(observed.x==80 && observed.y==80);
    assert(lv_timer_get_paused(lv_indev_get_read_timer(event_input)));
  }
};

int main(int argc, char **argv) {
  assert(argc==2); const std::string mode=argv[1];
  if (mode=="hit") { Scene s; s.tap_button(); }
  else if (mode=="s3") { Scene s(360,360); s.tap_button(); }
  else if (mode=="bounds_busy") {
    Scene s;
    for (auto point:std::array<lv_point_t,4>{{{-1,10},{10,-1},{1280,10},{10,800}}})
      assert(s.input.tap(point.x,point.y)==TapInput::Result::OUT_OF_BOUNDS);
    assert(s.event_count==0 && !s.input.busy());
    s.physical_read(true);
    assert(s.input.tap(80,80)==TapInput::Result::BUSY);
    s.physical_read(false);
    assert(s.input.tap(80,80)==TapInput::Result::ACCEPTED);
    assert(s.input.tap(90,90)==TapInput::Result::BUSY);
    s.pump(120); assert(s.count(LV_EVENT_CLICKED)==1);
    assert(s.input.tap(1000,700)==TapInput::Result::ACCEPTED);
    s.pump(120); assert(!s.input.busy() && s.count(LV_EVENT_CLICKED)==1);
    auto *next=lv_obj_create(nullptr);
    lv_screen_load_anim(next,LV_SCR_LOAD_ANIM_MOVE_LEFT,120,0,false);
    s.pump(4);
    assert(lv_display_get_screen_prev(s.display)!=nullptr);
    assert(s.input.tap(80,80)==TapInput::Result::BUSY);
    s.pump(160);
  } else if (mode=="cancel") {
    Scene s; assert(s.input.tap(80,80)==TapInput::Result::ACCEPTED);
    s.physical_read(true); s.pump(40);
    assert(!s.input.busy() && s.count(LV_EVENT_CLICKED)==0 && s.count(LV_EVENT_PRESS_LOST)==1);
    s.physical_read(false);
    assert(s.input.tap(80,80)==TapInput::Result::ACCEPTED);
    s.input.detach(); assert(!lv_obj_has_state(s.button, LV_STATE_PRESSED));
    assert(s.count(LV_EVENT_CLICKED)==0);
    assert(s.input.tap(80,80)==TapInput::Result::UNAVAILABLE);
  } else if (mode=="rotation") {
    for (auto rotation:{LV_DISPLAY_ROTATION_0,LV_DISPLAY_ROTATION_90,LV_DISPLAY_ROTATION_180,LV_DISPLAY_ROTATION_270}) {
      Scene s(360,240); lv_display_set_rotation(s.display,rotation); s.pump(40); s.tap_button();
    }
  } else if (mode=="swipe") {
    Scene s(360,360);
    struct Gestures { unsigned count{}; lv_dir_t direction{}; lv_indev_t *input{}; } gestures;
    lv_obj_add_event_cb(lv_display_get_screen_active(s.display), [](lv_event_t *e) {
      auto *g=static_cast<Gestures *>(lv_event_get_user_data(e));
      ++g->count; g->input=lv_indev_active(); g->direction=lv_indev_get_gesture_dir(g->input);
      lv_indev_wait_release(g->input); // Same ownership rule as round_navigate.
    }, LV_EVENT_GESTURE, &gestures);
    assert(s.input.swipe(300,300,300,60,320)==TapInput::Result::ACCEPTED);
    assert(s.input.tap(80,80)==TapInput::Result::BUSY);
    s.pump(380);
    assert(gestures.count==1 && gestures.direction==LV_DIR_TOP && !s.input.busy());
    assert(lv_timer_get_paused(lv_indev_get_read_timer(gestures.input)));
    // The same pointer must still hit and release the next hangup-like button.
    s.tap_button();
    assert(s.input.swipe(300,60,300,300,320)==TapInput::Result::ACCEPTED);
    s.pump(380);
    assert(gestures.count==2 && gestures.direction==LV_DIR_BOTTOM && !s.input.busy());
  } else if (mode=="swipe_scroll") {
    Scene s(360,360);
    auto *list=lv_obj_create(lv_display_get_screen_active(s.display));
    lv_obj_set_pos(list,150,20); lv_obj_set_size(list,180,280);
    lv_obj_set_scroll_dir(list,LV_DIR_VER); lv_obj_remove_flag(list,LV_OBJ_FLAG_SCROLL_ELASTIC);
    auto *content=lv_obj_create(list); lv_obj_set_size(content,120,800);
    lv_obj_set_pos(content,0,0); lv_obj_update_layout(list); s.pump(40);
    assert(lv_obj_get_scroll_y(list)==0);
    assert(s.input.swipe(230,250,230,70,320)==TapInput::Result::ACCEPTED);
    s.pump(1000);
    const auto scroll=lv_obj_get_scroll_y(list);
    assert(scroll>0 && !s.input.busy());
    s.pump(1000); assert(lv_obj_get_scroll_y(list)==scroll);
    // A real short click after a scroll remains usable.
    s.tap_button();
  } else if (mode=="swipe_cancel") {
    Scene s(360,360);
    for (int duration:{-1,0,79,2001})
      assert(s.input.swipe(300,300,300,60,duration)==TapInput::Result::INVALID_DURATION);
    assert(s.input.swipe(300,300,360,60,320)==TapInput::Result::OUT_OF_BOUNDS);
    assert(s.input.swipe(-1,300,300,60,320)==TapInput::Result::OUT_OF_BOUNDS);
    assert(s.input.swipe(300,300,300,60,320)==TapInput::Result::ACCEPTED);
    s.physical_read(true); s.pump(40); assert(!s.input.busy());
    s.physical_read(false);
    assert(s.input.swipe(300,300,300,60,320)==TapInput::Result::ACCEPTED);
    auto *next=lv_obj_create(nullptr); lv_screen_load(next); s.pump(40);
    assert(!s.input.busy());
    s.input.detach();
    assert(s.input.swipe(300,300,300,60,320)==TapInput::Result::UNAVAILABLE);
  } else if (mode=="allocation") {
    size_t virtual_count, virtual_bytes, reference_count, reference_bytes;
    {
      Scene s;
      std::printf("setup allocations=%zu bytes=%zu sizeof_state=%zu\n",allocations,allocated_bytes,sizeof(TapInput));
      s.tap_button(); s.event_count=0;
      allocations=allocated_bytes=0; count_allocations=true;
      s.tap_button(); count_allocations=false;
      virtual_count=allocations; virtual_bytes=allocated_bytes;
    }
    {
      Scene s;
      // Independent normal pointer driver, with no TapInput calls. LVGL owns
      // the same event hit test,80ms release and two ordinary redraws.
      struct Pointer { uint32_t started; } pointer{};
      auto *input=lv_indev_create();
      lv_indev_set_type(input,LV_INDEV_TYPE_POINTER);
      lv_indev_set_display(input,s.display);
      lv_indev_set_user_data(input,&pointer);
      lv_indev_set_mode(input,LV_INDEV_MODE_EVENT);
      lv_indev_set_read_cb(input,[](lv_indev_t *i,lv_indev_data_t *data) {
        auto *p=static_cast<Pointer *>(lv_indev_get_user_data(i));
        data->point={80,80};
        data->state=lv_tick_elaps(p->started)<80 ? LV_INDEV_STATE_PRESSED : LV_INDEV_STATE_RELEASED;
      });
      auto tap=[&]() {
        pointer.started=lv_tick_get(); lv_indev_read(input);
        lv_timer_resume(lv_indev_get_read_timer(input)); s.pump(40); s.pump(80);
        assert(s.event_input==input && s.count(LV_EVENT_CLICKED)==1 && s.event_count==4);
      };
      tap(); s.event_count=0;
      allocations=allocated_bytes=0; count_allocations=true;
      tap(); count_allocations=false;
      reference_count=allocations; reference_bytes=allocated_bytes;
      lv_indev_delete(input);
    }
    std::printf("virtual allocations=%zu bytes=%zu normal_pointer allocations=%zu bytes=%zu\n",
                virtual_count,virtual_bytes,reference_count,reference_bytes);
    assert(virtual_count==reference_count && virtual_bytes==reference_bytes);
  } else { assert(false); }
  std::printf("PASS real_lvgl_9_5 %s\n",mode.c_str());
}
