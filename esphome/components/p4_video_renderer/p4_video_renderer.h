#pragma once

#include "esphome/core/defines.h"

#if defined(USE_ESP_IDF) && defined(USE_ESPHOME_VOIP_STACK_VIDEO)

#include "esphome/components/display/display.h"
#include "esphome/components/voip_stack/audio_core_task_utils.h"
#include "esphome/components/voip_stack/video.h"
#include "esphome/core/automation.h"
#include "esphome/core/component.h"

#ifdef USE_P4_VIDEO_RENDERER_JPEG
#include "driver/jpeg_decode.h"
#endif
#if defined(USE_P4_VIDEO_RENDERER_H264) ||                                  \
    defined(USE_P4_VIDEO_RENDERER_DIRECT_DISPLAY)
#include "driver/ppa.h"
#endif
#ifdef USE_P4_VIDEO_RENDERER_H264
#include "esp_h264_dec_sw.h"
#include "esp_imgfx_color_convert.h"
#endif
#ifdef USE_P4_VIDEO_RENDERER_DIRECT_DISPLAY
#include "esphome/components/mipi_dsi/mipi_dsi.h"
#endif
#include "lvgl.h"

#include <atomic>
#include <cstddef>
#include <cstdint>

#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/semphr.h>
#include <freertos/task.h>

#if defined(USE_P4_VIDEO_RENDERER_H264) &&                                  \
    !defined(USE_P4_VIDEO_RENDERER_DIRECT_DISPLAY)
#error "P4 H.264 rendering requires the direct display path"
#endif

namespace esphome::p4_video_renderer {

/// Encoded video display adapter for ESP32-P4.
///
/// voip_stack owns SIP, RTP and call lifecycle. This component receives only
/// complete access units and decodes the compile-time selected codec. LVGL owns
/// page/control state; either codec may use a PPA-assisted direct display path.
class P4VideoRenderer : public Component, public voip_stack::EncodedVideoSink {
public:
  void setup() override;
  void on_shutdown() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::DATA; }

  void set_task_stacks_in_psram(bool value) {
    this->task_stacks_in_psram_ = value;
  }
  void set_width(uint16_t value) { this->width_ = value; }
  void set_height(uint16_t value) { this->height_ = value; }
  void set_framerate(uint8_t value) { this->framerate_ = value; }
  void set_max_decode_width(uint16_t value) { this->max_decode_width_ = value; }
  void set_max_decode_height(uint16_t value) {
    this->max_decode_height_ = value;
  }
#ifdef USE_P4_VIDEO_RENDERER_DIRECT_DISPLAY
  void set_direct_display(mipi_dsi::MipiDsi *value) {
    this->direct_display_ = value;
#ifdef USE_P4_VIDEO_RENDERER_H264
    this->direct_mipi_display_ = value;
#endif
  }
  void set_display_rotation(uint16_t value) {
    this->display_rotation_ = value;
  }
#endif

  /// Called from lvgl.on_boot after the YAML container exists.
  void attach_video_container(lv_obj_t *container);
  bool has_remote_frame() const {
    return this->remote_frame_visible_.load(std::memory_order_acquire);
  }
  Trigger<> *get_first_frame_trigger() { return &this->first_frame_trigger_; }
  Trigger<> *get_video_ended_trigger() { return &this->video_ended_trigger_; }

