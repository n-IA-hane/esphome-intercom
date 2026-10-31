#include "esp_screenshot.h"

#include "esp_heap_caps.h"
#include "esp_random.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"
#include <array>
#include <cstdio>
#include <cstring>

namespace esphome::esp_screenshot {

static const char *const TAG = "esp_screenshot";

static void send_http_status(AsyncWebServerRequest *request, const char *status,
                             const char *message) {
  // ESPHome's wrapper maps unlisted numeric codes (including 410 and 503)
  // to 500. Set these standard statuses through the public IDF HTTP API.
  auto *response = request->beginResponse(200, "text/plain", message);
  httpd_resp_set_status(*request, status);
  request->send(response);
}

void EspScreenshot::setup() {
  this->publish_status_("No screenshot");
  if (this->task_core_ >= 0 &&
      this->task_core_ >= static_cast<int>(portNUM_PROCESSORS)) {
    ESP_LOGE(TAG, "Requested worker core %d is unavailable", this->task_core_);
    this->fail_setup_("Requested worker core is unavailable");
    return;
  }
#ifdef USE_ESP_SCREENSHOT_MIPI_DSI
  if (this->mipi_display_ != nullptr) {
    this->width_ = this->mipi_display_->get_width();
    this->height_ = this->mipi_display_->get_height();
  }
#endif
#ifdef USE_ESP_SCREENSHOT_LVGL
  if (this->lvgl_ != nullptr && this->lvgl_->get_disp() != nullptr) {
    auto *display = this->lvgl_->get_disp();
    if (lv_display_get_color_format(display) != LV_COLOR_FORMAT_RGB565 ||
        lv_display_get_render_mode(display) != LV_DISPLAY_RENDER_MODE_PARTIAL ||
        lv_display_get_offset_x(display) != 0 ||
        lv_display_get_offset_y(display) != 0) {
      this->fail_setup_("LVGL capture requires RGB565 partial rendering without "
                        "display offsets");
      return;
    }
    this->width_ = lv_display_get_horizontal_resolution(display);
    this->height_ = lv_display_get_vertical_resolution(display);
  }
#endif
  if (this->width_ == 0 || this->height_ == 0 || this->width_ > 4096 ||
      this->height_ > 4096) {
    this->fail_setup_("Display dimensions are unavailable or exceed 4096 pixels");
    return;
  }
  const uint32_t buffer_caps =
      MALLOC_CAP_8BIT |
      (this->buffer_in_psram_ ? MALLOC_CAP_SPIRAM : MALLOC_CAP_INTERNAL);
  const uint32_t stack_caps =
      MALLOC_CAP_8BIT |
      (this->task_stack_in_psram_ ? MALLOC_CAP_SPIRAM : MALLOC_CAP_INTERNAL);
  this->pixel_size_ = size_t(this->width_) * this->height_ * sizeof(uint16_t);
  this->pixels_ = static_cast<uint8_t *>(
      heap_caps_aligned_alloc(16, this->pixel_size_, buffer_caps));
  for (auto &buffer : this->jpeg_)
    buffer = static_cast<uint8_t *>(
        heap_caps_malloc(this->jpeg_capacity_, buffer_caps));
#ifdef USE_ESP_SCREENSHOT_LVGL
  if (this->lvgl_ != nullptr)
    this->coverage_ = static_cast<uint8_t *>(
        heap_caps_malloc((this->pixel_size_ / 2 + 7) / 8, buffer_caps));
#endif
  this->worker_stack_ = static_cast<StackType_t *>(
      heap_caps_malloc(this->task_stack_size_, stack_caps));
  this->mutex_ = xSemaphoreCreateMutexStatic(&this->mutex_storage_);
  if (this->pixels_ == nullptr || this->jpeg_[0] == nullptr ||
      this->jpeg_[1] == nullptr || this->worker_stack_ == nullptr ||
      this->mutex_ == nullptr
#ifdef USE_ESP_SCREENSHOT_LVGL
      || (this->lvgl_ != nullptr && this->coverage_ == nullptr)
#endif
  ) {
    ESP_LOGE(TAG,
             "Requested memory unavailable: buffers=%s stack=%s (%u bytes)",
             this->buffer_in_psram_ ? "PSRAM" : "internal",
             this->task_stack_in_psram_ ? "PSRAM" : "internal",
             unsigned(this->task_stack_size_));
    this->fail_setup_(
        "Unable to preallocate screenshot resources; no memory fallback");
    return;
  }
  jpeg_enc_config_t config = DEFAULT_JPEG_ENC_CONFIG();
  config.width = this->width_;
  config.height = this->height_;
  config.src_type = JPEG_PIXEL_FORMAT_RGB565_LE;
  config.subsampling = JPEG_SUBSAMPLE_444;
  config.quality = this->quality_;
  config.task_enable = false;
  if (jpeg_enc_open(&config, &this->encoder_) != JPEG_ERR_OK) {
    this->fail_setup_("Unable to initialize the software JPEG encoder");
    return;
  }
  this->boot_id_ = esp_random();
  this->worker_handle_ = xTaskCreateStaticPinnedToCore(
      worker_entry_, "screenshot", this->task_stack_size_, this,
      this->task_priority_, this->worker_stack_, &this->worker_tcb_,
      this->task_core_ < 0 ? tskNO_AFFINITY : this->task_core_);
  if (this->worker_handle_ == nullptr) {
    this->fail_setup_("Unable to create screenshot worker");
    return;
  }
  this->web_server_->add_handler(this);
  this->web_server_->init();
  this->disable_loop();
}

void EspScreenshot::fail_setup_(const char *reason) {
  ESP_LOGE(TAG, "%s", reason);
  this->publish_status_(std::string("Error: ") + reason);
  if (this->encoder_ != nullptr) {
    jpeg_enc_close(this->encoder_);
    this->encoder_ = nullptr;
  }
  heap_caps_free(this->pixels_);
  heap_caps_free(this->coverage_);
  heap_caps_free(this->worker_stack_);
  for (auto &buffer : this->jpeg_) {
    heap_caps_free(buffer);
    buffer = nullptr;
  }
  this->pixels_ = nullptr;
  this->coverage_ = nullptr;
  this->worker_stack_ = nullptr;
  this->mark_failed();
}

void EspScreenshot::publish_status_(const std::string &status) {
  if (this->capture_status_sensor_ != nullptr)
    this->capture_status_sensor_->publish_state(status);
}

bool EspScreenshot::capture() {
  if (this->is_failed() || this->worker_handle_ == nullptr ||
      this->stopping_.load()) {
    this->publish_status_("Error: Screenshot is unavailable");
    return false;
  }
  bool expected = false;
  if (!this->busy_.compare_exchange_strong(expected, true)) {
    ESP_LOGW(TAG, "Screenshot capture is busy");
    this->publish_status_("Busy");
    return false;
  }
  this->publish_status_("Capturing");
  this->requested_.store(true, std::memory_order_release);
  this->enable_loop_soon_any_context();
  return true;
}

void EspScreenshot::cancel_capture_(const char *reason) {
  ESP_LOGW(TAG, "%s; previous image retained", reason);
  xSemaphoreTake(this->mutex_, portMAX_DELAY);
  this->slots_.cancel_write();
  xSemaphoreGive(this->mutex_);
  this->writing_slot_ = -1;
  this->busy_.store(false, std::memory_order_release);
  this->publish_status_(std::string("Error: ") + reason);
}

void EspScreenshot::loop() {
  if (this->requested_.exchange(false, std::memory_order_acq_rel)) {
    xSemaphoreTake(this->mutex_, portMAX_DELAY);
    this->writing_slot_ = this->slots_.begin_write();
    xSemaphoreGive(this->mutex_);
    if (this->writing_slot_ < 0) {
      this->cancel_capture_("Image readers still own the available buffer");
    } else if (!this->copy_display_()) {
      this->cancel_capture_("A complete display capture is unavailable");
    } else {
      xTaskNotifyGive(this->worker_handle_);
    }
  }
  if (this->completed_.exchange(false, std::memory_order_acq_rel)) {
    if (this->encode_success_) {
      if (this->capture_id_sensor_ != nullptr)
        this->capture_id_sensor_->publish_state(
            this->capture_id_[this->writing_slot_]);
      ESP_LOGD(TAG, "Captured %ux%u, %u JPEG bytes", this->width_,
               this->height_, unsigned(this->jpeg_size_[this->writing_slot_]));
      this->writing_slot_ = -1;
      this->busy_.store(false, std::memory_order_release);
      this->publish_status_("Ready");
    } else {
      this->cancel_capture_("JPEG encoding failed or exceeded max_jpeg_size");
    }
  }
  this->disable_loop();
}

bool EspScreenshot::copy_display_() {
#ifdef USE_ESP_SCREENSHOT_MIPI_DSI
  if (this->mipi_display_ != nullptr)
    return this->mipi_display_->copy_frame_buffer_rgb565(this->pixels_,
                                                         this->pixel_size_);
#endif
#ifdef USE_ESP_SCREENSHOT_LVGL
  if (this->lvgl_ != nullptr) {
    if (this->lvgl_->is_paused())
      return false;
    auto *display = this->lvgl_->get_disp();
    if (unsigned(lv_display_get_horizontal_resolution(display)) !=
            this->width_ ||
        unsigned(lv_display_get_vertical_resolution(display)) != this->height_)
      return false;
    this->assembler_.reset(this->pixels_, this->coverage_, this->width_,
                           this->height_);
    this->flush_error_ = false;
    const auto event_count = lv_display_get_event_count(display);
    lv_display_add_event_cb(display, flush_event_, LV_EVENT_FLUSH_START, this);
    if (lv_display_get_event_count(display) != event_count + 1)
      return false;
    lv_obj_invalidate(lv_display_get_screen_active(display));
    lv_refr_now(display);
    lv_display_remove_event_cb_with_user_data(display, flush_event_, this);
    return !this->flush_error_ && this->assembler_.complete();
  }
#endif
  return false;
}

#ifdef USE_ESP_SCREENSHOT_LVGL
void EspScreenshot::flush_event_(lv_event_t *event) {
  auto *self = static_cast<EspScreenshot *>(lv_event_get_user_data(event));
  auto *display = self->lvgl_->get_disp();
  auto *area = static_cast<const lv_area_t *>(lv_event_get_param(event));
  auto *buffer = lv_display_get_buf_active(display);
  if (buffer == nullptr || area == nullptr ||
      !self->assembler_.add(area->x1, area->y1, lv_area_get_width(area),
                            lv_area_get_height(area), buffer->data,
                            buffer->header.stride))
    self->flush_error_ = true;
}
#endif

void EspScreenshot::worker_entry_(void *parameter) {
  static_cast<EspScreenshot *>(parameter)->worker_();
}

void EspScreenshot::worker_() {
  while (true) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    if (this->stopping_.load(std::memory_order_acquire))
      break;
    const int index = this->writing_slot_;
    int size = 0;
    const auto result =
        jpeg_enc_process(this->encoder_, this->pixels_, int(this->pixel_size_),
                         this->jpeg_[index], int(this->jpeg_capacity_), &size);
    this->encode_success_ = result == JPEG_ERR_OK && size > 0 &&
                            size_t(size) <= this->jpeg_capacity_;
    if (this->encode_success_) {
      this->jpeg_size_[index] = size_t(size);
      std::snprintf(this->capture_id_[index], sizeof(this->capture_id_[index]),
                    "%08lx-%08lx", static_cast<unsigned long>(this->boot_id_),
                    static_cast<unsigned long>(++this->sequence_));
      xSemaphoreTake(this->mutex_, portMAX_DELAY);
      this->slots_.publish(index);
      xSemaphoreGive(this->mutex_);
    }
    this->completed_.store(true, std::memory_order_release);
    this->enable_loop_soon_any_context();
  }
  vTaskDelete(nullptr);
}

