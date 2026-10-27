#pragma once

#include <algorithm>
#include <cassert>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <functional>
#include <map>
#include <string>
#include <utility>
#include <vector>

namespace esphome {
inline uint32_t test_now = 100;
inline bool test_output_callback = false;
inline unsigned test_mutations = 0;
inline unsigned test_app_description_reads = 0;
inline unsigned test_identity_reports = 0;
inline std::vector<std::string> test_logs;
inline uint32_t millis() { return test_now; }
namespace setup_priority { constexpr float AFTER_CONNECTION = 100.0f; constexpr float DATA = 600.0f; constexpr float LATE = -100.0f; }
inline void test_log(const char *tag, const char *format, ...) {
  assert(!test_output_callback && "logging from realtime output callback");
  if (std::strstr(format, "identity:") != nullptr) ++test_identity_reports;
  char line[4096];
  va_list args;
  va_start(args, format);
  vsnprintf(line, sizeof(line), format, args);
  va_end(args);
  test_logs.emplace_back(std::string(tag) + ": " + line);
}
class Component {
 public:
  virtual ~Component() = default;
  virtual void setup() {}
  virtual void loop() {}
  virtual void dump_config() {}
  virtual float get_setup_priority() const { return 0; }
  void disable_loop() { loop_disabled = true; }
  void enable_loop() { loop_disabled = false; }
  void mark_failed() { failed = true; }
  bool is_failed() const { return failed; }
  bool is_ready() const { return !failed; }
  bool status_has_error() const { return false; }
  void defer(const std::string &name, std::function<void()> fn) {
    assert(!test_output_callback && "scheduling from realtime output callback");
    deferred[name] = std::move(fn);
  }
  void defer(std::function<void()> fn) { defer(std::to_string(++anonymous), std::move(fn)); }
  void set_timeout(const std::string &name, uint32_t delay, std::function<void()> fn) {
    assert(!test_output_callback && "timer from realtime output callback");
    timers[name] = Timer{test_now, delay, std::move(fn)};
  }
  bool cancel_timeout(const std::string &name) { return timers.erase(name) != 0; }
  void flush_deferred() {
    unsigned turns = 0;
    while (!deferred.empty()) {
      assert(++turns < 100 && "unbounded diagnostic scheduling");
      auto pending = std::move(deferred);
      deferred.clear();
      for (auto &item : pending) item.second();
    }
  }
  void advance(uint32_t ms) {
    test_now += ms;
    std::vector<std::function<void()>> ready;
    for (auto it = timers.begin(); it != timers.end();) {
      if (static_cast<uint32_t>(test_now - it->second.at) >= it->second.delay) {
        ready.push_back(std::move(it->second.fn));
        it = timers.erase(it);
      } else { ++it; }
    }
    for (auto &fn : ready) fn();
    flush_deferred();
  }
  size_t timer_count() const { return timers.size(); }
  bool loop_disabled{false};
 protected:
  struct Timer { uint32_t at, delay; std::function<void()> fn; };
  std::map<std::string, Timer> timers;
  std::map<std::string, std::function<void()>> deferred;
  unsigned anonymous{0};
  bool failed{false};
};
namespace audio {
class AudioStreamInfo {
 public:
  AudioStreamInfo(uint8_t bits = 16, uint8_t channels = 1, uint32_t rate = 48000)
      : bits_(bits), channels_(channels), rate_(rate) {}
  uint32_t get_sample_rate() const { return rate_; }
  uint8_t get_bits_per_sample() const { return bits_; }
  uint8_t get_channels() const { return channels_; }
 private:
  uint8_t bits_, channels_;
  uint32_t rate_;
};
}
namespace speaker {
class Speaker {
 public:
  bool is_stopped() const { return stopped; }
  bool is_running() const { return running; }
  bool has_buffered_data() const { return buffered; }
  bool get_pause_state() const { return paused; }
  bool get_mute_state() const { return muted; }
  float get_volume() const { return volume; }
  audio::AudioStreamInfo &get_audio_stream_info() { return info; }
  template<typename F> void add_audio_output_callback(F &&fn) { callbacks.emplace_back(std::forward<F>(fn)); }
  void emit(uint32_t frames, int64_t timestamp) {
    test_output_callback = true;
    for (auto &fn : callbacks) fn(frames, timestamp);
    test_output_callback = false;
  }
  void start() { ++test_mutations; }
  void stop() { ++test_mutations; }
  void finish() { ++test_mutations; }
  void set_volume(float) { ++test_mutations; }
  void set_mute_state(bool) { ++test_mutations; }
  void set_pause_state(bool) { ++test_mutations; }
  bool stopped{true}, running{false}, buffered{false}, paused{false}, muted{false};
  float volume{0.01f};
  audio::AudioStreamInfo info;
  std::vector<std::function<void(uint32_t, int64_t)>> callbacks;
};
}
namespace media_source {
enum class MediaSourceState : uint8_t { IDLE, PLAYING, PAUSED, ERROR };
class MediaSource {
 public:
  MediaSourceState get_state() const { return state; }
  bool has_listener() const { return true; }
  MediaSourceState state{MediaSourceState::IDLE};
};
}
namespace media_player {
enum MediaPlayerState : uint8_t {
  MEDIA_PLAYER_STATE_NONE, MEDIA_PLAYER_STATE_IDLE, MEDIA_PLAYER_STATE_PLAYING,
  MEDIA_PLAYER_STATE_PAUSED, MEDIA_PLAYER_STATE_ANNOUNCING, MEDIA_PLAYER_STATE_OFF, MEDIA_PLAYER_STATE_ON
};
inline const char *media_player_state_to_string(MediaPlayerState state) {
  static const char *names[] = {"NONE", "IDLE", "PLAYING", "PAUSED", "ANNOUNCING", "OFF", "ON"};
  return names[static_cast<unsigned>(state)];
}
class MediaPlayer {
 public:
  template<typename F> void add_on_state_callback(F &&fn) { callbacks.emplace_back(std::forward<F>(fn)); }
  void publish(MediaPlayerState next) { state = next; for (auto &fn : callbacks) fn(next); }
  bool is_muted() const { return false; }
  MediaPlayerState state{MEDIA_PLAYER_STATE_IDLE};
  std::vector<std::function<void(MediaPlayerState)>> callbacks;
};
}
namespace speaker_source { class SpeakerSourceMediaPlayer : public media_player::MediaPlayer {}; }
namespace mixer_speaker {
class MixerSpeaker {
 public:
  uint32_t get_frames_in_pipeline() const { return frames; }
  uint32_t frames{0};
};
}
namespace esp_audio_stack {
class ESPAudioStack {
 public:
  bool is_running() const { return running; }
  bool is_starting() const { return starting; }
  bool is_idle() const { return idle; }
  bool is_mic_running() const { return mic; }
  bool is_speaker_running() const { return speaker; }
  bool is_speaker_paused() const { return paused; }
  bool has_failed_session() const { return failed; }
  bool has_i2s_error() const { return error; }
  size_t get_speaker_buffer_available() const { return queued; }
  size_t get_speaker_buffer_size() const { return capacity; }
  bool has_pending_speaker_output() const { return pending; }
  uint32_t get_sample_rate() const { return 48000; }
  template<typename F> void add_on_state_callback(F &&fn) { callbacks.emplace_back(std::forward<F>(fn)); }
  void dump_diagnostics() { ++dumps; }
  bool running{true}, starting{false}, idle{false}, mic{false}, speaker{false}, paused{false};
  bool failed{false}, error{false}, pending{false};
  size_t queued{0}, capacity{9600};
  unsigned dumps{0};
  std::vector<std::function<void(const std::string &)>> callbacks;
};
}
namespace voice_assistant {
class VoiceAssistant { public: bool is_running() const { return running; } bool running{false}; };
}
}

