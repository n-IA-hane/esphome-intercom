#if defined(USE_HOST) && defined(ASSIST_TEST_MICRO_DECODER)
#include <gtest/gtest.h>
#include <atomic>
#include <chrono>
#include <fstream>
#include <iterator>
#include <thread>
#include <pthread.h>
#include <vector>

#include "esphome/core/application.h"
#include "esphome/core/hal.h"
#include "esphome/components/host/preferences.h"
#include "platform/http_client.h"

// Use the same production AudioHTTP adapter as the ESP. Only the HTTP transport is injected;
// AudioReader, DecoderSource and the FLAC codec are compiled from the identified dependency.
#define USE_ESP32
#include "esphome/components/audio_http/audio_http_media_source.cpp"
#include "esphome/components/speaker_source/speaker_source_media_player.h"

namespace {
std::atomic<unsigned> threads_created{0}, threads_joined{0};
}
extern "C" int __real_pthread_create(pthread_t *, const pthread_attr_t *, void *(*)(void *), void *);
extern "C" int __real_pthread_join(pthread_t, void **);
extern "C" int __wrap_pthread_create(pthread_t *thread, const pthread_attr_t *attributes,
                                     void *(*entry)(void *), void *argument) {
  int result = __real_pthread_create(thread, attributes, entry, argument);
  if (result == 0)
    ++threads_created;
  return result;
}
extern "C" int __wrap_pthread_join(pthread_t thread, void **value) {
  int result = __real_pthread_join(thread, value);
  if (result == 0)
    ++threads_joined;
  return result;
}

namespace {
std::vector<uint8_t> body;
std::atomic<bool> natural_end{false};
std::atomic<bool> reader_fault{false};
std::atomic<unsigned> transport_errors{0}, transport_closed{0};

class FaultHttpClient : public micro_decoder::HttpClient {
 public:
  bool open(const micro_decoder::HttpRequest &) override { return true; }
  const micro_decoder::HttpResponse &response_info() const override { return this->response_; }
  int read(uint8_t *data, size_t capacity) override {
    if (this->position_ < body.size()) {
      size_t length = std::min(capacity, body.size() - this->position_);
      std::memcpy(data, body.data() + this->position_, length);
      this->position_ += length;
      return static_cast<int>(length);
    }
    if (!reader_fault.load())
      return 0;
    ++transport_errors;
    return -1;
  }
  bool is_complete() const override { return natural_end.load() && this->position_ == body.size(); }
  void close() override { if (!this->closed_) { this->closed_ = true; ++transport_closed; } }

 private:
  micro_decoder::HttpResponse response_{200, "audio/flac"};
  size_t position_{0};
  bool closed_{false};
};

class PcmSpeaker : public esphome::speaker::Speaker {
 public:
  size_t play(const uint8_t *data, size_t length) override {
    this->pcm.insert(this->pcm.end(), data, data + length);
    this->received.fetch_add(length);
    return length;
  }
  void start() override { this->state_ = esphome::speaker::STATE_RUNNING; }
  void stop() override { this->state_ = esphome::speaker::STATE_STOPPED; }
  void finish() override { ++this->finishes; this->stop(); }
  bool has_buffered_data() const override { return false; }
  std::vector<uint8_t> pcm;
  std::atomic<size_t> received{0};
  unsigned finishes{0};
};
}  // namespace

namespace micro_decoder {
std::unique_ptr<HttpClient> create_http_client() { return std::make_unique<FaultHttpClient>(); }
}  // namespace micro_decoder

class SpeakerSourceHttpError : public ::testing::Test {
 protected:
  static void SetUpTestSuite() { new (&esphome::App) esphome::Application; }
};