bool EspScreenshot::canHandle(AsyncWebServerRequest *request) const {
  std::array<char, AsyncWebServerRequest::URL_BUF_SIZE> buffer{};
  return request->method() == HTTP_GET && request->url_to(buffer) == this->url_;
}

void EspScreenshot::handleRequest(AsyncWebServerRequest *request) {
  if (this->is_failed() || this->mutex_ == nullptr) {
    send_http_status(request, "503 Service Unavailable",
                     "Screenshot is unavailable");
    return;
  }
  xSemaphoreTake(this->mutex_, portMAX_DELAY);
  const int index = this->slots_.acquire();
  xSemaphoreGive(this->mutex_);
  if (index < 0) {
    send_http_status(request, "503 Service Unavailable",
                     "Take a screenshot first");
    return;
  }
  const auto *requested = request->getParam("capture");
  if (requested != nullptr && requested->value() != this->capture_id_[index]) {
    send_http_status(request, "410 Gone", "Screenshot identifier has expired");
  } else {
    auto *response = request->beginResponse(
        200, "image/jpeg", this->jpeg_[index], this->jpeg_size_[index]);
    response->addHeader("Cache-Control", "no-cache");
    char etag[36];
    std::snprintf(etag, sizeof(etag), "\"%s\"", this->capture_id_[index]);
    response->addHeader("ETag", etag);
    request->send(response);
  }
  xSemaphoreTake(this->mutex_, portMAX_DELAY);
  this->slots_.release(index);
  xSemaphoreGive(this->mutex_);
}

