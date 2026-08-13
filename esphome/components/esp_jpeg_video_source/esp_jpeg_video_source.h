#pragma once

#include "esphome/core/defines.h"

#if defined(USE_ESP_IDF) && defined(USE_ESPHOME_VOIP_STACK_VIDEO) && \
    defined(USE_ESPHOME_VOIP_STACK_VIDEO_JPEG)

#include "esphome/components/esp_video_camera/esp_video_camera.h"
#include "esphome/components/voip_stack/video.h"
#include "esphome/core/component.h"

#include <atomic>
#include <cstdint>

#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

namespace esphome::esp_jpeg_video_source {

/// Bridges the P4 camera's already-encoded JPEG access units to voip_stack.
///
/// Capture and hardware encoding remain owned by ESPVideoCamera; RTP remains
/// owned by VideoRtpSession. The borrowed V4L2 payload is synchronously copied
/// into VideoRtpSession's bounded slot, with no extra frame allocation and no
/// ESPHome main-loop handoff.
class EspJpegVideoSource
    : public Component,
      public voip_stack::EncodedVideoSource,
      public esp_video_camera::JpegFrameConsumer {
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

  voip_stack::VideoCapability get_video_capability() const override;
  bool prepare_video(
      const voip_stack::VideoCapability &capability) override;
  bool start_video(
      voip_stack::EncodedVideoAccessUnitCallback callback, void *ctx,
      const voip_stack::VideoCapability &capability) override;
  void stop_video() override;

  void consume_jpeg_frame(
      const esp_video_camera::JpegFrame &frame) override;

 protected:
  esp_video_camera::ESPVideoCamera *camera_{nullptr};
  uint16_t width_{400};
  uint16_t height_{400};
  uint8_t framerate_{10};

  SemaphoreHandle_t control_mutex_{nullptr};
  StaticSemaphore_t control_mutex_storage_{};
  std::atomic<bool> active_{false};
  uint8_t negotiated_fps_{10};
  voip_stack::RtpFrameCadence90k cadence_{};
  voip_stack::EncodedVideoAccessUnitCallback callback_{nullptr};
  void *callback_ctx_{nullptr};

#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  std::atomic<uint32_t> frames_seen_{0};
  std::atomic<uint32_t> frames_published_{0};
  std::atomic<uint32_t> rate_drops_{0};
#endif
};

}  // namespace esphome::esp_jpeg_video_source

#endif
