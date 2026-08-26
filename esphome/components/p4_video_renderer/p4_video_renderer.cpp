#include "p4_video_renderer.h"

#if defined(USE_ESP_IDF) && defined(USE_ESPHOME_VOIP_STACK_VIDEO)

#include "esphome/core/helpers.h"
#include "esphome/core/log.h"

#include "esp_heap_caps.h"

#include <algorithm>
#include <cstdlib>
#include <cstring>

namespace esphome::p4_video_renderer {

static const char *const TAG = "p4_video_renderer";

namespace {

#if defined(USE_P4_VIDEO_RENDERER_JPEG) || defined(USE_P4_VIDEO_RENDERER_H264)
void *alloc_surface(size_t bytes, size_t reserve_after) {
  const size_t free = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
  const size_t largest = heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM);
  if (largest < bytes || free < bytes + reserve_after)
    return nullptr;
  return heap_caps_aligned_alloc(
      64, bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT | MALLOC_CAP_DMA);
}
#endif

#ifdef USE_P4_VIDEO_RENDERER_H264
void *alloc_psram_dma(size_t bytes) {
  return heap_caps_aligned_alloc(
      64, bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT | MALLOC_CAP_DMA);
}
#endif

#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
void update_max(std::atomic<uint32_t> &maximum, uint32_t value) {
  uint32_t current = maximum.load(std::memory_order_relaxed);
  while (value > current && !maximum.compare_exchange_weak(
                                current, value, std::memory_order_relaxed)) {
  }
}
#endif

} // namespace

void P4VideoRenderer::setup() {
  this->rx_done_ = xSemaphoreCreateBinaryStatic(&this->rx_done_storage_);
  this->presentation_mutex_ =
      xSemaphoreCreateMutexStatic(&this->presentation_mutex_storage_);
  if (this->rx_done_ == nullptr || this->presentation_mutex_ == nullptr) {
    ESP_LOGE(TAG, "P4 video receive synchronization setup failed");
    this->mark_failed();
    return;
  }
#ifdef USE_P4_VIDEO_RENDERER_JPEG
  // Claim the small INTERNAL/DMA descriptors before reserving multi-megabyte
  // PSRAM surfaces. The camera encoder may own another handle; esp-idf
  // serializes both handles on the one physical JPEG engine.
  if (!this->init_jpeg_decoder_()) {
    ESP_LOGE(TAG, "P4 hardware JPEG decoder setup failed");
    this->mark_failed();
    return;
  }
#endif
#ifdef USE_P4_VIDEO_RENDERER_DIRECT_DISPLAY
  if (this->direct_display_ == nullptr) {
    ESP_LOGE(TAG, "P4 direct video display setup failed");
    this->mark_failed();
    return;
  }
#ifdef USE_P4_VIDEO_RENDERER_JPEG
  if (!this->init_direct_display_ppa_()) {
    ESP_LOGE(TAG, "P4 direct JPEG display PPA setup failed");
    this->mark_failed();
    return;
  }
#endif
#endif
#ifdef USE_P4_VIDEO_RENDERER_H264
  this->h264_au_queue_ = xQueueCreateStatic(
      kH264AccessUnitQueueDepth, sizeof(uint8_t),
      this->h264_au_queue_storage_, &this->h264_au_queue_control_);
  if (this->h264_au_queue_ == nullptr) {
    ESP_LOGE(TAG, "P4 H.264 access-unit queue setup failed");
    this->mark_failed();
    return;
  }
  if (!this->init_ppa_()) {
    ESP_LOGE(TAG, "P4 video PPA receive path setup failed");
    this->mark_failed();
    return;
  }
#endif
  // Reserve the large codec and double-buffer surfaces before calls can be
  // offered. This keeps PSRAM heap walks and multi-megabyte allocations out
  // of INVITE/re-INVITE handling and fails deterministically at boot if no
  // suitable contiguous extent remains.
  if (!this->allocate_session_resources_()) {
    ESP_LOGE(TAG, "Unable to reserve P4 video receive resources at setup");
    this->mark_failed();
    return;
  }
#ifdef USE_P4_VIDEO_RENDERER_H264
  // Prime the normal negotiated resolution at boot. A peer that legally sends
  // another admitted resolution reconfigures this one worker-owned handle
  // once, rather than allocating or rebuilding conversion state per frame.
  if (!this->configure_i420_converter_(this->width_, this->height_)) {
    ESP_LOGE(TAG, "Unable to initialize P4 I420 conversion at setup");
    this->mark_failed();
    return;
  }
  // Allocate tinyH264's working set at boot. Normal call setup only changes
  // generation/admission state; it performs no decoder heap churn.
  if (!this->reset_h264_decoder_()) {
    ESP_LOGE(TAG, "Unable to initialize P4 H.264 decoder at setup");
    this->mark_failed();
    return;
  }
#endif
  // The decoder worker is component-owned, not call-owned. It sleeps on a
  // direct notification while idle and lets SIP teardown revoke a generation
  // without waiting for an in-flight hardware or software decode.
  this->rx_running_.store(true, std::memory_order_release);
  if (!this->start_rx_task_()) {
    this->rx_running_.store(false, std::memory_order_release);
    ESP_LOGE(TAG, "Unable to start persistent P4 video decoder worker");
    this->mark_failed();
    return;
  }
  this->disable_loop();
}

void P4VideoRenderer::on_shutdown() {
  this->rx_session_prepared_.store(false, std::memory_order_release);
  this->rx_active_.store(false, std::memory_order_release);
  this->rx_session_generation_.fetch_add(1, std::memory_order_acq_rel);
  this->stop_rx_task_();
}

void P4VideoRenderer::loop() {
  // Consume the current edge before inspecting the pipeline. Decoder and
  // display callbacks run concurrently and may publish the next edge while
  // this function is active; disabling at the end would erase that wake-up.
  this->disable_loop();
  if (this->video_ended_pending_.exchange(false, std::memory_order_acq_rel)) {
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
    ESP_LOGI(TAG, "Video presentation ended; dispatching UI trigger");
#endif
    this->video_ended_trigger_.trigger();
#ifdef USE_P4_VIDEO_RENDERER_DIRECT_DISPLAY
    // The media worker holds presentation_mutex_ for its complete PPA write.
    // Start LVGL's page repaint only after that transaction has finished so
    // the two PPA clients cannot enter teardown concurrently.
    lv_obj_invalidate(lv_screen_active());
#endif
  }

#if defined(USE_P4_VIDEO_RENDERER_H264) &&                                  \
    defined(USE_P4_VIDEO_RENDERER_DIRECT_DISPLAY)
  this->refresh_direct_display_layout_();
#endif

  bool first_frame = false;
  xSemaphoreTake(this->presentation_mutex_, portMAX_DELAY);
  const int pending = this->pending_surface_.load(std::memory_order_acquire);
  if (this->rx_running_.load(std::memory_order_acquire) &&
      this->rx_active_.load(std::memory_order_acquire) &&
      !this->presentation_in_flight_.load(std::memory_order_acquire) &&
      pending >= 0 && pending <= 1 &&
      this->surfaces_[pending] != nullptr) {
#ifdef USE_P4_VIDEO_RENDERER_DIRECT_DISPLAY
    if (this->video_container_ != nullptr) {
      // The first decoded surface is what opens the video page. Presentation
      // itself is gated on that page already being active, so delaying this
      // edge until after draw_pixels_at() would create a circular dependency.
      first_frame = !this->remote_frame_visible_.exchange(
          true, std::memory_order_acq_rel);
      const bool page_active =
          lv_obj_get_screen(this->video_container_) ==
          lv_disp_get_scr_act(nullptr);
#ifdef USE_P4_VIDEO_RENDERER_H264
      this->direct_page_active_.store(page_active, std::memory_order_release);
#endif
      const bool presented =
          page_active && this->commit_direct_surface_(pending);
      if (!presented) {
        // A hidden page or failed presentation consumed no pixels. Release
        // only the surface observed above; never erase a newer publication.
        int expected = pending;
        this->pending_surface_.compare_exchange_strong(
            expected, -1, std::memory_order_acq_rel);
      }
    }
#else
    // A displayed surface and a decoded-but-not-yet-presented surface are
    // separate pipeline stages. While LVGL/DSI reads the front surface, the
    // decoder may fill the other one; only the descriptor swap waits for the
    // previous refresh completion.
    if (this->video_image_ != nullptr) {
      this->front_surface_.store(pending, std::memory_order_release);
      this->image_descriptor_.data = this->surfaces_[pending];
#ifdef USE_P4_VIDEO_RENDERER_JPEG
      this->image_descriptor_.data_size = this->surface_data_size_[pending];
      this->image_descriptor_.header.w =
          this->surface_content_width_[pending];
      this->image_descriptor_.header.h =
          this->surface_content_height_[pending];
      this->image_descriptor_.header.stride =
          this->surface_stride_bytes_[pending];
      lv_obj_set_size(this->video_image_,
                      this->surface_content_width_[pending],
                      this->surface_content_height_[pending]);
      lv_obj_center(this->video_image_);
#endif
      lv_image_set_src(this->video_image_, &this->image_descriptor_);
      this->presentation_in_flight_.store(true, std::memory_order_release);
      // front_surface_ now protects `pending`; the other surface can accept the
      // next hardware decode while this refresh is in flight.
      this->pending_surface_.store(-1, std::memory_order_release);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
      this->presentation_started_ms_.store(millis(),
                                           std::memory_order_release);
#endif
      this->rx_presented_frames_.fetch_add(1, std::memory_order_relaxed);
      lv_obj_invalidate(this->video_image_);
      this->surface_ever_presented_.store(true, std::memory_order_release);
      first_frame = !this->remote_frame_visible_.exchange(
          true, std::memory_order_acq_rel);
    }
#endif
  }
  xSemaphoreGive(this->presentation_mutex_);
  if (first_frame)
    this->first_frame_trigger_.trigger();
}

