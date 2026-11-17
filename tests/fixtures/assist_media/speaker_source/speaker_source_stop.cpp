#ifdef USE_HOST
#include <gtest/gtest.h>

#include <cstring>
#include <string>
#include <vector>

#include "esphome/components/host/preferences.h"
#include "esphome/components/media_player/media_player.h"
#include "esphome/components/media_source/media_source.h"
#include "esphome/core/application.h"
#include "esphome/core/automation.h"
#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"
#include "esphome/core/preferences.h"

// Compile the ESP32-only player in this translation unit against the FreeRTOS queue double in __sdk.
#define USE_ESP32
// NOLINTNEXTLINE(bugprone-suspicious-include): exercise production code with the host FreeRTOS double.
#include "esphome/components/speaker_source/speaker_source_media_player.cpp"

namespace esphome::speaker_source::testing {

using media_player::MediaPlayerCommand;
using media_source::MediaSourceCommand;
using media_source::MediaSourceState;

/// A source that records what the player asks of it; the test decides when it reports a state.
class FakeSource : public media_source::MediaSource {
 public:
  bool play_uri(const std::string &uri) override {
    this->played.push_back(uri);
    return true;
  }
  void handle_command(MediaSourceCommand command) override {
    this->commands.push_back(command);
    if (command == MediaSourceCommand::STOP && this->idle_on_stop)
      this->set_state_(MediaSourceState::IDLE);
  }
  bool can_handle(const std::string &uri) const override { return true; }
  bool has_internal_playlist() const override { return this->internal_playlist; }
  void report(MediaSourceState state) { this->set_state_(state); }

  std::vector<std::string> played;
  std::vector<MediaSourceCommand> commands;
  bool idle_on_stop{false};
  bool internal_playlist{false};
};

class FakeSpeaker : public speaker::Speaker {
 public:
  size_t play(const uint8_t *data, size_t length) override { return length; }
  void start() override { this->state_ = speaker::STATE_RUNNING; }
  void stop() override { this->state_ = speaker::STATE_STOPPED; }
  bool has_buffered_data() const override { return false; }
};

class SpeakerSourceStop : public ::testing::Test {
 protected:
  // Host test main does not execute generated original_setup(), which constructs App.
  static void SetUpTestSuite() { new (&App) Application; }
  void SetUp() override {
    host::setup_preferences();
    queue_test::sent.clear();
    this->player_.set_volume_increment(0.05f);
    this->player_.set_volume_initial(0.5f);
    this->player_.set_volume_min(0.0f);
    this->player_.set_volume_max(1.0f);
    this->player_.set_speaker(MEDIA_PIPELINE, &this->speaker_);
    this->player_.add_media_source(MEDIA_PIPELINE, &this->source_);
    this->player_.call();  // setup()
  }
  // Runs the callbacks the player deferred while it is still alive
  void TearDown() override { App.scheduler.call(millis()); }

  /// Runs the player's loop enough times to drain the commands it queues for itself.
  void run_loop() {
    for (int i = 0; i < 4; i++)
      this->player_.call();
  }
  void play(const std::string &uri) {
    this->player_.make_call().set_media_url(uri).perform();
    this->run_loop();
  }
  void enqueue(const std::string &uri) {
    this->player_.make_call().set_media_url(uri).set_command(media_player::MEDIA_PLAYER_COMMAND_ENQUEUE).perform();
    this->run_loop();
  }
  void command(MediaPlayerCommand command) {
    this->player_.make_call().set_command(command).perform();
    this->run_loop();
  }
  void report(MediaSourceState state) {
    this->source_.report(state);
    this->run_loop();
  }
  /// How many times the player queued a playlist advance for itself.
  size_t playlist_advances() const {
    size_t count = 0;
    for (const auto &item : queue_test::sent) {
      MediaPlayerControlCommand command;
      std::memcpy(&command, item.data(), sizeof(command));
      if (command.type == MediaPlayerControlCommand::PLAYLIST_ADVANCE)
        count++;
    }
    return count;
  }