  voip_stack::VideoCapability get_receive_video_capability() const override;
  bool start_video(const voip_stack::VideoCapability &capability) override;
  bool set_video_active(bool active) override;
  void stop_video() override;
  bool consume_video_access_unit(
      const voip_stack::EncodedVideoAccessUnit &access_unit) override;

protected:
#ifdef USE_P4_VIDEO_RENDERER_H264
  // RTP reassembly already caps negotiated H.264 access units at 128 KiB.
  // Eight exact-size slots provide one second of headroom at the negotiated
  // 10 fps when the priority-18 AFE temporarily starves the priority-17
  // decoder. This remains below the former 1.5 MiB fixed-slot allocation and
  // keeps every allocation outside the hot path.
  static constexpr size_t kMaxAccessUnitBytes = 128 * 1024;
  static constexpr size_t kH264AccessUnitQueueDepth = 8;
#else
  // RFC 2435 frames are independent and can be substantially larger.
  static constexpr size_t kMaxAccessUnitBytes = 512 * 1024;
#endif
  static constexpr uint32_t kTaskStackBytes = 12288;
  // Match Espressif's dual-task decoder priority. Audio/AFE stays above it at
  // 18-19, while the two decoder halves can run in parallel on separate cores.
  static constexpr uint8_t kTaskPriority = 17;
  static constexpr TickType_t kTaskStopTimeoutTicks =
      configTICK_RATE_HZ >= 2 ? configTICK_RATE_HZ / 2 : 1;

  bool allocate_session_resources_();
#ifdef USE_P4_VIDEO_RENDERER_H264
  bool init_ppa_();
#endif
#ifdef USE_P4_VIDEO_RENDERER_JPEG
  bool init_jpeg_decoder_();
  bool update_jpeg_picture_info_(const uint8_t *data, size_t size);
  static uint16_t jpeg_storage_width_(
      const jpeg_decode_picture_info_t &picture);
  static uint16_t jpeg_storage_height_(
      const jpeg_decode_picture_info_t &picture);
#endif
#ifdef USE_P4_VIDEO_RENDERER_DIRECT_DISPLAY
  bool present_surface_direct_(int index);
  bool commit_direct_surface_(int index);
#endif
#if defined(USE_P4_VIDEO_RENDERER_JPEG) &&                                  \
    defined(USE_P4_VIDEO_RENDERER_DIRECT_DISPLAY)
  bool init_direct_display_ppa_();
#endif
#ifdef USE_P4_VIDEO_RENDERER_H264
  struct H264SurfaceGeometry {
    uint16_t scale_units{0};
    uint16_t surface_width{0};
    uint16_t surface_height{0};
    int16_t native_x{0};
    int16_t native_y{0};
    uint64_t layout_area{0};
    uint32_t native_size{0};
  };

  // H.264 Baseline Level 3.0 limits a decoded picture to 1620 macroblocks.
  static constexpr size_t kH264Level30MaxMacroblocks = 1620;
  static constexpr uint16_t kPpaScaleUnits = 16;
  static const char *h264_receive_profile_level_id_(uint16_t width,
                                                     uint16_t height,
                                                     uint8_t max_fps);
  size_t h264_optimized_yuv_bytes_() const {
    return static_cast<size_t>(this->max_decode_width_) *
           this->max_decode_height_ * 3 / 2;
  }
  bool reset_h264_decoder_();
  bool h264_resolution_fits_(uint16_t width, uint16_t height) const;
  bool decode_h264_access_unit_(const uint8_t *data, size_t size,
                                uint32_t timestamp_90khz, bool key_frame,
                                uint32_t session_generation,
                                uint32_t loss_generation, bool &decoded);
  bool configure_i420_converter_(uint16_t width, uint16_t height);
  bool refresh_direct_display_layout_();
  bool compute_h264_surface_geometry_(uint16_t width, uint16_t height,
                                      H264SurfaceGeometry *geometry) const;
  bool render_i420_(const uint8_t *i420, size_t size, uint16_t width,
                    uint16_t height, uint32_t session_generation);
#endif
  void free_codec_resources_();
  void free_unpublished_surfaces_();
#ifdef USE_P4_VIDEO_RENDERER_H264
  void prepare_surface_(int index, const H264SurfaceGeometry &geometry);
#endif

  static void rx_task_trampoline_(void *ctx);
  void rx_task_();
  bool start_rx_task_();
  bool stop_rx_task_();
  bool reap_rx_task_();
  static void display_refresh_ready_callback_(lv_event_t *event);
  void on_display_refresh_ready_();

