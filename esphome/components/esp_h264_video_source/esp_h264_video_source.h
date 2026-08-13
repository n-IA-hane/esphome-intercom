#pragma once

#include "esphome/core/defines.h"

#if defined(USE_ESP_IDF) && defined(USE_ESPHOME_VOIP_STACK_VIDEO) && \
    defined(USE_ESPHOME_VOIP_STACK_VIDEO_H264)

#include "esphome/components/esp_video_camera/esp_video_camera.h"
#include "esphome/components/voip_stack/video.h"
#include "esphome/core/component.h"

#include "driver/ppa.h"
#include "esp_h264_enc_single_hw.h"

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <string>

#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

namespace esphome::esp_h264_video_source {

/// ESP32-P4 camera-to-H.264 adapter for voip_stack.
///
/// Camera ownership stays with ESPVideoCamera and SIP/RTP ownership stays with
/// voip_stack. The camera lends the P4 encoder's native optimized YUV420 frame;
/// this adapter applies the configured camera orientation and crop through PPA,
/// then publishes one complete Annex-B access unit.
class EspH264VideoSource
    : public Component,
      public voip_stack::EncodedVideoSource,
      public esp_video_camera::RawVideoFrameConsumer {
 public:
  void setup() override;
  void on_shutdown() override;
  void dump_config() override;
  float get_setup_priority() const override {
    return setup_priority::DATA - 1.0f;
  }

  void set_camera(esp_video_camera::ESPVideoCamera *camera) {
    this->camera_ = camera;
  }
  void set_width(uint16_t value) { this->width_ = value; }
  void set_height(uint16_t value) { this->height_ = value; }
  void set_framerate(uint8_t value) { this->framerate_ = value; }
  void set_bitrate(uint32_t value) { this->bitrate_ = value; }
  void set_gop(uint8_t value) { this->gop_ = value; }

  voip_stack::VideoCapability get_video_capability() const override;
  bool prepare_video(
      const voip_stack::VideoCapability &capability) override;
  bool start_video(
      voip_stack::EncodedVideoAccessUnitCallback callback, void *ctx,
      const voip_stack::VideoCapability &capability) override;
  void stop_video() override;
  void request_key_frame() override;

  void consume_raw_video_frame(
      const esp_video_camera::RawVideoFrame &frame) override;

 protected:
  // Espressif's hardware-encoder test sizes output to one tenth of the raw
  // frame (46,080 bytes at 640x480 YUV420). The largest measured AU is below
  // 20 KiB, so 64 KiB preserves bounded headroom while halving the cache range
  // the driver invalidates after every encoded frame.
  static constexpr size_t kEncodedBufferBytes = 64 * 1024;

  size_t yuv_bytes_() const {
    return static_cast<size_t>(this->width_) * this->height_ * 3 / 2;
  }
  bool init_ppa_();
  bool allocate_resources_();
  void free_resources_();
  bool init_encoder_and_probe_();
  bool restart_encoder_();
  void close_encoder_();
  bool set_encoder_gop_(uint8_t gop);
  bool set_encoder_bitrate_(uint32_t bitrate);
  bool transform_to_encoder_yuv_(
      const esp_video_camera::RawVideoFrame &frame, uint8_t *target);
  bool encode_frame_(const uint8_t *yuv, uint32_t timestamp_90khz,
                     bool publish, uint32_t generation = 0);
  bool start_tx_task_();
  bool stop_tx_task_();
  static void tx_task_trampoline_(void *ctx);
  void tx_task_();

  esp_video_camera::ESPVideoCamera *camera_{nullptr};
  uint16_t width_{400};
  uint16_t height_{400};
  uint8_t framerate_{10};
  uint32_t bitrate_{400000};
  std::atomic<uint32_t> active_bitrate_{400000};
  uint8_t gop_{30};

  ppa_client_handle_t ppa_{nullptr};
  esp_h264_enc_handle_t encoder_{nullptr};
  struct TxSlot {
    uint8_t *yuv{nullptr};
    std::atomic<uint8_t> state{0};
    uint32_t timestamp_90khz{0};
    uint32_t generation{0};
    uint32_t sequence{0};
  };
  TxSlot tx_slots_[2]{};
  uint8_t *tx_encoded_{nullptr};
  std::string profile_level_id_;
  std::atomic<bool> encoder_ready_{false};

  SemaphoreHandle_t control_mutex_{nullptr};
  StaticSemaphore_t control_mutex_storage_{};
  voip_stack::EncodedVideoAccessUnitCallback callback_{nullptr};
  void *callback_ctx_{nullptr};
  voip_stack::VideoCapability negotiated_capability_{};

  std::atomic<bool> tx_active_{false};
  std::atomic<uint32_t> tx_generation_{0};
  std::atomic<bool> tx_task_running_{false};
  TaskHandle_t tx_task_handle_{nullptr};
  StaticTask_t tx_task_tcb_{};
  StackType_t *tx_task_stack_{nullptr};
  bool tx_task_with_caps_{false};
  SemaphoreHandle_t tx_done_{nullptr};
  StaticSemaphore_t tx_done_storage_{};
  // The generation value makes a key-frame request immune to a previous
  // call's in-flight encoder operation consuming it during rapid redial.
  std::atomic<uint32_t> force_idr_generation_{0};
  std::atomic<uint32_t> requested_bitrate_{400000};
  voip_stack::RtpFrameCadence90k cadence_{};
  uint32_t next_tx_sequence_{0};
  std::atomic<uint32_t> raw_frames_{0};
  std::atomic<uint32_t> queued_frames_{0};
  std::atomic<uint32_t> queue_drops_{0};
  std::atomic<uint32_t> encoded_frames_{0};

#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  std::atomic<uint32_t> converted_frames_{0};
  std::atomic<uint32_t> conversion_max_us_{0};
  std::atomic<uint32_t> conversion_total_us_{0};
  std::atomic<uint32_t> encode_max_us_{0};
  std::atomic<uint32_t> encode_total_us_{0};
  std::atomic<uint32_t> encoded_max_bytes_{0};
#endif
};

}  // namespace esphome::esp_h264_video_source

#endif
