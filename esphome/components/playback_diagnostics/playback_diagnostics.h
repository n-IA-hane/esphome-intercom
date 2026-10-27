#pragma once

#include "esphome/core/component.h"
#include "esphome/components/media_player/media_player.h"
#include "esphome/components/media_source/media_source.h"
#include "esphome/components/speaker/speaker.h"
#include "esphome/components/esp_audio_stack/esp_audio_stack.h"
#ifdef USE_PLAYBACK_DIAGNOSTICS_MIXER
#include "esphome/components/mixer/speaker/mixer_speaker.h"
#endif
#ifdef USE_PLAYBACK_DIAGNOSTICS_VOICE
#include "esphome/components/voice_assistant/voice_assistant.h"
#endif

#include <array>
#include <atomic>
#include <string>

namespace esphome::playback_diagnostics {

// Observes public interfaces only. It never becomes a MediaSource listener or
// inserts an audio writer, and never changes playback or hardware ownership.
class PlaybackDiagnostics : public Component {
 public:
  void setup() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::LATE; }
  void set_media_player(media_player::MediaPlayer *value) { this->player_ = value; }
  void set_announcement_source(media_source::MediaSource *value) { this->source_ = value; }
  void set_media_source(media_source::MediaSource *value) { this->media_source_ = value; }
  void set_tts_speaker(speaker::Speaker *value) { this->tts_ = value; }
  void set_mixer_input(speaker::Speaker *value) { this->mixer_input_ = value; }
  void set_hardware_speaker(speaker::Speaker *value) { this->hardware_ = value; }
  void set_audio_stack(esp_audio_stack::ESPAudioStack *value) { this->stack_ = value; }
#ifdef USE_PLAYBACK_DIAGNOSTICS_MIXER
  void set_mixer(mixer_speaker::MixerSpeaker *value) { this->mixer_ = value; }
#endif
#ifdef USE_PLAYBACK_DIAGNOSTICS_VOICE
  void set_voice_assistant(voice_assistant::VoiceAssistant *value) { this->voice_ = value; }
#endif
  void dump(const std::string &reason);
  void watch_next_announcement(uint32_t timeout_ms);
  void cancel_watch();
  void on_tts_start(size_t text_length);
  void on_tts_end();
  void on_va_end();
  void on_error(const std::string &code, const std::string &message);

 protected:
  struct Progress {
    std::atomic<uint32_t> frames{0};
    std::atomic<uint32_t> last_ms{0};
    std::atomic<uint32_t> callbacks{0};
    void observe(uint32_t count, int64_t timestamp);
  };
  struct Stage {
    uint32_t rate{0}, frames{0}, callbacks{0}, age_ms{0};
    uint8_t flags{0};
  };
  struct Snapshot {
    uint32_t report_id{0};
    uint32_t time_ms{0}, sequence{0}, text_length{0}, mixer_frames{0};
    uint32_t queued_bytes{0}, capacity_bytes{0}, high_delta{0}, low_delta{0};
    uint32_t error_code_length{0}, error_message_length{0};
    const char *reason{"manual"};
    const char *error_code{"none"};
    const char *error_message{"none"};
    Stage high, input, low;
    int player_state{0}, source_state{0}, media_active{-1}, watch{0}, va_running{-1};
    uint8_t stack_flags{0};
    bool pending_tx{false}, mixer_available{false};
  };
  void on_player_state_(media_player::MediaPlayerState state);
  void capture_(const char *reason);
  void emit_();
  Stage stage_(speaker::Speaker *speaker, const Progress *progress, uint32_t now) const;
  static const char *safe_reason_(const std::string &value);
  static const char *safe_code_(const std::string &value);
  static const char *safe_message_(const std::string &value);

  media_player::MediaPlayer *player_{nullptr};
  media_source::MediaSource *source_{nullptr}, *media_source_{nullptr};
  speaker::Speaker *tts_{nullptr}, *mixer_input_{nullptr}, *hardware_{nullptr};
  esp_audio_stack::ESPAudioStack *stack_{nullptr};
#ifdef USE_PLAYBACK_DIAGNOSTICS_MIXER
  mixer_speaker::MixerSpeaker *mixer_{nullptr};
#endif
#ifdef USE_PLAYBACK_DIAGNOSTICS_VOICE
  voice_assistant::VoiceAssistant *voice_{nullptr};
#endif
  Progress high_, low_;
  uint32_t sequence_{0}, text_length_{0}, high_start_{0}, low_start_{0};
  uint32_t error_code_length_{0}, error_message_length_{0};
  const char *error_code_{"none"}, *error_message_{"none"};
  bool watching_{false}, started_{false}, saw_announcing_{false};
  media_player::MediaPlayerState last_player_state_{media_player::MEDIA_PLAYER_STATE_NONE};
  // Bounded reports, one short log line per scheduler turn. New reports are
  // coalesced when full; the error summary remains available to the next dump.
  std::array<Snapshot, 4> reports_{};
  uint8_t report_head_{0}, report_count_{0}, report_step_{0};
  uint32_t coalesced_{0}, next_report_id_{0};
};

}  // namespace esphome::playback_diagnostics
