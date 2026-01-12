#include "i2s_audio_duplex.h"

#ifdef USE_ESP32

#include "esphome/core/log.h"
#include "esphome/core/application.h"

#ifdef USE_ESP_AEC
#include "../esp_aec/esp_aec.h"
#endif

namespace esphome {
namespace i2s_audio_duplex {

static const char *const TAG = "i2s_audio_duplex";

// Audio parameters
static const size_t DMA_BUFFER_COUNT = 8;
static const size_t DMA_BUFFER_SIZE = 512;
static const size_t FRAME_SIZE = 256;  // samples per frame
static const size_t FRAME_BYTES = FRAME_SIZE * sizeof(int16_t);
static const size_t SPEAKER_BUFFER_SIZE = 8192;

// I2S new driver uses milliseconds directly, NOT FreeRTOS ticks
static const uint32_t I2S_IO_TIMEOUT_MS = 50;

void I2SAudioDuplex::setup() {
  ESP_LOGCONFIG(TAG, "Setting up I2S Audio Duplex...");

  // Create speaker ring buffer
  this->speaker_buffer_ = RingBuffer::create(SPEAKER_BUFFER_SIZE);
  if (!this->speaker_buffer_) {
    ESP_LOGE(TAG, "Failed to create speaker ring buffer");
    this->mark_failed();
    return;
  }

  // Note: speaker_ref_buffer_ for AEC is created in set_aec() which is called after setup()

  ESP_LOGI(TAG, "I2S Audio Duplex ready");
}

void I2SAudioDuplex::set_aec(esp_aec::EspAec *aec) {
  this->aec_ = aec;
  // Create speaker reference buffer for AEC now (since set_aec is called after setup)
  if (aec != nullptr && !this->speaker_ref_buffer_) {
    this->speaker_ref_buffer_ = RingBuffer::create(SPEAKER_BUFFER_SIZE);
    if (this->speaker_ref_buffer_) {
      ESP_LOGI(TAG, "AEC speaker reference buffer created");
    } else {
      ESP_LOGE(TAG, "Failed to create AEC speaker reference buffer");
    }
  }
}

void I2SAudioDuplex::dump_config() {
  ESP_LOGCONFIG(TAG, "I2S Audio Duplex:");
  ESP_LOGCONFIG(TAG, "  LRCLK Pin: %d", this->lrclk_pin_);
  ESP_LOGCONFIG(TAG, "  BCLK Pin: %d", this->bclk_pin_);
  ESP_LOGCONFIG(TAG, "  MCLK Pin: %d", this->mclk_pin_);
  ESP_LOGCONFIG(TAG, "  DIN Pin: %d", this->din_pin_);
  ESP_LOGCONFIG(TAG, "  DOUT Pin: %d", this->dout_pin_);
  ESP_LOGCONFIG(TAG, "  Sample Rate: %d Hz", this->sample_rate_);
  ESP_LOGCONFIG(TAG, "  AEC: %s", this->aec_ != nullptr ? "enabled" : "disabled");
}

void I2SAudioDuplex::loop() {
  // Main loop - nothing needed, audio runs in separate task
}

bool I2SAudioDuplex::init_i2s_duplex_() {
  ESP_LOGD(TAG, "Initializing I2S in DUPLEX mode...");

  bool need_tx = (this->dout_pin_ >= 0);
  bool need_rx = (this->din_pin_ >= 0);

  if (!need_tx && !need_rx) {
    ESP_LOGE(TAG, "At least one of din_pin or dout_pin must be configured");
    return false;
  }

  // Channel configuration
  i2s_chan_config_t chan_cfg = {
      .id = I2S_NUM_0,
      .role = I2S_ROLE_MASTER,
      .dma_desc_num = DMA_BUFFER_COUNT,
      .dma_frame_num = DMA_BUFFER_SIZE,
      .auto_clear_after_cb = true,
      .auto_clear_before_cb = false,
      .intr_priority = 0,
  };

  // KEY FOR DUPLEX: Pass BOTH tx and rx pointers to create both channels at once
  i2s_chan_handle_t *tx_ptr = need_tx ? &this->tx_handle_ : nullptr;
  i2s_chan_handle_t *rx_ptr = need_rx ? &this->rx_handle_ : nullptr;

  esp_err_t err = i2s_new_channel(&chan_cfg, tx_ptr, rx_ptr);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "Failed to create I2S channel: %s", esp_err_to_name(err));
    return false;
  }

  ESP_LOGD(TAG, "I2S channel created: TX=%s RX=%s",
           this->tx_handle_ ? "yes" : "no",
           this->rx_handle_ ? "yes" : "no");

