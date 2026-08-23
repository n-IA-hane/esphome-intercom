#include "audio_http_media_source.h"

#ifdef USE_ESP32

#include "esphome/core/log.h"

#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <algorithm>

namespace esphome::audio_http {

static const char *const TAG = "audio_http_media_source";

static constexpr size_t DEFAULT_TRANSFER_BUFFER_SIZE = 8 * 1024;
static constexpr uint32_t HTTP_TIMEOUT_MS = 5000;
static constexpr uint32_t AUDIO_WRITE_TIMEOUT_MS = 50;
static constexpr uint32_t READER_WRITE_TIMEOUT_MS = 50;
static constexpr uint8_t READER_TASK_PRIORITY = 2;
static constexpr uint8_t DECODER_TASK_PRIORITY = 2;
static constexpr size_t READER_TASK_STACK_SIZE = 4096;
static constexpr size_t DECODER_TASK_STACK_SIZE = 5120;
static constexpr uint32_t PAUSE_POLL_DELAY_MS = 20;
static constexpr const char *const HTTP_URI_PREFIX = "http://";
static constexpr const char *const HTTPS_URI_PREFIX = "https://";

void AudioHTTPMediaSource::dump_config() {
  ESP_LOGCONFIG(TAG,
                "Audio HTTP Media Source:\n"
                "  Buffer Size: %zu bytes\n"
                "  Persistent Ring Buffer: %s\n"
                "  Decoder Task Stack in PSRAM: %s",
                this->buffer_size_, YESNO(this->persistent_ring_buffer_), YESNO(this->decoder_task_stack_in_psram_));
}

void AudioHTTPMediaSource::setup() {
  this->disable_loop();

  micro_decoder::DecoderConfig config;
  config.ring_buffer_size = this->buffer_size_;
  config.persistent_ring_buffer = this->persistent_ring_buffer_;
  config.transfer_buffer_size = std::min(DEFAULT_TRANSFER_BUFFER_SIZE, this->buffer_size_ / 2);
  config.http_timeout_ms = HTTP_TIMEOUT_MS;
  config.audio_write_timeout_ms = AUDIO_WRITE_TIMEOUT_MS;
  config.reader_write_timeout_ms = READER_WRITE_TIMEOUT_MS;
  config.reader_priority = READER_TASK_PRIORITY;
  config.decoder_priority = DECODER_TASK_PRIORITY;
  config.reader_stack_size = READER_TASK_STACK_SIZE;
  config.decoder_stack_size = DECODER_TASK_STACK_SIZE;
  config.decoder_stack_in_psram = this->decoder_task_stack_in_psram_;

  this->decoder_ = std::make_unique<micro_decoder::DecoderSource>(config);
  if (this->decoder_ == nullptr) {
    ESP_LOGE(TAG, "Failed to allocate decoder");
    this->mark_failed();
    return;
  }
  this->decoder_->set_listener(this);
}

void AudioHTTPMediaSource::loop() { this->decoder_->loop(); }

bool AudioHTTPMediaSource::can_handle(const std::string &uri) const {
  return uri.starts_with(HTTP_URI_PREFIX) || uri.starts_with(HTTPS_URI_PREFIX);
}

bool AudioHTTPMediaSource::play_uri(const std::string &uri) {
  if (!this->is_ready() || this->is_failed() || this->status_has_error() || !this->has_listener()) {
    return false;
  }
  if (this->get_state() != media_source::MediaSourceState::IDLE) {
    ESP_LOGE(TAG, "Cannot play '%s': source is busy", uri.c_str());
    return false;
  }
  if (!uri.starts_with(HTTP_URI_PREFIX) && !uri.starts_with(HTTPS_URI_PREFIX)) {
    ESP_LOGE(TAG, "Invalid URI: '%s'", uri.c_str());
    return false;
  }
  if (this->decoder_->play_url(uri)) {
    this->pause_.store(false, std::memory_order_relaxed);
    this->enable_loop();
    return true;
  }
  ESP_LOGE(TAG, "Failed to start playback of '%s'", uri.c_str());
  return false;
}

void AudioHTTPMediaSource::handle_command(media_source::MediaSourceCommand command) {
  switch (command) {
    case media_source::MediaSourceCommand::STOP:
      this->decoder_->stop();
      break;
    case media_source::MediaSourceCommand::PAUSE:
      if (this->get_state() != media_source::MediaSourceState::PLAYING)
        break;
      this->set_state_(media_source::MediaSourceState::PAUSED);
      this->pause_.store(true, std::memory_order_relaxed);
      break;
    case media_source::MediaSourceCommand::PLAY:
      if (this->get_state() != media_source::MediaSourceState::PAUSED)
        break;
      this->set_state_(media_source::MediaSourceState::PLAYING);
      this->pause_.store(false, std::memory_order_relaxed);
      break;
    default:
      break;
  }
}

size_t AudioHTTPMediaSource::on_audio_write(const uint8_t *data, size_t length, uint32_t timeout_ms) {
  if (this->pause_.load(std::memory_order_relaxed)) {
    vTaskDelay(pdMS_TO_TICKS(PAUSE_POLL_DELAY_MS));
    return 0;
  }
  return this->write_output(data, length, timeout_ms, this->stream_info_);
}

void AudioHTTPMediaSource::on_stream_info(const micro_decoder::AudioStreamInfo &info) {
  this->stream_info_ = audio::AudioStreamInfo(info.get_bits_per_sample(), info.get_channels(), info.get_sample_rate());
}

void AudioHTTPMediaSource::on_state_change(micro_decoder::DecoderState state) {
  switch (state) {
    case micro_decoder::DecoderState::IDLE:
      this->set_state_(media_source::MediaSourceState::IDLE);
      this->disable_loop();
      break;
    case micro_decoder::DecoderState::PLAYING:
      this->set_state_(media_source::MediaSourceState::PLAYING);
      break;
    case micro_decoder::DecoderState::FAILED:
      this->set_state_(media_source::MediaSourceState::ERROR);
      break;
    default:
      break;
  }
}

}  // namespace esphome::audio_http

#endif  // USE_ESP32
