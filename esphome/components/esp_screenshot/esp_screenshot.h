#pragma once

#include "esphome/components/text_sensor/text_sensor.h"
#include "esphome/components/web_server_base/web_server_base.h"
#include "esphome/core/automation.h"
#include "esphome/core/component.h"
#include "snapshot_store.h"
#ifdef USE_ESP_SCREENSHOT_MIPI_DSI
#include "esphome/components/mipi_dsi/mipi_dsi.h"
#endif
#ifdef USE_ESP_SCREENSHOT_LVGL
#include "esphome/components/lvgl/lvgl_esphome.h"
#endif
#include "esp_jpeg_enc.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include <atomic>
#include <string>

namespace esphome::esp_screenshot {

class EspScreenshot : public Component, public AsyncWebHandler {
public:
  void set_web_server(web_server_base::WebServerBase *server) {
    this->web_server_ = server;
  }
  void set_task_core(int8_t value) { this->task_core_ = value; }
  void set_task_priority(uint8_t value) { this->task_priority_ = value; }
  void set_task_stack_size(uint32_t value) { this->task_stack_size_ = value; }
  void set_task_stack_in_psram(bool value) {
    this->task_stack_in_psram_ = value;
  }
  void set_buffer_in_psram(bool value) { this->buffer_in_psram_ = value; }
  void set_quality(uint8_t quality) { this->quality_ = quality; }
  void set_max_jpeg_size(size_t size) { this->jpeg_capacity_ = size; }
  void set_path(const std::string &path) {
    this->url_ = "/esp_screenshot/" + path + "/image.jpg";
  }
  void set_capture_id_sensor(text_sensor::TextSensor *sensor) {
    this->capture_id_sensor_ = sensor;
  }
#ifdef USE_ESP_SCREENSHOT_MIPI_DSI
  void set_mipi_display(mipi_dsi::MipiDsi *display) {
    this->mipi_display_ = display;
  }
#endif
#ifdef USE_ESP_SCREENSHOT_LVGL
  void set_lvgl(lvgl::LvglComponent *display) { this->lvgl_ = display; }
#endif
  float get_setup_priority() const override { return setup_priority::LATE; }
  void setup() override;
  void loop() override;
  void dump_config() override;
  void dump_state();
  void on_shutdown() override;
  bool capture();
  bool canHandle(AsyncWebServerRequest *request) const override;
  void handleRequest(AsyncWebServerRequest *request) override;

protected:
  static void worker_entry_(void *parameter);
  void worker_();
  bool copy_display_();
  void cancel_capture_(const char *reason);
  void fail_setup_(const char *reason);
#ifdef USE_ESP_SCREENSHOT_LVGL
  static void flush_event_(lv_event_t *event);
  lvgl::LvglComponent *lvgl_{nullptr};
  FrameAssembler assembler_{};
  bool flush_error_{false};
#endif
#ifdef USE_ESP_SCREENSHOT_MIPI_DSI
  mipi_dsi::MipiDsi *mipi_display_{nullptr};
#endif
  web_server_base::WebServerBase *web_server_{nullptr};
  text_sensor::TextSensor *capture_id_sensor_{nullptr};
  std::string url_{"/esp_screenshot/screen/image.jpg"};
  uint8_t quality_{85};
  unsigned width_{0}, height_{0};
  uint8_t *pixels_{nullptr}, *coverage_{nullptr}, *jpeg_[2]{};
  size_t pixel_size_{0}, jpeg_capacity_{256 * 1024}, jpeg_size_[2]{};
  char capture_id_[2][32]{};
  uint32_t boot_id_{0}, sequence_{0};
  SnapshotSlots slots_{};
  int writing_slot_{-1};
  std::atomic_bool busy_{false}, requested_{false}, completed_{false},
      stopping_{false};
  bool encode_success_{false};
  jpeg_enc_handle_t encoder_{nullptr};
  TaskHandle_t worker_handle_{nullptr};
  StaticTask_t worker_tcb_{};
  StackType_t *worker_stack_{nullptr};
  uint32_t task_stack_size_{6144};
  int8_t task_core_{-1};
  uint8_t task_priority_{1};
  bool task_stack_in_psram_{true}, buffer_in_psram_{true};
  SemaphoreHandle_t mutex_{nullptr};
  StaticSemaphore_t mutex_storage_{};
};

template <typename... Ts> class CaptureAction : public Action<Ts...> {
public:
  explicit CaptureAction(EspScreenshot *parent) : parent_(parent) {}
  void play(const Ts &...args) override { this->parent_->capture(); }

protected:
  EspScreenshot *parent_;
};

} // namespace esphome::esp_screenshot
