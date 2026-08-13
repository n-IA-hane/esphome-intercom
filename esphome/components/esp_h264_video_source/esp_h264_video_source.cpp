#include "esp_h264_video_source.h"

#if defined(USE_ESP_IDF) && defined(USE_ESPHOME_VOIP_STACK_VIDEO) && \
    defined(USE_ESPHOME_VOIP_STACK_VIDEO_H264)

#include "esphome/core/log.h"
#include "esphome/components/voip_stack/audio_core_task_utils.h"

#include "esp_heap_caps.h"
#include "esp_timer.h"

#include <algorithm>
#include <cstring>

extern "C" void *__real_esp_h264_calloc_prefer(
    uint32_t n, uint32_t size, uint32_t *actual_size, uint32_t caps1,
    uint32_t caps2);

extern "C" void *__wrap_esp_h264_calloc_prefer(
    uint32_t n, uint32_t size, uint32_t *actual_size, uint32_t caps1,
    uint32_t caps2) {
  constexpr uint32_t kLargeAllocationBytes = 64U * 1024U;
  const uint64_t bytes = static_cast<uint64_t>(n) * size;
  if (bytes >= kLargeAllocationBytes && caps1 == MALLOC_CAP_INTERNAL &&
      caps2 == MALLOC_CAP_SPIRAM) {
    // esp_h264's large internal-first allocation is the hardware encoder's
    // deblocking buffer. Its own fallback already permits PSRAM, and the P4
    // VGA path uses that placement. Prefer it explicitly at smaller sizes so
    // API, TLS, lwIP and realtime audio retain bounded internal headroom.
    return __real_esp_h264_calloc_prefer(
        n, size, actual_size, caps2, caps1);
  }
  return __real_esp_h264_calloc_prefer(
      n, size, actual_size, caps1, caps2);
}

