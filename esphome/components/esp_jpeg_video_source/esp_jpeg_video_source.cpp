#include "esp_jpeg_video_source.h"

#if defined(USE_ESP_IDF) && defined(USE_ESPHOME_VOIP_STACK_VIDEO) && \
    defined(USE_ESPHOME_VOIP_STACK_VIDEO_JPEG)

#include "esphome/core/log.h"

#include <algorithm>

namespace esphome::esp_jpeg_video_source {

static const char *const TAG = "esp_jpeg_video_source";

void EspJpegVideoSource::setup() {
  this->control_mutex_ =
      xSemaphoreCreateMutexStatic(&this->control_mutex_storage_);
  if (this->camera_ == nullptr || this->control_mutex_ == nullptr ||
      !this->camera_->register_jpeg_frame_consumer(this)) {
    ESP_LOGE(TAG, "P4 hardware JPEG source setup failed");
    this->mark_failed();
  }
}

void EspJpegVideoSource::on_shutdown() { this->stop_video(); }

void EspJpegVideoSource::dump_config() {
  ESP_LOGCONFIG(TAG, "P4 hardware JPEG source:");
  ESP_LOGCONFIG(TAG, "  Encode: %ux%u @ %u fps", this->width_,
                this->height_, this->framerate_);
}

voip_stack::VideoCapability
EspJpegVideoSource::get_video_capability() const {
  voip_stack::VideoCapability capability;
  capability.payload_type = 26;
  capability.encoding = "JPEG";
  capability.width = this->width_;
  capability.height = this->height_;
  capability.max_fps = this->framerate_;
  return capability;
}

bool EspJpegVideoSource::prepare_video(
    const voip_stack::VideoCapability &capability) {
  return !this->is_failed() && this->camera_ != nullptr &&
         !this->active_.load(std::memory_order_acquire) &&
         capability.valid() && capability.is_jpeg() &&
         capability.width == this->width_ &&
         capability.height == this->height_;
}

bool EspJpegVideoSource::start_video(
    voip_stack::EncodedVideoAccessUnitCallback callback, void *ctx,
    const voip_stack::VideoCapability &capability) {
  if (callback == nullptr || !this->prepare_video(capability))
    return false;

  xSemaphoreTake(this->control_mutex_, portMAX_DELAY);
  this->callback_ = callback;
  this->callback_ctx_ = ctx;
  this->negotiated_fps_ = capability.max_fps == 0
                              ? this->framerate_
                              : std::min(this->framerate_, capability.max_fps);
  this->cadence_.reset(this->negotiated_fps_);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  this->frames_seen_.store(0, std::memory_order_release);
  this->frames_published_.store(0, std::memory_order_release);
  this->rate_drops_.store(0, std::memory_order_release);
#endif
  this->active_.store(true, std::memory_order_release);
  xSemaphoreGive(this->control_mutex_);

  if (!this->camera_->start_jpeg_frame_consumer(this)) {
    xSemaphoreTake(this->control_mutex_, portMAX_DELAY);
    this->active_.store(false, std::memory_order_release);
    this->callback_ = nullptr;
    this->callback_ctx_ = nullptr;
    xSemaphoreGive(this->control_mutex_);
    return false;
  }
  ESP_LOGI(TAG, "P4 hardware JPEG source started");
  return true;
}

void EspJpegVideoSource::stop_video() {
  if (this->control_mutex_ == nullptr)
    return;

  xSemaphoreTake(this->control_mutex_, portMAX_DELAY);
  const bool was_active =
      this->active_.exchange(false, std::memory_order_acq_rel);
  this->callback_ = nullptr;
  this->callback_ctx_ = nullptr;
  xSemaphoreGive(this->control_mutex_);

  if (this->camera_ != nullptr)
    this->camera_->stop_jpeg_frame_consumer(this);

#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  if (was_active) {
    ESP_LOGI(TAG, "JPEG TX stats: seen=%u published=%u rate_drop=%u",
             (unsigned) this->frames_seen_.load(std::memory_order_relaxed),
             (unsigned) this->frames_published_.load(
                 std::memory_order_relaxed),
             (unsigned) this->rate_drops_.load(std::memory_order_relaxed));
  }
#endif
}

void EspJpegVideoSource::consume_jpeg_frame(
    const esp_video_camera::JpegFrame &frame) {
  if (frame.data == nullptr || frame.size == 0 ||
      this->control_mutex_ == nullptr) {
    return;
  }

#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  this->frames_seen_.fetch_add(1, std::memory_order_relaxed);
#endif
  xSemaphoreTake(this->control_mutex_, portMAX_DELAY);
  if (!this->active_.load(std::memory_order_acquire) ||
      this->callback_ == nullptr) {
    xSemaphoreGive(this->control_mutex_);
    return;
  }

  if (!this->cadence_.accept(frame.timestamp_90khz)) {
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
    this->rate_drops_.fetch_add(1, std::memory_order_relaxed);
#endif
    xSemaphoreGive(this->control_mutex_);
    return;
  }
  this->callback_(
      this->callback_ctx_,
      voip_stack::EncodedVideoAccessUnit{
          frame.data, frame.size, frame.timestamp_90khz, true});
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  this->frames_published_.fetch_add(1, std::memory_order_relaxed);
#endif
  xSemaphoreGive(this->control_mutex_);
}

}  // namespace esphome::esp_jpeg_video_source

#endif