  FakeSpeaker speaker_;
  FakeSource source_;
  SpeakerSourceMediaPlayer player_;
};

TEST_F(SpeakerSourceStop, NaturalEndAdvancesThePlaylist) {
  this->play("http://a");
  this->report(MediaSourceState::PLAYING);
  this->enqueue("http://b");

  this->report(MediaSourceState::IDLE);
  EXPECT_EQ(this->playlist_advances(), 1);
  EXPECT_EQ(this->source_.played, (std::vector<std::string>{"http://a", "http://b"}));
  EXPECT_TRUE(this->source_.commands.empty());
}


TEST_F(SpeakerSourceStop, TerminalErrorStopsFailedMediaAndAdvancesWithoutRepeatRetry) {
  this->source_.idle_on_stop = true;
  this->play("http://failed");
  this->report(MediaSourceState::PLAYING);
  this->enqueue("http://next");
  this->command(media_player::MEDIA_PLAYER_COMMAND_REPEAT_ONE);
  this->source_.report(MediaSourceState::ERROR);
  this->run_loop();
  EXPECT_EQ(this->source_.commands, std::vector<MediaSourceCommand>{MediaSourceCommand::STOP});
  EXPECT_EQ(this->source_.played, (std::vector<std::string>{"http://failed", "http://next"}));
  EXPECT_NE(this->source_.get_state(), MediaSourceState::ERROR);
  this->report(MediaSourceState::PLAYING);
  EXPECT_EQ(this->player_.state, media_player::MEDIA_PLAYER_STATE_PLAYING);
}

TEST_F(SpeakerSourceStop, TerminalStartupErrorRetiresPendingSourceWithoutRetry) {
  this->source_.idle_on_stop = true;
  this->play("http://failed");
  this->command(media_player::MEDIA_PLAYER_COMMAND_REPEAT_ALL);
  this->source_.report(MediaSourceState::ERROR);
  this->run_loop();
  EXPECT_EQ(this->source_.commands, std::vector<MediaSourceCommand>{MediaSourceCommand::STOP});
  EXPECT_EQ(this->source_.played, std::vector<std::string>{"http://failed"});
  EXPECT_EQ(this->source_.get_state(), MediaSourceState::IDLE);
  EXPECT_EQ(this->player_.state, media_player::MEDIA_PLAYER_STATE_IDLE);
}

TEST_F(SpeakerSourceStop, TerminalAnnouncementErrorPublishesCompletionAndKeepsMusic) {
  FakeSpeaker ann_speaker;
  FakeSource ann_source;
  ann_source.idle_on_stop = true;
  this->player_.set_speaker(ANNOUNCEMENT_PIPELINE, &ann_speaker);
  this->player_.add_media_source(ANNOUNCEMENT_PIPELINE, &ann_source);
  std::vector<media_player::MediaPlayerState> states;
  this->player_.add_on_state_callback([&](auto state) { states.push_back(state); });
  this->play("http://music");
  this->report(MediaSourceState::PLAYING);
  this->player_.make_call().set_media_url("http://tts").set_announcement(true).perform();
  this->run_loop();
  ann_source.report(MediaSourceState::PLAYING);
  this->run_loop();
  ASSERT_EQ(this->player_.state, media_player::MEDIA_PLAYER_STATE_ANNOUNCING);
  ann_speaker.start();
  ann_source.report(MediaSourceState::ERROR);
  this->run_loop();
  EXPECT_EQ(ann_source.commands, std::vector<MediaSourceCommand>{MediaSourceCommand::STOP});
  EXPECT_EQ(ann_source.get_state(), MediaSourceState::IDLE);
  EXPECT_TRUE(ann_speaker.is_stopped());
  EXPECT_EQ(this->player_.state, media_player::MEDIA_PLAYER_STATE_PLAYING);
  EXPECT_EQ(states.back(), media_player::MEDIA_PLAYER_STATE_PLAYING);
  EXPECT_TRUE(this->source_.commands.empty());
  // Clear the binding before local sources leave scope.
  this->player_.make_call().set_command(media_player::MEDIA_PLAYER_COMMAND_STOP).set_announcement(true).perform();
  this->run_loop();
}

TEST_F(SpeakerSourceStop, ErrorWaitsForSourceIdleBeforeStartingTheNextItem) {
  this->play("http://failed");
  this->report(MediaSourceState::PLAYING);
  this->enqueue("http://next");
  this->report(MediaSourceState::ERROR);
  EXPECT_EQ(this->source_.commands, std::vector<MediaSourceCommand>{MediaSourceCommand::STOP});
  EXPECT_EQ(this->source_.played, std::vector<std::string>{"http://failed"});
  this->report(MediaSourceState::IDLE);
  EXPECT_EQ(this->source_.played, (std::vector<std::string>{"http://failed", "http://next"}));
  EXPECT_EQ(this->playlist_advances(), 0);
}

TEST_F(SpeakerSourceStop, AnUnownedErrorCannotStopCurrentMedia) {
  FakeSource other;
  this->player_.add_media_source(MEDIA_PIPELINE, &other);
  this->play("http://current");
  this->report(MediaSourceState::PLAYING);
  other.report(MediaSourceState::ERROR);
  this->run_loop();
  EXPECT_TRUE(other.commands.empty());
  EXPECT_TRUE(this->source_.commands.empty());
  EXPECT_EQ(this->player_.state, media_player::MEDIA_PLAYER_STATE_PLAYING);
}

TEST_F(SpeakerSourceStop, AStoppedSourceErrorCannotDiscardItsReplacement) {
  this->play("http://old");
  this->report(MediaSourceState::PLAYING);
  this->player_.make_call().set_media_url("http://replacement").perform();
  this->player_.call();  // Process PLAY_URI, with the new item staged but the old source not yet IDLE.
  this->source_.report(MediaSourceState::ERROR);
  this->run_loop();
  this->report(MediaSourceState::IDLE);
  EXPECT_EQ(this->source_.played, (std::vector<std::string>{"http://old", "http://replacement"}));
  EXPECT_EQ(this->playlist_advances(), 0);
}

TEST_F(SpeakerSourceStop, PendingReplacementAlreadyIdleDoesNotSuppressTheNextNaturalEnd) {
  this->play("http://pending");
  ASSERT_EQ(this->source_.get_state(), MediaSourceState::IDLE);
  this->play("http://replacement");
  this->report(MediaSourceState::PLAYING);
  this->enqueue("http://following");
  this->report(MediaSourceState::IDLE);
  EXPECT_EQ(this->source_.played,
            (std::vector<std::string>{"http://pending", "http://replacement", "http://following"}));
  EXPECT_EQ(this->playlist_advances(), 1);
}

}  // namespace esphome::speaker_source::testing
#endif  // USE_HOST