  // Helper to convert pin number to gpio_num_t, using GPIO_NUM_NC for unused pins
  auto pin_or_nc = [](int pin) -> gpio_num_t {
    return pin >= 0 ? (gpio_num_t) pin : GPIO_NUM_NC;
  };

  // Standard mode configuration
  i2s_std_config_t std_cfg = {
      .clk_cfg = {
          .sample_rate_hz = this->sample_rate_,
          .clk_src = I2S_CLK_SRC_DEFAULT,
          .mclk_multiple = I2S_MCLK_MULTIPLE_256,
      },
      .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
      .gpio_cfg = {
          .mclk = pin_or_nc(this->mclk_pin_),
          .bclk = pin_or_nc(this->bclk_pin_),
          .ws = pin_or_nc(this->lrclk_pin_),
          .dout = pin_or_nc(this->dout_pin_),
          .din = pin_or_nc(this->din_pin_),
          .invert_flags = {
              .mclk_inv = false,
              .bclk_inv = false,
              .ws_inv = false,
          },
      },
  };

  // Set slot mask to left channel
  std_cfg.slot_cfg.slot_mask = I2S_STD_SLOT_LEFT;

  // Initialize TX channel if available
  if (this->tx_handle_) {
    err = i2s_channel_init_std_mode(this->tx_handle_, &std_cfg);
    if (err != ESP_OK) {
      ESP_LOGE(TAG, "Failed to init TX channel: %s", esp_err_to_name(err));
      this->deinit_i2s_();
      return false;
    }
    ESP_LOGD(TAG, "TX channel initialized");
  }

  // Initialize RX channel if available
  if (this->rx_handle_) {
    err = i2s_channel_init_std_mode(this->rx_handle_, &std_cfg);
    if (err != ESP_OK) {
      ESP_LOGE(TAG, "Failed to init RX channel: %s", esp_err_to_name(err));
      this->deinit_i2s_();
      return false;
    }
    ESP_LOGD(TAG, "RX channel initialized");
  }

  // Enable channels with error checking
  if (this->tx_handle_) {
    err = i2s_channel_enable(this->tx_handle_);
    if (err != ESP_OK) {
      ESP_LOGE(TAG, "Failed to enable TX channel: %s", esp_err_to_name(err));
      this->deinit_i2s_();
      return false;
    }
  }
  if (this->rx_handle_) {
    err = i2s_channel_enable(this->rx_handle_);
    if (err != ESP_OK) {
      ESP_LOGE(TAG, "Failed to enable RX channel: %s", esp_err_to_name(err));
      this->deinit_i2s_();
      return false;
    }
  }

  ESP_LOGI(TAG, "I2S DUPLEX initialized successfully");
  return true;
}

void I2SAudioDuplex::deinit_i2s_() {
  // Used for cleanup during init errors; stop() handles normal shutdown
  if (this->tx_handle_) {
    i2s_channel_disable(this->tx_handle_);
    i2s_del_channel(this->tx_handle_);
    this->tx_handle_ = nullptr;
  }
  if (this->rx_handle_) {
    i2s_channel_disable(this->rx_handle_);
    i2s_del_channel(this->rx_handle_);
    this->rx_handle_ = nullptr;
  }
  ESP_LOGD(TAG, "I2S deinitialized");
}

void I2SAudioDuplex::start() {
  if (this->duplex_running_) {
    ESP_LOGW(TAG, "Already running");
    return;
  }

  // Small delay to ensure I2S is fully deinitialized from previous session
  vTaskDelay(pdMS_TO_TICKS(50));

  ESP_LOGI(TAG, "Starting duplex audio...");

  if (!this->init_i2s_duplex_()) {
    ESP_LOGE(TAG, "Failed to initialize I2S");
    return;
  }

  this->duplex_running_ = true;
  this->mic_running_ = (this->rx_handle_ != nullptr);
  this->speaker_running_ = (this->tx_handle_ != nullptr);

  // Reset debug counters
  this->aec_frame_count_ = 0;

  // Clear speaker buffer
  this->speaker_buffer_->reset();

  // Create audio task on core 1
  xTaskCreatePinnedToCore(
      audio_task,
      "i2s_duplex",
      8192,
      this,
      9,  // Priority below WiFi/BLE (typically 18), above normal tasks
      &this->audio_task_handle_,
      1    // Core 1
  );

  ESP_LOGI(TAG, "Duplex audio started");
}