TEST_F(SpeakerSourceHttpError, RealFlacReaderFailureRetiresSourceAndPreservesDecodedPrefix) {
  using namespace esphome;
  const char *fixture_dir = std::getenv("ASSIST_MEDIA_FIXTURES");
  ASSERT_NE(fixture_dir, nullptr);
  std::ifstream input(std::string(fixture_dir) + "/fixture.flac", std::ios::binary);
  body.assign(std::istreambuf_iterator<char>(input), {});
  ASSERT_GT(body.size(), 1024u);
  body.resize(body.size() * 3 / 4);  // Valid FLAC frames followed by a failed, incomplete HTTP body.
  reader_fault.store(false);
  natural_end.store(false);
  transport_errors.store(0);
  transport_closed.store(0);
  host::setup_preferences();
  PcmSpeaker speaker;
  speaker_source::SpeakerSourceMediaPlayer player;
  audio_http::AudioHTTPMediaSource source;  // Joins its workers before the player is destroyed, also after an assertion.
  player.set_volume_increment(0.05f);
  player.set_volume_initial(0.5f);
  player.set_volume_min(0.0f);
  player.set_volume_max(1.0f);
  player.set_speaker(speaker_source::ANNOUNCEMENT_PIPELINE, &speaker);
  player.add_media_source(speaker_source::ANNOUNCEMENT_PIPELINE, &source);
  source.set_buffer_size(32768);
  source.set_persistent_ring_buffer(true);
  source.set_task_stack_in_psram(true);
  source.call();
  player.call();
  std::vector<media_player::MediaPlayerState> states;
  player.add_on_state_callback([&](auto state) { states.push_back(state); });
  player.make_call().set_media_url("http://fixture.test/read-error.flac").set_announcement(true).perform();
  auto pump_until = [&](auto predicate) {
    auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (!predicate() && std::chrono::steady_clock::now() < deadline) {
      source.call();
      player.call();
      App.scheduler.call(millis());
      std::this_thread::sleep_for(std::chrono::milliseconds(1));
    }
    return predicate();
  };
  ASSERT_TRUE(pump_until([&] { return speaker.received.load() >= 320; }));
  ASSERT_EQ(player.state, media_player::MEDIA_PLAYER_STATE_ANNOUNCING);
  reader_fault.store(true);
  bool retired = pump_until([&] {
    return transport_errors.load() == 1 && source.get_state() == media_source::MediaSourceState::IDLE &&
           player.state == media_player::MEDIA_PLAYER_STATE_IDLE;
  });
  // Necessary cleanup also on the original RED, then evaluate the independently captured outcome.
  source.handle_command(media_source::MediaSourceCommand::STOP);
  source.call();
  player.call();
  App.scheduler.call(millis());
  EXPECT_TRUE(retired);
  EXPECT_EQ(transport_errors.load(), 1u);
  EXPECT_EQ(transport_closed.load(), 1u);
  EXPECT_EQ(speaker.finishes, 1u);
  ASSERT_FALSE(states.empty());
  EXPECT_EQ(states.front(), media_player::MEDIA_PLAYER_STATE_ANNOUNCING);
  EXPECT_EQ(states.back(), media_player::MEDIA_PLAYER_STATE_IDLE);
  std::ifstream reference(std::string(fixture_dir) + "/reference.pcm", std::ios::binary);
  std::vector<uint8_t> pcm(std::istreambuf_iterator<char>(reference), {});
  ASSERT_GT(speaker.pcm.size(), 320u);
  ASSERT_LE(speaker.pcm.size(), pcm.size());
  EXPECT_TRUE(std::equal(speaker.pcm.begin(), speaker.pcm.end(), pcm.begin()));
}
namespace {
std::vector<uint8_t> fixture_bytes(const char *name) {
  const char *directory = std::getenv("ASSIST_MEDIA_FIXTURES");
  if (directory == nullptr)
    return {};
  std::ifstream input(std::string(directory) + "/" + name, std::ios::binary);
  return {std::istreambuf_iterator<char>(input), {}};
}

template<typename Predicate, typename Pump> bool pump_until(Predicate predicate, Pump pump) {
  auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);
  while (!predicate() && std::chrono::steady_clock::now() < deadline) {
    pump();
    esphome::App.scheduler.call(esphome::millis());
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }
  return predicate();
}

class RetirementListener : public esphome::media_source::MediaSourceListener {
 public:
  size_t write_audio(const uint8_t *data, size_t length, uint32_t,
                     const esphome::audio::AudioStreamInfo &) override {
    this->pcm.insert(this->pcm.end(), data, data + length);
    return length;
  }
  void report_state(esphome::media_source::MediaSourceState state) override {
    if (state == esphome::media_source::MediaSourceState::IDLE) {
      this->at_idle.emplace_back(threads_created.load() - this->created_before,
                                 threads_joined.load() - this->joined_before);
      if (this->at_idle.size() == 1 && this->restart_source != nullptr)
        this->restart_accepted = this->restart_source->play_uri("http://fixture.test/next.flac");
    }
  }
  const unsigned created_before{threads_created.load()};
  const unsigned joined_before{threads_joined.load()};
  std::vector<std::pair<unsigned, unsigned>> at_idle;
  std::vector<uint8_t> pcm;
  esphome::audio_http::AudioHTTPMediaSource *restart_source{nullptr};
  bool restart_accepted{false};
};

void prepare_natural_http() {
  body = fixture_bytes("fixture.flac");
  natural_end.store(true);
  reader_fault.store(false);
  transport_errors.store(0);
  transport_closed.store(0);
}
}  // namespace