void EspScreenshot::on_shutdown() {
  this->stopping_.store(true, std::memory_order_release);
  if (this->worker_handle_ != nullptr)
    xTaskNotifyGive(this->worker_handle_);
  // Keep buffers alive through shutdown. The worker or an HTTP reader can
  // still own them; the imminent reboot releases all component allocations.
}

void EspScreenshot::dump_state() {
  ESP_LOGI(
      TAG,
      "runtime uptime_ms=%u busy=%u internal_free=%u internal_largest=%u "
      "dma_free=%u dma_largest=%u psram_free=%u",
      unsigned(millis()), unsigned(this->busy_.load()),
      unsigned(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
      unsigned(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL)),
      unsigned(heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA)),
      unsigned(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL |
                                                MALLOC_CAP_DMA)),
      unsigned(heap_caps_get_free_size(MALLOC_CAP_SPIRAM)));
  ESP_LOGI(
      TAG,
      "worker requested_core=%d priority=%u stack_bytes=%u stack_free_bytes=%u "
      "stack_psram=%u buffers_psram=%u",
      this->task_core_, this->task_priority_, unsigned(this->task_stack_size_),
      this->worker_handle_ == nullptr
          ? 0U
          : unsigned(uxTaskGetStackHighWaterMark(this->worker_handle_)),
      unsigned(this->task_stack_in_psram_), unsigned(this->buffer_in_psram_));
}

void EspScreenshot::dump_config() {
  ESP_LOGCONFIG(TAG,
                "ESP screenshot: %ux%u, quality %u, JPEG slots %u bytes each",
                this->width_, this->height_, this->quality_,
                unsigned(this->jpeg_capacity_));
  ESP_LOGCONFIG(TAG, "  HTTP image: %s", this->url_.c_str());
  ESP_LOGCONFIG(TAG,
                "  Worker core=%d (-1=any), priority=%u, stack=%u bytes in %s; "
                "buffers in %s",
                this->task_core_, this->task_priority_,
                unsigned(this->task_stack_size_),
                this->task_stack_in_psram_ ? "PSRAM" : "internal",
                this->buffer_in_psram_ ? "PSRAM" : "internal");
}

} // namespace esphome::esp_screenshot
