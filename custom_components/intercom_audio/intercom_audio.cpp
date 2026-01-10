#include "intercom_audio.h"

#ifdef USE_I2S_AUDIO_DUPLEX
#include "esphome/components/i2s_audio_duplex/i2s_audio_duplex.h"
#endif

#ifdef USE_ESP_AEC
#include "esphome/components/esp_aec/esp_aec.h"
#endif

#ifdef USE_ESP32

#include "esphome/core/log.h"
#include "esphome/core/application.h"
#ifdef USE_SPEAKER
#include "esphome/components/audio/audio.h"
#endif

#include <lwip/netdb.h>
#include <arpa/inet.h>
#include <cstring>

namespace esphome {
namespace intercom_audio {

static const char *const TAG = "intercom_audio";

// Audio parameters
static const uint32_t SAMPLE_RATE = 16000;
static const size_t FRAME_SAMPLES = 256;  // 16ms @ 16kHz
static const size_t FRAME_BYTES = FRAME_SAMPLES * sizeof(int16_t);

void IntercomAudio::setup() {
  ESP_LOGCONFIG(TAG, "Setting up Intercom Audio...");

  // Create jitter buffer
  this->rx_buffer_ = RingBuffer::create(this->buffer_size_);
  if (!this->rx_buffer_) {
    ESP_LOGE(TAG, "Failed to create RX ring buffer");
    this->mark_failed();
    return;
  }

  // Create mic TX buffer (always needed - moves TX out of callback)
  this->mic_input_buffer_ = RingBuffer::create(this->buffer_size_);
  if (this->mic_input_buffer_) {
    ESP_LOGI(TAG, "Mic TX buffer created");
  } else {
    ESP_LOGE(TAG, "Failed to create mic TX buffer");
    this->mark_failed();
    return;
  }

  // Create speaker reference buffer for AEC (if AEC is configured)
  if (this->aec_ != nullptr) {
    this->speaker_ref_buffer_ = RingBuffer::create(this->buffer_size_);
    if (this->speaker_ref_buffer_) {
      ESP_LOGI(TAG, "AEC speaker reference buffer created");
    } else {
      ESP_LOGW(TAG, "Failed to create AEC speaker reference buffer");
    }
    // Pre-allocate AEC processing buffers (256 samples = 16ms @ 16kHz)
    this->aec_converted_data_.resize(FRAME_SAMPLES);
    this->aec_speaker_ref_.resize(FRAME_SAMPLES);
    this->aec_output_.resize(FRAME_SAMPLES);
    ESP_LOGD(TAG, "AEC processing buffers pre-allocated");
  }

  // Register audio data callback - either from duplex or separate microphone
#ifdef USE_I2S_AUDIO_DUPLEX
  if (this->duplex_ != nullptr) {
    ESP_LOGI(TAG, "Using DUPLEX mode - registering callback");
    this->duplex_->add_mic_data_callback([this](const std::vector<uint8_t> &data) {
      this->on_microphone_data_(data);
    });
  } else
#endif
#ifdef USE_MICROPHONE
  if (this->microphone_ != nullptr) {
    ESP_LOGI(TAG, "Using SEPARATE mode - registering mic callback");
    this->microphone_->add_data_callback([this](const std::vector<uint8_t> &data) {
      this->on_microphone_data_(data);
    });
  } else
#endif
  {
    ESP_LOGW(TAG, "No audio source configured!");
  }

  ESP_LOGI(TAG, "Intercom Audio ready, listen port: %d", this->listen_port_);
}

void IntercomAudio::dump_config() {
  ESP_LOGCONFIG(TAG, "Intercom Audio:");
  ESP_LOGCONFIG(TAG, "  Listen Port: %d", this->listen_port_);
  if (!this->remote_ip_.empty()) {
    ESP_LOGCONFIG(TAG, "  Remote IP: %s", this->remote_ip_.c_str());
    ESP_LOGCONFIG(TAG, "  Remote Port: %d", this->remote_port_);
  }
  ESP_LOGCONFIG(TAG, "  Buffer Size: %zu bytes", this->buffer_size_);
  ESP_LOGCONFIG(TAG, "  Prebuffer Size: %zu bytes", this->prebuffer_size_);

#ifdef USE_I2S_AUDIO_DUPLEX
  if (this->duplex_ != nullptr) {
    ESP_LOGCONFIG(TAG, "  Mode: FULL DUPLEX (i2s_audio_duplex)");
  } else
#endif
  {
    // Determine mode based on configured components
    bool has_mic = false;
    bool has_spk = false;
#ifdef USE_MICROPHONE
    has_mic = (this->microphone_ != nullptr);
#endif
#ifdef USE_SPEAKER
    has_spk = (this->speaker_ != nullptr);
#endif

    if (has_mic && has_spk) {
      ESP_LOGCONFIG(TAG, "  Mode: FULL DUPLEX (separate mic+speaker)");
    } else if (has_mic) {
      ESP_LOGCONFIG(TAG, "  Mode: TX ONLY (microphone -> network)");
    } else if (has_spk) {
      ESP_LOGCONFIG(TAG, "  Mode: RX ONLY (network -> speaker)");
    } else {
      ESP_LOGCONFIG(TAG, "  Mode: NO AUDIO CONFIGURED");
    }

#ifdef USE_MICROPHONE
    if (this->microphone_ != nullptr) {
      ESP_LOGCONFIG(TAG, "  Microphone: configured");
    }
#endif
#ifdef USE_SPEAKER
    if (this->speaker_ != nullptr) {
      ESP_LOGCONFIG(TAG, "  Speaker: configured");
    }
#endif
  }

  if (this->aec_ != nullptr) {
    ESP_LOGCONFIG(TAG, "  AEC: configured");
  }
}

void IntercomAudio::loop() {
  // Handle state transitions in main loop
  switch (this->state_) {
    case StreamState::STARTING:
      if (this->task_running_) {
        this->state_ = StreamState::STREAMING;
        this->start_trigger_.trigger();
        ESP_LOGI(TAG, "Streaming started");
      }
      break;

    case StreamState::STOPPING:
      if (!this->task_running_) {
        this->state_ = StreamState::IDLE;
        this->stop_trigger_.trigger();
        ESP_LOGI(TAG, "Streaming stopped");
      }
      break;

    default:
      break;
  }
}

void IntercomAudio::start() {
  // Use getters to evaluate lambda if configured
  this->start(this->get_remote_ip(), this->get_remote_port());
}

void IntercomAudio::start(const std::string &remote_ip, uint16_t remote_port) {
  if (this->state_ != StreamState::IDLE) {
    ESP_LOGW(TAG, "Cannot start: not idle (state=%d)", (int)this->state_);
    return;
  }

  ESP_LOGI(TAG, "Starting stream to %s:%d", remote_ip.c_str(), remote_port);

  // Update remote address
  this->remote_ip_ = remote_ip;
  this->remote_port_ = remote_port;

  // Setup sockets
  if (!this->setup_sockets_()) {
    ESP_LOGE(TAG, "Failed to setup sockets");
    return;
  }

  // Reset metrics
  this->tx_packets_ = 0;
  this->rx_packets_ = 0;
  this->rx_buffer_->reset();

  // Start audio hardware
#ifdef USE_I2S_AUDIO_DUPLEX
  if (this->duplex_ != nullptr) {
    // DUPLEX mode: start the duplex component which handles both mic and speaker
    ESP_LOGI(TAG, "Starting duplex audio...");
    this->duplex_->start();
  } else
#endif
  {
    // SEPARATE mode: start mic and speaker components
#ifdef USE_MICROPHONE
    if (this->microphone_ != nullptr) {
      ESP_LOGI(TAG, "Starting microphone...");
      this->microphone_->start();
    }
#endif
#ifdef USE_SPEAKER
    if (this->speaker_ != nullptr) {
      ESP_LOGI(TAG, "Starting speaker...");
      // Set audio format: 16-bit, mono, 16kHz (matching our UDP stream)
      audio::AudioStreamInfo stream_info(16, 1, SAMPLE_RATE);
      this->speaker_->set_audio_stream_info(stream_info);
      this->speaker_->start();
    }
#endif
  }

  // Create audio task on core 1
  this->state_ = StreamState::STARTING;
  xTaskCreatePinnedToCore(
      audio_task,
      "intercom_audio",
      4096,
      this,
      10,  // High priority
      &this->audio_task_handle_,
      1   // Core 1
  );

#ifdef USE_ESP_AEC
  // Create AEC task with larger stack (8KB for ESP-SR processing)
  if (this->aec_ != nullptr && this->mic_input_buffer_ != nullptr) {
    ESP_LOGI(TAG, "Starting AEC task...");
    xTaskCreatePinnedToCore(
        aec_task,
        "aec_task",
        8192,  // 8KB stack for AEC processing
        this,
        9,     // Slightly lower priority than audio task
        &this->aec_task_handle_,
        1      // Core 1
    );
  }
#endif
}

void IntercomAudio::stop() {
  if (this->state_ != StreamState::STREAMING) {
    return;
  }

  ESP_LOGI(TAG, "Stopping stream");
  this->state_ = StreamState::STOPPING;

  // Close sockets FIRST to unblock any I/O operations in tasks
  this->close_sockets_();

  static const uint32_t STOP_TIMEOUT_MS = 1500;

#ifdef USE_ESP_AEC
  // Wait for AEC task to exit (with timeout)
  if (this->aec_task_handle_ != nullptr) {
    uint32_t start = millis();
    while (this->aec_task_running_ && (millis() - start) < STOP_TIMEOUT_MS) {
      vTaskDelay(pdMS_TO_TICKS(10));
    }
    if (this->aec_task_running_) {
      ESP_LOGW(TAG, "AEC task did not stop in time");
    }
    this->aec_task_handle_ = nullptr;
  }
#endif

  // Wait for audio task to exit (with timeout)
  if (this->audio_task_handle_ != nullptr) {
    uint32_t start = millis();
    while (this->task_running_ && (millis() - start) < STOP_TIMEOUT_MS) {
      vTaskDelay(pdMS_TO_TICKS(10));
    }
    if (this->task_running_) {
      ESP_LOGW(TAG, "Audio task did not stop in time");
    }
    this->audio_task_handle_ = nullptr;
  }

  // NOW stop audio hardware (after tasks have exited)
#ifdef USE_I2S_AUDIO_DUPLEX
  if (this->duplex_ != nullptr) {
    this->duplex_->stop();
  }
#endif
#ifdef USE_MICROPHONE
  if (this->microphone_ != nullptr) {
    this->microphone_->stop();
  }
#endif
#ifdef USE_SPEAKER
  if (this->speaker_ != nullptr) {
    this->speaker_->stop();
  }
#endif
}

bool IntercomAudio::setup_sockets_() {
  // Create RX socket (receive from remote)
  this->rx_socket_ = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
  if (this->rx_socket_ < 0) {
    ESP_LOGE(TAG, "Failed to create RX socket: %d", errno);
    return false;
  }

  // Socket options for RX
  int reuse = 1;
  setsockopt(this->rx_socket_, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
  int rcvbuf = 16384;  // 16KB receive buffer
  setsockopt(this->rx_socket_, SOL_SOCKET, SO_RCVBUF, &rcvbuf, sizeof(rcvbuf));

  // Bind to listen port
  struct sockaddr_in bind_addr;
  memset(&bind_addr, 0, sizeof(bind_addr));
  bind_addr.sin_family = AF_INET;
  bind_addr.sin_addr.s_addr = INADDR_ANY;
  bind_addr.sin_port = htons(this->listen_port_);

  if (bind(this->rx_socket_, (struct sockaddr *)&bind_addr, sizeof(bind_addr)) < 0) {
    ESP_LOGE(TAG, "Failed to bind on port %d: %d", this->listen_port_, errno);
    close(this->rx_socket_);
    this->rx_socket_ = -1;
    return false;
  }

  // Set non-blocking
  int flags = fcntl(this->rx_socket_, F_GETFL, 0);
  fcntl(this->rx_socket_, F_SETFL, flags | O_NONBLOCK);

  // Create TX socket (send to remote)
  this->tx_socket_ = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
  if (this->tx_socket_ < 0) {
    ESP_LOGE(TAG, "Failed to create TX socket: %d", errno);
    close(this->rx_socket_);
    this->rx_socket_ = -1;
    return false;
  }

  // Socket options for TX
  int sndbuf = 16384;  // 16KB send buffer
  setsockopt(this->tx_socket_, SOL_SOCKET, SO_SNDBUF, &sndbuf, sizeof(sndbuf));

  // Validate and setup remote address
  if (this->remote_ip_.empty()) {
    ESP_LOGE(TAG, "Remote IP is empty");
    close(this->rx_socket_);
    close(this->tx_socket_);
    this->rx_socket_ = -1;
    this->tx_socket_ = -1;
    return false;
  }

  memset(&this->remote_addr_, 0, sizeof(this->remote_addr_));
  this->remote_addr_.sin_family = AF_INET;
  this->remote_addr_.sin_port = htons(this->remote_port_);

  if (inet_pton(AF_INET, this->remote_ip_.c_str(), &this->remote_addr_.sin_addr) <= 0) {
    ESP_LOGE(TAG, "Invalid remote IP address: %s", this->remote_ip_.c_str());
    close(this->rx_socket_);
    close(this->tx_socket_);
    this->rx_socket_ = -1;
    this->tx_socket_ = -1;
    return false;
  }

  ESP_LOGI(TAG, "Sockets ready: RX on :%d, TX to %s:%d",
           this->listen_port_, this->remote_ip_.c_str(), this->remote_port_);

  return true;
}

void IntercomAudio::close_sockets_() {
  if (this->rx_socket_ >= 0) {
    close(this->rx_socket_);
    this->rx_socket_ = -1;
  }
  if (this->tx_socket_ >= 0) {
    close(this->tx_socket_);
    this->tx_socket_ = -1;
  }
}

void IntercomAudio::on_microphone_data_(const std::vector<uint8_t> &data) {
  if (this->state_ != StreamState::STREAMING) {
    return;
  }

  if (data.empty() || this->tx_socket_ < 0) {
    return;
  }

  // Check if we need to convert 32-bit to 16-bit
  size_t expected_16bit_size = FRAME_BYTES;  // 512 bytes for 256 samples

  int16_t *mic_samples = nullptr;
  size_t num_samples = 0;
  static std::vector<int16_t> converted_buffer;

  if (data.size() == expected_16bit_size * 2) {
    // 32-bit data detected, convert to 16-bit
    num_samples = data.size() / sizeof(int32_t);
    converted_buffer.resize(num_samples);

    const int32_t *src = reinterpret_cast<const int32_t *>(data.data());

    // Calculate DC offset if enabled (for mics with significant DC bias like SPH0645)
    int32_t dc_offset = 0;
    if (this->dc_offset_removal_) {
      int64_t sum = 0;
      for (size_t i = 0; i < num_samples; i++) {
        sum += src[i] >> 16;
      }
      dc_offset = static_cast<int32_t>(sum / num_samples);
    }

    // Convert 32-bit to 16-bit, optionally removing DC offset, then apply gain
    for (size_t i = 0; i < num_samples; i++) {
      int32_t sample = ((src[i] >> 16) - dc_offset) * this->mic_gain_;
      if (sample > 32767) sample = 32767;
      if (sample < -32768) sample = -32768;
      converted_buffer[i] = static_cast<int16_t>(sample);
    }
    mic_samples = converted_buffer.data();
  } else {
    // Already 16-bit - apply gain if not 1
    num_samples = data.size() / sizeof(int16_t);
    if (this->mic_gain_ != 1) {
      converted_buffer.resize(num_samples);
      const int16_t *src = reinterpret_cast<const int16_t *>(data.data());
      for (size_t i = 0; i < num_samples; i++) {
        int32_t sample = static_cast<int32_t>(src[i]) * this->mic_gain_;
        if (sample > 32767) sample = 32767;
        if (sample < -32768) sample = -32768;
        converted_buffer[i] = static_cast<int16_t>(sample);
      }
      mic_samples = converted_buffer.data();
    } else {
      mic_samples = (int16_t *)data.data();
    }
  }

  // Always buffer mic data - TX happens in audio_task_ (or aec_task_ if AEC enabled)
  if (this->mic_input_buffer_ != nullptr) {
    size_t bytes = num_samples * sizeof(int16_t);
    size_t written = this->mic_input_buffer_->write((void *)mic_samples, bytes);
    if (written < bytes) {
      this->tx_drops_++;  // Buffer overrun
    }
  }
}

bool IntercomAudio::send_audio_(const uint8_t *data, size_t bytes) {
  if (this->tx_socket_ < 0 || this->remote_ip_.empty()) {
    return false;
  }

  ssize_t sent = sendto(
      this->tx_socket_,
      data,
      bytes,
      0,
      (struct sockaddr *)&this->remote_addr_,
      sizeof(this->remote_addr_)
  );

  if (sent > 0) {
    this->tx_packets_++;
    return true;
  }

  return false;
}

size_t IntercomAudio::receive_audio_(int16_t *buffer, size_t max_samples) {
  if (this->rx_socket_ < 0) {
    return 0;
  }

  struct sockaddr_in sender_addr;
  socklen_t sender_len = sizeof(sender_addr);

  ssize_t received = recvfrom(
      this->rx_socket_,
      buffer,
      max_samples * sizeof(int16_t),
      0,
      (struct sockaddr *)&sender_addr,
      &sender_len
  );

  if (received > 0) {
    this->rx_packets_++;
    return received / sizeof(int16_t);
  }

  return 0;
}

void IntercomAudio::audio_task(void *param) {
  IntercomAudio *self = static_cast<IntercomAudio *>(param);
  self->audio_task_();
  vTaskDelete(nullptr);
}

void IntercomAudio::audio_task_() {
  ESP_LOGI(TAG, "Audio task started");
  this->task_running_ = true;

  // Allocate frame buffers (use larger buffer to handle variable frame sizes)
  static const size_t MAX_FRAME_BYTES = 1024;  // Up to 512 samples
  int16_t *rx_frame = (int16_t *)heap_caps_malloc(MAX_FRAME_BYTES, MALLOC_CAP_INTERNAL);
  int16_t *tx_frame = (int16_t *)heap_caps_malloc(FRAME_BYTES, MALLOC_CAP_INTERNAL);
  if (rx_frame == nullptr || tx_frame == nullptr) {
    ESP_LOGE(TAG, "Failed to allocate frame buffers");
    if (rx_frame) heap_caps_free(rx_frame);
    if (tx_frame) heap_caps_free(tx_frame);
    this->task_running_ = false;
    return;
  }

  bool prebuffered = false;
  uint32_t loop_count = 0;
  uint32_t play_count = 0;

  // Check if AEC is handling TX
  bool aec_handles_tx = false;
#ifdef USE_ESP_AEC
  aec_handles_tx = (this->aec_ != nullptr && this->aec_enabled_);
#endif

  while (this->state_ == StreamState::STARTING || this->state_ == StreamState::STREAMING) {
    loop_count++;

    // Receive UDP audio into ring buffer (handle variable frame sizes)
    size_t samples = this->receive_audio_(rx_frame, MAX_FRAME_BYTES / sizeof(int16_t));
    if (samples > 0) {
      size_t bytes = samples * sizeof(int16_t);
      size_t written = this->rx_buffer_->write(rx_frame, bytes);
      if (written < bytes) {
        this->rx_drops_++;  // Buffer overrun
      }
      if (this->rx_packets_ % 500 == 1) {
        ESP_LOGD(TAG, "RX: %d pkts, drops: %d, buf: %zu",
                 this->rx_packets_, this->rx_drops_, this->rx_buffer_->available());
      }
    }

    // TX: read from mic buffer and send (if AEC is not handling it)
    if (!aec_handles_tx && this->mic_input_buffer_ != nullptr) {
      if (this->mic_input_buffer_->available() >= FRAME_BYTES) {
        this->mic_input_buffer_->read(tx_frame, FRAME_BYTES, 0);
        this->send_audio_(reinterpret_cast<uint8_t *>(tx_frame), FRAME_BYTES);
        if (this->tx_packets_ % 500 == 1) {
          ESP_LOGD(TAG, "TX: %d pkts, drops: %d", this->tx_packets_, this->tx_drops_);
        }
      }
    }

    // Wait for prebuffer before sending to speaker
    if (!prebuffered) {
      if (this->rx_buffer_->available() >= this->prebuffer_size_) {
        prebuffered = true;
        ESP_LOGI(TAG, "Prebuffer filled (%zu bytes), starting playback", this->rx_buffer_->available());
      } else {
        vTaskDelay(pdMS_TO_TICKS(5));
        continue;
      }
    }

    // Read from ring buffer and send to speaker (using either duplex or separate speaker)
    if (this->rx_buffer_->available() >= FRAME_BYTES) {
      size_t read = this->rx_buffer_->read(rx_frame, FRAME_BYTES, pdMS_TO_TICKS(10));
      if (read > 0) {
        play_count++;
        // Store speaker data as reference for AEC (before sending to speaker)
        if (this->speaker_ref_buffer_ != nullptr) {
          this->speaker_ref_buffer_->write((void *)rx_frame, read);
        }

#ifdef USE_I2S_AUDIO_DUPLEX
        if (this->duplex_ != nullptr) {
          // DUPLEX mode: use duplex component's play method
          this->duplex_->play((uint8_t *)rx_frame, read, pdMS_TO_TICKS(50));
        }
#endif
#ifdef USE_SPEAKER
#ifdef USE_I2S_AUDIO_DUPLEX
        else
#endif
        if (this->speaker_ != nullptr) {
          // SEPARATE mode: use speaker component
          size_t written = this->speaker_->play((uint8_t *)rx_frame, read, pdMS_TO_TICKS(50));
          if (play_count % 500 == 1) {
            ESP_LOGD(TAG, "SPK: %zu/%zu bytes, run=%d", written, read, this->speaker_->is_running());
          }
        }
#endif
      }
    }

    // Small delay to prevent tight loop
    vTaskDelay(pdMS_TO_TICKS(1));
  }

  heap_caps_free(rx_frame);
  heap_caps_free(tx_frame);
  this->task_running_ = false;
}

size_t IntercomAudio::get_buffer_fill() const {
  if (this->rx_buffer_) {
    return this->rx_buffer_->available();
  }
  return 0;
}

void IntercomAudio::set_volume(float volume) {
#ifdef USE_SPEAKER
  if (this->speaker_ != nullptr) {
    this->speaker_->set_volume(volume);
  }
#endif
}

float IntercomAudio::get_volume() const {
#ifdef USE_SPEAKER
  if (this->speaker_ != nullptr) {
    return this->speaker_->get_volume();
  }
#endif
  return 0.0f;
}

#ifdef USE_ESP_AEC
void IntercomAudio::aec_task(void *param) {
  IntercomAudio *self = static_cast<IntercomAudio *>(param);
  self->aec_task_();
  vTaskDelete(nullptr);
}

void IntercomAudio::aec_task_() {
  ESP_LOGI(TAG, "AEC task started");
  this->aec_task_running_ = true;

  // Allocate processing buffers
  int16_t *mic_frame = (int16_t *)heap_caps_malloc(FRAME_BYTES, MALLOC_CAP_INTERNAL);
  int16_t *ref_frame = (int16_t *)heap_caps_malloc(FRAME_BYTES, MALLOC_CAP_INTERNAL);
  int16_t *out_frame = (int16_t *)heap_caps_malloc(FRAME_BYTES, MALLOC_CAP_INTERNAL);

  if (!mic_frame || !ref_frame || !out_frame) {
    ESP_LOGE(TAG, "Failed to allocate AEC frames");
    this->aec_task_running_ = false;
    if (mic_frame) heap_caps_free(mic_frame);
    if (ref_frame) heap_caps_free(ref_frame);
    if (out_frame) heap_caps_free(out_frame);
    return;
  }

  uint32_t frame_count = 0;

  while (this->state_ == StreamState::STARTING || this->state_ == StreamState::STREAMING) {
    // Check if AEC is enabled and buffers are ready
    if (!this->aec_enabled_ || this->aec_ == nullptr || !this->aec_->is_initialized() ||
        this->mic_input_buffer_ == nullptr) {
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }

    // Wait for enough mic data
    if (this->mic_input_buffer_->available() < FRAME_BYTES) {
      vTaskDelay(pdMS_TO_TICKS(1));
      continue;
    }

    // Read mic data
    this->mic_input_buffer_->read(mic_frame, FRAME_BYTES, 0);

    // Get speaker reference
    if (this->speaker_ref_buffer_ != nullptr && this->speaker_ref_buffer_->available() >= FRAME_BYTES) {
      this->speaker_ref_buffer_->read(ref_frame, FRAME_BYTES, 0);
    } else {
      memset(ref_frame, 0, FRAME_BYTES);
    }

    // Process AEC
    this->aec_->process(mic_frame, ref_frame, out_frame, FRAME_SAMPLES);

    // Send processed audio
    this->send_audio_(reinterpret_cast<uint8_t *>(out_frame), FRAME_BYTES);

    frame_count++;
    if (frame_count % 500 == 0) {
      ESP_LOGD(TAG, "AEC: %d frames, mic_buf=%zu, ref_buf=%zu",
               frame_count, this->mic_input_buffer_->available(),
               this->speaker_ref_buffer_ ? this->speaker_ref_buffer_->available() : 0);
    }
  }

  heap_caps_free(mic_frame);
  heap_caps_free(ref_frame);
  heap_caps_free(out_frame);
  this->aec_task_running_ = false;
  ESP_LOGI(TAG, "AEC task stopped");
}
#endif  // USE_ESP_AEC

}  // namespace intercom_audio
}  // namespace esphome

#endif  // USE_ESP32