void I2SAudioDuplex::stop() {
  if (!this->duplex_running_) {
    return;
  }

  ESP_LOGI(TAG, "Stopping duplex audio...");
  this->duplex_running_ = false;

  // Disable channels FIRST to unblock any pending read/write operations
  esp_err_t err;
  if (this->tx_handle_) {
    err = i2s_channel_disable(this->tx_handle_);
    if (err != ESP_OK) {
      ESP_LOGW(TAG, "TX channel disable failed: %s", esp_err_to_name(err));
    }
  }
  if (this->rx_handle_) {
    err = i2s_channel_disable(this->rx_handle_);
    if (err != ESP_OK) {
      ESP_LOGW(TAG, "RX channel disable failed: %s", esp_err_to_name(err));
    }
  }

  // Now wait for task to finish (should exit quickly since channels are disabled)
  if (this->audio_task_handle_) {
    int wait_count = 0;
    while (eTaskGetState(this->audio_task_handle_) != eDeleted && wait_count < 100) {
      vTaskDelay(pdMS_TO_TICKS(10));
      wait_count++;
    }
    this->audio_task_handle_ = nullptr;
  }

  // Delete channels (already disabled)
  if (this->tx_handle_) {
    i2s_del_channel(this->tx_handle_);
    this->tx_handle_ = nullptr;
  }
  if (this->rx_handle_) {
    i2s_del_channel(this->rx_handle_);
    this->rx_handle_ = nullptr;
  }

  this->mic_running_ = false;
  this->speaker_running_ = false;

  // Small delay to let I2S fully deinitialize
  vTaskDelay(pdMS_TO_TICKS(30));

  ESP_LOGI(TAG, "Duplex audio stopped");
}

void I2SAudioDuplex::start_mic() {
  if (!this->duplex_running_) {
    this->start();
  }
}

void I2SAudioDuplex::stop_mic() {
  // In duplex mode, stopping mic stops everything
  this->stop();
}

void I2SAudioDuplex::start_speaker() {
  if (!this->duplex_running_) {
    this->start();
  }
}

void I2SAudioDuplex::stop_speaker() {
  // In duplex mode, stopping speaker stops everything
  this->stop();
}

size_t I2SAudioDuplex::play(const uint8_t *data, size_t len, TickType_t ticks_to_wait) {
  if (!this->speaker_buffer_) {
    return 0;
  }

  // Store speaker data as reference for AEC (non-blocking, best effort)
  if (this->speaker_ref_buffer_ != nullptr) {
    this->speaker_ref_buffer_->write_without_replacement((void *) data, len, 0, true);
  }

  // Use write_without_replacement which properly supports timeout
  // This avoids the non-thread-safe free() call in regular write()
  // Note: RingBuffer timeout is in FreeRTOS ticks (NOT milliseconds)
  return this->speaker_buffer_->write_without_replacement((void *) data, len, ticks_to_wait, true);
}

void I2SAudioDuplex::audio_task(void *param) {
  I2SAudioDuplex *self = static_cast<I2SAudioDuplex *>(param);
  self->audio_task_();
  vTaskDelete(nullptr);
}