void P4VideoRenderer::dump_config() {
#ifdef USE_P4_VIDEO_RENDERER_H264
  ESP_LOGCONFIG(TAG, "P4 software H.264 video renderer:");
#else
  ESP_LOGCONFIG(TAG, "P4 hardware JPEG video renderer:");
#endif
  ESP_LOGCONFIG(TAG, "  Preferred receive: %ux%u @ %u fps", this->width_,
                this->height_, this->framerate_);
#ifdef USE_P4_VIDEO_RENDERER_H264
  ESP_LOGCONFIG(TAG, "  Decode admission: <= %ux%u and <= %u macroblocks",
                this->max_decode_width_, this->max_decode_height_,
                static_cast<unsigned>(kH264Level30MaxMacroblocks));
  const size_t surface_bytes = 0;
  const size_t access_unit_bytes =
      kH264AccessUnitQueueDepth * kMaxAccessUnitBytes;
  const size_t optimized_yuv_bytes = this->h264_optimized_yuv_bytes_();
  ESP_LOGCONFIG(TAG,
                "  Reserved RX buffers: %u bytes (surfaces=%u, AU=%u, YUV=%u)",
                static_cast<unsigned>(surface_bytes + access_unit_bytes +
                                      optimized_yuv_bytes),
                static_cast<unsigned>(surface_bytes),
                static_cast<unsigned>(access_unit_bytes),
                static_cast<unsigned>(optimized_yuv_bytes));
  ESP_LOGCONFIG(TAG, "  PSRAM after setup: free=%u, largest=%u",
                static_cast<unsigned>(
                    heap_caps_get_free_size(MALLOC_CAP_SPIRAM)),
                static_cast<unsigned>(
                    heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM)));
#else
  ESP_LOGCONFIG(TAG, "  Decode admission: <= %ux%u", this->max_decode_width_,
                this->max_decode_height_);
#endif
#ifdef USE_P4_VIDEO_RENDERER_DIRECT_DISPLAY
  ESP_LOGCONFIG(TAG, "  Presentation: direct display, rotation %u degrees",
                this->display_rotation_);
#else
  ESP_LOGCONFIG(TAG, "  Presentation: LVGL image");
#endif
  ESP_LOGCONFIG(TAG, "  Task stack in PSRAM: %s",
                YESNO(this->task_stacks_in_psram_));
}

void P4VideoRenderer::attach_video_container(lv_obj_t *container) {
  if (container == nullptr || this->video_container_ != nullptr)
    return;
  this->video_container_ = container;
#ifdef USE_P4_VIDEO_RENDERER_DIRECT_DISPLAY
#ifdef USE_P4_VIDEO_RENDERER_H264
  // The video page is inactive when this hook runs, so LVGL has not laid out
  // its objects yet. Prime that page once before the decoder worker reads the
  // cached geometry; otherwise every first frame is dropped waiting for a page
  // that can only be opened by that same frame.
  lv_obj_update_layout(this->video_container_);
  if (!this->refresh_direct_display_layout_()) {
    ESP_LOGW(TAG, "P4 H.264 display layout is not ready at LVGL attach");
  }
#endif
  if (this->pending_surface_.load(std::memory_order_acquire) >= 0)
    this->enable_loop();
  return;
#else
  this->video_image_ = lv_image_create(container);
#ifdef USE_P4_VIDEO_RENDERER_JPEG
  lv_obj_set_size(this->video_image_, this->width_, this->height_);
#endif
  lv_obj_center(this->video_image_);
  lv_obj_clear_flag(this->video_image_, LV_OBJ_FLAG_SCROLLABLE);
  memset(&this->image_descriptor_, 0, sizeof(this->image_descriptor_));
  this->image_descriptor_.header.magic = LV_IMAGE_HEADER_MAGIC;
  this->image_descriptor_.header.cf = LV_COLOR_FORMAT_RGB565;
  this->image_descriptor_.header.flags = 0;
#ifdef USE_P4_VIDEO_RENDERER_JPEG
  this->image_descriptor_.header.w = this->width_;
  this->image_descriptor_.header.h = this->height_;
  this->image_descriptor_.header.stride =
      this->width_ * sizeof(uint16_t);
#endif
  lv_display_t *display = lv_obj_get_display(this->video_image_);
  if (display == nullptr) {
    ESP_LOGE(TAG, "Unable to attach video surface to an LVGL display");
    this->mark_failed();
    return;
  }
  lv_display_add_event_cb(display,
                          P4VideoRenderer::display_refresh_ready_callback_,
                          LV_EVENT_REFR_READY, this);
  if (this->pending_surface_.load(std::memory_order_acquire) >= 0)
    this->enable_loop();
#endif
}

void P4VideoRenderer::display_refresh_ready_callback_(lv_event_t *event) {
  auto *renderer =
      static_cast<P4VideoRenderer *>(lv_event_get_user_data(event));
  if (renderer != nullptr)
    renderer->on_display_refresh_ready_();
}

void P4VideoRenderer::on_display_refresh_ready_() {
  if (!this->presentation_in_flight_.exchange(false,
                                              std::memory_order_acq_rel)) {
    return;
  }
  // LV_EVENT_REFR_READY is emitted after rendering and after the display flush
  // callback returns. The front surface remains immutable; if the decoder
  // filled the other surface meanwhile, schedule its descriptor swap now.
  this->rx_refresh_completed_.fetch_add(1, std::memory_order_relaxed);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  const uint32_t elapsed =
      millis() - this->presentation_started_ms_.load(std::memory_order_acquire);
  uint32_t maximum = this->rx_refresh_max_ms_.load(std::memory_order_relaxed);
  while (elapsed > maximum &&
         !this->rx_refresh_max_ms_.compare_exchange_weak(
             maximum, elapsed, std::memory_order_relaxed)) {
  }
#endif
  if (this->rx_running_.load(std::memory_order_acquire) &&
      this->rx_active_.load(std::memory_order_acquire) &&
      this->pending_surface_.load(std::memory_order_acquire) >= 0) {
    this->enable_loop_soon_any_context();
  }
}

voip_stack::VideoCapability
P4VideoRenderer::get_receive_video_capability() const {
  voip_stack::VideoCapability capability;
#ifdef USE_P4_VIDEO_RENDERER_H264
  capability.payload_type = 103;
  capability.encoding = "H264";
  capability.profile_level_id = this->h264_receive_profile_level_id_(
      this->max_decode_width_, this->max_decode_height_, this->framerate_);
  capability.packetization_mode = 1;
  capability.level_asymmetry_allowed = true;
  capability.max_bitrate_bps = 800000;
  capability.rtcp_feedback_pli = true;
  capability.rtcp_feedback_fir = true;
#else
  capability.payload_type = 26;
  capability.encoding = "JPEG";
  capability.profile_level_id.clear();
  capability.packetization_mode = 0;
  capability.level_asymmetry_allowed = false;
#endif
  capability.width = this->width_;
  capability.height = this->height_;
  capability.max_fps = this->framerate_;
  return capability;
}

