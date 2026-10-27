#include "playback_diagnostics.h"

#include "esphome/core/hal.h"
#include "esphome/core/log.h"
#include "esphome/core/version.h"

#include <esp_app_desc.h>

#include <algorithm>
#include <limits>

namespace esphome::playback_diagnostics {
static const char *const TAG = "playback_diagnostics";
static_assert(sizeof(std::atomic<uint32_t>) == sizeof(uint32_t));
static_assert(alignof(std::atomic<uint32_t>) >= alignof(uint32_t));

void PlaybackDiagnostics::Progress::observe(uint32_t count, int64_t timestamp) {
  if (count == 0)
    return;
  // One serialized writer per stage: the validated Audio Stack -> mixer ->
  // resampler completion chain. Main-loop readers never reset these values.
  // Do not use RMW operations here: IDF's Xtensa PSRAM workaround can route
  // those through a lock. Word-sized relaxed loads/stores emit native moves
  // with the supported compiler, verified by the target assembly gate.
  this->frames.store(this->frames.load(std::memory_order_relaxed) + count, std::memory_order_relaxed);
  this->last_ms.store(static_cast<uint32_t>(timestamp / 1000), std::memory_order_relaxed);
  this->callbacks.store(this->callbacks.load(std::memory_order_relaxed) + 1, std::memory_order_relaxed);
}

void PlaybackDiagnostics::setup() {
  this->last_player_state_ = this->player_->state;
  this->tts_->add_audio_output_callback([this](uint32_t frames, int64_t timestamp) {
    this->high_.observe(frames, timestamp);
  });
  this->hardware_->add_audio_output_callback([this](uint32_t frames, int64_t timestamp) {
    this->low_.observe(frames, timestamp);
  });
  this->player_->add_on_state_callback([this](media_player::MediaPlayerState state) { this->on_player_state_(state); });
  this->disable_loop();
}

void PlaybackDiagnostics::dump_config() {
  ESP_LOGCONFIG(TAG, "Playback Diagnostics: public interfaces, reports only; no playback recovery");
}

const char *PlaybackDiagnostics::safe_reason_(const std::string &value) {
  for (const char *label : {"manual", "before", "stalled", "after"})
    if (value == label)
      return label;
  return "manual";
}

const char *PlaybackDiagnostics::safe_code_(const std::string &value) {
  // Error identifiers are protocol labels, but arbitrary remote values can
  // contain credentials. Never echo unknown input, including a truncated URL.
  for (const char *label : {"tts-start-timeout", "mic-channel-stalled", "not-connected", "failed-to-start",
                            "wake-engine-missing", "wake-provider-missing", "wake-stream-failed", "wake-word-timeout",
                            "stt-provider-missing", "stt-provider-unsupported-metadata", "cloud-auth-failed",
                            "stt-stream-failed", "stt-no-text-recognized", "intent-agent-not-found",
                            "intent-not-supported", "intent-failed", "tts-not-supported"})
    if (value == label)
      return label;
  return "redacted";
}

const char *PlaybackDiagnostics::safe_message_(const std::string &value) {
  for (const char *label : {"TTS playback did not start before the timeout", "A microphone channel stopped producing audio",
                            "Could not request start", "Failed to start server. See Home Assistant logs for more details.",
                            "Wake word was not detected", "No text recognized"})
    if (value == label)
      return label;
  return "redacted";
}

void PlaybackDiagnostics::dump(const std::string &reason) { this->capture_(safe_reason_(reason)); }

void PlaybackDiagnostics::watch_next_announcement(uint32_t timeout_ms) {
  this->cancel_timeout("watch");
  this->watching_ = true;
  this->started_ = false;
  this->saw_announcing_ = false;
  this->high_start_ = this->high_.frames.load(std::memory_order_relaxed);
  this->low_start_ = this->low_.frames.load(std::memory_order_relaxed);
  // The entire session, including waiting for the next request, is finite.
  timeout_ms = std::clamp<uint32_t>(timeout_ms, 1000, 300000);
  this->set_timeout("watch", timeout_ms, [this]() {
    this->capture_(this->started_ ? "watch_deadline" : "watch_no_announcement");
    this->watching_ = false;
    this->started_ = false;
  });
  this->capture_("watch_armed");
}

void PlaybackDiagnostics::cancel_watch() {
  this->cancel_timeout("watch");
  this->watching_ = false;
  this->started_ = false;
  this->capture_("watch_cancelled");
}

void PlaybackDiagnostics::on_tts_start(size_t text_length) {
  ++this->sequence_;
  this->text_length_ = static_cast<uint32_t>(std::min<size_t>(text_length, UINT32_MAX));
  if (this->watching_ && !this->started_) {
    this->started_ = true;
    this->high_start_ = this->high_.frames.load(std::memory_order_relaxed);
    this->low_start_ = this->low_.frames.load(std::memory_order_relaxed);
  }
  this->capture_("tts_start");
}
void PlaybackDiagnostics::on_tts_end() { this->capture_("tts_url_submitted"); }
void PlaybackDiagnostics::on_va_end() { this->capture_("va_end"); }

void PlaybackDiagnostics::on_error(const std::string &code, const std::string &message) {
  this->error_code_ = safe_code_(code);
  this->error_message_ = safe_message_(message);
  this->error_code_length_ = static_cast<uint32_t>(std::min<size_t>(code.size(), UINT32_MAX));
  this->error_message_length_ = static_cast<uint32_t>(std::min<size_t>(message.size(), UINT32_MAX));
  this->capture_("va_error");
  this->cancel_timeout("watch");
  this->watching_ = false;
  this->started_ = false;
}

void PlaybackDiagnostics::on_player_state_(media_player::MediaPlayerState state) {
  if (state == this->last_player_state_)
    return;
  this->last_player_state_ = state;
  if (this->watching_) {
    if (state == media_player::MEDIA_PLAYER_STATE_ANNOUNCING) {
      // Native MediaPlayer::publish_state() also delivers announcements made
      // without VA. Use that transition only when no earlier TTS baseline exists.
      if (!this->started_) {
        this->started_ = true;
        this->high_start_ = this->high_.frames.load(std::memory_order_relaxed);
        this->low_start_ = this->low_.frames.load(std::memory_order_relaxed);
      }
      this->saw_announcing_ = true;
    } else if (this->saw_announcing_) {
      this->capture_("watch_player_finished");
      this->cancel_timeout("watch");
      this->watching_ = false;
      this->started_ = false;
      return;
    }
  }
  this->capture_("media_state");
}

PlaybackDiagnostics::Stage PlaybackDiagnostics::stage_(speaker::Speaker *speaker, const Progress *progress,
                                                       uint32_t now) const {
  Stage value;
  value.rate = speaker->get_audio_stream_info().get_sample_rate();
  value.flags = (speaker->is_stopped() ? 1 : 0) | (speaker->is_running() ? 2 : 0) |
                (speaker->get_pause_state() ? 4 : 0) | (speaker->has_buffered_data() ? 8 : 0);
  if (progress != nullptr) {
    value.frames = progress->frames.load(std::memory_order_relaxed);
    value.callbacks = progress->callbacks.load(std::memory_order_relaxed);
    value.age_ms = value.callbacks == 0 ? UINT32_MAX : now - progress->last_ms.load(std::memory_order_relaxed);
  }
  return value;
}

void PlaybackDiagnostics::capture_(const char *reason) {
  const uint32_t report_id = ++this->next_report_id_;
  if (this->report_count_ == this->reports_.size()) {
    ++this->coalesced_;
    return;
  }
  auto &out = this->reports_[(this->report_head_ + this->report_count_) % this->reports_.size()];
  out = Snapshot{};
  out.report_id = report_id;
  out.reason = reason;
  out.time_ms = millis();
  out.sequence = this->sequence_;
  out.text_length = this->text_length_;
  out.player_state = static_cast<int>(this->player_->state);
  out.source_state = static_cast<int>(this->source_->get_state());
  out.media_active = this->media_source_ == nullptr ? -1 :
      (this->media_source_->get_state() != media_source::MediaSourceState::IDLE);
  out.watch = !this->watching_ ? 0 : (this->started_ ? 2 : 1);
  out.high = this->stage_(this->tts_, &this->high_, out.time_ms);
  out.input = this->stage_(this->mixer_input_, nullptr, out.time_ms);
  out.low = this->stage_(this->hardware_, &this->low_, out.time_ms);
  out.high_delta = out.high.frames - this->high_start_;
  out.low_delta = out.low.frames - this->low_start_;
  out.queued_bytes = this->stack_->get_speaker_buffer_available();
  out.capacity_bytes = this->stack_->get_speaker_buffer_size();
  out.pending_tx = this->stack_->has_pending_speaker_output();
  out.stack_flags = (this->stack_->is_running() ? 1 : 0) | (this->stack_->is_starting() ? 2 : 0) |
                    (this->stack_->is_idle() ? 4 : 0) | (this->stack_->has_failed_session() ? 8 : 0) |
                    (this->stack_->has_i2s_error() ? 16 : 0) | (this->stack_->is_mic_running() ? 32 : 0) |
                    (this->stack_->is_speaker_running() ? 64 : 0) | (this->stack_->is_speaker_paused() ? 128 : 0);
#ifdef USE_PLAYBACK_DIAGNOSTICS_MIXER
  if (this->mixer_ != nullptr) {
    out.mixer_available = true;
    out.mixer_frames = this->mixer_->get_frames_in_pipeline();
  }
#endif
#ifdef USE_PLAYBACK_DIAGNOSTICS_VOICE
  if (this->voice_ != nullptr)
    out.va_running = this->voice_->is_running();
#endif
  out.error_code = this->error_code_;
  out.error_message = this->error_message_;
  out.error_code_length = this->error_code_length_;
  out.error_message_length = this->error_message_length_;
  if (++this->report_count_ == 1)
    this->defer("report", [this]() { this->emit_(); });
}

void PlaybackDiagnostics::emit_() {
  if (this->report_count_ == 0)
    return;
  const auto &s = this->reports_[this->report_head_];
  const auto u = [](auto value) { return static_cast<unsigned>(value); };
  switch (this->report_step_++) {
    case 0:
      ESP_LOGI(TAG, "BEGIN v=1 report_id=%u part=1/7 parts=7 t_ms=%u seq=%u reason=%s watch=%d va_running=%d player=%d source=%d text_bytes=%u media_active=%d hardware_shared=YES",
               u(s.report_id), u(s.time_ms), u(s.sequence), s.reason, s.watch, s.va_running, s.player_state, s.source_state,
               u(s.text_length), s.media_active);
      break;
    case 1: {
      // Fixed firmware metadata, read only from the main-loop report phase.
      const esp_app_desc_t *description = esp_app_get_description();
      static constexpr char HEX[] = "0123456789abcdef";
      char elf_sha256[65];
      for (size_t i = 0; i < sizeof(description->app_elf_sha256); ++i) {
        const uint8_t value = description->app_elf_sha256[i];
        elf_sha256[i * 2] = HEX[value >> 4];
        elf_sha256[i * 2 + 1] = HEX[value & 15];
      }
      elf_sha256[64] = '\0';
      ESP_LOGI(TAG, "identity: report_id=%u part=2/7 elf_sha256=%s build_date=%.*s build_time=%.*s idf=%.*s esphome=%s",
               u(s.report_id), elf_sha256, static_cast<int>(sizeof(description->date)), description->date,
               static_cast<int>(sizeof(description->time)), description->time,
               static_cast<int>(sizeof(description->idf_ver)), description->idf_ver, ESPHOME_VERSION);
      break;
    }
    case 2:
      ESP_LOGI(TAG, "high: report_id=%u part=3/7 flags=%u rate_hz=%u frames=%u callbacks=%u age_ms=%u watch_frames=%u; mixer_input: flags=%u rate_hz=%u",
               u(s.report_id), u(s.high.flags), u(s.high.rate), u(s.high.frames), u(s.high.callbacks), u(s.high.age_ms), u(s.high_delta),
               u(s.input.flags), u(s.input.rate));
      break;
    case 3:
      ESP_LOGI(TAG, "low: report_id=%u part=4/7 flags=%u rate_hz=%u frames=%u callbacks=%u age_ms=%u watch_frames=%u; mixer_available=%u mixer_frames=%u",
               u(s.report_id), u(s.low.flags), u(s.low.rate), u(s.low.frames), u(s.low.callbacks), u(s.low.age_ms), u(s.low_delta),
               u(s.mixer_available), u(s.mixer_frames));
      break;
    case 4:
      ESP_LOGI(TAG, "stack: report_id=%u part=5/7 flags=%u queued_bytes=%u capacity_bytes=%u pending_tx=%u; hardware_state=unavailable queue_depth=unavailable decoder_state=unavailable source_credits=unavailable",
               u(s.report_id), u(s.stack_flags), u(s.queued_bytes), u(s.capacity_bytes), u(s.pending_tx));
      break;
    case 5:
      ESP_LOGI(TAG, "error: report_id=%u part=6/7 code=%s message=%s code_bytes=%u message_bytes=%u", u(s.report_id), s.error_code, s.error_message,
               u(s.error_code_length), u(s.error_message_length));
      break;
    default:
      ESP_LOGI(TAG, "END v=1 report_id=%u part=7/7 parts=7 t_ms=%u seq=%u coalesced=%u", u(s.report_id), u(s.time_ms), u(s.sequence), u(this->coalesced_));
      this->report_head_ = (this->report_head_ + 1) % this->reports_.size();
      --this->report_count_;
      this->report_step_ = 0;
      break;
  }
  if (this->report_count_ != 0)
    this->defer("report", [this]() { this->emit_(); });
}

}  // namespace esphome::playback_diagnostics