void I2SAudioDuplex::audio_task_() {
  ESP_LOGI(TAG, "Audio task started");

  // Allocate DMA-capable buffers for I2S operations
  int16_t *mic_buffer = (int16_t *) heap_caps_malloc(FRAME_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
  int16_t *spk_buffer = (int16_t *) heap_caps_malloc(FRAME_BYTES, MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA);
  int16_t *spk_ref_buffer = nullptr;  // Speaker reference for AEC
  int16_t *aec_output = nullptr;      // AEC processed output

#ifdef USE_ESP_AEC
  if (this->aec_ != nullptr) {
    spk_ref_buffer = (int16_t *) heap_caps_malloc(FRAME_BYTES, MALLOC_CAP_INTERNAL);
    aec_output = (int16_t *) heap_caps_malloc(FRAME_BYTES, MALLOC_CAP_INTERNAL);
  }
#endif

  if (!mic_buffer || !spk_buffer) {
    ESP_LOGE(TAG, "Failed to allocate audio buffers");
    if (mic_buffer) heap_caps_free(mic_buffer);
    if (spk_buffer) heap_caps_free(spk_buffer);
    if (spk_ref_buffer) heap_caps_free(spk_ref_buffer);
    if (aec_output) heap_caps_free(aec_output);
    return;
  }

  size_t bytes_read, bytes_written;

  while (this->duplex_running_) {
    bool did_work = false;  // Track if we did useful I/O this iteration

    // ══════════════════════════════════════════════════════════════════
    // MICROPHONE READ (RX)
    // ══════════════════════════════════════════════════════════════════
    if (this->rx_handle_ && this->mic_running_) {
      // Note: i2s_channel_read timeout is in milliseconds (new driver), not ticks
      esp_err_t err = i2s_channel_read(this->rx_handle_, mic_buffer, FRAME_BYTES,
                                        &bytes_read, I2S_IO_TIMEOUT_MS);
      if (err != ESP_OK && err != ESP_ERR_TIMEOUT) {
        ESP_LOGW(TAG, "i2s_channel_read failed: %s", esp_err_to_name(err));
      }
      if (err == ESP_OK && bytes_read == FRAME_BYTES) {
        did_work = true;
        int16_t *output_buffer = mic_buffer;  // Default: no AEC processing

#ifdef USE_ESP_AEC
        // Process through AEC if enabled and initialized
        if (this->aec_ != nullptr && this->aec_enabled_ && this->aec_->is_initialized() &&
            spk_ref_buffer != nullptr && aec_output != nullptr) {
          // Get speaker reference (best effort, pad with silence if not enough data)
          // Avoid available() which is not thread-safe; read directly and pad
          if (this->speaker_ref_buffer_ != nullptr) {
            size_t got_ref = this->speaker_ref_buffer_->read((void *) spk_ref_buffer, FRAME_BYTES, 0);
            if (got_ref < FRAME_BYTES) {
              memset(((uint8_t *) spk_ref_buffer) + got_ref, 0, FRAME_BYTES - got_ref);
            }
          } else {
            memset(spk_ref_buffer, 0, FRAME_BYTES);
          }
          // Process AEC: removes echo from mic_buffer using spk_ref_buffer
          this->aec_->process(mic_buffer, spk_ref_buffer, aec_output, FRAME_SIZE);
          output_buffer = aec_output;
          if (++this->aec_frame_count_ % 500 == 0) {
            ESP_LOGD(TAG, "AEC processing: %lu frames", (unsigned long) this->aec_frame_count_);
          }
        } else {
          static bool aec_skip_logged = false;
          if (!aec_skip_logged) {
            ESP_LOGW(TAG, "AEC skipped: aec=%p enabled=%d init=%d ref=%p out=%p",
                     this->aec_, this->aec_enabled_,
                     this->aec_ ? this->aec_->is_initialized() : false,
                     spk_ref_buffer, aec_output);
            aec_skip_logged = true;
          }
        }
#endif

        // Apply mic gain
        if (this->mic_gain_ != 1.0f) {
          for (size_t i = 0; i < FRAME_SIZE; i++) {
            int32_t sample = (int32_t)(output_buffer[i] * this->mic_gain_);
            // Clamp to int16_t range
            if (sample > 32767) sample = 32767;
            if (sample < -32768) sample = -32768;
            output_buffer[i] = (int16_t) sample;
          }
        }

        // Call callbacks with zero-copy pointer (no vector allocation per frame)
        for (auto &callback : this->mic_callbacks_) {
          callback((const uint8_t *) output_buffer, FRAME_BYTES);
        }
      }
    }

    // ══════════════════════════════════════════════════════════════════
    // SPEAKER WRITE (TX)
    // Avoid available() which is not thread-safe; read directly and pad
    // ══════════════════════════════════════════════════════════════════
    if (this->tx_handle_ && this->speaker_running_) {
      // Read whatever is available (non-blocking), pad remainder with silence
      size_t got = this->speaker_buffer_->read((void *) spk_buffer, FRAME_BYTES, 0);
      if (got > 0) {
        did_work = true;  // Had actual audio data to play
      }
      if (got < FRAME_BYTES) {
        // Pad with silence to maintain frame alignment
        memset(((uint8_t *) spk_buffer) + got, 0, FRAME_BYTES - got);
      }

      // Apply speaker volume with clamp
      if (this->speaker_volume_ != 1.0f) {
        for (size_t i = 0; i < FRAME_SIZE; i++) {
          int32_t sample = (int32_t)(spk_buffer[i] * this->speaker_volume_);
          if (sample > 32767) sample = 32767;
          if (sample < -32768) sample = -32768;
          spk_buffer[i] = (int16_t) sample;
        }
      }

      // Note: i2s_channel_write timeout is in milliseconds (new driver), not ticks
      esp_err_t err = i2s_channel_write(this->tx_handle_, spk_buffer, FRAME_BYTES, &bytes_written, I2S_IO_TIMEOUT_MS);
      if (err != ESP_OK && err != ESP_ERR_TIMEOUT) {
        ESP_LOGW(TAG, "i2s_channel_write failed: %s", esp_err_to_name(err));
      }
    }

    // Smart yield: taskYIELD when working (minimal latency), delay when idle (save CPU)
    if (did_work) {
      taskYIELD();
    } else {
      vTaskDelay(pdMS_TO_TICKS(1));
    }
  }

  heap_caps_free(mic_buffer);
  heap_caps_free(spk_buffer);
  if (spk_ref_buffer) heap_caps_free(spk_ref_buffer);
  if (aec_output) heap_caps_free(aec_output);
  ESP_LOGI(TAG, "Audio task stopped");
}

}  // namespace i2s_audio_duplex
}  // namespace esphome

#endif  // USE_ESP32