#define ESPHOME_VERSION "2026.10.0"

struct esp_app_desc_t {
  uint32_t magic_word, secure_version, reserv1[2];
  char version[32], project_name[32], time[16], date[16], idf_ver[32];
  uint8_t app_elf_sha256[32];
  uint16_t min_efuse_blk_rev_full, max_efuse_blk_rev_full;
  uint8_t mmu_page_size, reserv3[3];
  uint32_t reserv2[18];
};
static_assert(sizeof(esp_app_desc_t) == 256);
inline const esp_app_desc_t *esp_app_get_description() {
  assert(!esphome::test_output_callback);
  ++esphome::test_app_description_reads;
  static const esp_app_desc_t value = []() {
    esp_app_desc_t app{};
    std::strcpy(app.date, "2026-10-26");
    std::strcpy(app.time, "23:30:00");
    std::strcpy(app.idf_ver, "v5.5.6");
    for (unsigned i = 0; i < 32; ++i) app.app_elf_sha256[i] = static_cast<uint8_t>(i);
    return app;
  }();
  return &value;
}

#define ESP_LOGI(tag, ...) ::esphome::test_log(tag, __VA_ARGS__)
#define ESP_LOGD(tag, ...) ::esphome::test_log(tag, __VA_ARGS__)
#define ESP_LOGW(tag, ...) ::esphome::test_log(tag, __VA_ARGS__)
#define ESP_LOGE(tag, ...) ::esphome::test_log(tag, __VA_ARGS__)
#define ESP_LOGCONFIG(tag, ...) ::esphome::test_log(tag, __VA_ARGS__)
#define YESNO(value) ((value) ? "YES" : "NO")