#ifdef USE_P4_VIDEO_RENDERER_H264
bool P4VideoRenderer::init_ppa_() {
  if (this->ppa_ != nullptr)
    return true;
  // The driver recommends one client per task; it does not bind a client to
  // the task that registered it. This component has exactly one runtime PPA
  // caller (the RX worker), never submits concurrently, and retains the
  // client across calls to avoid lifecycle allocation churn.
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
#endif

#ifdef USE_P4_VIDEO_RENDERER_JPEG
bool P4VideoRenderer::init_jpeg_decoder_() {
  if (this->jpeg_decoder_ != nullptr)
    return true;
  jpeg_decode_engine_cfg_t engine_config{};
  // This timeout covers the hardware transaction itself. Waiting for a
  // concurrent camera encode is serialized separately by esp-idf's codec
  // mutex and therefore does not consume this budget.
  engine_config.timeout_ms = 100;
  const esp_err_t error =
      jpeg_new_decoder_engine(&engine_config, &this->jpeg_decoder_);
  if (error != ESP_OK || this->jpeg_decoder_ == nullptr) {
    ESP_LOGE(TAG, "JPEG decoder engine allocation failed: %s",
             esp_err_to_name(error));
    this->jpeg_decoder_ = nullptr;
    return false;
  }
  this->jpeg_decode_config_.output_format = JPEG_DECODE_OUT_FORMAT_RGB565;
  // ESP-IDF names the native little-endian RGB565 layout "BGR" because it
  // describes byte/component order rather than the logical LVGL color.
  this->jpeg_decode_config_.rgb_order = JPEG_DEC_RGB_ELEMENT_ORDER_BGR;
  this->jpeg_decode_config_.conv_std = JPEG_YUV_RGB_CONV_STD_BT601;
  return true;
}
#endif

#if defined(USE_P4_VIDEO_RENDERER_DIRECT_DISPLAY) &&                       \
    defined(USE_P4_VIDEO_RENDERER_JPEG)
bool P4VideoRenderer::init_direct_display_ppa_() {
  if (this->direct_display_ppa_ != nullptr)
    return true;
  // This client is used only by loopTask. Camera conversion has its own client
  // and the IDF driver serializes SRM transactions across callers. A short
  // burst gives the realtime audio path access to PSRAM between video blocks.
  ppa_client_config_t config{};
  config.oper_type = PPA_OPERATION_SRM;
  config.data_burst_length = PPA_DATA_BURST_LENGTH_16;
  const esp_err_t error =
      ppa_register_client(&config, &this->direct_display_ppa_);
  if (error != ESP_OK || this->direct_display_ppa_ == nullptr) {
    ESP_LOGE(TAG, "Direct display PPA registration failed: %s",
             esp_err_to_name(error));
    this->direct_display_ppa_ = nullptr;
    return false;
  }
  return true;
}
#endif

#ifdef USE_P4_VIDEO_RENDERER_DIRECT_DISPLAY
bool P4VideoRenderer::present_surface_direct_(int index) {
#ifdef USE_P4_VIDEO_RENDERER_H264
  if (index < 0 || index > 1 || this->direct_display_ == nullptr ||
      this->surfaces_[index] == nullptr ||
      this->surface_content_width_[index] == 0 ||
      this->surface_content_height_[index] == 0) {
    return false;
  }

  const uint64_t layout_area =
      this->direct_layout_area_.load(std::memory_order_acquire);
  const uint32_t native_size =
      this->direct_layout_native_size_.load(std::memory_order_acquire);
  const int output_width = this->surface_content_width_[index];
  const int output_height = this->surface_content_height_[index];
  const int native_x = this->surface_native_x_[index];
  const int native_y = this->surface_native_y_[index];
  const int native_width = static_cast<int>(native_size >> 16U);
  const int native_height = static_cast<int>(native_size & 0xffffU);
  const size_t expected_bytes =
      static_cast<size_t>(output_width) * output_height * sizeof(uint16_t);
  if (layout_area == 0 || native_size == 0 ||
      this->surface_layout_area_[index] != layout_area ||
      this->surface_native_size_[index] != native_size ||
      this->surface_stride_bytes_[index] !=
          output_width * sizeof(uint16_t) ||
      this->surface_data_size_[index] != expected_bytes ||
      expected_bytes > this->surface_capacity_bytes_ ||
      native_x < 0 || native_y < 0 ||
      native_x + output_width > native_width ||
      native_y + output_height > native_height) {
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
    this->rx_geometry_drops_.fetch_add(1, std::memory_order_relaxed);
#endif
    return false;
  }

  int expected = index;
  if (!this->pending_surface_.compare_exchange_strong(
          expected, -1, std::memory_order_acq_rel)) {
    return false;
  }
  this->front_surface_.store(index, std::memory_order_release);
  return this->direct_mipi_display_->present_buffer_region(
      this->surfaces_[index], native_x, native_y, output_width,
      output_height);
#else
  if (index < 0 || index > 1 || this->direct_display_ == nullptr ||
      this->direct_display_ppa_ == nullptr ||
      this->video_container_ == nullptr || this->surfaces_[index] == nullptr ||
      this->surface_content_width_[index] == 0 ||
      this->surface_content_height_[index] == 0 ||
      this->decoded_storage_width_ == 0 ||
      this->decoded_storage_height_ == 0) {
    return false;
  }

  const int scratch = 1 - index;
  if (this->surfaces_[scratch] == nullptr)
    return false;

  lv_area_t container_area{};
  lv_obj_get_coords(this->video_container_, &container_area);
  const int content_width = this->surface_content_width_[index];
  const int content_height = this->surface_content_height_[index];
  const int input_width = this->decoded_storage_width_;
  const int input_height = this->decoded_storage_height_;
  const int container_width = lv_area_get_width(&container_area);
  const int container_height = lv_area_get_height(&container_area);
  if (container_width <= 0 || container_height <= 0)
    return false;

  // Fill as much of the LVGL-owned rectangle as the decoded-surface storage
  // permits while preserving aspect ratio. PPA performs both upscaling and
  // downscaling in hardware; no software resampler or full-frame LVGL redraw
  // enters this path.
  static constexpr int kPpaScaleUnits = 16;
  int scale_units = std::min(
      container_width * kPpaScaleUnits / content_width,
      container_height * kPpaScaleUnits / content_height);
  if (scale_units < 1)
    return false;

  // Both decode buffers alternate as the PPA target. Bound enlargement to the
  // already allocated surface instead of reserving another display-sized
  // framebuffer. Also cap each presentation to a quarter-megapixel: PPA and
  // the DPI DMA copy share PSRAM with camera, JPEG and AFE, and a stale video
  // frame is less useful than uninterrupted call audio. Geometry selection is
  // a short integer-only binary search; every pixel operation remains in PPA.
  static constexpr size_t kMaxPresentationPixels = 256U * 1024U;
  const auto output_fits = [&](int units) {
    const size_t width =
        static_cast<size_t>(content_width) * units / kPpaScaleUnits;
    const size_t height =
        static_cast<size_t>(content_height) * units / kPpaScaleUnits;
    if (width == 0 || height == 0)
      return false;
    const size_t pixels = width * height;
    const size_t pixel_capacity =
        this->surface_capacity_bytes_ / sizeof(uint16_t);
    return width <= pixel_capacity / height &&
           pixels <= kMaxPresentationPixels;
  };
  if (!output_fits(scale_units)) {
    int low = 1;
    int high = scale_units;
    while (low < high) {
      const int candidate = low + (high - low + 1) / 2;
      if (output_fits(candidate))
        low = candidate;
      else
        high = candidate - 1;
    }
    scale_units = low;
  }
  if (!output_fits(scale_units))
    return false;

  const float scale =
      static_cast<float>(scale_units) / kPpaScaleUnits;
  const int fitted_width = content_width * scale_units / kPpaScaleUnits;
  const int fitted_height = content_height * scale_units / kPpaScaleUnits;
  const int logical_x =
      container_area.x1 +
      (container_width - fitted_width) / 2;
  const int logical_y =
      container_area.y1 +
      (container_height - fitted_height) / 2;
  const int native_width = this->direct_display_->get_native_width();
  const int native_height = this->direct_display_->get_native_height();

  int native_x = logical_x;
  int native_y = logical_y;
  int output_width = fitted_width;
  int output_height = fitted_height;
  ppa_srm_rotation_angle_t angle = PPA_SRM_ROTATION_ANGLE_0;
  switch (this->display_rotation_) {
  case 90:
    native_x = native_width - logical_y - fitted_height;
    native_y = logical_x;
    output_width = fitted_height;
    output_height = fitted_width;
    angle = PPA_SRM_ROTATION_ANGLE_270;
    break;
  case 180:
    native_x = native_width - logical_x - fitted_width;
    native_y = native_height - logical_y - fitted_height;
    angle = PPA_SRM_ROTATION_ANGLE_180;
    break;
  case 270:
    native_x = logical_y;
    native_y = native_height - logical_x - fitted_width;
    output_width = fitted_height;
    output_height = fitted_width;
    angle = PPA_SRM_ROTATION_ANGLE_90;
    break;
  default:
    break;
  }
  if (native_x < 0 || native_y < 0 ||
      native_x + output_width > native_width ||
      native_y + output_height > native_height ||
      static_cast<size_t>(output_width) * output_height *
              sizeof(uint16_t) >
          this->surface_capacity_bytes_) {
    return false;
  }

  ppa_srm_oper_config_t config{};
  config.in.buffer = this->surfaces_[index];
  config.in.pic_w = input_width;
  config.in.pic_h = input_height;
  config.in.block_w = content_width;
  config.in.block_h = content_height;
  config.in.srm_cm = PPA_SRM_COLOR_MODE_RGB565;
  config.out.buffer = this->surfaces_[scratch];
  config.out.buffer_size = this->surface_capacity_bytes_;
  config.out.pic_w = output_width;
  config.out.pic_h = output_height;
  config.out.srm_cm = PPA_SRM_COLOR_MODE_RGB565;
  config.rotation_angle = angle;
  config.scale_x = scale;
  config.scale_y = scale;
  config.mode = PPA_TRANS_MODE_BLOCKING;
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  const uint32_t ppa_started_us = micros();
#endif
  const esp_err_t error =
      ppa_do_scale_rotate_mirror(this->direct_display_ppa_, &config);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  update_max(this->rx_present_ppa_max_us_, micros() - ppa_started_us);
#endif
  if (error != ESP_OK) {
    ESP_LOGW(TAG, "Direct video presentation PPA failed: %s",
             esp_err_to_name(error));
    return false;
  }

  // PPA has finished reading `index`. Publish the rotated scratch buffer as
  // the display-owned front and release `index` before the blocking DSI
  // transfer. The decoder can now prepare the next frame in parallel with the
  // current panel transfer, using the same two buffers and no polling.
  this->front_surface_.store(scratch, std::memory_order_release);
  int expected = index;
  if (!this->pending_surface_.compare_exchange_strong(
          expected, -1, std::memory_order_acq_rel)) {
    return false;
  }
  this->direct_display_->draw_pixels_at(
      native_x, native_y, output_width, output_height,
      this->surfaces_[scratch], display::COLOR_ORDER_RGB,
      display::COLOR_BITNESS_565, false);
  return true;
#endif
}

bool P4VideoRenderer::commit_direct_surface_(int index) {
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  const uint32_t started_ms = millis();
#endif
  if (!this->present_surface_direct_(index))
    return false;
  this->surface_ever_presented_.store(true, std::memory_order_release);
  this->rx_presented_frames_.fetch_add(1, std::memory_order_relaxed);
  this->rx_refresh_completed_.fetch_add(1, std::memory_order_relaxed);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  update_max(this->rx_refresh_max_ms_, millis() - started_ms);
#endif
  return true;
}
#endif

#ifdef USE_P4_VIDEO_RENDERER_JPEG
uint16_t P4VideoRenderer::jpeg_storage_width_(
    const jpeg_decode_picture_info_t &picture) {
  const uint32_t alignment =
      picture.sample_method == JPEG_DOWN_SAMPLING_YUV444 ? 8U : 16U;
  return static_cast<uint16_t>((picture.width + alignment - 1U) &
                               ~(alignment - 1U));
}

uint16_t P4VideoRenderer::jpeg_storage_height_(
    const jpeg_decode_picture_info_t &picture) {
  const uint32_t alignment =
      picture.sample_method == JPEG_DOWN_SAMPLING_YUV420 ? 16U : 8U;
  return static_cast<uint16_t>((picture.height + alignment - 1U) &
                               ~(alignment - 1U));
}

bool P4VideoRenderer::update_jpeg_picture_info_(const uint8_t *data,
                                                size_t size) {
  jpeg_decode_picture_info_t picture{};
  if (data == nullptr || size == 0 || size > UINT32_MAX ||
      jpeg_decoder_get_info(data, static_cast<uint32_t>(size), &picture) !=
          ESP_OK ||
      picture.sample_method == JPEG_DOWN_SAMPLING_GRAY ||
      picture.width == 0 || picture.height == 0 ||
      picture.width > this->max_decode_width_ ||
      picture.height > this->max_decode_height_) {
    this->jpeg_picture_info_valid_ = false;
    return false;
  }
  const uint16_t storage_width = jpeg_storage_width_(picture);
  const uint16_t storage_height = jpeg_storage_height_(picture);
  const size_t required =
      static_cast<size_t>(storage_width) * storage_height * 2U;
  if (storage_width == 0 || storage_height == 0 ||
      this->surfaces_[0] == nullptr || this->surfaces_[1] == nullptr ||
      required > this->surface_capacity_bytes_) {
    this->jpeg_picture_info_valid_ = false;
    return false;
  }
  this->jpeg_picture_info_ = picture;
  this->decoded_storage_width_ = storage_width;
  this->decoded_storage_height_ = storage_height;
  this->jpeg_picture_info_valid_ = true;
  return true;
}
#endif

bool P4VideoRenderer::allocate_session_resources_() {
  const size_t encoded_buffer_bytes = kMaxAccessUnitBytes;
#ifdef USE_P4_VIDEO_RENDERER_H264
  for (auto &slot : this->h264_au_slots_) {
    if (slot.data == nullptr) {
      slot.data = static_cast<uint8_t *>(heap_caps_aligned_alloc(
          16, encoded_buffer_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    }
    if (slot.data == nullptr) {
      this->free_codec_resources_();
      this->free_unpublished_surfaces_();
      return false;
    }
  }
#else
  if (this->rx_au_ == nullptr) {
    this->rx_au_ = static_cast<uint8_t *>(heap_caps_aligned_alloc(
        16, encoded_buffer_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    this->rx_au_capacity_ = this->rx_au_ == nullptr ? 0 : encoded_buffer_bytes;
  }
  if (this->rx_au_ == nullptr || this->rx_au_capacity_ < encoded_buffer_bytes) {
    this->free_codec_resources_();
    this->free_unpublished_surfaces_();
    return false;
  }
#endif

  // Secure codec working memory before display storage. Keep owned buffers
  // across calls so they do not fragment esp_video's MMAP allocations.
#ifdef USE_P4_VIDEO_RENDERER_JPEG
  // Decode directly into the non-front LVGL surface. A separate full-screen
  // PPA target made a 400x400 call invalidate 1280x720 pixels and throttled
  // both presentation and the standard camera callback on the main loop.
  const size_t surface_bytes =
      static_cast<size_t>((this->max_decode_width_ + 15U) & ~15U) *
      ((this->max_decode_height_ + 15U) & ~15U) * sizeof(uint16_t);
#endif

#ifdef USE_P4_VIDEO_RENDERER_H264
  const size_t h264_yuv_bytes = this->h264_optimized_yuv_bytes_();
  if (this->optimized_yuv420_ == nullptr) {
    this->optimized_yuv420_ =
        static_cast<uint8_t *>(alloc_psram_dma(h264_yuv_bytes));
    this->optimized_yuv420_capacity_ =
        this->optimized_yuv420_ == nullptr ? 0 : h264_yuv_bytes;
  }
  if (this->optimized_yuv420_ == nullptr ||
      this->optimized_yuv420_capacity_ < h264_yuv_bytes) {
    this->free_codec_resources_();
    this->free_unpublished_surfaces_();
    return false;
  }
  const size_t surface_bytes =
      this->direct_mipi_display_->get_frame_buffer_size();
  if (surface_bytes == 0) {
    this->free_codec_resources_();
    return false;
  }
#endif

  this->surface_capacity_bytes_ = surface_bytes;
  const size_t surface_count = 2;
  for (size_t index = 0; index < surface_count; index++) {
    if (this->surfaces_[index] != nullptr)
      continue;
    const size_t remaining_surfaces =
        (surface_count - index - 1) * surface_bytes;
    this->surfaces_[index] = static_cast<uint8_t *>(
        alloc_surface(surface_bytes, remaining_surfaces));
    if (this->surfaces_[index] == nullptr) {
      this->free_codec_resources_();
      this->free_unpublished_surfaces_();
      return false;
    }
    memset(this->surfaces_[index], 0, surface_bytes);
    this->surface_data_size_[index] = 0;
    this->surface_stride_bytes_[index] = 0;
    this->surface_content_width_[index] = 0;
    this->surface_content_height_[index] = 0;
#ifdef USE_P4_VIDEO_RENDERER_H264
    this->surface_native_x_[index] = 0;
    this->surface_native_y_[index] = 0;
    this->surface_layout_area_[index] = 0;
    this->surface_native_size_[index] = 0;
#endif
  }
  return true;
}

#ifdef USE_P4_VIDEO_RENDERER_H264
bool P4VideoRenderer::reset_h264_decoder_() {
  if (this->h264_decoder_ != nullptr) {
    esp_h264_dec_close(this->h264_decoder_);
    esp_h264_dec_del(this->h264_decoder_);
    this->h264_decoder_ = nullptr;
  }
  esp_h264_dec_cfg_sw_t config{};
  config.pic_type = ESP_H264_RAW_FMT_I420;
  return esp_h264_dec_sw_new(&config, &this->h264_decoder_) ==
             ESP_H264_ERR_OK &&
         this->h264_decoder_ != nullptr &&
         esp_h264_dec_open(this->h264_decoder_) == ESP_H264_ERR_OK;
}

const char *P4VideoRenderer::h264_receive_profile_level_id_(
    uint16_t width, uint16_t height, uint8_t max_fps) {
  const uint32_t macroblocks =
      ((static_cast<uint32_t>(width) + 15U) / 16U) *
      ((static_cast<uint32_t>(height) + 15U) / 16U);
  const uint32_t macroblocks_per_second =
      macroblocks * std::max<uint32_t>(1U, max_fps);
  struct LevelLimit {
    uint16_t max_macroblocks;
    uint32_t max_macroblocks_per_second;
    const char *profile_level_id;
  };
  // Smallest constrained-baseline level whose Annex A limits cover the
  // configured receive envelope. With bilateral level asymmetry this limits
  // browser -> P4 complexity without reducing the P4 hardware encoder's
  // independent outbound level.
  static constexpr LevelLimit LEVELS[] = {
      {99, 1485, "42c00a"},   {396, 3000, "42c00b"},
      {396, 6000, "42c00c"},  {396, 11880, "42c00d"},
      {792, 19800, "42c015"}, {1620, 20250, "42c016"},
      {1620, 40500, "42c01e"},
  };
  for (const auto &level : LEVELS) {
    if (macroblocks <= level.max_macroblocks &&
        macroblocks_per_second <= level.max_macroblocks_per_second) {
      return level.profile_level_id;
    }
  }
  return "42c01e";
}

bool P4VideoRenderer::h264_resolution_fits_(uint16_t width,
                                            uint16_t height) const {
  const bool configured_orientation =
      width <= this->max_decode_width_ && height <= this->max_decode_height_;
  const bool rotated_orientation =
      width <= this->max_decode_height_ && height <= this->max_decode_width_;
  if (width == 0 || height == 0 || (width & 1U) != 0 ||
      (height & 1U) != 0 ||
      (!configured_orientation && !rotated_orientation)) {
    return false;
  }
  const size_t macroblocks_w = (static_cast<size_t>(width) + 15) / 16;
  const size_t macroblocks_h = (static_cast<size_t>(height) + 15) / 16;
  return macroblocks_w * macroblocks_h <= kH264Level30MaxMacroblocks;
}
#endif

bool P4VideoRenderer::start_video(
    const voip_stack::VideoCapability &capability) {
  if (this->is_failed() || !capability.valid()
#ifdef USE_P4_VIDEO_RENDERER_H264
      || !capability.is_h264() ||
      !voip_stack::h264_level_fits(
          capability.profile_level_id,
          this->h264_receive_profile_level_id_(
              this->max_decode_width_, this->max_decode_height_,
              this->framerate_)) ||
      this->h264_decoder_ == nullptr
#else
      || !capability.is_jpeg()
#endif
      || !this->rx_running_.load(std::memory_order_acquire) ||
      this->rx_session_prepared_.exchange(true, std::memory_order_acq_rel)
  ) {
    return false;
  }
  this->rx_capability_ = capability;
  // Session generations make an AU already owned by the persistent worker
  // harmless across a fast hangup/redial.
  this->rx_session_generation_.fetch_add(1, std::memory_order_acq_rel);
#ifdef USE_P4_VIDEO_RENDERER_H264
  // A fresh IDR carries the session-owned parameter sets and resets reference
  // picture state. Keep the decoder allocation itself component-owned.
  this->loss_generation_.fetch_add(1, std::memory_order_acq_rel);
  this->waiting_for_key_frame_.store(true, std::memory_order_release);
#endif
  if (this->rx_task_handle_ != nullptr)
    xTaskNotifyGive(this->rx_task_handle_);
  this->cadence_.reset(std::max<uint8_t>(
      1, std::min(this->framerate_, capability.max_fps == 0
                                       ? this->framerate_
                                       : capability.max_fps)));
  this->rx_admitted_frames_.store(0, std::memory_order_release);
  this->rx_rendered_frames_.store(0, std::memory_order_release);
  this->rx_presented_frames_.store(0, std::memory_order_release);
  this->rx_refresh_completed_.store(0, std::memory_order_release);
#ifdef USE_P4_VIDEO_RENDERER_H264
  this->h264_first_au_logged_ = false;
#endif
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  this->rx_refresh_max_ms_.store(0, std::memory_order_release);
  this->rx_present_ppa_max_us_.store(0, std::memory_order_release);
  this->presentation_started_ms_.store(0, std::memory_order_release);
  this->rx_rate_drops_.store(0, std::memory_order_release);
  this->rx_busy_drops_.store(0, std::memory_order_release);
  this->rx_decode_drops_.store(0, std::memory_order_release);
#ifdef USE_P4_VIDEO_RENDERER_JPEG
  this->rx_jpeg_decode_max_us_.store(0, std::memory_order_release);
  this->rx_au_work_max_us_.store(0, std::memory_order_release);
#endif
#ifdef USE_P4_VIDEO_RENDERER_H264
  this->h264_decode_failure_logged_ = false;
  this->rx_h264_decode_max_us_.store(0, std::memory_order_release);
  this->rx_i420_convert_max_us_.store(0, std::memory_order_release);
  this->rx_ppa_max_us_.store(0, std::memory_order_release);
  this->rx_au_work_max_us_.store(0, std::memory_order_release);
  this->rx_queue_high_watermark_.store(0, std::memory_order_release);
  this->rx_queue_wait_max_us_.store(0, std::memory_order_release);
  this->rx_dependency_drops_.store(0, std::memory_order_release);
  this->rx_geometry_drops_.store(0, std::memory_order_release);
  this->rx_au_copy_max_us_.store(0, std::memory_order_release);
  this->rx_au_copy_total_us_.store(0, std::memory_order_release);
  this->rx_au_copy_total_bytes_.store(0, std::memory_order_release);
#endif
#endif
  this->rx_active_.store(false, std::memory_order_release);
  this->remote_frame_visible_.store(false, std::memory_order_release);
#ifdef USE_P4_VIDEO_RENDERER_H264
  ESP_LOGI(TAG, "P4 software H.264 receive path prepared");
#else
  ESP_LOGI(TAG, "P4 hardware JPEG receive path prepared");
#endif
  return true;
}

bool P4VideoRenderer::set_video_active(bool active) {
  if (active &&
      (!this->rx_running_.load(std::memory_order_acquire) ||
       !this->rx_session_prepared_.load(std::memory_order_acquire)))
    return false;
  this->rx_active_.store(active, std::memory_order_release);
  if (active) {
    this->cadence_.reset(std::max<uint8_t>(
        1, std::min(this->framerate_, this->rx_capability_.max_fps == 0
                                         ? this->framerate_
                                         : this->rx_capability_.max_fps)));
#ifdef USE_P4_VIDEO_RENDERER_H264
    this->waiting_for_key_frame_.store(true, std::memory_order_release);
#endif
    return true;
  }

  // Invalidate the generation before touching presentation state. A decoder
  // AU may already be owned, but every publish site rechecks rx_active_. Drop
  // a decoded surface still waiting for presentation. A surface handed to DSI
  // remains immutable until the serialized presentation owner releases it.
  xSemaphoreTake(this->presentation_mutex_, portMAX_DELAY);
  this->pending_surface_.store(-1, std::memory_order_release);
  this->rx_session_generation_.fetch_add(1, std::memory_order_acq_rel);
#ifdef USE_P4_VIDEO_RENDERER_H264
  this->loss_generation_.fetch_add(1, std::memory_order_acq_rel);
  this->waiting_for_key_frame_.store(true, std::memory_order_release);
#endif
  if (this->rx_task_handle_ != nullptr)
    xTaskNotifyGive(this->rx_task_handle_);
  const bool was_visible =
      this->remote_frame_visible_.exchange(false, std::memory_order_acq_rel);
  xSemaphoreGive(this->presentation_mutex_);
  if (was_visible) {
    this->video_ended_pending_.store(true, std::memory_order_release);
    this->enable_loop_soon_any_context();
  }
  return true;
}

bool P4VideoRenderer::consume_video_access_unit(
    const voip_stack::EncodedVideoAccessUnit &access_unit) {
  if (!this->rx_running_.load(std::memory_order_acquire) ||
      !this->rx_active_.load(std::memory_order_acquire) ||
      access_unit.data == nullptr || access_unit.size == 0) {
    return false;
  }
#ifdef USE_P4_VIDEO_RENDERER_H264
  if (!this->rx_session_prepared_.load(std::memory_order_acquire) ||
      this->h264_au_queue_ == nullptr ||
      access_unit.size > kMaxAccessUnitBytes) {
    this->loss_generation_.fetch_add(1, std::memory_order_acq_rel);
    this->waiting_for_key_frame_.store(true, std::memory_order_release);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
    this->rx_busy_drops_.fetch_add(1, std::memory_order_relaxed);
#endif
    return false;
  }
  if (this->waiting_for_key_frame_.load(std::memory_order_acquire) &&
      !access_unit.key_frame) {
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
    this->rx_dependency_drops_.fetch_add(1, std::memory_order_relaxed);
#endif
    return false;
  }

  uint8_t slot_index = UINT8_MAX;
  for (uint8_t index = 0; index < kH264AccessUnitQueueDepth; index++) {
    uint8_t expected = 0;
    if (this->h264_au_slots_[index].state.compare_exchange_strong(
            expected, 2, std::memory_order_acq_rel)) {
      slot_index = index;
      break;
    }
  }
  if (slot_index == UINT8_MAX) {
    // A full bounded queue means the decoder has fallen behind. Dependent
    // frames already queued under the previous generation are discarded and
    // RTP requests a fresh IDR; memory use never grows with network jitter.
    this->loss_generation_.fetch_add(1, std::memory_order_acq_rel);
    this->waiting_for_key_frame_.store(true, std::memory_order_release);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
    this->rx_busy_drops_.fetch_add(1, std::memory_order_relaxed);
#endif
    return false;
  }

  auto &slot = this->h264_au_slots_[slot_index];
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  const uint32_t copy_started_us = micros();
#endif
  memcpy(slot.data, access_unit.data, access_unit.size);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  const uint32_t copy_us = micros() - copy_started_us;
  update_max(this->rx_au_copy_max_us_, copy_us);
  this->rx_au_copy_total_us_.fetch_add(copy_us, std::memory_order_relaxed);
  this->rx_au_copy_total_bytes_.fetch_add(access_unit.size,
                                          std::memory_order_relaxed);
#endif
  slot.size = access_unit.size;
  slot.timestamp = access_unit.timestamp_90khz;
  slot.key_frame = access_unit.key_frame;
  slot.session_generation =
      this->rx_session_generation_.load(std::memory_order_acquire);
  slot.loss_generation =
      this->loss_generation_.load(std::memory_order_acquire);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  slot.queued_at_us = micros();
#endif
  slot.state.store(1, std::memory_order_release);
  if (xQueueSend(this->h264_au_queue_, &slot_index, 0) != pdTRUE) {
    slot.state.store(0, std::memory_order_release);
    this->loss_generation_.fetch_add(1, std::memory_order_acq_rel);
    this->waiting_for_key_frame_.store(true, std::memory_order_release);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
    this->rx_busy_drops_.fetch_add(1, std::memory_order_relaxed);
#endif
    return false;
  }
  if (access_unit.key_frame)
    this->waiting_for_key_frame_.store(false, std::memory_order_release);
  this->rx_admitted_frames_.fetch_add(1, std::memory_order_relaxed);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  update_max(this->rx_queue_high_watermark_,
             uxQueueMessagesWaiting(this->h264_au_queue_));
#endif
  if (this->rx_task_handle_ != nullptr)
    xTaskNotifyGive(this->rx_task_handle_);
  return true;
#else
  if (access_unit.size > this->rx_au_capacity_)
    return false;
  // JPEG frames are independent and can be discarded while the previous
  // surface awaits LVGL.
  if (this->pending_surface_.load(std::memory_order_acquire) >= 0) {
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
    this->rx_busy_drops_.fetch_add(1, std::memory_order_relaxed);
#endif
    return false;
  }
  if (!this->cadence_.accept(access_unit.timestamp_90khz)) {
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
    this->rx_rate_drops_.fetch_add(1, std::memory_order_relaxed);
#endif
    return false;
  }
  uint8_t expected = 0;
  if (!this->rx_slot_state_.compare_exchange_strong(
          expected, 2, std::memory_order_acq_rel)) {
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
    this->rx_busy_drops_.fetch_add(1, std::memory_order_relaxed);
#endif
    return false;
  }
  memcpy(this->rx_au_, access_unit.data, access_unit.size);
  this->rx_au_size_ = access_unit.size;
  this->rx_timestamp_ = access_unit.timestamp_90khz;
  this->rx_au_generation_ =
      this->rx_session_generation_.load(std::memory_order_acquire);
  this->rx_key_frame_ = access_unit.key_frame;
  this->rx_admitted_frames_.fetch_add(1, std::memory_order_relaxed);
  this->rx_slot_state_.store(1, std::memory_order_release);
  if (this->rx_task_handle_ != nullptr)
    xTaskNotifyGive(this->rx_task_handle_);
  return true;
#endif
}

bool P4VideoRenderer::start_rx_task_() {
  if (this->rx_task_handle_ != nullptr)
    return false;
  xSemaphoreTake(this->rx_done_, 0);
  this->rx_done_observed_ = false;
  // Codec work stays below RTP/audio priorities and blocks indefinitely on a
  // direct notification. H.264 drains only its fixed-depth AU queue.
  return voip_audio_core::start_managed_pinned_task(
      P4VideoRenderer::rx_task_trampoline_, "p4_video_rx", kTaskStackBytes,
      this, kTaskPriority, 1, this->task_stacks_in_psram_, TAG,
      &this->rx_task_handle_, &this->rx_task_tcb_, &this->rx_task_stack_,
      &this->rx_task_with_caps_);
}

void P4VideoRenderer::rx_task_trampoline_(void *ctx) {
  static_cast<P4VideoRenderer *>(ctx)->rx_task_();
}

#ifdef USE_P4_VIDEO_RENDERER_H264
bool P4VideoRenderer::decode_h264_access_unit_(const uint8_t *data, size_t size,
                                               uint32_t timestamp_90khz,
                                               bool key_frame,
                                               uint32_t session_generation,
                                               uint32_t loss_generation,
                                               bool &decoded) {
  decoded = false;
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  const uint32_t au_started_us = micros();
#endif
  if (key_frame &&
      this->decoder_reset_pending_.exchange(false, std::memory_order_acq_rel) &&
      !this->reset_h264_decoder_()) {
    ESP_LOGE(TAG, "Failed to reset H.264 decoder for recovery IDR");
    this->rx_running_.store(false, std::memory_order_release);
    return false;
  }
  if (!this->h264_first_au_logged_) {
    this->h264_first_au_logged_ = true;
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
    ESP_LOGI(TAG,
             "H.264 first AU: bytes=%u key=%s; PSRAM free=%u largest=%u; "
             "internal free=%u largest=%u",
             static_cast<unsigned>(size), YESNO(key_frame),
             static_cast<unsigned>(
                 heap_caps_get_free_size(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)),
             static_cast<unsigned>(heap_caps_get_largest_free_block(
                 MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)),
             static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL |
                                                           MALLOC_CAP_8BIT)),
             static_cast<unsigned>(heap_caps_get_largest_free_block(
                 MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)));
#else
    ESP_LOGI(TAG, "H.264 first AU: bytes=%u key=%s",
             static_cast<unsigned>(size), YESNO(key_frame));
#endif
  }

  bool rendered = false;
  bool decode_failed = false;
  // Espressif's decoder consumes an Annex-B access unit incrementally and
  // reports the byte count consumed for each NAL. Keep the start codes in
  // place and follow the component's official single-decoder example.
  esp_h264_dec_in_frame_t input{};
  input.raw_data.buffer = const_cast<uint8_t *>(data);
  input.raw_data.len = size;
  input.pts = timestamp_90khz;
  while (input.raw_data.len > 0 &&
         this->rx_running_.load(std::memory_order_acquire) &&
         this->rx_active_.load(std::memory_order_acquire) &&
         session_generation ==
             this->rx_session_generation_.load(std::memory_order_acquire)) {
    esp_h264_dec_out_frame_t output{};
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
    const uint32_t decode_started_us = micros();
#endif
    const esp_h264_err_t error =
        esp_h264_dec_process(this->h264_decoder_, &input, &output);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
    update_max(this->rx_h264_decode_max_us_, micros() - decode_started_us);
#endif
    if (error != ESP_H264_ERR_OK || input.consume == 0 ||
        input.consume > input.raw_data.len) {
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
      if (!this->h264_decode_failure_logged_) {
        this->h264_decode_failure_logged_ = true;
        ESP_LOGW(TAG,
                 "H.264 decode failure: error=%d consume=%u remaining=%u; "
                 "PSRAM free=%u largest=%u; internal free=%u largest=%u",
                 static_cast<int>(error), static_cast<unsigned>(input.consume),
                 static_cast<unsigned>(input.raw_data.len),
                 static_cast<unsigned>(heap_caps_get_free_size(
                     MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)),
                 static_cast<unsigned>(heap_caps_get_largest_free_block(
                     MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)),
                 static_cast<unsigned>(heap_caps_get_free_size(
                     MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)),
                 static_cast<unsigned>(heap_caps_get_largest_free_block(
                     MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)));
      }
#endif
      decode_failed = true;
      break;
    }
    input.raw_data.buffer += input.consume;
    input.raw_data.len -= input.consume;
    if (output.outbuf == nullptr || output.out_size == 0)
      continue;
    decoded = true;
    if (session_generation !=
            this->rx_session_generation_.load(std::memory_order_acquire) ||
        !this->rx_active_.load(std::memory_order_acquire)) {
      break;
    }
    // Decode every H.264 picture so dependent P-frames retain a valid GOP,
    // but spend conversion, PPA and DSI bandwidth only at the negotiated
    // presentation cadence. Peers sometimes exceed max-fps; dropping before
    // decode would corrupt subsequent references and force repeated IDRs.
    if (!this->cadence_.accept(output.pts)) {
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
      this->rx_rate_drops_.fetch_add(1, std::memory_order_relaxed);
#endif
      continue;
    }
    esp_h264_dec_param_sw_handle_t parameters = nullptr;
    esp_h264_resolution_t resolution{};
    if (esp_h264_dec_sw_get_param_hd(this->h264_decoder_, &parameters) ==
            ESP_H264_ERR_OK &&
        parameters != nullptr &&
        esp_h264_dec_get_resolution(parameters, &resolution) ==
            ESP_H264_ERR_OK) {
      rendered = this->render_i420_(output.outbuf, output.out_size,
                                    static_cast<uint16_t>(resolution.width),
                                    static_cast<uint16_t>(resolution.height),
                                    session_generation) ||
                 rendered;
    }
  }
  if (decode_failed) {
    this->loss_generation_.fetch_add(1, std::memory_order_acq_rel);
    this->waiting_for_key_frame_.store(true, std::memory_order_release);
    this->decoder_reset_pending_.store(true, std::memory_order_release);
  } else if (session_generation ==
                 this->rx_session_generation_.load(std::memory_order_acquire) &&
             loss_generation ==
                 this->loss_generation_.load(std::memory_order_acquire)) {
    // Keep admission open only if neither teardown nor a later slot loss
    // invalidated this GOP while the AU was being decoded.
    this->waiting_for_key_frame_.store(false, std::memory_order_release);
  }
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  update_max(this->rx_au_work_max_us_, micros() - au_started_us);
#endif
  return rendered;
}
#endif

void P4VideoRenderer::rx_task_() {
  while (this->rx_running_.load(std::memory_order_acquire)) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    if (!this->rx_running_.load(std::memory_order_acquire))
      break;
#ifdef USE_P4_VIDEO_RENDERER_H264
    uint8_t slot_index = UINT8_MAX;
    while (xQueueReceive(this->h264_au_queue_, &slot_index, 0) == pdTRUE) {
      if (slot_index >= kH264AccessUnitQueueDepth)
        continue;
      auto &slot = this->h264_au_slots_[slot_index];
      uint8_t expected = 1;
      if (!slot.state.compare_exchange_strong(
              expected, 2, std::memory_order_acq_rel)) {
        continue;
      }
      const bool current =
          this->rx_active_.load(std::memory_order_acquire) &&
          this->rx_session_prepared_.load(std::memory_order_acquire) &&
          slot.session_generation ==
              this->rx_session_generation_.load(std::memory_order_acquire) &&
          slot.loss_generation ==
              this->loss_generation_.load(std::memory_order_acquire);
      bool rendered = false;
      bool decoded = false;
      if (current) {
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
        update_max(this->rx_queue_wait_max_us_,
                   micros() - slot.queued_at_us);
#endif
        rendered = this->decode_h264_access_unit_(
            slot.data, slot.size, slot.timestamp, slot.key_frame,
            slot.session_generation, slot.loss_generation, decoded);
      }
      if (current) {
        if (rendered)
          this->rx_rendered_frames_.fetch_add(1, std::memory_order_relaxed);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
        else if (!decoded)
          this->rx_decode_drops_.fetch_add(1, std::memory_order_relaxed);
#endif
      }
      slot.state.store(0, std::memory_order_release);
    }
#else
    uint8_t expected = 1;
    if (!this->rx_slot_state_.compare_exchange_strong(
            expected, 2, std::memory_order_acq_rel)) {
      continue;
    }
    const uint32_t session_generation = this->rx_au_generation_;
    const bool current =
        this->rx_active_.load(std::memory_order_acquire) &&
        this->rx_session_prepared_.load(std::memory_order_acquire) &&
        session_generation ==
            this->rx_session_generation_.load(std::memory_order_acquire);
    if (!current) {
      this->rx_slot_state_.store(0, std::memory_order_release);
      continue;
    }
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
    const uint32_t au_started_us = micros();
#endif
    bool rendered = false;
    bool picture_info_current =
        this->jpeg_picture_info_valid_ &&
        this->jpeg_picture_info_generation_ == session_generation;
    if (!picture_info_current &&
        this->update_jpeg_picture_info_(this->rx_au_, this->rx_au_size_)) {
      this->jpeg_picture_info_generation_ = session_generation;
      picture_info_current = true;
    }
    if (this->pending_surface_.load(std::memory_order_acquire) < 0 &&
        this->jpeg_decoder_ != nullptr && picture_info_current) {
      const int output_index =
          1 - this->front_surface_.load(std::memory_order_acquire);
      size_t expected_size =
          static_cast<size_t>(this->decoded_storage_width_) *
          this->decoded_storage_height_ * 2U;
      uint32_t decoded_size = 0;
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
      const uint32_t decode_started_us = micros();
#endif
      esp_err_t decode_error = ESP_ERR_INVALID_SIZE;
      if (output_index >= 0 && output_index <= 1 &&
          this->surfaces_[output_index] != nullptr &&
          expected_size <= this->surface_capacity_bytes_ &&
          expected_size <= UINT32_MAX) {
        // ESP-IDF synchronizes exactly outbuf_size bytes before and after the
        // DMA transaction. Pass the padded size of this JPEG, not the maximum
        // 800x800 surface capacity, so a small remote frame does not evict an
        // unrelated megabyte of PSRAM cache while AFE is producing audio.
        decode_error = jpeg_decoder_process(
            this->jpeg_decoder_, &this->jpeg_decode_config_, this->rx_au_,
            static_cast<uint32_t>(this->rx_au_size_),
            this->surfaces_[output_index],
            static_cast<uint32_t>(expected_size),
            &decoded_size);
      }
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
      update_max(this->rx_jpeg_decode_max_us_, micros() - decode_started_us);
#endif
      // The SIP source normally keeps one resolution for the whole session.
      // Reparse only on the first frame or if the hardware reports a changed
      // output size, avoiding a calloc/free pair in every hot-path frame.
      if (decode_error == ESP_OK && decoded_size != expected_size &&
          this->update_jpeg_picture_info_(this->rx_au_, this->rx_au_size_)) {
        this->jpeg_picture_info_generation_ = session_generation;
        expected_size = static_cast<size_t>(this->decoded_storage_width_) *
                        this->decoded_storage_height_ * 2U;
      }
      if (decode_error == ESP_OK && decoded_size == expected_size &&
          this->rx_active_.load(std::memory_order_acquire) &&
          this->rx_session_prepared_.load(std::memory_order_acquire) &&
          session_generation ==
              this->rx_session_generation_.load(std::memory_order_acquire)) {
        this->surface_data_size_[output_index] = decoded_size;
        this->surface_stride_bytes_[output_index] =
            this->decoded_storage_width_ * sizeof(uint16_t);
        this->surface_content_width_[output_index] =
            static_cast<uint16_t>(this->jpeg_picture_info_.width);
        this->surface_content_height_[output_index] =
            static_cast<uint16_t>(this->jpeg_picture_info_.height);
        this->pending_surface_.store(output_index,
                                     std::memory_order_release);
        this->enable_loop_soon_any_context();
        rendered = true;
      }
    }
    if (rendered)
      this->rx_rendered_frames_.fetch_add(1, std::memory_order_relaxed);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
    else
      this->rx_decode_drops_.fetch_add(1, std::memory_order_relaxed);
    update_max(this->rx_au_work_max_us_, micros() - au_started_us);
#endif
    this->rx_slot_state_.store(0, std::memory_order_release);
#endif
  }
#ifdef USE_P4_VIDEO_RENDERER_H264
  for (auto &slot : this->h264_au_slots_)
    slot.state.store(0, std::memory_order_release);
#else
  this->rx_slot_state_.store(0, std::memory_order_release);
#endif
  voip_audio_core::finish_managed_pinned_task(this->rx_done_);
}

#ifdef USE_P4_VIDEO_RENDERER_H264
bool P4VideoRenderer::configure_i420_converter_(uint16_t width,
                                                 uint16_t height) {
  if (this->i420_converter_ != nullptr &&
      this->i420_converter_width_ == width &&
      this->i420_converter_height_ == height) {
    return true;
  }
  if (this->i420_converter_ != nullptr) {
    esp_imgfx_color_convert_close(this->i420_converter_);
    this->i420_converter_ = nullptr;
  }
  this->i420_converter_width_ = 0;
  this->i420_converter_height_ = 0;

  esp_imgfx_color_convert_cfg_t config{};
  config.in_res.width = static_cast<int16_t>(width);
  config.in_res.height = static_cast<int16_t>(height);
  config.in_pixel_fmt = ESP_IMGFX_PIXEL_FMT_I420;
  config.out_pixel_fmt = ESP_IMGFX_PIXEL_FMT_O_UYY_E_VYY;
  config.color_space_std = ESP_IMGFX_COLOR_SPACE_STD_BT601;
  const esp_imgfx_err_t error =
      esp_imgfx_color_convert_open(&config, &this->i420_converter_);
  if (error != ESP_IMGFX_ERR_OK || this->i420_converter_ == nullptr) {
    ESP_LOGE(TAG, "Espressif I420 converter setup failed: %d (%ux%u)",
             static_cast<int>(error), width, height);
    this->i420_converter_ = nullptr;
    return false;
  }
  this->i420_converter_width_ = width;
  this->i420_converter_height_ = height;
  this->i420_converter_failure_logged_ = false;
  return true;
}

bool P4VideoRenderer::refresh_direct_display_layout_() {
  if (this->video_container_ == nullptr || this->direct_display_ == nullptr)
    return false;
  lv_area_t area{};
  lv_obj_get_coords(this->video_container_, &area);
  const int width = lv_area_get_width(&area);
  const int height = lv_area_get_height(&area);
  const int native_width = this->direct_display_->get_native_width();
  const int native_height = this->direct_display_->get_native_height();
  if (width <= 0 || height <= 0 || native_width <= 0 || native_height <= 0 ||
      area.x1 < INT16_MIN || area.x1 > INT16_MAX ||
      area.y1 < INT16_MIN || area.y1 > INT16_MAX ||
      width > UINT16_MAX || height > UINT16_MAX ||
      native_width > UINT16_MAX || native_height > UINT16_MAX) {
    this->direct_layout_area_.store(0, std::memory_order_release);
    this->direct_layout_native_size_.store(0, std::memory_order_release);
    return false;
  }
  const uint64_t packed_area =
      (static_cast<uint64_t>(static_cast<uint16_t>(area.x1)) << 48U) |
      (static_cast<uint64_t>(static_cast<uint16_t>(area.y1)) << 32U) |
      (static_cast<uint64_t>(static_cast<uint16_t>(width)) << 16U) |
      static_cast<uint16_t>(height);
  const uint32_t packed_native =
      (static_cast<uint32_t>(static_cast<uint16_t>(native_width)) << 16U) |
      static_cast<uint16_t>(native_height);
  this->direct_layout_native_size_.store(packed_native,
                                         std::memory_order_release);
  this->direct_layout_area_.store(packed_area, std::memory_order_release);
  return true;
}

bool P4VideoRenderer::compute_h264_surface_geometry_(
    uint16_t width, uint16_t height, H264SurfaceGeometry *geometry) const {
  if (width == 0 || height == 0 || geometry == nullptr)
    return false;
  const uint64_t layout_area =
      this->direct_layout_area_.load(std::memory_order_acquire);
  const uint32_t native_size =
      this->direct_layout_native_size_.load(std::memory_order_acquire);
  if (layout_area == 0 || native_size == 0)
    return false;

  const int container_x = static_cast<int16_t>(layout_area >> 48U);
  const int container_y =
      static_cast<int16_t>((layout_area >> 32U) & 0xffffU);
  const int container_width = static_cast<int>((layout_area >> 16U) & 0xffffU);
  const int container_height = static_cast<int>(layout_area & 0xffffU);
  const int native_width = static_cast<int>(native_size >> 16U);
  const int native_height = static_cast<int>(native_size & 0xffffU);
  if (container_width <= 0 || container_height <= 0 ||
      native_width <= 0 || native_height <= 0)
    return false;

  int scale_units = std::min(
      container_width * static_cast<int>(kPpaScaleUnits) / width,
      container_height * static_cast<int>(kPpaScaleUnits) / height);
  const bool swaps_dimensions =
      this->display_rotation_ == 90 || this->display_rotation_ == 270;
  const auto output_fits = [&](int units) {
    const size_t logical_width =
        static_cast<size_t>(width) * units / kPpaScaleUnits;
    const size_t logical_height =
        static_cast<size_t>(height) * units / kPpaScaleUnits;
    return logical_width > 0 && logical_height > 0 &&
           logical_width <= UINT16_MAX && logical_height <= UINT16_MAX &&
           logical_width <=
               this->surface_capacity_bytes_ /
                   (logical_height * sizeof(uint16_t));
  };
  while (scale_units > 0 && !output_fits(scale_units))
    scale_units--;
  if (scale_units <= 0)
    return false;

  const int logical_width =
      static_cast<int>(width) * scale_units / kPpaScaleUnits;
  const int logical_height =
      static_cast<int>(height) * scale_units / kPpaScaleUnits;
  const int logical_x =
      container_x + (container_width - logical_width) / 2;
  const int logical_y =
      container_y + (container_height - logical_height) / 2;
  int native_x = logical_x;
  int native_y = logical_y;
  int surface_width = swaps_dimensions ? logical_height : logical_width;
  int surface_height = swaps_dimensions ? logical_width : logical_height;
  switch (this->display_rotation_) {
  case 90:
    native_x = native_width - logical_y - logical_height;
    native_y = logical_x;
    break;
  case 180:
    native_x = native_width - logical_x - logical_width;
    native_y = native_height - logical_y - logical_height;
    break;
  case 270:
    native_x = logical_y;
    native_y = native_height - logical_x - logical_width;
    break;
  default:
    break;
  }
  if (native_x < 0 || native_y < 0 || native_x > INT16_MAX ||
      native_y > INT16_MAX || surface_width <= 0 || surface_height <= 0 ||
      native_x + surface_width > native_width ||
      native_y + surface_height > native_height)
    return false;

  geometry->scale_units = static_cast<uint16_t>(scale_units);
  geometry->surface_width = static_cast<uint16_t>(surface_width);
  geometry->surface_height = static_cast<uint16_t>(surface_height);
  geometry->native_x = static_cast<int16_t>(native_x);
  geometry->native_y = static_cast<int16_t>(native_y);
  geometry->layout_area = layout_area;
  geometry->native_size = native_size;
  return true;
}

bool P4VideoRenderer::render_i420_(const uint8_t *i420, size_t size,
                                   uint16_t width, uint16_t height,
                                   uint32_t session_generation) {
  const size_t decoded_bytes = static_cast<size_t>(width) * height * 3 / 2;
  if (i420 == nullptr || !this->h264_resolution_fits_(width, height) ||
      size < decoded_bytes || this->optimized_yuv420_ == nullptr ||
      this->optimized_yuv420_capacity_ < decoded_bytes ||
      this->ppa_ == nullptr || this->direct_mipi_display_ == nullptr ||
      this->surfaces_[0] == nullptr ||
      this->pending_surface_.load(std::memory_order_acquire) >= 0) {
    return false;
  }
  H264SurfaceGeometry geometry{};
  if (!this->compute_h264_surface_geometry_(width, height, &geometry)) {
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
    this->rx_geometry_drops_.fetch_add(1, std::memory_order_relaxed);
#endif
    return false;
  }
  if (!this->configure_i420_converter_(width, height))
    return false;
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  const uint32_t convert_started_us = micros();
#endif
  esp_imgfx_data_t input{
      .data = const_cast<uint8_t *>(i420),
      .data_len = static_cast<uint32_t>(decoded_bytes),
  };
  esp_imgfx_data_t output{
      .data = this->optimized_yuv420_,
      .data_len = static_cast<uint32_t>(decoded_bytes),
  };
  const esp_imgfx_err_t convert_error = esp_imgfx_color_convert_process(
      this->i420_converter_, &input, &output);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  update_max(this->rx_i420_convert_max_us_, micros() - convert_started_us);
#endif
  if (convert_error != ESP_IMGFX_ERR_OK) {
    if (!this->i420_converter_failure_logged_) {
      ESP_LOGE(TAG, "Espressif I420 conversion failed: %d (%ux%u)",
               static_cast<int>(convert_error), width, height);
      this->i420_converter_failure_logged_ = true;
    }
    return false;
  }
  ppa_srm_rotation_angle_t rotation = PPA_SRM_ROTATION_ANGLE_0;
  switch (this->display_rotation_) {
  case 90:
    rotation = PPA_SRM_ROTATION_ANGLE_270;
    break;
  case 180:
    rotation = PPA_SRM_ROTATION_ANGLE_180;
    break;
  case 270:
    rotation = PPA_SRM_ROTATION_ANGLE_90;
    break;
  default:
    break;
  }
  ppa_srm_oper_config_t config{};
  config.in.buffer = this->optimized_yuv420_;
  config.in.pic_w = width;
  config.in.pic_h = height;
  config.in.block_w = width;
  config.in.block_h = height;
  config.in.srm_cm = PPA_SRM_COLOR_MODE_YUV420;
  config.in.yuv_range = PPA_COLOR_RANGE_LIMIT;
  config.in.yuv_std = PPA_COLOR_CONV_STD_RGB_YUV_BT601;
  const int output_index =
      1 - this->front_surface_.load(std::memory_order_acquire);
  config.out.buffer = this->surfaces_[output_index];
  config.out.buffer_size = this->surface_capacity_bytes_;
  config.out.pic_w = geometry.surface_width;
  config.out.pic_h = geometry.surface_height;
  config.out.srm_cm = PPA_SRM_COLOR_MODE_RGB565;
  config.rotation_angle = rotation;
  const float scale =
      static_cast<float>(geometry.scale_units) / kPpaScaleUnits;
  config.scale_x = scale;
  config.scale_y = scale;
  config.mode = PPA_TRANS_MODE_BLOCKING;
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  const uint32_t ppa_started_us = micros();
#endif
  const esp_err_t ppa_error = ppa_do_scale_rotate_mirror(this->ppa_, &config);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  update_max(this->rx_ppa_max_us_, micros() - ppa_started_us);
#endif
  if (ppa_error != ESP_OK ||
      !this->rx_active_.load(std::memory_order_acquire) ||
      session_generation !=
          this->rx_session_generation_.load(std::memory_order_acquire)) {
    return false;
  }
  xSemaphoreTake(this->presentation_mutex_, portMAX_DELAY);
  if (!this->rx_active_.load(std::memory_order_acquire) ||
      session_generation !=
          this->rx_session_generation_.load(std::memory_order_acquire)) {
    xSemaphoreGive(this->presentation_mutex_);
    return false;
  }
  this->prepare_surface_(output_index, geometry);
  this->pending_surface_.store(output_index, std::memory_order_release);
  if (this->direct_page_active_.load(std::memory_order_acquire) &&
      this->commit_direct_surface_(output_index)) {
    xSemaphoreGive(this->presentation_mutex_);
    return true;
  }
  this->enable_loop_soon_any_context();
  xSemaphoreGive(this->presentation_mutex_);
  return true;
}
#endif

#ifdef USE_P4_VIDEO_RENDERER_H264
void P4VideoRenderer::prepare_surface_(
    int index, const H264SurfaceGeometry &geometry) {
  if (index < 0 || index > 1 || this->surfaces_[index] == nullptr)
    return;
  // H.264 direct presentation always fills a tightly packed output rectangle.
  // The unused capacity is never read or sent to the display.
  this->surface_stride_bytes_[index] =
      geometry.surface_width * sizeof(uint16_t);
  this->surface_data_size_[index] =
      static_cast<size_t>(geometry.surface_width) * geometry.surface_height *
      sizeof(uint16_t);
  this->surface_content_width_[index] = geometry.surface_width;
  this->surface_content_height_[index] = geometry.surface_height;
  this->surface_native_x_[index] = geometry.native_x;
  this->surface_native_y_[index] = geometry.native_y;
  this->surface_layout_area_[index] = geometry.layout_area;
  this->surface_native_size_[index] = geometry.native_size;
}
#endif

bool P4VideoRenderer::stop_rx_task_() {
  this->rx_running_.store(false, std::memory_order_release);
  if (this->rx_task_handle_ != nullptr)
    xTaskNotifyGive(this->rx_task_handle_);
  bool stopped = this->reap_rx_task_();
  if (!stopped && !this->rx_done_observed_ && this->rx_done_ != nullptr &&
      xSemaphoreTake(this->rx_done_, kTaskStopTimeoutTicks) == pdTRUE) {
    this->rx_done_observed_ = true;
    stopped = this->reap_rx_task_();
  }
  if (!stopped)
    ESP_LOGE(TAG, "Video RX task did not stop; retaining owned resources");
#ifndef USE_P4_VIDEO_RENDERER_H264
  if (stopped)
    this->rx_slot_state_.store(0, std::memory_order_release);
#endif
  return stopped;
}

bool P4VideoRenderer::reap_rx_task_() {
  if (this->rx_task_handle_ == nullptr) {
    this->rx_done_observed_ = false;
    return true;
  }
  if (!this->rx_done_observed_) {
    if (this->rx_done_ == nullptr ||
        xSemaphoreTake(this->rx_done_, 0) != pdTRUE) {
      return false;
    }
    this->rx_done_observed_ = true;
  }
  voip_audio_core::cleanup_managed_pinned_task(
      &this->rx_task_handle_, &this->rx_task_stack_, kTaskStackBytes,
      this->rx_task_with_caps_);
  if (this->rx_task_handle_ == nullptr) {
    this->rx_done_observed_ = false;
    return true;
  }
  return false;
}

void P4VideoRenderer::free_codec_resources_() {
#ifdef USE_P4_VIDEO_RENDERER_H264
  for (auto &slot : this->h264_au_slots_) {
    if (slot.data != nullptr)
      heap_caps_free(slot.data);
    slot.data = nullptr;
    slot.size = 0;
    slot.state.store(0, std::memory_order_release);
  }
#else
  if (this->rx_au_ != nullptr)
    free(this->rx_au_);
#endif
#ifdef USE_P4_VIDEO_RENDERER_JPEG
  if (this->jpeg_decoder_ != nullptr)
    jpeg_del_decoder_engine(this->jpeg_decoder_);
#endif
#if defined(USE_P4_VIDEO_RENDERER_DIRECT_DISPLAY) &&                       \
    defined(USE_P4_VIDEO_RENDERER_JPEG)
  if (this->direct_display_ppa_ != nullptr)
    ppa_unregister_client(this->direct_display_ppa_);
#endif
#ifdef USE_P4_VIDEO_RENDERER_H264
  if (this->h264_decoder_ != nullptr) {
    esp_h264_dec_close(this->h264_decoder_);
    esp_h264_dec_del(this->h264_decoder_);
    this->h264_decoder_ = nullptr;
  }
  if (this->i420_converter_ != nullptr)
    esp_imgfx_color_convert_close(this->i420_converter_);
  if (this->optimized_yuv420_ != nullptr)
    heap_caps_free(this->optimized_yuv420_);
#endif
#ifndef USE_P4_VIDEO_RENDERER_H264
  this->rx_au_ = nullptr;
  this->rx_au_capacity_ = 0;
#endif
#ifdef USE_P4_VIDEO_RENDERER_JPEG
  this->jpeg_decoder_ = nullptr;
  this->jpeg_picture_info_ = {};
  this->jpeg_picture_info_valid_ = false;
  this->jpeg_picture_info_generation_ = 0;
  this->decoded_storage_width_ = 0;
  this->decoded_storage_height_ = 0;
#endif
#if defined(USE_P4_VIDEO_RENDERER_DIRECT_DISPLAY) &&                       \
    defined(USE_P4_VIDEO_RENDERER_JPEG)
  this->direct_display_ppa_ = nullptr;
#endif
#ifdef USE_P4_VIDEO_RENDERER_H264
  this->i420_converter_ = nullptr;
  this->i420_converter_width_ = 0;
  this->i420_converter_height_ = 0;
  this->i420_converter_failure_logged_ = false;
  this->optimized_yuv420_ = nullptr;
  this->optimized_yuv420_capacity_ = 0;
#endif
}

void P4VideoRenderer::free_unpublished_surfaces_() {
  if (this->surface_ever_presented_.load(std::memory_order_acquire))
    return;
  for (size_t index = 0; index < 2; index++) {
    if (this->surfaces_[index] != nullptr)
      heap_caps_free(this->surfaces_[index]);
    this->surfaces_[index] = nullptr;
    this->surface_data_size_[index] = 0;
    this->surface_stride_bytes_[index] = 0;
    this->surface_content_width_[index] = 0;
    this->surface_content_height_[index] = 0;
#ifdef USE_P4_VIDEO_RENDERER_H264
    this->surface_native_x_[index] = 0;
    this->surface_native_y_[index] = 0;
    this->surface_layout_area_[index] = 0;
    this->surface_native_size_[index] = 0;
#endif
  }
  this->surface_capacity_bytes_ = 0;
  this->front_surface_.store(0, std::memory_order_release);
  this->presentation_in_flight_.store(false, std::memory_order_release);
  this->pending_surface_.store(-1, std::memory_order_release);
}

void P4VideoRenderer::stop_video() {
#ifdef USE_P4_VIDEO_RENDERER_H264
  this->direct_page_active_.store(false, std::memory_order_release);
#endif
  // Keep the large PSRAM buffers reserved across calls so esp_video can
  // recreate its MMAP queues in the same contiguous regions every time.
  this->set_video_active(false);
  // A codec or PPA transaction may be uninterruptible. The component-owned
  // worker discards stale generation-tagged AUs and returns to its blocking
  // notification; SIP teardown never joins it.
  this->rx_session_prepared_.store(false, std::memory_order_release);
#ifdef USE_P4_VIDEO_RENDERER_H264
  ESP_LOGI(TAG, "H.264 RX evidence: admitted=%u rendered=%u presented=%u "
                "refresh_done=%u",
           (unsigned)this->rx_admitted_frames_.load(std::memory_order_relaxed),
           (unsigned)this->rx_rendered_frames_.load(std::memory_order_relaxed),
           (unsigned)this->rx_presented_frames_.load(std::memory_order_relaxed),
           (unsigned)this->rx_refresh_completed_.load(std::memory_order_relaxed));
#endif
#ifdef USE_P4_VIDEO_RENDERER_JPEG
  ESP_LOGI(TAG, "JPEG RX evidence: admitted=%u rendered=%u presented=%u "
                "refresh_done=%u",
           (unsigned)this->rx_admitted_frames_.load(std::memory_order_relaxed),
           (unsigned)this->rx_rendered_frames_.load(std::memory_order_relaxed),
           (unsigned)this->rx_presented_frames_.load(std::memory_order_relaxed),
           (unsigned)this->rx_refresh_completed_.load(std::memory_order_relaxed));
#endif
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
#ifdef USE_P4_VIDEO_RENDERER_H264
  ESP_LOGI(
      TAG,
      "H.264 RX stats: admitted=%u rendered=%u rate_drop=%u "
      "busy_drop=%u dependency_drop=%u decode_drop=%u "
      "geometry_drop=%u presented=%u refresh_done=%u",
      (unsigned)this->rx_admitted_frames_.load(std::memory_order_relaxed),
      (unsigned)this->rx_rendered_frames_.load(std::memory_order_relaxed),
      (unsigned)this->rx_rate_drops_.load(std::memory_order_relaxed),
      (unsigned)this->rx_busy_drops_.load(std::memory_order_relaxed),
      (unsigned)this->rx_dependency_drops_.load(std::memory_order_relaxed),
      (unsigned)this->rx_decode_drops_.load(std::memory_order_relaxed),
      (unsigned)this->rx_geometry_drops_.load(std::memory_order_relaxed),
      (unsigned)this->rx_presented_frames_.load(std::memory_order_relaxed),
      (unsigned)this->rx_refresh_completed_.load(std::memory_order_relaxed));
  ESP_LOGI(
      TAG,
      "H.264 RX timing: refresh_max_ms=%u dec_max_us=%u "
      "convert_max_us=%u ppa_max_us=%u present_ppa_max_us=%u "
      "au_max_us=%u",
      (unsigned)this->rx_refresh_max_ms_.load(std::memory_order_relaxed),
      (unsigned)this->rx_h264_decode_max_us_.load(std::memory_order_relaxed),
      (unsigned)this->rx_i420_convert_max_us_.load(std::memory_order_relaxed),
      (unsigned)this->rx_ppa_max_us_.load(std::memory_order_relaxed),
      (unsigned)this->rx_present_ppa_max_us_.load(
          std::memory_order_relaxed),
      (unsigned)this->rx_au_work_max_us_.load(std::memory_order_relaxed));
  ESP_LOGI(
      TAG,
      "H.264 RX queue: peak=%u wait_max_us=%u copy_max_us=%u "
      "copy_total_us=%llu copy_total_bytes=%llu",
      (unsigned)this->rx_queue_high_watermark_.load(
          std::memory_order_relaxed),
      (unsigned)this->rx_queue_wait_max_us_.load(
          std::memory_order_relaxed),
      (unsigned)this->rx_au_copy_max_us_.load(std::memory_order_relaxed),
      (unsigned long long)this->rx_au_copy_total_us_.load(
          std::memory_order_relaxed),
      (unsigned long long)this->rx_au_copy_total_bytes_.load(
          std::memory_order_relaxed));
#else
  ESP_LOGI(
      TAG,
      "JPEG RX stats: admitted=%u rendered=%u rate_drop=%u "
      "busy_drop=%u decode_drop=%u presented=%u "
      "refresh_done=%u refresh_max_ms=%u "
      "dec_max_us=%u present_ppa_max_us=%u au_max_us=%u",
      (unsigned)this->rx_admitted_frames_.load(std::memory_order_relaxed),
      (unsigned)this->rx_rendered_frames_.load(std::memory_order_relaxed),
      (unsigned)this->rx_rate_drops_.load(std::memory_order_relaxed),
      (unsigned)this->rx_busy_drops_.load(std::memory_order_relaxed),
      (unsigned)this->rx_decode_drops_.load(std::memory_order_relaxed),
      (unsigned)this->rx_presented_frames_.load(std::memory_order_relaxed),
      (unsigned)this->rx_refresh_completed_.load(std::memory_order_relaxed),
      (unsigned)this->rx_refresh_max_ms_.load(std::memory_order_relaxed),
      (unsigned)this->rx_jpeg_decode_max_us_.load(std::memory_order_relaxed),
      (unsigned)this->rx_present_ppa_max_us_.load(
          std::memory_order_relaxed),
      (unsigned)this->rx_au_work_max_us_.load(std::memory_order_relaxed));
#endif
#endif
}

} // namespace esphome::p4_video_renderer

#endif