  bool task_stacks_in_psram_{false};
  uint16_t width_{640};
  uint16_t height_{480};
  uint8_t framerate_{10};
  uint16_t max_decode_width_{1280};
  uint16_t max_decode_height_{800};

#ifdef USE_P4_VIDEO_RENDERER_H264
  struct H264AccessUnitSlot {
    uint8_t *data{nullptr};
    size_t size{0};
    uint32_t timestamp{0};
    uint32_t session_generation{0};
    uint32_t loss_generation{0};
    bool key_frame{false};
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
    uint32_t queued_at_us{0};
#endif
    std::atomic<uint8_t> state{0}; // 0=free, 1=queued, 2=owned
  };
  H264AccessUnitSlot h264_au_slots_[kH264AccessUnitQueueDepth]{};
  QueueHandle_t h264_au_queue_{nullptr};
  StaticQueue_t h264_au_queue_control_{};
  uint8_t h264_au_queue_storage_[kH264AccessUnitQueueDepth]{};
#else
  uint8_t *rx_au_{nullptr};
  size_t rx_au_capacity_{0};
#endif
#ifdef USE_P4_VIDEO_RENDERER_JPEG
  jpeg_decoder_handle_t jpeg_decoder_{nullptr};
  jpeg_decode_cfg_t jpeg_decode_config_{};
  jpeg_decode_picture_info_t jpeg_picture_info_{};
  bool jpeg_picture_info_valid_{false};
  uint32_t jpeg_picture_info_generation_{0};
  uint16_t decoded_storage_width_{0};
  uint16_t decoded_storage_height_{0};
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  std::atomic<uint32_t> rx_jpeg_decode_max_us_{0};
  std::atomic<uint32_t> rx_au_work_max_us_{0};
#endif
#endif
#ifdef USE_P4_VIDEO_RENDERER_DIRECT_DISPLAY
  display::Display *direct_display_{nullptr};
  uint16_t display_rotation_{0};
#ifdef USE_P4_VIDEO_RENDERER_H264
  mipi_dsi::MipiDsi *direct_mipi_display_{nullptr};
  std::atomic<uint64_t> direct_layout_area_{0};
  std::atomic<uint32_t> direct_layout_native_size_{0};
#endif
#ifdef USE_P4_VIDEO_RENDERER_JPEG
  ppa_client_handle_t direct_display_ppa_{nullptr};
#endif
#endif
#ifdef USE_P4_VIDEO_RENDERER_H264
  ppa_client_handle_t ppa_{nullptr};
  esp_h264_dec_handle_t h264_decoder_{nullptr};
  esp_imgfx_color_convert_handle_t i420_converter_{nullptr};
  uint16_t i420_converter_width_{0};
  uint16_t i420_converter_height_{0};
  bool i420_converter_failure_logged_{false};
  uint8_t *optimized_yuv420_{nullptr};
  size_t optimized_yuv420_capacity_{0};
  bool h264_first_au_logged_{false};
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  bool h264_decode_failure_logged_{false};
  std::atomic<uint32_t> rx_h264_decode_max_us_{0};
  std::atomic<uint32_t> rx_i420_convert_max_us_{0};
  std::atomic<uint32_t> rx_ppa_max_us_{0};
  std::atomic<uint32_t> rx_au_work_max_us_{0};
#endif
#endif
  uint8_t *surfaces_[2]{nullptr, nullptr};
  size_t surface_capacity_bytes_{0};
  size_t surface_data_size_[2]{0, 0};
  uint16_t surface_stride_bytes_[2]{0, 0};
  uint16_t surface_content_width_[2]{0, 0};
  uint16_t surface_content_height_[2]{0, 0};
#ifdef USE_P4_VIDEO_RENDERER_H264
  int16_t surface_native_x_[2]{0, 0};
  int16_t surface_native_y_[2]{0, 0};
  uint64_t surface_layout_area_[2]{0, 0};
  uint32_t surface_native_size_[2]{0, 0};
#endif