namespace esphome::esp_h264_video_source {

static const char *const TAG = "esp_h264_video_source";

namespace {

constexpr uint32_t kPpaScaleUnits = 16U;
constexpr uint32_t kPpaYuv420ScaleStepUnits = 2U;
constexpr uint32_t kPpaMaxScaleUnits = 255U * kPpaScaleUnits + 14U;

uint32_t divide_round_up(uint32_t numerator, uint32_t denominator) {
  return (numerator + denominator - 1U) / denominator;
}

void *alloc_psram_dma(size_t bytes) {
  return heap_caps_aligned_alloc(
      64, bytes,
      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT | MALLOC_CAP_DMA);
}

ppa_srm_rotation_angle_t ppa_rotation_for_clockwise(uint16_t rotation) {
  switch (rotation) {
    case 90:
      return PPA_SRM_ROTATION_ANGLE_270;
    case 180:
      return PPA_SRM_ROTATION_ANGLE_180;
    case 270:
      return PPA_SRM_ROTATION_ANGLE_90;
    case 0:
    default:
      return PPA_SRM_ROTATION_ANGLE_0;
  }
}

#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
template<typename T> void update_max(std::atomic<T> &target, T value) {
  T previous = target.load(std::memory_order_relaxed);
  while (value > previous &&
         !target.compare_exchange_weak(
             previous, value, std::memory_order_relaxed)) {
  }
}
#endif

}  // namespace

void EspH264VideoSource::setup() {
  this->control_mutex_ =
      xSemaphoreCreateMutexStatic(&this->control_mutex_storage_);
  this->tx_done_ = xSemaphoreCreateBinaryStatic(&this->tx_done_storage_);
  if (this->camera_ == nullptr || this->control_mutex_ == nullptr ||
      this->tx_done_ == nullptr ||
      (this->width_ & 15U) != 0 ||
      (this->height_ & 15U) != 0 || !this->init_ppa_() ||
      !this->init_encoder_and_probe_() ||
      !this->camera_->register_raw_frame_consumer(
          this,
          esp_video_camera::RawVideoPixelFormat::YUV420_OUYY_EVYY)) {
    ESP_LOGE(TAG, "P4 hardware H.264 source setup failed");
    this->encoder_ready_.store(false, std::memory_order_release);
    this->close_encoder_();
    this->free_resources_();
    if (this->ppa_ != nullptr) {
      ppa_unregister_client(this->ppa_);
      this->ppa_ = nullptr;
    }
    this->mark_failed();
    return;
  }
  // The setup probe runs on the ESPHome task. The runtime conversion runs
  // synchronously in the camera capture task, so let that task register a
  // separate client on its first frame as recommended by the PPA driver.
  if (ppa_unregister_client(this->ppa_) != ESP_OK) {
    ESP_LOGE(TAG, "Unable to release H.264 setup-probe PPA client");
    this->encoder_ready_.store(false, std::memory_order_release);
    this->close_encoder_();
    this->free_resources_();
    this->mark_failed();
    return;
  }
  this->ppa_ = nullptr;
}

void EspH264VideoSource::on_shutdown() {
  this->stop_video();
  this->encoder_ready_.store(false, std::memory_order_release);
  this->close_encoder_();
  this->free_resources_();
  if (this->ppa_ != nullptr) {
    ppa_unregister_client(this->ppa_);
    this->ppa_ = nullptr;
  }
}

void EspH264VideoSource::dump_config() {
  ESP_LOGCONFIG(TAG, "ESP32-P4 hardware H.264 source:");
  ESP_LOGCONFIG(TAG, "  Encode: %ux%u @ %u fps, %u bit/s, GOP %u",
                this->width_, this->height_, this->framerate_,
                (unsigned) this->bitrate_, this->gop_);
  ESP_LOGCONFIG(TAG, "  SPS profile-level-id: %s",
                this->profile_level_id_.empty()
                    ? "unavailable"
                    : this->profile_level_id_.c_str());
}

voip_stack::VideoCapability
EspH264VideoSource::get_video_capability() const {
  voip_stack::VideoCapability capability;
  capability.payload_type = 103;
  capability.encoding = "H264";
  capability.profile_level_id =
      this->encoder_ready_.load(std::memory_order_acquire)
          ? this->profile_level_id_
          : "";
  capability.packetization_mode = 1;
  capability.level_asymmetry_allowed = true;
  capability.width = this->width_;
  capability.height = this->height_;
  capability.max_fps = this->framerate_;
  capability.max_bitrate_bps = this->bitrate_;
  capability.rtcp_feedback_pli = true;
  capability.rtcp_feedback_fir = true;
  return capability;
}

bool EspH264VideoSource::prepare_video(
    const voip_stack::VideoCapability &capability) {
  return !this->is_failed() &&
         this->encoder_ready_.load(std::memory_order_acquire) &&
         !this->tx_active_.load(std::memory_order_acquire) &&
         capability.valid() && capability.is_h264() &&
         capability.width == this->width_ &&
         capability.height == this->height_ &&
         voip_stack::h264_level_fits(
             this->profile_level_id_, capability.profile_level_id);
}

bool EspH264VideoSource::start_video(
    voip_stack::EncodedVideoAccessUnitCallback callback, void *ctx,
    const voip_stack::VideoCapability &capability) {
  if (callback == nullptr || !this->prepare_video(capability)) {
    return false;
  }
  const uint32_t negotiated_bitrate =
      capability.max_bitrate_bps == 0
          ? this->bitrate_
          : std::min(this->bitrate_, capability.max_bitrate_bps);

  xSemaphoreTake(this->control_mutex_, portMAX_DELAY);
  const uint32_t generation =
      this->tx_generation_.fetch_add(1, std::memory_order_acq_rel) + 1;
  this->callback_ = callback;
  this->callback_ctx_ = ctx;
  this->negotiated_capability_ = capability;
  this->requested_bitrate_.store(
      negotiated_bitrate, std::memory_order_release);
  this->cadence_.reset(std::max<uint8_t>(
      1, std::min(this->framerate_, capability.max_fps == 0
                                       ? this->framerate_
                                       : capability.max_fps)));
  this->next_tx_sequence_ = 0;
  this->force_idr_generation_.store(
      generation, std::memory_order_release);
  this->raw_frames_.store(0, std::memory_order_release);
  this->queued_frames_.store(0, std::memory_order_release);
  this->queue_drops_.store(0, std::memory_order_release);
  this->encoded_frames_.store(0, std::memory_order_release);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  this->converted_frames_.store(0, std::memory_order_release);
  this->conversion_max_us_.store(0, std::memory_order_release);
  this->conversion_total_us_.store(0, std::memory_order_release);
  this->encode_max_us_.store(0, std::memory_order_release);
  this->encode_total_us_.store(0, std::memory_order_release);
  this->encoded_max_bytes_.store(0, std::memory_order_release);
#endif
  if (!this->start_tx_task_()) {
    this->callback_ = nullptr;
    this->callback_ctx_ = nullptr;
    xSemaphoreGive(this->control_mutex_);
    return false;
  }
  this->tx_active_.store(true, std::memory_order_release);
  xSemaphoreGive(this->control_mutex_);

  if (!this->camera_->start_raw_frame_consumer(this)) {
    xSemaphoreTake(this->control_mutex_, portMAX_DELAY);
    if (this->tx_generation_.load(std::memory_order_acquire) == generation) {
      this->tx_active_.store(false, std::memory_order_release);
      this->tx_generation_.fetch_add(1, std::memory_order_acq_rel);
    }
    xSemaphoreGive(this->control_mutex_);
    if (this->stop_tx_task_()) {
      xSemaphoreTake(this->control_mutex_, portMAX_DELAY);
      this->callback_ = nullptr;
      this->callback_ctx_ = nullptr;
      xSemaphoreGive(this->control_mutex_);
    }
    return false;
  }
  ESP_LOGI(TAG, "P4 hardware H.264 source started at %u bit/s",
           (unsigned) negotiated_bitrate);
  return true;
}

void EspH264VideoSource::stop_video() {
  if (this->camera_ != nullptr)
    this->camera_->stop_raw_frame_consumer(this);
  xSemaphoreTake(this->control_mutex_, portMAX_DELAY);
  const bool was_active =
      this->tx_active_.exchange(false, std::memory_order_acq_rel);
  if (was_active)
    this->tx_generation_.fetch_add(1, std::memory_order_acq_rel);
  this->force_idr_generation_.store(0, std::memory_order_release);
  xSemaphoreGive(this->control_mutex_);
  if (this->stop_tx_task_()) {
    xSemaphoreTake(this->control_mutex_, portMAX_DELAY);
    this->callback_ = nullptr;
    this->callback_ctx_ = nullptr;
    xSemaphoreGive(this->control_mutex_);
  }
  if (was_active) {
    ESP_LOGI(TAG, "H.264 TX evidence: raw=%u queued=%u dropped=%u encoded=%u",
             (unsigned) this->raw_frames_.load(std::memory_order_relaxed),
             (unsigned) this->queued_frames_.load(std::memory_order_relaxed),
             (unsigned) this->queue_drops_.load(std::memory_order_relaxed),
             (unsigned) this->encoded_frames_.load(std::memory_order_relaxed));
  }
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  if (was_active) {
    const uint32_t converted =
        this->converted_frames_.load(std::memory_order_relaxed);
    const uint32_t encoded =
        this->encoded_frames_.load(std::memory_order_relaxed);
    ESP_LOGI(TAG,
             "H.264 TX stats: raw=%u converted=%u encoded=%u "
             "convert_avg_us=%u convert_max_us=%u "
             "encode_avg_us=%u encode_max_us=%u encoded_max_bytes=%u",
             (unsigned) this->raw_frames_.load(std::memory_order_relaxed),
             (unsigned) converted, (unsigned) encoded,
             (unsigned) (
                 this->conversion_total_us_.load(std::memory_order_relaxed) /
                 std::max<uint32_t>(1, converted)),
             (unsigned) this->conversion_max_us_.load(
                 std::memory_order_relaxed),
             (unsigned) (
                 this->encode_total_us_.load(std::memory_order_relaxed) /
                 std::max<uint32_t>(1, encoded)),
             (unsigned) this->encode_max_us_.load(
                 std::memory_order_relaxed),
             (unsigned) this->encoded_max_bytes_.load(
                 std::memory_order_relaxed));
  }
#endif
}

void EspH264VideoSource::request_key_frame() {
  // The request is consumed by the next queued frame of this call generation.
  if (this->tx_active_.load(std::memory_order_acquire)) {
    this->force_idr_generation_.store(
        this->tx_generation_.load(std::memory_order_acquire),
        std::memory_order_release);
  }
}

bool EspH264VideoSource::init_ppa_() {
  if (this->ppa_ != nullptr) return true;
  ppa_client_config_t config{};
  config.oper_type = PPA_OPERATION_SRM;
  const esp_err_t error = ppa_register_client(&config, &this->ppa_);
  if (error != ESP_OK || this->ppa_ == nullptr) {
    ESP_LOGE(TAG, "PPA SRM client registration failed: %s",
             esp_err_to_name(error));
    return false;
  }
  return true;
}

bool EspH264VideoSource::allocate_resources_() {
  for (auto &slot : this->tx_slots_) {
    if (slot.yuv == nullptr)
      slot.yuv = static_cast<uint8_t *>(alloc_psram_dma(this->yuv_bytes_()));
  }
  if (this->tx_encoded_ == nullptr)
    this->tx_encoded_ =
        static_cast<uint8_t *>(alloc_psram_dma(kEncodedBufferBytes));
  return this->tx_slots_[0].yuv != nullptr &&
         this->tx_slots_[1].yuv != nullptr && this->tx_encoded_ != nullptr;
}

void EspH264VideoSource::free_resources_() {
  for (auto &slot : this->tx_slots_) {
    if (slot.yuv != nullptr)
      heap_caps_free(slot.yuv);
    slot.yuv = nullptr;
    slot.state.store(0, std::memory_order_release);
  }
  if (this->tx_encoded_ != nullptr) heap_caps_free(this->tx_encoded_);
  this->tx_encoded_ = nullptr;
}

bool EspH264VideoSource::init_encoder_and_probe_() {
  if (!this->allocate_resources_() || !this->restart_encoder_()) {
    ESP_LOGE(TAG, "Persistent H.264 encoder resources unavailable");
    return false;
  }
  const size_t rgb_bytes =
      static_cast<size_t>(this->width_) * this->height_ * 2;
  auto *probe_rgb =
      static_cast<uint8_t *>(alloc_psram_dma(rgb_bytes));
  if (probe_rgb == nullptr) {
    ESP_LOGE(TAG, "H.264 setup probe RGB allocation failed");
    return false;
  }
  auto *pixels = reinterpret_cast<uint16_t *>(probe_rgb);
  for (size_t index = 0; index < rgb_bytes / 2; index++)
    pixels[index] = static_cast<uint16_t>(0x0010U + (index & 0x001FU));
  const esp_video_camera::RawVideoFrame probe{
      probe_rgb, rgb_bytes,
      esp_video_camera::RawVideoPixelFormat::RGB565_LE,
      this->width_, this->height_,
      static_cast<uint16_t>(this->width_ * 2), 0, 0};
  const bool converted =
      this->transform_to_encoder_yuv_(
          probe, this->tx_slots_[0].yuv);
  heap_caps_free(probe_rgb);
  if (!converted ||
      !this->encode_frame_(this->tx_slots_[0].yuv, 0, false) ||
      this->profile_level_id_.empty() ||
      !voip_stack::h264_same_subprofile(
          this->profile_level_id_, "42c01e")) {
    ESP_LOGE(TAG, "H.264 setup probe did not emit Constrained Baseline SPS");
    return false;
  }
  this->encoder_ready_.store(true, std::memory_order_release);
  ESP_LOGI(TAG, "PPA/H.264 probe passed, SPS profile-level-id=%s",
           this->profile_level_id_.c_str());
  return true;
}

bool EspH264VideoSource::restart_encoder_() {
  if (this->encoder_ != nullptr) return true;
  esp_h264_enc_cfg_hw_t config{};
  // ESP32-P4 builds that do not promise a rev >= 3 accept only Espressif's
  // optimized YUV420 layout in the public hardware-encoder contract. PPA
  // produces that layout directly from the camera's RGB565_LE frame.
  config.pic_type = ESP_H264_RAW_FMT_O_UYY_E_VYY;
  config.gop = this->gop_;
  config.fps = this->framerate_;
  config.res.width = this->width_;
  config.res.height = this->height_;
  config.rc.bitrate = this->bitrate_;
  config.rc.qp_min = 18;
  config.rc.qp_max = 42;
  if (esp_h264_enc_hw_new(&config, &this->encoder_) != ESP_H264_ERR_OK ||
      this->encoder_ == nullptr ||
      esp_h264_enc_open(this->encoder_) != ESP_H264_ERR_OK) {
    this->close_encoder_();
    return false;
  }
  this->active_bitrate_.store(this->bitrate_, std::memory_order_release);
  return true;
}

void EspH264VideoSource::close_encoder_() {
  if (this->encoder_ == nullptr) return;
  esp_h264_enc_close(this->encoder_);
  esp_h264_enc_del(this->encoder_);
  this->encoder_ = nullptr;
}

bool EspH264VideoSource::set_encoder_gop_(uint8_t gop) {
  if (this->encoder_ == nullptr || gop == 0) return false;
  esp_h264_enc_param_hw_handle_t parameters = nullptr;
  if (esp_h264_enc_hw_get_param_hd(
          this->encoder_, &parameters) != ESP_H264_ERR_OK ||
      parameters == nullptr) {
    return false;
  }
  return esp_h264_enc_set_gop(&parameters->base, gop) ==
         ESP_H264_ERR_OK;
}

bool EspH264VideoSource::set_encoder_bitrate_(uint32_t bitrate) {
  if (this->encoder_ == nullptr || bitrate == 0) return false;
  esp_h264_enc_param_hw_handle_t parameters = nullptr;
  if (esp_h264_enc_hw_get_param_hd(
          this->encoder_, &parameters) != ESP_H264_ERR_OK ||
      parameters == nullptr) {
    return false;
  }
  return esp_h264_enc_set_bitrate(&parameters->base, bitrate) ==
         ESP_H264_ERR_OK;
}

bool EspH264VideoSource::transform_to_encoder_yuv_(
    const esp_video_camera::RawVideoFrame &frame, uint8_t *target) {
  const bool swaps_dimensions =
      frame.rotation_degrees == 90 || frame.rotation_degrees == 270;
  const uint16_t target_block_width =
      swaps_dimensions ? this->height_ : this->width_;
  const uint16_t target_block_height =
      swaps_dimensions ? this->width_ : this->height_;
  const bool rgb565 =
      frame.pixel_format ==
      esp_video_camera::RawVideoPixelFormat::RGB565_LE;
  const bool optimized_yuv420 =
      frame.pixel_format ==
      esp_video_camera::RawVideoPixelFormat::YUV420_OUYY_EVYY;
  const size_t minimum_frame_bytes =
      rgb565
          ? static_cast<size_t>(frame.stride_bytes) * frame.height
          : static_cast<size_t>(frame.width) * frame.height * 3 / 2;
  if (frame.data == nullptr || target == nullptr || this->ppa_ == nullptr ||
      (!rgb565 && !optimized_yuv420) ||
      frame.width == 0 || frame.height == 0 ||
      (rgb565 &&
       frame.stride_bytes < frame.width * sizeof(uint16_t)) ||
      (optimized_yuv420 &&
       (frame.stride_bytes != frame.width ||
        (frame.width & 1U) != 0 || (frame.height & 1U) != 0)) ||
      frame.size < minimum_frame_bytes ||
      (frame.rotation_degrees != 0 && frame.rotation_degrees != 90 &&
       frame.rotation_degrees != 180 && frame.rotation_degrees != 270)) {
    return false;
  }

  // IDF quantizes PPA scaling to 1/16 units. For YUV420 output it also
  // clears the fractional low bit, so only even unit counts reach hardware.
  uint32_t scale_units = std::max(
      divide_round_up(
          static_cast<uint32_t>(target_block_width) * kPpaScaleUnits,
          frame.width),
      divide_round_up(
          static_cast<uint32_t>(target_block_height) * kPpaScaleUnits,
          frame.height));
  scale_units = std::max(scale_units, kPpaYuv420ScaleStepUnits);
  scale_units = (scale_units + 1U) & ~1U;
  uint32_t input_block_width = 0;
  uint32_t input_block_height = 0;
  for (; scale_units <= kPpaMaxScaleUnits;
       scale_units += kPpaYuv420ScaleStepUnits) {
    input_block_width =
        (divide_round_up(
             static_cast<uint32_t>(target_block_width) * kPpaScaleUnits,
             scale_units) +
         1U) &
        ~1U;
    input_block_height =
        (divide_round_up(
             static_cast<uint32_t>(target_block_height) * kPpaScaleUnits,
             scale_units) +
         1U) &
        ~1U;
    if (input_block_width <= frame.width &&
        input_block_height <= frame.height &&
        input_block_width * scale_units / kPpaScaleUnits ==
            target_block_width &&
        input_block_height * scale_units / kPpaScaleUnits ==
            target_block_height) {
      break;
    }
  }
  if (scale_units > kPpaMaxScaleUnits) {
    return false;
  }

  const float scale =
      static_cast<float>(scale_units) / kPpaScaleUnits;
  ppa_srm_oper_config_t config{};
  config.in.buffer = frame.data;
  config.in.pic_w = frame.width;
  config.in.pic_h = frame.height;
  config.in.block_w = input_block_width;
  config.in.block_h = input_block_height;
  config.in.block_offset_x =
      ((frame.width - input_block_width) / 2U) & ~1U;
  config.in.block_offset_y =
      ((frame.height - input_block_height) / 2U) & ~1U;
  config.in.srm_cm = rgb565 ? PPA_SRM_COLOR_MODE_RGB565
                            : PPA_SRM_COLOR_MODE_YUV420;
  config.out.buffer = target;
  config.out.buffer_size = this->yuv_bytes_();
  config.out.pic_w = this->width_;
  config.out.pic_h = this->height_;
  config.out.srm_cm = PPA_SRM_COLOR_MODE_YUV420;
  config.out.yuv_range = PPA_COLOR_RANGE_LIMIT;
  config.out.yuv_std = PPA_COLOR_CONV_STD_RGB_YUV_BT601;
  config.rotation_angle =
      ppa_rotation_for_clockwise(frame.rotation_degrees);
  config.scale_x = scale;
  config.scale_y = scale;
  config.rgb_swap = false;
  config.byte_swap = false;
  config.mode = PPA_TRANS_MODE_BLOCKING;
  return ppa_do_scale_rotate_mirror(this->ppa_, &config) == ESP_OK;
}

bool EspH264VideoSource::encode_frame_(
    const uint8_t *yuv, uint32_t timestamp_90khz, bool publish,
    uint32_t generation) {
  if (this->encoder_ == nullptr || yuv == nullptr ||
      this->tx_encoded_ == nullptr) {
    return false;
  }
  esp_h264_enc_in_frame_t input{};
  input.raw_data.buffer = const_cast<uint8_t *>(yuv);
  input.raw_data.len = this->yuv_bytes_();
  // esp_h264 1.3.6 passes PTS through unchanged and drives rate control from
  // config.fps, so preserving the RTP clock avoids a second timestamp domain.
  input.pts = timestamp_90khz;
  esp_h264_enc_out_frame_t output{};
  output.raw_data.buffer = this->tx_encoded_;
  output.raw_data.len = kEncodedBufferBytes;
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  const int64_t encode_started_us = esp_timer_get_time();
#endif
  const esp_h264_err_t error =
      esp_h264_enc_process(this->encoder_, &input, &output);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  const uint32_t encode_us =
      static_cast<uint32_t>(esp_timer_get_time() - encode_started_us);
  this->encode_total_us_.fetch_add(
      encode_us, std::memory_order_relaxed);
  update_max(
      this->encode_max_us_, encode_us);
#endif
  if (error != ESP_H264_ERR_OK || output.length == 0 ||
      output.length > kEncodedBufferBytes) {
    ESP_LOGW(TAG, "H.264 encode failed: %d", (int) error);
    return false;
  }
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  update_max(
      this->encoded_max_bytes_, static_cast<uint32_t>(output.length));
#endif

  if (this->profile_level_id_.empty()) {
    std::string profile;
    if (!voip_stack::h264_profile_level_id_from_annex_b(
            output.raw_data.buffer, output.length, &profile) ||
        !voip_stack::h264_same_subprofile(profile, "42c01e")) {
      ESP_LOGE(TAG, "Encoder probe emitted no supported H.264 SPS");
      return false;
    }
    this->profile_level_id_ = profile;
  }

  if (publish) {
    // stop_video() first revokes this generation, then joins the TX worker,
    // and only then clears the callback. The callback therefore remains owned
    // for this entire operation without a lock spanning RTP publication.
    if (this->tx_active_.load(std::memory_order_acquire) &&
        generation ==
            this->tx_generation_.load(std::memory_order_acquire) &&
        this->callback_ != nullptr) {
      const voip_stack::EncodedVideoAccessUnit access_unit{
          output.raw_data.buffer, output.length, timestamp_90khz,
          output.frame_type == ESP_H264_FRAME_TYPE_IDR};
      this->callback_(this->callback_ctx_, access_unit);
    }
  }
  return !this->profile_level_id_.empty();
}

void EspH264VideoSource::consume_raw_video_frame(
    const esp_video_camera::RawVideoFrame &frame) {
  if (!this->tx_active_.load(std::memory_order_acquire)) {
    return;
  }
  // This callback executes on the camera capture task. Registering the
  // runtime client here keeps each task on its own PPA client; setup used and
  // released a separate client for the deterministic encoder probe.
  if (this->ppa_ == nullptr && !this->init_ppa_()) {
    ESP_LOGE(TAG, "Unable to register runtime H.264 PPA client");
    xSemaphoreTake(this->control_mutex_, portMAX_DELAY);
    this->tx_active_.store(false, std::memory_order_release);
    this->callback_ = nullptr;
    this->callback_ctx_ = nullptr;
    xSemaphoreGive(this->control_mutex_);
    if (this->tx_task_handle_ != nullptr)
      xTaskNotifyGive(this->tx_task_handle_);
    return;
  }
  this->raw_frames_.fetch_add(1, std::memory_order_relaxed);
  if (!this->cadence_.accept(frame.timestamp_90khz))
    return;
  // Advance the ideal clock before doing any work. PPA copies the borrowed
  // camera frame into an owned bounded slot. Encoding then runs independently,
  // so capture never retains a CSI buffer for the full encoder latency.
  const uint32_t generation =
      this->tx_generation_.load(std::memory_order_acquire);
  TxSlot *slot = nullptr;
  for (auto &candidate : this->tx_slots_) {
    uint8_t free = 0;
    if (candidate.state.compare_exchange_strong(
            free, 1, std::memory_order_acq_rel)) {
      slot = &candidate;
      break;
    }
  }
  if (slot == nullptr) {
    this->queue_drops_.fetch_add(1, std::memory_order_relaxed);
    return;
  }
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  const int64_t conversion_started_us = esp_timer_get_time();
#endif
  if (!this->transform_to_encoder_yuv_(
          frame, slot->yuv)) {
    ESP_LOGE(TAG, "Unable to transform H.264 camera frame");
    slot->state.store(0, std::memory_order_release);
    return;
  }
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  const uint32_t conversion_us =
      static_cast<uint32_t>(
          esp_timer_get_time() - conversion_started_us);
  this->converted_frames_.fetch_add(1, std::memory_order_relaxed);
  this->conversion_total_us_.fetch_add(
      conversion_us, std::memory_order_relaxed);
  update_max(this->conversion_max_us_, conversion_us);
#endif
  if (!this->tx_active_.load(std::memory_order_acquire) ||
      generation != this->tx_generation_.load(std::memory_order_acquire)) {
    slot->state.store(0, std::memory_order_release);
    return;
  }
  slot->timestamp_90khz = frame.timestamp_90khz;
  slot->generation = generation;
  slot->sequence = this->next_tx_sequence_++;
  slot->state.store(2, std::memory_order_release);
  this->queued_frames_.fetch_add(1, std::memory_order_relaxed);
  if (this->tx_task_handle_ != nullptr)
    xTaskNotifyGive(this->tx_task_handle_);
}

bool EspH264VideoSource::start_tx_task_() {
  if (this->tx_task_handle_ != nullptr)
    return false;
  for (auto &slot : this->tx_slots_)
    slot.state.store(0, std::memory_order_release);
  xSemaphoreTake(this->tx_done_, 0);
  this->tx_task_running_.store(true, std::memory_order_release);
  if (voip_audio_core::start_managed_pinned_task(
          EspH264VideoSource::tx_task_trampoline_, "p4_video_tx", 8192,
          this, 16, 0, true, TAG, &this->tx_task_handle_,
          &this->tx_task_tcb_, &this->tx_task_stack_,
          &this->tx_task_with_caps_)) {
    return true;
  }
  this->tx_task_running_.store(false, std::memory_order_release);
  return false;
}

bool EspH264VideoSource::stop_tx_task_() {
  if (this->tx_task_handle_ == nullptr)
    return true;
  this->tx_task_running_.store(false, std::memory_order_release);
  xTaskNotifyGive(this->tx_task_handle_);
  if (xSemaphoreTake(this->tx_done_, pdMS_TO_TICKS(3000)) != pdTRUE) {
    ESP_LOGE(TAG, "H.264 TX worker did not stop; retaining owned resources");
    return false;
  }
  voip_audio_core::cleanup_managed_pinned_task(
      &this->tx_task_handle_, &this->tx_task_stack_, 8192,
      this->tx_task_with_caps_);
  this->tx_task_with_caps_ = false;
  for (auto &slot : this->tx_slots_)
    slot.state.store(0, std::memory_order_release);
  return true;
}

void EspH264VideoSource::tx_task_trampoline_(void *ctx) {
  static_cast<EspH264VideoSource *>(ctx)->tx_task_();
}

void EspH264VideoSource::tx_task_() {
  while (this->tx_task_running_.load(std::memory_order_acquire)) {
    TxSlot *slot = nullptr;
    uint32_t oldest_sequence = 0;
    for (auto &candidate : this->tx_slots_) {
      if (candidate.state.load(std::memory_order_acquire) != 2)
        continue;
      if (slot == nullptr ||
          static_cast<int32_t>(candidate.sequence - oldest_sequence) < 0) {
        slot = &candidate;
        oldest_sequence = candidate.sequence;
      }
    }
    if (slot != nullptr) {
      uint8_t ready = 2;
      if (!slot->state.compare_exchange_strong(
              ready, 3, std::memory_order_acq_rel))
        continue;
    }
    if (slot == nullptr) {
      ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
      continue;
    }
    const uint32_t generation = slot->generation;
    if (!this->tx_active_.load(std::memory_order_acquire) ||
        generation != this->tx_generation_.load(std::memory_order_acquire)) {
      slot->state.store(0, std::memory_order_release);
      continue;
    }
    const uint32_t requested_bitrate =
        this->requested_bitrate_.load(std::memory_order_acquire);
    if (requested_bitrate !=
        this->active_bitrate_.load(std::memory_order_acquire)) {
      if (this->set_encoder_bitrate_(requested_bitrate)) {
        this->active_bitrate_.store(
            requested_bitrate, std::memory_order_release);
      } else {
        ESP_LOGE(TAG, "Unable to apply negotiated H.264 bitrate %u",
                 (unsigned) requested_bitrate);
      }
    }
    uint32_t requested_idr_generation = generation;
    const bool force_idr =
        this->force_idr_generation_.compare_exchange_strong(
            requested_idr_generation, 0, std::memory_order_acq_rel);
    if (force_idr && !this->set_encoder_gop_(1))
      ESP_LOGE(TAG, "Unable to request H.264 IDR");
    if (this->encode_frame_(
            slot->yuv, slot->timestamp_90khz, true, generation)) {
      this->encoded_frames_.fetch_add(1, std::memory_order_relaxed);
    }
    if (force_idr && !this->set_encoder_gop_(this->gop_))
      ESP_LOGE(TAG, "Unable to restore H.264 GOP");
    slot->state.store(0, std::memory_order_release);
  }
  for (auto &slot : this->tx_slots_)
    slot.state.store(0, std::memory_order_release);
  voip_audio_core::finish_managed_pinned_task(this->tx_done_);
}

}  // namespace esphome::esp_h264_video_source

#endif
