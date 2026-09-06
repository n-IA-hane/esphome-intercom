#include "audio_stack_capture.h"

#include "esphome/core/log.h"
#include <esp_heap_caps.h>
#include <esp_timer.h>
#include <algorithm>
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <lwip/inet.h>
#include <lwip/sockets.h>
#include <freertos/task.h>

namespace esphome::audio_stack_capture {
static const char *const TAG = "audio_stack_capture";

static bool send_all(int socket, const void *buffer, size_t bytes) {
  const auto *data = static_cast<const uint8_t *>(buffer);
  while (bytes != 0) {
    const ssize_t sent = ::send(socket, data, bytes, 0);
    if (sent <= 0) return false;
    data += sent;
    bytes -= sent;
  }
  return true;
}

void AudioStackCapture::setup() {
  this->storage_ = static_cast<uint8_t *>(heap_caps_malloc(this->buffer_bytes_ + 1, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (this->storage_ == nullptr) {
    ESP_LOGE(TAG, "Cannot allocate diagnostic stream buffer");
    this->mark_failed();
    return;
  }
  this->stream_ = xStreamBufferCreateStatic(this->buffer_bytes_ + 1, 1, this->storage_, &this->stream_state_);
  if (this->stream_ == nullptr || !this->stack_->add_mic_data_callback(mic_callback_, this) ||
      !this->stack_->add_speaker_output_callback(played_callback_, this)) {
    ESP_LOGE(TAG, "Cannot register diagnostic observers");
    this->mark_failed();
    return;
  }
  this->stack_->set_audio_capture_callback(raw_callback_, this);
  ESP_LOGI(TAG, "Diagnostic observers ready, %u bytes PSRAM", static_cast<unsigned>(this->buffer_bytes_ + 1));
}

bool AudioStackCapture::capture(uint32_t duration_ms, uint32_t mask) {
  if (duration_ms != 0 && (duration_ms < 1000 || duration_ms > 3600000)) return false;
  if (mask == 0 || (mask & ~0x1eU) != 0) return false;
  if (this->is_failed() || this->stream_ == nullptr || this->busy_.exchange(true)) return false;
  this->capture_duration_ms_ = duration_ms != 0 ? duration_ms : this->duration_ms_;
  this->capture_mask_ = mask;
  if (xTaskCreate(task_, "audio_capture", 4096, this, 1, nullptr) != pdPASS) {
    this->busy_.store(false);
    ESP_LOGE(TAG, "Cannot start diagnostic sender");
    return false;
  }
  return true;
}

void AudioStackCapture::raw_callback_(void *ctx, uint32_t kind, const uint8_t *data, size_t bytes,
                                     uint32_t rate, uint8_t channels, uint8_t bits) {
  auto *self = static_cast<AudioStackCapture *>(ctx);
  self->record_(kind, data, bytes, rate, channels, bits, bytes / (channels * (bits / 8)), esp_timer_get_time());
}

void AudioStackCapture::mic_callback_(void *ctx, const uint8_t *data, size_t bytes) {
  auto *self = static_cast<AudioStackCapture *>(ctx);
  // Mic callbacks carry post-processor mono PCM, independently of the TX
  // bus slots. This is the same contract as ESPAudioStackMicrophone.
  constexpr uint16_t channels = 1;
  self->record_(2, data, bytes, self->stack_->get_output_sample_rate(), channels, 16,
                bytes / (channels * sizeof(int16_t)), esp_timer_get_time());
}

void AudioStackCapture::played_callback_(void *ctx, uint32_t frames, int64_t timestamp) {
  auto *self = static_cast<AudioStackCapture *>(ctx);
  self->record_(3, nullptr, 0, self->stack_->get_sample_rate(), self->stack_->get_speaker_channels(), 16,
                frames, timestamp);
}

void AudioStackCapture::record_(uint32_t kind, const uint8_t *data, size_t bytes, uint32_t rate,
                               uint16_t channels, uint16_t bits, uint32_t frames, uint64_t timestamp) {
  uint32_t expected = ACTIVE;
  if (!this->gate_.compare_exchange_strong(expected, ACTIVE | WRITING, std::memory_order_acquire)) {
    if (expected == (ACTIVE | WRITING)) this->producer_conflict_.store(true);
    return;
  }
  const uint64_t started = esp_timer_get_time();
  if ((this->capture_mask_ & (1U << kind)) == 0) {
    this->gate_.fetch_and(~WRITING, std::memory_order_release);
    return;
  }
  if (started >= this->deadline_us_) {
    this->gate_.fetch_and(~(ACTIVE | WRITING), std::memory_order_release);
    return;
  }
  CaptureHeader header{kind, ++this->sequence_, rate, channels, bits, frames,
                       static_cast<uint32_t>(bytes), timestamp};
  // The existing audio task is the single producer for raw, processed and
  // played callbacks. Space cannot decrease while this producer writes.
  if (xStreamBufferSpacesAvailable(this->stream_) >= sizeof(header) + bytes) {
    xStreamBufferSend(this->stream_, &header, sizeof(header), 0);
    if (bytes != 0) xStreamBufferSend(this->stream_, data, bytes, 0);
  } else {
    ++this->dropped_;
  }
  this->max_callback_us_ = std::max(this->max_callback_us_, static_cast<uint32_t>(esp_timer_get_time() - started));
  this->gate_.fetch_and(~WRITING, std::memory_order_release);
}

bool AudioStackCapture::receive_exact_(void *buffer, size_t bytes, TickType_t wait) {
  auto *data = static_cast<uint8_t *>(buffer);
  while (bytes != 0) {
    const size_t got = xStreamBufferReceive(this->stream_, data, bytes, wait);
    if (got == 0) return false;
    data += got;
    bytes -= got;
  }
  return true;
}

void AudioStackCapture::task_(void *ctx) {
  auto *self = static_cast<AudioStackCapture *>(ctx);
  self->run_();
  self->busy_.store(false, std::memory_order_release);
  vTaskDelete(nullptr);
}

void AudioStackCapture::run_() {
  const int socket = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (socket < 0) { ESP_LOGE(TAG, "Cannot open diagnostic socket"); return; }
  sockaddr_in destination{};
  destination.sin_family = AF_INET;
  destination.sin_port = htons(this->port_);
  const int flags = ::fcntl(socket, F_GETFL, 0);
  ::fcntl(socket, F_SETFL, flags | O_NONBLOCK);
  bool connected = ::inet_pton(AF_INET, this->host_.c_str(), &destination.sin_addr) == 1;
  if (connected && ::connect(socket, reinterpret_cast<sockaddr *>(&destination), sizeof(destination)) != 0) {
    connected = errno == EINPROGRESS;
    if (connected) {
      fd_set writable;
      FD_ZERO(&writable); FD_SET(socket, &writable);
      timeval timeout{3, 0};
      connected = ::select(socket + 1, nullptr, &writable, nullptr, &timeout) > 0;
      int error = 0; socklen_t size = sizeof(error);
      connected = connected && ::getsockopt(socket, SOL_SOCKET, SO_ERROR, &error, &size) == 0 && error == 0;
    }
  }
  if (!connected) {
    ESP_LOGE(TAG, "Diagnostic receiver unavailable");
    ::close(socket); return;
  }
  ::fcntl(socket, F_SETFL, flags);
  timeval timeout{2, 0};
  ::setsockopt(socket, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
  bool sent = send_all(socket, "ASTCAP1\n", 8);
  if (!sent) { ::close(socket); return; }
  xStreamBufferReset(this->stream_);
  this->sequence_ = this->dropped_ = this->max_callback_us_ = 0;
  this->producer_conflict_.store(false);
  this->deadline_us_ = esp_timer_get_time() + static_cast<uint64_t>(this->capture_duration_ms_) * 1000;
  this->gate_.store(ACTIVE, std::memory_order_release);
  ESP_LOGI(TAG, "Recording hardware RX, processed microphone and DAC completions");
  if (this->deferred_transfer_) {
    // Keep diagnostic network traffic outside the measured interval. The
    // existing bounded stream stores the snapshot; overflow invalidates it.
    vTaskDelay(pdMS_TO_TICKS(this->capture_duration_ms_) + 1);
    this->gate_.fetch_and(~ACTIVE, std::memory_order_acq_rel);
  }
  uint8_t transfer[1024];
  for (;;) {
    const int64_t remaining = static_cast<int64_t>(this->deadline_us_) - esp_timer_get_time();
    if (remaining <= 0 || !sent) this->gate_.fetch_and(~ACTIVE, std::memory_order_acq_rel);
    const uint32_t gate = this->gate_.load(std::memory_order_acquire);
    if (gate == 0 && xStreamBufferBytesAvailable(this->stream_) == 0) break;
    const TickType_t wait = remaining > 0 && sent
        ? pdMS_TO_TICKS(static_cast<uint32_t>(remaining / 1000) + 1) : pdMS_TO_TICKS(100);
    CaptureHeader header{};
    if (!this->receive_exact_(&header, sizeof(header), wait)) continue;
    sent = sent && send_all(socket, &header, sizeof(header));
    size_t bytes = header.bytes;
    while (bytes != 0) {
      const size_t chunk = std::min(bytes, sizeof(transfer));
      if (!this->receive_exact_(transfer, chunk, pdMS_TO_TICKS(1000))) {
        ESP_LOGE(TAG, "Incomplete diagnostic record");
        sent = false;
        break;
      }
      if (sent) sent = send_all(socket, transfer, chunk);
      bytes -= chunk;
    }
  }
  const uint32_t stats[]{this->sequence_, this->dropped_, this->producer_conflict_.load() ? 1U : 0U,
                         this->max_callback_us_};
  CaptureHeader end{0, this->sequence_, 0, 0, 0, 0, sizeof(stats), static_cast<uint64_t>(esp_timer_get_time())};
  sent = sent && send_all(socket, &end, sizeof(end)) && send_all(socket, stats, sizeof(stats));
  ::shutdown(socket, SHUT_RDWR); ::close(socket);
  ESP_LOGI(TAG, "Capture finished: sent=%d records=%u dropped=%u conflict=%u max_callback=%uus",
           sent, static_cast<unsigned>(stats[0]), static_cast<unsigned>(stats[1]),
           static_cast<unsigned>(stats[2]), static_cast<unsigned>(stats[3]));
}

}  // namespace esphome::audio_stack_capture