TEST_F(SpeakerSourceHttpError, NaturalEndJoinsRealWorkersBeforePublishingIdle) {
  using namespace esphome;
  prepare_natural_http();
  ASSERT_GT(body.size(), 1024u);
  RetirementListener listener;
  audio_http::AudioHTTPMediaSource source;
  source.media_source::MediaSource::set_listener(&listener);
  source.set_persistent_ring_buffer(true);
  source.set_task_stack_in_psram(true);
  source.call();
  ASSERT_TRUE(source.play_uri("http://fixture.test/natural.flac"));
  bool ended = pump_until([&] { return listener.at_idle.size() == 1; }, [&] { source.call(); });
  // Capture before explicit cleanup, which must not hide retained workers on the baseline.
  const auto at_idle = listener.at_idle;
  source.handle_command(media_source::MediaSourceCommand::STOP);
  source.call();
  ASSERT_TRUE(ended);
  ASSERT_EQ(at_idle.size(), 1u);
  EXPECT_EQ(at_idle.front(), (std::pair<unsigned, unsigned>{2, 2}));
  EXPECT_EQ(listener.pcm, fixture_bytes("reference.pcm"));
  EXPECT_EQ(transport_errors.load(), 0u);
  EXPECT_EQ(transport_closed.load(), 1u);
  EXPECT_EQ(source.get_state(), media_source::MediaSourceState::IDLE);
  EXPECT_TRUE(source.is_idle());
}

TEST_F(SpeakerSourceHttpError, IdleListenerCanStartTheNextRealHttpPlayback) {
  using namespace esphome;
  prepare_natural_http();
  RetirementListener listener;
  audio_http::AudioHTTPMediaSource source;
  listener.restart_source = &source;
  source.media_source::MediaSource::set_listener(&listener);
  source.set_persistent_ring_buffer(true);
  source.call();
  ASSERT_TRUE(source.play_uri("http://fixture.test/first.flac"));
  bool ended = pump_until([&] { return listener.at_idle.size() == 2; }, [&] { source.call(); });
  const auto at_idle = listener.at_idle;
  source.handle_command(media_source::MediaSourceCommand::STOP);
  source.call();
  ASSERT_TRUE(ended);
  ASSERT_TRUE(listener.restart_accepted);
  ASSERT_EQ(at_idle.size(), 2u);
  EXPECT_EQ(at_idle[0], (std::pair<unsigned, unsigned>{2, 2}));
  EXPECT_EQ(at_idle[1], (std::pair<unsigned, unsigned>{4, 4}));
  auto expected = fixture_bytes("reference.pcm");
  const auto second = expected;
  expected.insert(expected.end(), second.begin(), second.end());
  EXPECT_EQ(listener.pcm, expected);
  EXPECT_EQ(transport_closed.load(), 2u);
  EXPECT_EQ(transport_errors.load(), 0u);
  EXPECT_TRUE(source.is_idle());
}

TEST_F(SpeakerSourceHttpError, QueuedHttpMusicRetiresEachPlaybackAndPreservesPcm) {
  using namespace esphome;
  prepare_natural_http();
  host::setup_preferences();
  const unsigned created_before = threads_created.load(), joined_before = threads_joined.load();
  PcmSpeaker speaker;
  speaker_source::SpeakerSourceMediaPlayer player;
  audio_http::AudioHTTPMediaSource source;
  player.set_volume_increment(0.05f);
  player.set_volume_initial(0.5f);
  player.set_volume_min(0.0f);
  player.set_volume_max(1.0f);
  player.set_speaker(speaker_source::MEDIA_PIPELINE, &speaker);
  player.add_media_source(speaker_source::MEDIA_PIPELINE, &source);
  source.set_persistent_ring_buffer(true);
  source.call();
  player.call();
  std::vector<media_player::MediaPlayerState> states;
  player.add_on_state_callback([&](auto state) { states.push_back(state); });
  player.make_call().set_media_url("http://fixture.test/first.flac").perform();
  player.make_call().set_media_url("http://fixture.test/second.flac")
      .set_command(media_player::MEDIA_PLAYER_COMMAND_ENQUEUE).perform();
  bool ended = pump_until([&] {
    return transport_closed.load() == 2 && player.state == media_player::MEDIA_PLAYER_STATE_IDLE;
  }, [&] { source.call(); player.call(); });
  const unsigned created = threads_created.load() - created_before;
  const unsigned joined = threads_joined.load() - joined_before;
  source.handle_command(media_source::MediaSourceCommand::STOP);
  source.call();
  player.call();
  ASSERT_TRUE(ended);
  EXPECT_EQ(created, 4u);
  EXPECT_EQ(joined, 4u);
  auto expected = fixture_bytes("reference.pcm");
  const auto second = expected;
  expected.insert(expected.end(), second.begin(), second.end());
  EXPECT_EQ(speaker.pcm, expected);
  EXPECT_EQ(speaker.finishes, 2u);
  EXPECT_EQ(transport_errors.load(), 0u);
  EXPECT_NE(std::find(states.begin(), states.end(), media_player::MEDIA_PLAYER_STATE_PLAYING), states.end());
  ASSERT_FALSE(states.empty());
  EXPECT_EQ(states.back(), media_player::MEDIA_PLAYER_STATE_IDLE);
}
#endif