  voip_stack::VideoCapability rx_capability_{};
  std::atomic<bool> rx_running_{false};
  std::atomic<bool> rx_active_{false};
  std::atomic<bool> rx_session_prepared_{false};
  std::atomic<uint32_t> rx_session_generation_{0};
#ifndef USE_P4_VIDEO_RENDERER_H264
  std::atomic<uint8_t> rx_slot_state_{0}; // 0=free, 1=ready, 2=owned
#endif
  // RTP timestamps provide the producer clock, so receive admission is
  // frame-driven and needs neither a timer nor a periodic main-loop poll.
  voip_stack::RtpFrameCadence90k cadence_{};
#ifndef USE_P4_VIDEO_RENDERER_H264
  size_t rx_au_size_{0};
  uint32_t rx_timestamp_{0};
  uint32_t rx_au_generation_{0};
  bool rx_key_frame_{false};
#endif
#ifdef USE_P4_VIDEO_RENDERER_H264
  std::atomic<bool> waiting_for_key_frame_{true};
  // Recreate tinyH264 only after an actual decoder error. Ordinary session
  // changes and locally dropped reference pictures recover on the next IDR.
  std::atomic<bool> decoder_reset_pending_{false};
  std::atomic<uint32_t> loss_generation_{0};
#endif
  TaskHandle_t rx_task_handle_{nullptr};
  StaticTask_t rx_task_tcb_{};
  StackType_t *rx_task_stack_{nullptr};
  bool rx_task_with_caps_{false};
  SemaphoreHandle_t rx_done_{nullptr};
  StaticSemaphore_t rx_done_storage_{};
  bool rx_done_observed_{false};
  // Serializes main-loop presentation state with stop_video(). Decoder publish
  // sites recheck the active session generation after every blocking codec or
  // PPA operation, so the persistent worker remains entirely frame-driven.
  SemaphoreHandle_t presentation_mutex_{nullptr};
  StaticSemaphore_t presentation_mutex_storage_{};

  lv_obj_t *video_container_{nullptr};
  lv_obj_t *video_image_{nullptr};
  lv_img_dsc_t image_descriptor_{};
  std::atomic<int> front_surface_{0};
  std::atomic<int> pending_surface_{-1};
  // pending_surface_ owns the backing store until LVGL completes its flush, or
  // until the direct-display PPA has copied it into the rotated front buffer.
  // No timer or main-loop polling is involved.
  std::atomic<bool> presentation_in_flight_{false};
  std::atomic<bool> surface_ever_presented_{false};
  std::atomic<bool> remote_frame_visible_{false};
#ifdef USE_P4_VIDEO_RENDERER_H264
  std::atomic<bool> direct_page_active_{false};
#endif
  std::atomic<bool> video_ended_pending_{false};
  std::atomic<uint32_t> rx_admitted_frames_{0};
  std::atomic<uint32_t> rx_rendered_frames_{0};
  std::atomic<uint32_t> rx_presented_frames_{0};
  std::atomic<uint32_t> rx_refresh_completed_{0};
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  std::atomic<uint32_t> rx_refresh_max_ms_{0};
  std::atomic<uint32_t> rx_present_ppa_max_us_{0};
  std::atomic<uint32_t> presentation_started_ms_{0};
  std::atomic<uint32_t> rx_rate_drops_{0};
  std::atomic<uint32_t> rx_busy_drops_{0};
  std::atomic<uint32_t> rx_decode_drops_{0};
#ifdef USE_P4_VIDEO_RENDERER_H264
  std::atomic<uint32_t> rx_queue_high_watermark_{0};
  std::atomic<uint32_t> rx_queue_wait_max_us_{0};
  std::atomic<uint32_t> rx_dependency_drops_{0};
  std::atomic<uint32_t> rx_geometry_drops_{0};
  std::atomic<uint32_t> rx_au_copy_max_us_{0};
  std::atomic<uint64_t> rx_au_copy_total_us_{0};
  std::atomic<uint64_t> rx_au_copy_total_bytes_{0};
#endif
#endif
  Trigger<> first_frame_trigger_;
  Trigger<> video_ended_trigger_;
};

} // namespace esphome::p4_video_renderer

#endif
