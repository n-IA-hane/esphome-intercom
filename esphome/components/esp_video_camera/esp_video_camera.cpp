#include "esp_video_camera.h"

#ifdef USE_ESP_IDF

#include "i2c_helper.h"
#include "esphome/core/log.h"
#include "esphome/core/hal.h"

#include "esp_heap_caps.h"
#include "esp_timer.h"

#include <algorithm>
#include <cerrno>
#include <cmath>
#include <cstring>
#include <fcntl.h>
#include <limits>
#include <new>
#include <unistd.h>
#include <sys/ioctl.h>
#include <sys/mman.h>

extern "C" {
#include "esp_video_init.h"
#include "esp_video_device.h"
#include "esp_video_ioctl.h"
#include "linux/videodev2.h"
#include "driver/ledc.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#if CONFIG_ESP_VIDEO_ENABLE_USB_UVC_VIDEO_DEVICE
#include "esp_intr_alloc.h"
#include "usb/usb_host.h"
#endif
}

#ifndef V4L2_CID_JPEG_COMPRESSION_QUALITY
#define V4L2_CID_JPEG_COMPRESSION_QUALITY (V4L2_CID_JPEG_CLASS_BASE + 1)
#endif

namespace esphome::esp_video_camera {

static const char *const TAG = "esp_video_camera";

// ===========================================================================
// Pipeline init helpers (run esp_video_init on core 0, optional LEDC XCLK)
// ===========================================================================
namespace {

#ifdef CONFIG_CACHE_L2_CACHE_LINE_SIZE
static constexpr size_t PPA_BUFFER_ALIGNMENT =
    CONFIG_CACHE_L2_CACHE_LINE_SIZE;
#else
static constexpr size_t PPA_BUFFER_ALIGNMENT = 64;
#endif

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

// esp_video 2.2.0 enables DEBUG logging inside its ISP/IPA pipeline even when
// the application logger is set lower. On a live camera this can emit several
// lines per frame, stall the main loop and starve realtime audio. Keep faults
// visible while silencing the per-frame tuning telemetry.
void clamp_isp_log_levels() {
  esp_log_level_set("ISP", ESP_LOG_WARN);
  esp_log_level_set("esp_ipa_adn", ESP_LOG_WARN);
  esp_log_level_set("esp_ipa_aen", ESP_LOG_WARN);
  esp_log_level_set("esp_ipa_ian", ESP_LOG_WARN);
  esp_log_level_set("esp_ipa_acc", ESP_LOG_WARN);
  esp_log_level_set("esp_ipa_af", ESP_LOG_WARN);
}

struct VideoInitParams {
  esp_video_init_config_t config{};
  esp_video_init_csi_config_t csi_config{};
#if CONFIG_ESP_VIDEO_ENABLE_USB_UVC_VIDEO_DEVICE
  esp_video_init_usb_uvc_config_t uvc_config{};
#endif
  esp_err_t result{ESP_FAIL};
  SemaphoreHandle_t done{nullptr};
  std::atomic<uint8_t> references{2};
};

void release_video_init_params(VideoInitParams *params) {
  if (params == nullptr ||
      params->references.fetch_sub(1, std::memory_order_acq_rel) != 1) {
    return;
  }
  if (params->done != nullptr) vSemaphoreDelete(params->done);
  delete params;
}

// ESP32-P4 camera hardware must be initialised on core 0; run esp_video_init
// there regardless of which core ESPHome runs on.
void video_init_task_core0(void *param) {
  auto *p = static_cast<VideoInitParams *>(param);
  p->result = esp_video_init(&p->config);
  xSemaphoreGive(p->done);
  release_video_init_params(p);
  vTaskDelete(nullptr);
}

#if CONFIG_ESP_VIDEO_ENABLE_USB_UVC_VIDEO_DEVICE
// Pump USB Host Library events. esp_video is told not to own the USB host lib
// (init_usb_host_lib = false) so that we can tolerate it already being
// installed by another component; when we install it ourselves we run this
// daemon, when it is shared the existing owner pumps the events instead.
void usb_host_lib_daemon_task(void *param) {
  while (true) {
    uint32_t event_flags;
    if (usb_host_lib_handle_events(portMAX_DELAY, &event_flags) == ESP_OK) {
      if (event_flags & USB_HOST_LIB_EVENT_FLAGS_NO_CLIENTS)
        usb_host_device_free_all();
    }
  }
}
#endif

// Generate the sensor XCLK with LEDC. For MIPI-CSI sensors esp_video_init() does
// not start XCLK, so non-M5Stack boards must do it before init or the sensor
// stays silent on I2C.
esp_err_t init_xclk_ledc(gpio_num_t gpio_num, uint32_t freq_hz) {
  ledc_timer_config_t timer_conf = {};
  timer_conf.speed_mode = LEDC_LOW_SPEED_MODE;
  timer_conf.timer_num = LEDC_TIMER_0;
  timer_conf.duty_resolution = LEDC_TIMER_1_BIT;
  timer_conf.freq_hz = freq_hz;
  timer_conf.clk_cfg = LEDC_AUTO_CLK;
  esp_err_t ret = ledc_timer_config(&timer_conf);
  if (ret != ESP_OK)
    return ret;

  ledc_channel_config_t ch_conf = {};
  ch_conf.speed_mode = LEDC_LOW_SPEED_MODE;
  ch_conf.channel = LEDC_CHANNEL_0;
  ch_conf.timer_sel = LEDC_TIMER_0;
  ch_conf.intr_type = LEDC_INTR_DISABLE;
  ch_conf.gpio_num = gpio_num;
  ch_conf.duty = 1;  // 50 % duty cycle
  ch_conf.hpoint = 0;
  return ledc_channel_config(&ch_conf);
}

// Parse a resolution string into width/height. Accepts the aliases validated by
// the Python schema or an explicit "WIDTHxHEIGHT". Returns false for "auto".
bool parse_resolution(const std::string &res, uint32_t &width, uint32_t &height) {
  if (res.empty() || res == "auto")
    return false;

  struct ResAlias {
    const char *name;
    uint32_t width;
    uint32_t height;
  };
  static constexpr ResAlias ALIASES[] = {
      {"QVGA", 320, 240}, {"VGA", 640, 480}, {"480P", 640, 480}, {"720P", 1280, 720}, {"1080P", 1920, 1080},
  };
  for (const auto &alias : ALIASES) {
    if (res == alias.name) {
      width = alias.width;
      height = alias.height;
      return true;
    }
  }

  // Parse "WIDTHxHEIGHT" (already validated as digits by the Python schema).
  size_t x_pos = res.find('x');
  if (x_pos == std::string::npos || x_pos == 0 || x_pos + 1 >= res.size())
    return false;
  uint32_t w = 0, h = 0;
  for (size_t i = 0; i < x_pos; i++) {
    if (res[i] < '0' || res[i] > '9')
      return false;
    w = w * 10 + (res[i] - '0');
  }
  for (size_t i = x_pos + 1; i < res.size(); i++) {
    if (res[i] < '0' || res[i] > '9')
      return false;
    h = h * 10 + (res[i] - '0');
  }
  if (w == 0 || h == 0)
    return false;
  width = w;
  height = h;
  return true;
}

}  // namespace

// ===========================================================================
// ESPVideoCameraImage
// ===========================================================================
ESPVideoCameraImage::ESPVideoCameraImage(uint8_t *data, size_t length, uint8_t requesters)
    : data_(data), length_(length), requesters_(requesters) {}

ESPVideoCameraImage::~ESPVideoCameraImage() {
  if (this->data_ != nullptr) {
    heap_caps_free(this->data_);
    this->data_ = nullptr;
  }
}

bool ESPVideoCameraImage::was_requested_by(camera::CameraRequester requester) const {
  return (this->requesters_ & (1 << requester)) != 0;
}

// ===========================================================================
// ESPVideoCameraImageReader
// ===========================================================================
void ESPVideoCameraImageReader::set_image(std::shared_ptr<camera::CameraImage> image) {
  this->image_ = std::move(image);
  this->offset_ = 0;
}

size_t ESPVideoCameraImageReader::available() const {
  if (this->image_ == nullptr)
    return 0;
  return this->image_->get_data_length() - this->offset_;
}

uint8_t *ESPVideoCameraImageReader::peek_data_buffer() {
  if (this->image_ == nullptr)
    return nullptr;
  return this->image_->get_data_buffer() + this->offset_;
}

void ESPVideoCameraImageReader::consume_data(size_t consumed) { this->offset_ += consumed; }

void ESPVideoCameraImageReader::return_image() {
  this->image_.reset();
  this->offset_ = 0;
}

// ===========================================================================
// ESPVideoCamera: setup and pipeline initialization
// ===========================================================================
void ESPVideoCamera::setup() {
  if (!this->init_pipeline_()) {
    this->mark_failed();
    return;
  }

  // Resolve the device alias to a concrete /dev/videoN path.
  const std::string &d = this->device_;
  this->is_hw_jpeg_ = false;
  this->is_raw_csi_ = false;
  if (d.empty() || d == "jpeg" || d == ESP_VIDEO_JPEG_DEVICE_NAME) {
    this->resolved_device_ = ESP_VIDEO_JPEG_DEVICE_NAME;  // /dev/video10
    this->is_hw_jpeg_ = true;
  } else if (d == "csi") {
    this->resolved_device_ = ESP_VIDEO_MIPI_CSI_DEVICE_NAME;  // /dev/video0
    this->is_raw_csi_ = true;
  } else if (d.starts_with("uvc")) {
    // "uvc" -> /dev/video40, "uvcN" -> /dev/video4N (N validated as a digit).
    const char *index = (d.size() == 4) ? (d.c_str() + 3) : "0";
    this->resolved_device_ = std::string(ESP_VIDEO_USB_UVC_NAME_PREFIX) + index;
  } else {
    this->resolved_device_ = d;
  }

  int test_fd = open(this->resolved_device_.c_str(), O_RDWR | O_NONBLOCK);
  if (test_fd < 0) {
    ESP_LOGE(TAG, "V4L2 device '%s' unavailable (errno=%d: %s)", this->resolved_device_.c_str(), errno,
             strerror(errno));
    this->mark_failed();
    return;
  }
  close(test_fd);

  // Local gate: blocking DQBUF calls live in this capture task, not loopTask.
  // The task is not subscribed to task_wdt. Its 8 KiB internal stack runs at
  // priority 3 on CPU0, separate from loopTask on CPU1.
  this->frame_mutex_ = xSemaphoreCreateMutex();
  this->capture_task_done_ =
      xSemaphoreCreateBinaryStatic(&this->capture_task_done_storage_);
  this->capture_task_running_.store(true, std::memory_order_release);
  if (this->frame_mutex_ == nullptr || this->capture_task_done_ == nullptr ||
      xTaskCreatePinnedToCore(ESPVideoCamera::capture_task_trampoline, "esp_video_cap", 8192, this, 3,
                              &this->capture_task_, 0) != pdPASS) {
    this->capture_task_running_.store(false, std::memory_order_release);
    ESP_LOGE(TAG, "Failed to create capture task");
    this->mark_failed();
    return;
  }

  ESP_LOGI(TAG, "Camera ready on %s (source: %s)", this->resolved_device_.c_str(), this->device_.c_str());
  // The capture task wakes us only when a JPEG is ready or consumer state
  // changes. An idle camera must not add a permanent main-loop poller.
  this->disable_loop();
}

void ESPVideoCamera::on_shutdown() {
  this->stream_requesters_.store(0, std::memory_order_release);
  this->single_requesters_.store(0, std::memory_order_release);
  this->raw_frame_consumer_active_.store(false, std::memory_order_release);
  this->jpeg_frame_consumer_active_.store(false, std::memory_order_release);
  this->capture_wanted_.store(false, std::memory_order_release);
  this->capture_task_running_.store(false, std::memory_order_release);

  if (this->capture_task_ != nullptr) {
    xTaskNotifyGive(this->capture_task_);
    if (this->capture_task_done_ == nullptr ||
        xSemaphoreTake(
            this->capture_task_done_,
            pdMS_TO_TICKS(CAPTURE_STOP_TIMEOUT_MS)) != pdTRUE) {
      ESP_LOGE(TAG,
               "Camera capture task did not stop before shutdown; "
               "retaining esp_video resources");
      return;
    }
    this->capture_task_ = nullptr;
  } else if (this->pipeline_ready_) {
    const esp_err_t error = esp_video_deinit();
    if (error != ESP_OK) {
      ESP_LOGE(TAG, "esp_video shutdown failed: %s",
               esp_err_to_name(error));
      return;
    }
    this->pipeline_ready_ = false;
  }
  ESP_LOGI(TAG, "Camera pipeline stopped cleanly");
}

bool ESPVideoCamera::init_pipeline_() {
  if (this->i2c_bus_ == nullptr) {
    ESP_LOGE(TAG, "No I2C bus set");
    return false;
  }
  i2c_master_bus_handle_t i2c_handle = get_i2c_bus_handle(this->i2c_bus_);
  if (i2c_handle == nullptr) {
    ESP_LOGE(TAG, "Could not obtain the ESP-IDF I2C bus handle");
    return false;
  }

  // A "uvc" device streams from a USB camera only. In that case skip the
  // MIPI-CSI pipeline entirely: esp_video_init() runs sensor detection only
  // when config->csi != NULL, so leaving it NULL avoids trying (and failing)
  // to detect a MIPI sensor that isn't present on a USB-only board.
  const bool uvc_only = this->device_.rfind("uvc", 0) == 0;

  // Start XCLK via LEDC if requested (MIPI sensors need it before init).
  if (!uvc_only && this->enable_xclk_init_ && this->xclk_pin_ != (gpio_num_t) -1) {
    if (init_xclk_ledc(this->xclk_pin_, this->xclk_freq_) != ESP_OK) {
      ESP_LOGE(TAG, "XCLK init failed");
      return false;
    }
    vTaskDelay(pdMS_TO_TICKS(50));
  }

  esp_video_init_csi_config_t csi_config = {};
  csi_config.sccb_config.init_sccb = false;  // reuse the ESPHome I2C bus
  csi_config.sccb_config.i2c_handle = i2c_handle;
  csi_config.sccb_config.freq = 400000;
  csi_config.reset_pin = (gpio_num_t) -1;
  csi_config.pwdn_pin = (gpio_num_t) -1;
  // Note: esp_video >= 2.x no longer takes xclk_pin/xclk_freq in the CSI config.
  // The sensor XCLK is generated separately via LEDC (see init_xclk_ledc above).

  esp_video_init_config_t video_config = {};
  if (!uvc_only)
    video_config.csi = &csi_config;

#if CONFIG_ESP_VIDEO_ENABLE_USB_UVC_VIDEO_DEVICE
  esp_video_init_usb_uvc_config_t uvc_config = {};
  if (this->enable_uvc_) {
    uvc_config.uvc.uvc_dev_num = 1;
    uvc_config.uvc.task_stack = 4096;
    uvc_config.uvc.task_priority = 5;
    uvc_config.uvc.task_affinity = -1;

    // The USB Host Library can only be installed once per system. Manage it here
    // instead of letting esp_video own it, so that if another component (e.g.
    // ESPHome's usb_host) has already installed it we share the existing stack
    // instead of aborting esp_video_init(). When we install it ourselves we also
    // run the library event daemon; when it is already installed we leave the
    // events to the existing owner.
    usb_host_config_t host_config = {};
    host_config.skip_phy_setup = false;
    host_config.intr_flags = ESP_INTR_FLAG_LEVEL1;
    esp_err_t host_ret = usb_host_install(&host_config);
    if (host_ret == ESP_OK) {
      xTaskCreatePinnedToCore(usb_host_lib_daemon_task, "usb_lib", 4096, nullptr, 5, nullptr, tskNO_AFFINITY);
    } else if (host_ret == ESP_ERR_INVALID_STATE) {
      ESP_LOGW(TAG, "USB Host already installed by another component; sharing it for UVC");
    } else {
      ESP_LOGE(TAG, "usb_host_install() failed: %s", esp_err_to_name(host_ret));
    }
    uvc_config.usb.init_usb_host_lib = false;  // we manage the USB host library (see above)
    uvc_config.usb.task_stack = 4096;
    uvc_config.usb.task_priority = 5;
    uvc_config.usb.task_affinity = -1;
    video_config.usb_uvc = &uvc_config;
  }
#endif

  // Run esp_video_init() on core 0 (hardware requirement). The worker can
  // legally outlive our bounded setup wait, so it owns a second reference to
  // every config object and to the semaphore it will eventually signal.
  auto *params = new (std::nothrow) VideoInitParams();
  if (params == nullptr)
    return false;
  params->done = xSemaphoreCreateBinary();
  if (params->done == nullptr) {
    params->references.store(1, std::memory_order_release);
    release_video_init_params(params);
    return false;
  }
  params->config = video_config;
  params->csi_config = csi_config;
  params->config.csi = uvc_only ? nullptr : &params->csi_config;
#if CONFIG_ESP_VIDEO_ENABLE_USB_UVC_VIDEO_DEVICE
  params->uvc_config = uvc_config;
  params->config.usb_uvc =
      this->enable_uvc_ ? &params->uvc_config : nullptr;
#endif
  if (xTaskCreatePinnedToCore(video_init_task_core0, "esp_video_init", 8192,
                              params, 5, nullptr, 0) != pdPASS) {
    params->references.store(1, std::memory_order_release);
    release_video_init_params(params);
    return false;
  }
  if (xSemaphoreTake(params->done, pdMS_TO_TICKS(10000)) != pdTRUE) {
    ESP_LOGE(TAG, "esp_video_init() timed out");
    release_video_init_params(params);
    return false;
  }
  const esp_err_t init_result = params->result;
  release_video_init_params(params);
  if (init_result != ESP_OK) {
    ESP_LOGE(TAG, "esp_video_init() failed: %s",
             esp_err_to_name(init_result));
    return false;
  }
  clamp_isp_log_levels();
  this->pipeline_ready_ = true;
  return true;
}

// ===========================================================================
// ESPVideoCamera: streaming and capture
// ===========================================================================
// loop() never captures: blocking DQBUF calls previously stalled loopTask and
// tripped task_wdt. It only takes a completed frame from the task-owned pending
// slot and publishes it here because camera API callbacks are not thread-safe.
void ESPVideoCamera::loop() {
  // Consume the wake-up at entry. A capture-task publication or a listener
  // request that races with this callback must remain armed for the next
  // iteration; disabling at the end loses that edge and can strand a pending
  // JPEG until an unrelated component update.
  this->disable_loop();
  uint8_t *data = nullptr;
  size_t len = 0;
  uint8_t requesters = 0;
  if (xSemaphoreTake(this->frame_mutex_, 0) == pdTRUE) {
    if (this->pending_jpeg_ != nullptr) {
      data = this->pending_jpeg_;
      len = this->pending_jpeg_len_;
      requesters = this->pending_requesters_;
      this->pending_jpeg_ = nullptr;
      this->pending_jpeg_len_ = 0;
      this->pending_requesters_ = 0;
    }
    xSemaphoreGive(this->frame_mutex_);
  }
  if (data != nullptr)
    this->deliver_frame_owned_(data, len, requesters);

  if (this->capture_retry_requested_.exchange(
          false, std::memory_order_acq_rel) &&
      this->has_consumers_() && !this->capture_retry_armed_) {
    this->capture_retry_armed_ = true;
    this->set_timeout("capture_retry", CAPTURE_RETRY_MS, [this]() {
      this->capture_retry_armed_ = false;
      if (!this->has_consumers_()) return;
      this->capture_wanted_.store(true, std::memory_order_release);
      if (this->capture_task_ != nullptr)
        xTaskNotifyGive(this->capture_task_);
    });
  }

  // Keep AE warm briefly after the last consumer, but schedule that transition
  // once instead of waking the main loop continuously for five seconds.
  if (this->has_consumers_()) {
    if (this->capture_linger_armed_) {
      this->cancel_timeout("capture_linger");
      this->capture_linger_armed_ = false;
    }
  } else if (!this->capture_linger_armed_) {
    this->capture_linger_armed_ = true;
    this->set_timeout("capture_linger", LINGER_MS, [this]() {
      this->capture_linger_armed_ = false;
      if (!this->has_consumers_())
        this->capture_wanted_.store(false, std::memory_order_release);
    });
  }
}

// Called by the capture task: fallback throttle, PSRAM copy and pending slot.
void ESPVideoCamera::queue_frame_(const uint8_t *data, size_t length) {
  if (length == 0)
    return;
  if (!this->hardware_framerate_active_.load(std::memory_order_acquire)) {
    const uint32_t now = millis();
    const uint32_t min_interval =
        this->min_interval_ms_.load(std::memory_order_acquire);
    if (this->last_frame_ms_ != 0 && min_interval > 0 &&
        (now - this->last_frame_ms_) < min_interval) {
      return;
    }
    this->last_frame_ms_ = now;
  }

  uint8_t *copy = static_cast<uint8_t *>(
      heap_caps_malloc(length, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (copy == nullptr) {
    const uint32_t now = millis();
    if (this->last_alloc_warning_ms_ == 0 ||
        now - this->last_alloc_warning_ms_ >= 5000) {
      ESP_LOGW(TAG, "Failed to allocate %u PSRAM bytes (frame dropped)",
               (unsigned) length);
      this->last_alloc_warning_ms_ = now;
    }
    return;
  }
  memcpy(copy, data, length);
  // A one-shot requester owns exactly one encoded frame. Clear it as soon as
  // that frame has a durable pending copy, not later when the main loop
  // happens to publish it. Otherwise a delayed LVGL loop makes the capture
  // task encode and discard every sensor frame while waiting for one callback,
  // starving the concurrent hardware JPEG decoder.
  const uint8_t single =
      this->single_requesters_.exchange(0, std::memory_order_acq_rel);
  const uint8_t streaming =
      this->stream_requesters_.load(std::memory_order_acquire);
  const uint8_t requesters = static_cast<uint8_t>(single | streaming);
  if (requesters == 0) {
    heap_caps_free(copy);
    return;
  }
  this->capture_retry_attempts_.store(0, std::memory_order_release);
  xSemaphoreTake(this->frame_mutex_, portMAX_DELAY);
  if (this->pending_jpeg_ != nullptr) {
    heap_caps_free(this->pending_jpeg_);  // Main loop lost the race: drop oldest.
    this->pending_requesters_ =
        static_cast<uint8_t>(this->pending_requesters_ | requesters);
  } else {
    this->pending_requesters_ = requesters;
  }
  this->pending_jpeg_ = copy;
  this->pending_jpeg_len_ = length;
  xSemaphoreGive(this->frame_mutex_);
  this->enable_loop_soon_any_context();
}

// Called from loop(): CameraImage takes ownership and frees data on destruction.
void ESPVideoCamera::deliver_frame_owned_(uint8_t *data, size_t length,
                                          uint8_t requesters) {
  this->current_image_ = std::make_shared<ESPVideoCameraImage>(
      data, length, requesters);
  for (auto *listener : this->listeners_)
    listener->on_camera_image(this->current_image_);
}

// esp_video exposes VIDIOC_S_EXT_CTRLS, not legacy VIDIOC_S_CTRL; the latter
// returns EINVAL, so runtime controls use the extended interface.
static bool gate_set_ext_ctrl(int fd, uint32_t id, int32_t value, const char *what) {
  struct v4l2_ext_control c;
  struct v4l2_ext_controls cs;
  memset(&c, 0, sizeof(c));
  memset(&cs, 0, sizeof(cs));
  c.id = id;
  c.value = value;
#ifdef V4L2_CTRL_ID2CLASS
  cs.ctrl_class = V4L2_CTRL_ID2CLASS(id);
#else
  cs.ctrl_class = (id & 0x0fff0000UL);
#endif
  cs.count = 1;
  cs.controls = &c;
  if (ioctl(fd, VIDIOC_S_EXT_CTRLS, &cs) < 0) {
    ESP_LOGW(TAG, "set %s=%d failed: %s", what, (int) value, strerror(errno));
    return false;
  }
  ESP_LOGI(TAG, "set %s=%d ok", what, (int) value);
  return true;
}

// Apply runtime settings only from the capture task against live descriptors.
// IPA auto-exposure may partially override a manual exposure on real hardware.
void ESPVideoCamera::apply_runtime_ctrls_() {
  if (!this->ctrls_dirty_.exchange(false))
    return;
  int v;
  if (this->capture_fd_ >= 0) {
    v = this->rt_exposure_.load();
    if (v >= 0)
      gate_set_ext_ctrl(this->capture_fd_, V4L2_CID_EXPOSURE, v, "exposure");
    v = this->rt_vflip_.load();
    if (v >= 0)
      gate_set_ext_ctrl(this->capture_fd_, V4L2_CID_VFLIP, v, "vflip");
    v = this->rt_hflip_.load();
    if (v >= 0)
      gate_set_ext_ctrl(this->capture_fd_, V4L2_CID_HFLIP, v, "hflip");
  }
  if (this->jpeg_fd_ >= 0) {
    v = this->rt_quality_.load();
    if (v > 0)
      gate_set_ext_ctrl(this->jpeg_fd_, V4L2_CID_JPEG_COMPRESSION_QUALITY, v, "jpeg_quality");
  }
}

void ESPVideoCamera::capture_task_trampoline(void *param) {
  static_cast<ESPVideoCamera *>(param)->capture_task_run_();
}

void ESPVideoCamera::capture_task_run_() {
  while (this->capture_task_running_.load(std::memory_order_acquire)) {
    // request_image/start_stream wakes the otherwise dormant capture task.
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    if (!this->capture_task_running_.load(std::memory_order_acquire))
      break;
    if (this->capture_faulted_.load(std::memory_order_acquire)) {
      if (this->stop_capture_())
        this->capture_faulted_.store(false, std::memory_order_release);
      this->capture_wanted_.store(false, std::memory_order_release);
      this->schedule_capture_retry_();
      continue;
    }
    if (!this->capture_wanted_.load())
      continue;
    if (!this->start_capture_()) {
      this->capture_faulted_.store(true, std::memory_order_release);
      if (this->stop_capture_())
        this->capture_faulted_.store(false, std::memory_order_release);
      this->capture_wanted_.store(false, std::memory_order_release);
      this->schedule_capture_retry_();
      continue;
    }
    // Fresh descriptors require the current runtime controls.
    this->ctrls_dirty_.store(true);
    if (this->is_raw_csi_)
      this->raw_warmup_until_ms_ = millis() + RAW_WARMUP_MS;
    while (this->capture_task_running_.load(std::memory_order_acquire) &&
           this->capture_wanted_.load(std::memory_order_acquire) &&
           !this->capture_faulted_.load(std::memory_order_acquire)) {
      // VIDIOC_DQBUF sleeps on esp_video's frame queue. Its driver timeout is
      // only a dead-sensor/teardown watchdog; a healthy source wakes this task
      // exactly when a frame becomes ready.
      // Pick up HA/web control changes between frames.
      this->apply_runtime_ctrls_();
      if (this->is_hw_jpeg_ || this->is_raw_csi_) {
        this->loop_jpeg_pipeline_();
      } else {
        this->loop_direct_capture_();
      }
    }
    if (this->capture_faulted_.load(std::memory_order_acquire)) {
      if (this->stop_capture_())
        this->capture_faulted_.store(false, std::memory_order_release);
      this->schedule_capture_retry_();
    } else {
      // No consumers remain. Stop the hardware clocks/queues, retain the
      // prepared MMAP buffers, then block again on the task notification.
      if (!this->suspend_capture_())
        this->capture_faulted_.store(true, std::memory_order_release);
    }
  }

  this->capture_wanted_.store(false, std::memory_order_release);
  const bool capture_stopped = this->stop_capture_();
  if (capture_stopped && this->pipeline_ready_) {
    const esp_err_t error = esp_video_deinit();
    if (error == ESP_OK) {
      this->pipeline_ready_ = false;
    } else {
      ESP_LOGE(TAG, "esp_video shutdown failed: %s",
               esp_err_to_name(error));
    }
  }
  if (!capture_stopped) {
    ESP_LOGE(TAG,
             "Camera descriptors still active; retaining esp_video resources");
  }
  if (this->capture_task_done_ != nullptr)
    xSemaphoreGive(this->capture_task_done_);
  vTaskDelete(nullptr);
}

void ESPVideoCamera::loop_direct_capture_() {
  // The device already delivers JPEG/MJPEG frames; one MMAP capture queue.
  struct v4l2_buffer buf;
  memset(&buf, 0, sizeof(buf));
  buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  buf.memory = V4L2_MEMORY_MMAP;

  if (ioctl(this->capture_fd_, VIDIOC_DQBUF, &buf) < 0) {
    if (errno == EINTR) return;
    ESP_LOGW(TAG, "VIDIOC_DQBUF failed; restarting capture: %s",
             strerror(errno));
    this->capture_faulted_.store(true, std::memory_order_release);
    this->capture_wanted_.store(false, std::memory_order_release);
    return;
  }

  if (buf.index < (uint32_t) this->num_capture_buffers_) {
    const auto *data = static_cast<const uint8_t *>(
        this->capture_buffers_[buf.index].start);
    const uint32_t timestamp_90khz = static_cast<uint32_t>(
        (static_cast<uint64_t>(esp_timer_get_time()) * 9ULL) / 100ULL);
    this->deliver_jpeg_frame_(data, buf.bytesused, timestamp_90khz);
    if (this->stream_requesters_.load(std::memory_order_acquire) != 0 ||
        this->single_requesters_.load(std::memory_order_acquire) != 0) {
      this->queue_frame_(data, buf.bytesused);
    }
  }

  if (ioctl(this->capture_fd_, VIDIOC_QBUF, &buf) < 0) {
    ESP_LOGW(TAG, "VIDIOC_QBUF failed; restarting capture: %s",
             strerror(errno));
    this->capture_faulted_.store(true, std::memory_order_release);
    this->capture_wanted_.store(false, std::memory_order_release);
  }
}

void ESPVideoCamera::loop_jpeg_pipeline_() {
  // Dequeue one RGB565 frame from the sensor/ISP device. This task blocks on
  // the driver's frame queue and wakes only when a frame is ready.
  struct v4l2_buffer cap_buf;
  memset(&cap_buf, 0, sizeof(cap_buf));
  cap_buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  cap_buf.memory = V4L2_MEMORY_MMAP;
  if (ioctl(this->capture_fd_, VIDIOC_DQBUF, &cap_buf) < 0) {
    if (errno == EINTR) return;
    ESP_LOGW(TAG, "capture DQBUF failed; restarting pipeline: %s",
             strerror(errno));
    this->capture_faulted_.store(true, std::memory_order_release);
    this->capture_wanted_.store(false, std::memory_order_release);
    return;
  }

  bool raw_requeued = false;
  auto requeue_raw = [&]() -> bool {
    if (raw_requeued)
      return true;
    if (ioctl(this->capture_fd_, VIDIOC_QBUF, &cap_buf) < 0) {
      ESP_LOGE(TAG, "capture QBUF failed: %s", strerror(errno));
      this->capture_faulted_.store(true, std::memory_order_release);
      this->capture_wanted_.store(false, std::memory_order_release);
      return false;
    }
    raw_requeued = true;
    return true;
  };
  auto fail_owned_jpeg_buffer = [&](const char *operation) {
    // Once JPEG OUTPUT has accepted a USERPTR, only OUTPUT DQBUF or STREAMOFF
    // can release it. Stop the pipeline rather than reusing a possibly-owned
    // raw/intermediate buffer on the next frame.
    ESP_LOGE(TAG, "%s failed; stopping camera pipeline: %s", operation,
             strerror(errno));
    this->capture_faulted_.store(true, std::memory_order_release);
    this->capture_wanted_.store(false, std::memory_order_release);
  };

  if (cap_buf.index >= (uint32_t) this->num_capture_buffers_ ||
      cap_buf.bytesused == 0) {
    requeue_raw();
    return;
  }

  // The first CSI buffers after STREAMON can precede stable ISP color and
  // exposure. Discard them before any raw or JPEG consumer can turn one into
  // the first frame of a new video session.
  if (this->startup_frames_remaining_ > 0) {
    this->startup_frames_remaining_--;
    requeue_raw();
    return;
  }

  // During the short AE linger window the sensor stays streaming, but there is
  // no reason to rotate, encode, copy or wake the ESPHome loop without a
  // consumer. Requeue this naturally delivered frame immediately.
  if (!this->has_consumers_()) {
    requeue_raw();
    return;
  }

  const auto &raw_buffer = this->capture_buffers_[cap_buf.index];
  const bool raw_warming_up =
      this->is_raw_csi_ &&
      static_cast<int32_t>(millis() - this->raw_warmup_until_ms_) < 0;
  RawVideoFrameConsumer *raw_consumer =
      this->raw_frame_consumer_.load(std::memory_order_acquire);
  if (!raw_warming_up && raw_consumer != nullptr &&
      this->raw_frame_consumer_active_.load(std::memory_order_acquire)) {
    const uint32_t timestamp_90khz = static_cast<uint32_t>(
        (static_cast<uint64_t>(esp_timer_get_time()) * 9ULL) / 100ULL);
    // The consumer is synchronous, so the sensor MMAP buffer remains owned by
    // this task until its codec/PPA work finishes. Camera JPEG rotation and
    // scaling are a separate consumer-specific path below.
    this->deliver_raw_frame_(
        static_cast<const uint8_t *>(raw_buffer.start), cap_buf.bytesused,
        this->capture_pixel_format_,
        static_cast<uint16_t>(this->capture_width_),
        static_cast<uint16_t>(this->capture_height_),
        static_cast<uint16_t>(this->capture_stride_bytes_), timestamp_90khz,
        this->rotation_degrees_);
  }

  // A raw consumer drives capture directly. Do not run the camera PPA or
  // hardware JPEG encoder when no ESPHome camera requester needs an image.
  const bool jpeg_wanted =
      this->stream_requesters_.load(std::memory_order_acquire) != 0 ||
      this->single_requesters_.load(std::memory_order_acquire) != 0 ||
      this->jpeg_frame_consumer_active_.load(std::memory_order_acquire);
  if (this->is_raw_csi_ || !jpeg_wanted) {
    requeue_raw();
    return;
  }

#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  const bool debug_jpeg =
      this->jpeg_frame_consumer_active_.load(std::memory_order_acquire);
  const uint32_t debug_generation =
      this->jpeg_debug_generation_.load(std::memory_order_acquire);
  if (debug_jpeg &&
      debug_generation != this->jpeg_debug_observed_generation_) {
    this->jpeg_debug_observed_generation_ = debug_generation;
    this->jpeg_debug_frames_ = 0;
    this->jpeg_debug_ppa_total_us_ = 0;
    this->jpeg_debug_encode_total_us_ = 0;
    this->jpeg_debug_ppa_max_us_ = 0;
    this->jpeg_debug_encode_max_us_ = 0;
    this->jpeg_debug_last_log_ms_ = millis();
  }
#endif

  const uint8_t *jpeg_input =
      static_cast<const uint8_t *>(raw_buffer.start);
  size_t jpeg_input_bytes = cap_buf.bytesused;
  size_t jpeg_input_alloc_size = raw_buffer.length;
  uint16_t processed_width = static_cast<uint16_t>(this->capture_width_);
  uint16_t processed_height = static_cast<uint16_t>(this->capture_height_);
  uint16_t processed_stride =
      static_cast<uint16_t>(this->capture_stride_bytes_);

  if (this->ppa_transform_required_) {
    if (this->ppa_srm_ == nullptr || this->rotated_rgb565_ == nullptr ||
        cap_buf.bytesused < this->capture_frame_bytes_) {
      ESP_LOGE(TAG, "PPA transform input is unavailable or truncated");
      requeue_raw();
      return;
    }

    ppa_srm_oper_config_t operation {};
    operation.in.buffer = raw_buffer.start;
    operation.in.pic_w = this->capture_width_;
    operation.in.pic_h = this->capture_height_;
    operation.in.block_w = this->capture_width_;
    operation.in.block_h = this->capture_height_;
    operation.in.srm_cm = PPA_SRM_COLOR_MODE_RGB565;
    operation.out.buffer = this->rotated_rgb565_;
    operation.out.buffer_size =
        static_cast<uint32_t>(this->rotated_rgb565_alloc_size_);
    operation.out.pic_w = this->jpeg_width_;
    operation.out.pic_h = this->jpeg_height_;
    operation.out.srm_cm = PPA_SRM_COLOR_MODE_RGB565;
    operation.rotation_angle =
        ppa_rotation_for_clockwise(this->rotation_degrees_);
    operation.scale_x = this->ppa_scale_x_;
    operation.scale_y = this->ppa_scale_y_;
    operation.rgb_swap = false;
    operation.byte_swap = false;
    operation.mode = PPA_TRANS_MODE_BLOCKING;

#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
    const uint32_t ppa_started_us = debug_jpeg ? micros() : 0;
#endif
    const esp_err_t error =
        ppa_do_scale_rotate_mirror(this->ppa_srm_, &operation);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
    if (debug_jpeg) {
      const uint32_t elapsed_us = micros() - ppa_started_us;
      this->jpeg_debug_ppa_total_us_ += elapsed_us;
      this->jpeg_debug_ppa_max_us_ =
          std::max(this->jpeg_debug_ppa_max_us_, elapsed_us);
    }
#endif
    if (error != ESP_OK) {
      ESP_LOGE(TAG, "PPA camera transform failed; frame dropped: %s",
               esp_err_to_name(error));
      requeue_raw();
      return;
    }

    // PPA blocking has finished reading the sensor MMAP buffer. Return it
    // immediately; JPEG owns only the persistent rotated buffer.
    if (!requeue_raw())
      return;
    jpeg_input = this->rotated_rgb565_;
    jpeg_input_bytes = this->rotated_rgb565_bytes_;
    jpeg_input_alloc_size = this->rotated_rgb565_alloc_size_;
    processed_width = static_cast<uint16_t>(this->jpeg_width_);
    processed_height = static_cast<uint16_t>(this->jpeg_height_);
    processed_stride =
        static_cast<uint16_t>(this->jpeg_width_ * sizeof(uint16_t));
  }

  struct v4l2_buffer out_buf {};
  out_buf.type = V4L2_BUF_TYPE_VIDEO_OUTPUT;
  out_buf.memory = V4L2_MEMORY_USERPTR;
  out_buf.index = 0;
  out_buf.m.userptr = reinterpret_cast<unsigned long>(jpeg_input);
  out_buf.length = jpeg_input_alloc_size;
  out_buf.bytesused = jpeg_input_bytes;

  struct v4l2_buffer jpeg_buf {};
  jpeg_buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  jpeg_buf.memory = V4L2_MEMORY_MMAP;
  jpeg_buf.index = 0;

#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  const uint32_t encode_started_us = debug_jpeg ? micros() : 0;
#endif
  if (ioctl(this->jpeg_fd_, VIDIOC_QBUF, &out_buf) < 0) {
    ESP_LOGW(TAG, "JPEG OUTPUT QBUF failed: %s", strerror(errno));
    requeue_raw();
    return;
  }
  if (ioctl(this->jpeg_fd_, VIDIOC_QBUF, &jpeg_buf) < 0) {
    fail_owned_jpeg_buffer("JPEG CAPTURE QBUF");
    return;
  }

  // esp_video 2.3.0 triggers lazy M2M processing from CAPTURE DQBUF. OUTPUT
  // DQBUF must follow it; reversing this order blocks forever on ready_sem.
  memset(&jpeg_buf, 0, sizeof(jpeg_buf));
  jpeg_buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  jpeg_buf.memory = V4L2_MEMORY_MMAP;
  if (ioctl(this->jpeg_fd_, VIDIOC_DQBUF, &jpeg_buf) < 0) {
    fail_owned_jpeg_buffer("JPEG CAPTURE DQBUF");
    return;
  }

  struct v4l2_buffer done_buf {};
  done_buf.type = V4L2_BUF_TYPE_VIDEO_OUTPUT;
  done_buf.memory = V4L2_MEMORY_USERPTR;
  if (ioctl(this->jpeg_fd_, VIDIOC_DQBUF, &done_buf) < 0) {
    fail_owned_jpeg_buffer("JPEG OUTPUT DQBUF");
    return;
  }
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  if (debug_jpeg) {
    const uint32_t elapsed_us = micros() - encode_started_us;
    this->jpeg_debug_encode_total_us_ += elapsed_us;
    this->jpeg_debug_encode_max_us_ =
        std::max(this->jpeg_debug_encode_max_us_, elapsed_us);
    this->jpeg_debug_frames_++;
    const uint32_t now = millis();
    if (now - this->jpeg_debug_last_log_ms_ >= 5000U) {
      this->jpeg_debug_last_log_ms_ = now;
      ESP_LOGI(
          TAG,
          "Camera JPEG hardware: frames=%u ppa_avg=%uus ppa_max=%uus "
          "encode_avg=%uus encode_max=%uus",
          (unsigned) this->jpeg_debug_frames_,
          (unsigned) (this->jpeg_debug_ppa_total_us_ /
                      std::max<uint32_t>(1, this->jpeg_debug_frames_)),
          (unsigned) this->jpeg_debug_ppa_max_us_,
          (unsigned) (this->jpeg_debug_encode_total_us_ /
                      std::max<uint32_t>(1, this->jpeg_debug_frames_)),
          (unsigned) this->jpeg_debug_encode_max_us_);
    }
  }
#endif

  // With rotation disabled JPEG has just released the sensor USERPTR.
  if (!requeue_raw())
    return;

  if (jpeg_buf.bytesused > this->jpeg_out_buffer_.length) {
    ESP_LOGE(TAG, "JPEG encoder returned an oversized frame");
    this->capture_faulted_.store(true, std::memory_order_release);
    this->capture_wanted_.store(false, std::memory_order_release);
    return;
  }
  const auto *jpeg_data =
      static_cast<const uint8_t *>(this->jpeg_out_buffer_.start);
  const uint32_t timestamp_90khz = static_cast<uint32_t>(
      (static_cast<uint64_t>(esp_timer_get_time()) * 9ULL) / 100ULL);
  this->deliver_jpeg_frame_(jpeg_data, jpeg_buf.bytesused, timestamp_90khz);
  if (this->stream_requesters_.load(std::memory_order_acquire) != 0 ||
      this->single_requesters_.load(std::memory_order_acquire) != 0) {
    this->queue_frame_(jpeg_data, jpeg_buf.bytesused);
  }
}

void ESPVideoCamera::deliver_raw_frame_(
    const uint8_t *data, size_t size, RawVideoPixelFormat pixel_format,
    uint16_t width, uint16_t height, uint16_t stride_bytes,
    uint32_t timestamp_90khz,
    uint16_t rotation_degrees) {
  RawVideoFrameConsumer *consumer =
      this->raw_frame_consumer_.load(std::memory_order_acquire);
  if (consumer == nullptr ||
      !this->raw_frame_consumer_active_.load(std::memory_order_acquire)) {
    return;
  }
  consumer->consume_raw_video_frame(
      RawVideoFrame{data, size, pixel_format, width, height, stride_bytes,
                    timestamp_90khz, rotation_degrees});
}

void ESPVideoCamera::deliver_jpeg_frame_(const uint8_t *data, size_t size,
                                         uint32_t timestamp_90khz) {
  JpegFrameConsumer *consumer =
      this->jpeg_frame_consumer_.load(std::memory_order_acquire);
  if (consumer == nullptr ||
      !this->jpeg_frame_consumer_active_.load(std::memory_order_acquire)) {
    return;
  }
  consumer->consume_jpeg_frame(JpegFrame{data, size, timestamp_90khz});
}

camera::CameraImageReader *ESPVideoCamera::create_image_reader() { return new ESPVideoCameraImageReader(); }

void ESPVideoCamera::request_image(camera::CameraRequester requester) {
  this->single_requesters_.fetch_or(
      static_cast<uint8_t>(1U << requester), std::memory_order_acq_rel);
  this->update_capture_state_();
}

void ESPVideoCamera::start_stream(camera::CameraRequester requester) {
  for (auto *listener : this->listeners_)
    listener->on_stream_start();
  this->stream_requesters_.fetch_or(
      static_cast<uint8_t>(1U << requester), std::memory_order_acq_rel);
  this->update_capture_state_();
}

void ESPVideoCamera::stop_stream(camera::CameraRequester requester) {
  for (auto *listener : this->listeners_)
    listener->on_stream_stop();
  this->stream_requesters_.fetch_and(
      static_cast<uint8_t>(~(1U << requester)), std::memory_order_acq_rel);
  this->update_capture_state_();
}

bool ESPVideoCamera::register_raw_frame_consumer(
    RawVideoFrameConsumer *consumer, RawVideoPixelFormat pixel_format) {
  if (consumer == nullptr) return false;
  RawVideoFrameConsumer *expected = nullptr;
  if (this->raw_frame_consumer_.compare_exchange_strong(
          expected, consumer, std::memory_order_acq_rel)) {
    this->raw_frame_pixel_format_.store(
        pixel_format, std::memory_order_release);
    return true;
  }
  return expected == consumer &&
         this->raw_frame_pixel_format_.load(std::memory_order_acquire) ==
             pixel_format;
}

bool ESPVideoCamera::start_raw_frame_consumer(
    RawVideoFrameConsumer *consumer) {
  if (consumer == nullptr ||
      this->raw_frame_consumer_.load(std::memory_order_acquire) != consumer ||
      (!this->is_hw_jpeg_ && !this->is_raw_csi_)) {
    return false;
  }
  this->raw_frame_consumer_active_.store(true, std::memory_order_release);
  this->update_capture_state_();
  return true;
}

void ESPVideoCamera::stop_raw_frame_consumer(
    RawVideoFrameConsumer *consumer) {
  if (consumer == nullptr ||
      this->raw_frame_consumer_.load(std::memory_order_acquire) != consumer) {
    return;
  }
  this->raw_frame_consumer_active_.store(false, std::memory_order_release);
  this->update_capture_state_();
}

bool ESPVideoCamera::register_jpeg_frame_consumer(
    JpegFrameConsumer *consumer) {
  if (consumer == nullptr) return false;
  JpegFrameConsumer *expected = nullptr;
  if (this->jpeg_frame_consumer_.compare_exchange_strong(
          expected, consumer, std::memory_order_acq_rel)) {
    return true;
  }
  return expected == consumer;
}

bool ESPVideoCamera::start_jpeg_frame_consumer(
    JpegFrameConsumer *consumer) {
  if (consumer == nullptr ||
      this->jpeg_frame_consumer_.load(std::memory_order_acquire) != consumer ||
      this->is_raw_csi_) {
    return false;
  }
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  this->jpeg_debug_generation_.fetch_add(1, std::memory_order_acq_rel);
#endif
  this->jpeg_frame_consumer_active_.store(true, std::memory_order_release);
  this->update_capture_state_();
  return true;
}

void ESPVideoCamera::stop_jpeg_frame_consumer(
    JpegFrameConsumer *consumer) {
  if (consumer == nullptr ||
      this->jpeg_frame_consumer_.load(std::memory_order_acquire) != consumer) {
    return;
  }
  this->jpeg_frame_consumer_active_.store(false, std::memory_order_release);
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  this->jpeg_debug_generation_.fetch_add(1, std::memory_order_acq_rel);
#endif
  this->update_capture_state_();
}

// Capture state is owned by the worker. Callers only update atomic intent and
// wake the worker/main-loop one-shot choreography.
bool ESPVideoCamera::has_consumers_() const {
  return this->stream_requesters_.load(std::memory_order_acquire) != 0 ||
         this->single_requesters_.load(std::memory_order_acquire) != 0 ||
         this->raw_frame_consumer_active_.load(std::memory_order_acquire) ||
         this->jpeg_frame_consumer_active_.load(std::memory_order_acquire);
}

void ESPVideoCamera::update_capture_state_() {
  const bool wanted = this->has_consumers_();
  if (wanted) {
    this->capture_retry_attempts_.store(0, std::memory_order_release);
    this->capture_wanted_.store(true, std::memory_order_release);
    if (this->capture_task_ != nullptr)
      xTaskNotifyGive(this->capture_task_);
  }
  // Arm/cancel the one-shot linger timer on the ESPHome task.
  this->enable_loop_soon_any_context();
}

void ESPVideoCamera::schedule_capture_retry_() {
  if (!this->has_consumers_()) return;
  const uint8_t attempt =
      this->capture_retry_attempts_.fetch_add(1, std::memory_order_acq_rel);
  if (attempt >= MAX_CAPTURE_RETRIES) {
    if (attempt == MAX_CAPTURE_RETRIES) {
      ESP_LOGE(TAG,
               "Camera recovery exhausted; waiting for a new image request");
    }
    return;
  }
  this->capture_retry_requested_.store(true, std::memory_order_release);
  this->enable_loop_soon_any_context();
}

bool ESPVideoCamera::configure_capture_format_(uint32_t pixelformat) {
  uint32_t width = 0, height = 0;
  bool force_res = parse_resolution(this->resolution_, width, height);

  struct v4l2_format fmt;
  memset(&fmt, 0, sizeof(fmt));
  fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  ioctl(this->capture_fd_, VIDIOC_G_FMT, &fmt);  // best-effort starting point
  fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  fmt.fmt.pix.pixelformat = pixelformat;
  if (force_res) {
    fmt.fmt.pix.width = width;
    fmt.fmt.pix.height = height;
  }
  fmt.fmt.pix.field = V4L2_FIELD_NONE;
  if (ioctl(this->capture_fd_, VIDIOC_S_FMT, &fmt) < 0)
    ESP_LOGW(TAG, "VIDIOC_S_FMT (best-effort resolution) failed: %s", strerror(errno));

  // Read back the resolution actually negotiated by the sensor/ISP.
  memset(&fmt, 0, sizeof(fmt));
  fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  if (ioctl(this->capture_fd_, VIDIOC_G_FMT, &fmt) == 0) {
    this->capture_width_ = fmt.fmt.pix.width;
    this->capture_height_ = fmt.fmt.pix.height;
    if (fmt.fmt.pix.pixelformat != pixelformat) {
      ESP_LOGE(TAG, "Capture format mismatch: requested %08x, got %08x",
               (unsigned) pixelformat,
               (unsigned) fmt.fmt.pix.pixelformat);
      return false;
    }
    if (pixelformat == V4L2_PIX_FMT_RGB565) {
      const uint32_t packed_stride =
          this->capture_width_ * sizeof(uint16_t);
      const uint32_t actual_stride =
          fmt.fmt.pix.bytesperline == 0 ? packed_stride
                                       : fmt.fmt.pix.bytesperline;
      if (actual_stride != packed_stride) {
        ESP_LOGE(TAG,
                 "Unsupported RGB565 capture stride: %u bytes for width %u",
                 (unsigned) actual_stride,
                 (unsigned) this->capture_width_);
        return false;
      }
      this->capture_stride_bytes_ = actual_stride;
      if (this->capture_height_ >
          std::numeric_limits<size_t>::max() /
              this->capture_stride_bytes_) {
        ESP_LOGE(TAG, "RGB565 capture size overflow");
        return false;
      }
      this->capture_frame_bytes_ =
          static_cast<size_t>(this->capture_stride_bytes_) *
          this->capture_height_;
      this->capture_pixel_format_ = RawVideoPixelFormat::RGB565_LE;
    } else if (pixelformat == V4L2_PIX_FMT_YUV420) {
      if ((this->capture_width_ & 1U) != 0 ||
          (this->capture_height_ & 1U) != 0) {
        ESP_LOGE(TAG, "YUV420 capture dimensions must be even");
        return false;
      }
      this->capture_stride_bytes_ = this->capture_width_;
      this->capture_frame_bytes_ =
          static_cast<size_t>(this->capture_width_) *
          this->capture_height_ * 3 / 2;
      this->capture_pixel_format_ =
          RawVideoPixelFormat::YUV420_OUYY_EVYY;
    }
  } else {
    this->capture_width_ = width;
    this->capture_height_ = height;
    if (pixelformat == V4L2_PIX_FMT_RGB565) {
      this->capture_stride_bytes_ =
          this->capture_width_ * sizeof(uint16_t);
      this->capture_frame_bytes_ =
          static_cast<size_t>(this->capture_stride_bytes_) *
          this->capture_height_;
      this->capture_pixel_format_ = RawVideoPixelFormat::RGB565_LE;
    } else if (pixelformat == V4L2_PIX_FMT_YUV420 &&
               (this->capture_width_ & 1U) == 0 &&
               (this->capture_height_ & 1U) == 0) {
      this->capture_stride_bytes_ = this->capture_width_;
      this->capture_frame_bytes_ =
          static_cast<size_t>(this->capture_width_) *
          this->capture_height_ * 3 / 2;
      this->capture_pixel_format_ =
          RawVideoPixelFormat::YUV420_OUYY_EVYY;
    }
  }
  ESP_LOGI(TAG, "Capture resolution: %ux%u", (unsigned) this->capture_width_, (unsigned) this->capture_height_);
  return true;
}

bool ESPVideoCamera::configure_capture_framerate_() {
  struct v4l2_streamparm parm {};
  parm.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  if (ioctl(this->capture_fd_, VIDIOC_G_PARM, &parm) < 0) {
    ESP_LOGW(TAG, "capture VIDIOC_G_PARM failed; using software throttle: %s",
             strerror(errno));
    return false;
  }

  auto &capture = parm.parm.capture;
  const uint32_t reported_fps = capture.timeperframe.denominator;
  if ((capture.capability & V4L2_CAP_TIMEPERFRAME) == 0 ||
      capture.timeperframe.numerator != 1 || reported_fps == 0) {
    ESP_LOGW(TAG, "capture device does not expose a usable integer frame rate");
    return false;
  }
  if (this->native_capture_fps_ == 0 ||
      reported_fps > this->native_capture_fps_) {
    this->native_capture_fps_ = reported_fps;
  }
  const uint32_t source_fps = this->native_capture_fps_;

  uint32_t target_fps = static_cast<uint32_t>(
      std::floor(this->max_framerate_.load(std::memory_order_acquire)));
  target_fps = std::clamp<uint32_t>(target_fps, 1, source_fps);
  while (target_fps > 1 && source_fps % target_fps != 0)
    target_fps--;

  if (target_fps == reported_fps) {
    ESP_LOGI(TAG, "Capture framerate: %u fps", (unsigned) reported_fps);
    return true;
  }

  // esp_video's CSI implementation requires numerator=1 and an integer divisor
  // of the sensor rate. It then skips frames in the ISR, so DQBUF remains
  // event-driven and PPA/JPEG only process frames that can actually be used.
  capture.timeperframe.numerator = 1;
  capture.timeperframe.denominator = target_fps;
  if (ioctl(this->capture_fd_, VIDIOC_S_PARM, &parm) < 0) {
    ESP_LOGW(TAG, "capture VIDIOC_S_PARM %u fps failed; using software throttle: %s",
             (unsigned) target_fps, strerror(errno));
    return false;
  }

  struct v4l2_streamparm actual {};
  actual.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  if (ioctl(this->capture_fd_, VIDIOC_G_PARM, &actual) == 0 &&
      actual.parm.capture.timeperframe.numerator == 1 &&
      actual.parm.capture.timeperframe.denominator > 0) {
    const uint32_t actual_fps =
        actual.parm.capture.timeperframe.denominator;
    ESP_LOGI(TAG, "Capture framerate: %u fps (source %u fps)",
             (unsigned) actual_fps,
             (unsigned) source_fps);
    if (actual_fps <= target_fps)
      return true;
  }
  ESP_LOGW(TAG, "capture framerate could not be confirmed; using software throttle");
  return false;
}

bool ESPVideoCamera::setup_rotation_() {
  this->ppa_transform_required_ = false;
  this->ppa_scale_x_ = 1.0f;
  this->ppa_scale_y_ = 1.0f;

  uint32_t transform_width = this->capture_width_;
  uint32_t transform_height = this->capture_height_;

  // Some MIPI sensors expose only a small set of native modes. If S_FMT
  // coerces a requested smaller size upward, use the same PPA SRM transaction
  // that rotates the image to downscale it before JPEG encoding. PPA scale
  // factors have 1/16 precision, so accept only dimensions represented exactly
  // and otherwise retain the negotiated capture size.
  uint32_t requested_width = 0;
  uint32_t requested_height = 0;
  if (parse_resolution(this->resolution_, requested_width, requested_height) &&
      requested_width <= this->capture_width_ &&
      requested_height <= this->capture_height_) {
    static constexpr uint32_t kPpaScaleUnits = 16;
    const uint32_t scale_x_units = static_cast<uint32_t>(
        static_cast<uint64_t>(requested_width) * kPpaScaleUnits /
        this->capture_width_);
    const uint32_t scale_y_units = static_cast<uint32_t>(
        static_cast<uint64_t>(requested_height) * kPpaScaleUnits /
        this->capture_height_);
    if (scale_x_units >= 1 && scale_y_units >= 1 &&
        static_cast<uint64_t>(this->capture_width_) * scale_x_units /
                kPpaScaleUnits ==
            requested_width &&
        static_cast<uint64_t>(this->capture_height_) * scale_y_units /
                kPpaScaleUnits ==
            requested_height) {
      transform_width = requested_width;
      transform_height = requested_height;
      this->ppa_scale_x_ =
          static_cast<float>(scale_x_units) / kPpaScaleUnits;
      this->ppa_scale_y_ =
          static_cast<float>(scale_y_units) / kPpaScaleUnits;
    } else if (requested_width != this->capture_width_ ||
               requested_height != this->capture_height_) {
      ESP_LOGW(TAG,
               "Requested %ux%u cannot be represented by PPA 1/16 scaling; "
               "using negotiated %ux%u",
               (unsigned) requested_width, (unsigned) requested_height,
               (unsigned) this->capture_width_,
               (unsigned) this->capture_height_);
    }
  }

  this->jpeg_width_ = transform_width;
  this->jpeg_height_ = transform_height;
  if (this->rotation_degrees_ == 90 || this->rotation_degrees_ == 270) {
    this->jpeg_width_ = transform_height;
    this->jpeg_height_ = transform_width;
  }
  this->ppa_transform_required_ =
      this->rotation_degrees_ != 0 ||
      transform_width != this->capture_width_ ||
      transform_height != this->capture_height_;
  if (!this->ppa_transform_required_)
    return true;

  if (this->ppa_srm_ != nullptr) {
    ESP_LOGE(TAG, "PPA transform client was not released");
    return false;
  }

#if defined(CONFIG_SECURE_FLASH_ENC_ENABLED) && CONFIG_SECURE_FLASH_ENC_ENABLED
  // PPA SRM processes macroblocks at addresses that cannot satisfy encrypted
  // external-memory alignment. A full RGB565 frame is too large for internal
  // RAM, so fail explicitly instead of producing corrupted output.
  ESP_LOGE(TAG, "PPA camera transform is unsupported with encrypted PSRAM");
  return false;
#endif

  if (this->jpeg_width_ == 0 || this->jpeg_height_ == 0 ||
      this->jpeg_width_ >
          std::numeric_limits<size_t>::max() / this->jpeg_height_ /
              sizeof(uint16_t)) {
    ESP_LOGE(TAG, "Invalid output dimensions for PPA transform");
    return false;
  }

  const size_t required_bytes =
      static_cast<size_t>(this->jpeg_width_) * this->jpeg_height_ *
      sizeof(uint16_t);
  if (required_bytes >
      std::numeric_limits<size_t>::max() - (PPA_BUFFER_ALIGNMENT - 1)) {
    ESP_LOGE(TAG, "PPA transform buffer size overflow");
    return false;
  }
  const size_t required_alloc_size =
      (required_bytes + PPA_BUFFER_ALIGNMENT - 1) &
      ~(PPA_BUFFER_ALIGNMENT - 1);
  if (required_alloc_size >
      std::numeric_limits<uint32_t>::max()) {
    ESP_LOGE(TAG, "PPA transform buffer is too large");
    return false;
  }

  if (this->rotated_rgb565_ == nullptr ||
      this->rotated_rgb565_capacity_ < required_alloc_size) {
    if (this->rotated_rgb565_ != nullptr)
      heap_caps_free(this->rotated_rgb565_);
    this->rotated_rgb565_ = static_cast<uint8_t *>(
        heap_caps_aligned_alloc(
            PPA_BUFFER_ALIGNMENT, required_alloc_size,
            MALLOC_CAP_SPIRAM | MALLOC_CAP_DMA | MALLOC_CAP_8BIT));
    if (this->rotated_rgb565_ == nullptr) {
      this->rotated_rgb565_capacity_ = 0;
      ESP_LOGE(TAG, "Failed to allocate %u-byte PPA transform buffer",
               (unsigned) required_alloc_size);
      return false;
    }
    this->rotated_rgb565_capacity_ = required_alloc_size;
  }
  this->rotated_rgb565_bytes_ = required_bytes;
  this->rotated_rgb565_alloc_size_ = required_alloc_size;

  ppa_client_config_t client {};
  client.oper_type = PPA_OPERATION_SRM;
  client.max_pending_trans_num = 1;
  const esp_err_t error = ppa_register_client(&client, &this->ppa_srm_);
  if (error != ESP_OK || this->ppa_srm_ == nullptr) {
    ESP_LOGE(TAG, "PPA SRM client registration failed: %s",
             esp_err_to_name(error));
    this->rotated_rgb565_bytes_ = 0;
    this->rotated_rgb565_alloc_size_ = 0;
    return false;
  }

  ESP_LOGI(TAG,
           "PPA camera transform: rotation=%u, scale=%.3fx%.3f, JPEG=%ux%u",
           (unsigned) this->rotation_degrees_, this->ppa_scale_x_,
           this->ppa_scale_y_, (unsigned) this->jpeg_width_,
           (unsigned) this->jpeg_height_);
  return true;
}

bool ESPVideoCamera::release_rotation_() {
  bool client_released = true;
  if (this->ppa_srm_ != nullptr) {
    const esp_err_t error = ppa_unregister_client(this->ppa_srm_);
    if (error == ESP_OK) {
      this->ppa_srm_ = nullptr;
    } else {
      // Never free a DMA buffer while the driver still reports an unfinished
      // transaction. Blocking mode makes this exceptional, but leaking is
      // safer than a use-after-free if the peripheral ever wedges.
      ESP_LOGE(TAG, "PPA SRM client unregister failed: %s",
               esp_err_to_name(error));
      client_released = false;
    }
  }
  if (client_released) {
    // Keep the DMA allocation warm. Camera and LVGL video sessions otherwise
    // alternate large PSRAM allocations and can leave no contiguous block for
    // the next RGB565 transform, despite plenty of total PSRAM.
    this->ppa_transform_required_ = false;
    this->ppa_scale_x_ = 1.0f;
    this->ppa_scale_y_ = 1.0f;
    this->rotated_rgb565_bytes_ = 0;
    this->rotated_rgb565_alloc_size_ = 0;
    this->jpeg_width_ = 0;
    this->jpeg_height_ = 0;
  }
  return client_released;
}

bool ESPVideoCamera::setup_capture_buffers_() {
  struct v4l2_requestbuffers req;
  memset(&req, 0, sizeof(req));
  req.count = MAX_BUFFERS;
  req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  req.memory = V4L2_MEMORY_MMAP;
  if (ioctl(this->capture_fd_, VIDIOC_REQBUFS, &req) < 0) {
    ESP_LOGE(TAG, "VIDIOC_REQBUFS failed: %s", strerror(errno));
    return false;
  }

  this->num_capture_buffers_ = 0;
  for (unsigned int i = 0; i < req.count && i < MAX_BUFFERS; i++) {
    struct v4l2_buffer buf;
    memset(&buf, 0, sizeof(buf));
    buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    buf.memory = V4L2_MEMORY_MMAP;
    buf.index = i;
    if (ioctl(this->capture_fd_, VIDIOC_QUERYBUF, &buf) < 0) {
      ESP_LOGE(TAG, "VIDIOC_QUERYBUF[%u] failed: %s", i, strerror(errno));
      return false;
    }
    this->capture_buffers_[i].length = buf.length;
    this->capture_buffers_[i].start =
        mmap(nullptr, buf.length, PROT_READ | PROT_WRITE, MAP_SHARED, this->capture_fd_, buf.m.offset);
    if (this->capture_buffers_[i].start == MAP_FAILED) {
      this->capture_buffers_[i].start = nullptr;
      ESP_LOGE(TAG, "mmap[%u] failed: %s", i, strerror(errno));
      return false;
    }
    this->num_capture_buffers_++;
    if (ioctl(this->capture_fd_, VIDIOC_QBUF, &buf) < 0) {
      ESP_LOGE(TAG, "VIDIOC_QBUF[%u] failed: %s", i, strerror(errno));
      return false;
    }
  }
  return true;
}

bool ESPVideoCamera::start_capture_() {
  if (this->streaming_) {
    return !this->capture_faulted_.load(std::memory_order_acquire) &&
           this->capture_streaming_ &&
           (!this->is_hw_jpeg_ ||
            (this->jpeg_output_streaming_ &&
             this->jpeg_capture_streaming_));
  }
  if (this->is_failed())
    return false;
  if (this->capture_prepared_) {
    if (!this->resume_capture_())
      return false;
    if (this->is_hw_jpeg_)
      this->startup_frames_remaining_ = STARTUP_FRAME_COUNT;
    this->last_frame_ms_ = 0;
    return true;
  }
  if (this->capture_fd_ >= 0 || this->jpeg_fd_ >= 0 ||
      this->ppa_srm_ != nullptr) {
    if (!this->stop_capture_())
      return false;
    if (this->capture_fd_ >= 0 || this->jpeg_fd_ >= 0 ||
        this->ppa_srm_ != nullptr) {
      ESP_LOGE(TAG, "Previous camera resources are still owned");
      return false;
    }
  }

  this->hardware_framerate_active_.store(false, std::memory_order_release);
  bool ok = (this->is_hw_jpeg_ || this->is_raw_csi_)
                ? this->start_jpeg_pipeline_()
                : this->start_direct_capture_();
  if (!ok) {
    this->stop_capture_();
    return false;
  }
  this->capture_prepared_ = true;
  this->capture_faulted_.store(false, std::memory_order_release);
  this->streaming_ = true;
  if (this->is_hw_jpeg_)
    this->startup_frames_remaining_ = STARTUP_FRAME_COUNT;
  this->last_frame_ms_ = 0;
  return true;
}

bool ESPVideoCamera::resume_capture_() {
  if (!this->capture_prepared_ || this->capture_fd_ < 0 ||
      this->num_capture_buffers_ <= 0) {
    return false;
  }
  if (this->capture_streaming_ || this->jpeg_output_streaming_ ||
      this->jpeg_capture_streaming_) {
    ESP_LOGE(TAG, "Cannot resume partially active V4L2 queues");
    return false;
  }
  if (this->is_hw_jpeg_ &&
      (this->jpeg_fd_ < 0 || this->jpeg_out_buffer_.start == nullptr ||
       (this->ppa_transform_required_ &&
        (this->ppa_srm_ == nullptr || this->rotated_rgb565_ == nullptr)))) {
    ESP_LOGE(TAG, "Prepared JPEG pipeline is incomplete");
    return false;
  }

  if (this->is_hw_jpeg_ || this->is_raw_csi_) {
    this->hardware_framerate_active_.store(
        this->configure_capture_framerate_(), std::memory_order_release);
  }

  // Espressif's V4L2 buffer-sequence contract queues the same prepared MMAP
  // buffers before every STREAMON. No REQBUFS or heap allocation is needed.
  for (int i = 0; i < this->num_capture_buffers_; i++) {
    struct v4l2_buffer buffer {};
    buffer.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    buffer.memory = V4L2_MEMORY_MMAP;
    buffer.index = i;
    if (ioctl(this->capture_fd_, VIDIOC_QBUF, &buffer) < 0) {
      ESP_LOGE(TAG, "resume VIDIOC_QBUF[%d] failed: %s", i,
               strerror(errno));
      return false;
    }
  }

  clamp_isp_log_levels();
  int capture_type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  if (ioctl(this->capture_fd_, VIDIOC_STREAMON, &capture_type) < 0) {
    ESP_LOGE(TAG, "resume capture STREAMON failed: %s", strerror(errno));
    return false;
  }
  this->capture_streaming_ = true;

  if (this->is_hw_jpeg_) {
    int output_type = V4L2_BUF_TYPE_VIDEO_OUTPUT;
    int jpeg_type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (ioctl(this->jpeg_fd_, VIDIOC_STREAMON, &output_type) < 0) {
      ESP_LOGE(TAG, "resume JPEG OUTPUT STREAMON failed: %s",
               strerror(errno));
      this->suspend_capture_();
      return false;
    }
    this->jpeg_output_streaming_ = true;
    if (ioctl(this->jpeg_fd_, VIDIOC_STREAMON, &jpeg_type) < 0) {
      ESP_LOGE(TAG, "resume JPEG CAPTURE STREAMON failed: %s",
               strerror(errno));
      this->suspend_capture_();
      return false;
    }
    this->jpeg_capture_streaming_ = true;
  }

  this->capture_faulted_.store(false, std::memory_order_release);
  this->streaming_ = true;
  return true;
}

bool ESPVideoCamera::suspend_capture_() {
  bool stopped = true;
  if (this->jpeg_fd_ >= 0 && this->jpeg_output_streaming_) {
    int output_type = V4L2_BUF_TYPE_VIDEO_OUTPUT;
    if (ioctl(this->jpeg_fd_, VIDIOC_STREAMOFF, &output_type) == 0) {
      this->jpeg_output_streaming_ = false;
    } else {
      ESP_LOGE(TAG, "JPEG OUTPUT STREAMOFF failed: %s", strerror(errno));
      stopped = false;
    }
  }
  if (this->jpeg_fd_ >= 0 && this->jpeg_capture_streaming_) {
    int jpeg_type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (ioctl(this->jpeg_fd_, VIDIOC_STREAMOFF, &jpeg_type) == 0) {
      this->jpeg_capture_streaming_ = false;
    } else {
      ESP_LOGE(TAG, "JPEG CAPTURE STREAMOFF failed: %s", strerror(errno));
      stopped = false;
    }
  }
  if (this->capture_fd_ >= 0 && this->capture_streaming_) {
    int capture_type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    if (ioctl(this->capture_fd_, VIDIOC_STREAMOFF, &capture_type) == 0) {
      this->capture_streaming_ = false;
    } else {
      ESP_LOGE(TAG, "capture STREAMOFF failed: %s", strerror(errno));
      stopped = false;
    }
  }
  this->streaming_ = this->capture_streaming_ ||
                     this->jpeg_output_streaming_ ||
                     this->jpeg_capture_streaming_;
  return stopped && !this->streaming_;
}

bool ESPVideoCamera::start_direct_capture_() {
  // esp_video's DQBUF timeout is a blocking queue wait (and is tested as such
  // upstream). O_NONBLOCK turns an otherwise event-driven capture task into an
  // EAGAIN spin that can starve the real-time audio path.
  this->capture_fd_ = open(this->resolved_device_.c_str(), O_RDWR);
  if (this->capture_fd_ < 0) {
    ESP_LOGE(TAG, "open(%s) failed: %s", this->resolved_device_.c_str(), strerror(errno));
    return false;
  }
  if (!this->configure_capture_format_(V4L2_PIX_FMT_MJPEG))
    return false;
  if (!this->setup_capture_buffers_())
    return false;
  struct timeval dequeue_timeout{2, 0};
  if (ioctl(this->capture_fd_, VIDIOC_S_DQBUF_TIMEOUT, &dequeue_timeout) < 0) {
    ESP_LOGE(TAG, "VIDIOC_S_DQBUF_TIMEOUT failed: %s", strerror(errno));
    return false;
  }
  int type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  if (ioctl(this->capture_fd_, VIDIOC_STREAMON, &type) < 0) {
    ESP_LOGE(TAG, "VIDIOC_STREAMON failed: %s", strerror(errno));
    return false;
  }
  this->capture_streaming_ = true;
  return true;
}

bool ESPVideoCamera::start_jpeg_pipeline_() {
  // Stage 1: sensor/ISP capture device. The H.264 consumer asks for the P4's
  // encoder-native optimized YUV420 layout; the JPEG path keeps RGB565.
  this->capture_fd_ = open(ESP_VIDEO_MIPI_CSI_DEVICE_NAME, O_RDWR);
  if (this->capture_fd_ < 0) {
    ESP_LOGE(TAG, "open(%s) failed: %s", ESP_VIDEO_MIPI_CSI_DEVICE_NAME, strerror(errno));
    return false;
  }
  const uint32_t capture_format =
      this->is_raw_csi_ &&
              this->raw_frame_pixel_format_.load(std::memory_order_acquire) ==
                  RawVideoPixelFormat::YUV420_OUYY_EVYY
          ? V4L2_PIX_FMT_YUV420
          : V4L2_PIX_FMT_RGB565;
  if (!this->configure_capture_format_(capture_format))
    return false;
  this->hardware_framerate_active_.store(
      this->configure_capture_framerate_(), std::memory_order_release);
  if (!this->setup_capture_buffers_())
    return false;
  if (this->is_hw_jpeg_ && !this->setup_rotation_())
    return false;
  struct timeval dequeue_timeout{2, 0};
  if (ioctl(this->capture_fd_, VIDIOC_S_DQBUF_TIMEOUT, &dequeue_timeout) < 0) {
    ESP_LOGE(TAG, "capture VIDIOC_S_DQBUF_TIMEOUT failed: %s",
             strerror(errno));
    return false;
  }
  // Clamp before STREAMON because the IPA worker starts logging immediately.
  clamp_isp_log_levels();
  int ctype = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  if (ioctl(this->capture_fd_, VIDIOC_STREAMON, &ctype) < 0) {
    ESP_LOGE(TAG, "capture STREAMON failed: %s", strerror(errno));
    return false;
  }
  this->capture_streaming_ = true;
  clamp_isp_log_levels();

  if (this->is_raw_csi_)
    return true;

  // Stage 2: JPEG hardware encoder (M2M). Blocking so the per-frame DQBUFs wait
  // for the (fast) hardware encode instead of busy-looping on EAGAIN.
  this->jpeg_fd_ = open(ESP_VIDEO_JPEG_DEVICE_NAME, O_RDWR);
  if (this->jpeg_fd_ < 0) {
    ESP_LOGE(TAG, "open(%s) failed: %s", ESP_VIDEO_JPEG_DEVICE_NAME, strerror(errno));
    return false;
  }
  if (ioctl(this->jpeg_fd_, VIDIOC_S_DQBUF_TIMEOUT, &dequeue_timeout) < 0) {
    ESP_LOGE(TAG, "JPEG VIDIOC_S_DQBUF_TIMEOUT failed: %s",
             strerror(errno));
    return false;
  }

  struct v4l2_format fmt;
  memset(&fmt, 0, sizeof(fmt));
  fmt.type = V4L2_BUF_TYPE_VIDEO_OUTPUT;
  fmt.fmt.pix.width = this->jpeg_width_;
  fmt.fmt.pix.height = this->jpeg_height_;
  fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_RGB565;
  if (ioctl(this->jpeg_fd_, VIDIOC_S_FMT, &fmt) < 0) {
    ESP_LOGE(TAG, "JPEG OUTPUT S_FMT failed: %s", strerror(errno));
    return false;
  }
  memset(&fmt, 0, sizeof(fmt));
  fmt.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  fmt.fmt.pix.pixelformat = V4L2_PIX_FMT_JPEG;
  // esp_video 2.2.0 validates width and height on the JPEG M2M capture side as
  // well. Passing the zeroed 0x0 format used by the original PR returns EINVAL
  // from jpeg_video_set_format during boot.
  fmt.fmt.pix.width = this->jpeg_width_;
  fmt.fmt.pix.height = this->jpeg_height_;
  if (ioctl(this->jpeg_fd_, VIDIOC_S_FMT, &fmt) < 0) {
    ESP_LOGE(TAG, "JPEG CAPTURE S_FMT failed: %s", strerror(errno));
    return false;
  }

  // esp_video implements JPEG quality through the extended-controls ioctl.
  // The legacy VIDIOC_S_CTRL call returns EINVAL and silently leaves the
  // encoder at its much larger default quality (80).
  if (!gate_set_ext_ctrl(this->jpeg_fd_, V4L2_CID_JPEG_COMPRESSION_QUALITY,
                         this->jpeg_quality_, "jpeg_quality"))
    return false;

  struct v4l2_requestbuffers req;
  memset(&req, 0, sizeof(req));
  req.count = MAX_BUFFERS;
  req.type = V4L2_BUF_TYPE_VIDEO_OUTPUT;
  req.memory = V4L2_MEMORY_USERPTR;
  if (ioctl(this->jpeg_fd_, VIDIOC_REQBUFS, &req) < 0) {
    ESP_LOGE(TAG, "JPEG OUTPUT REQBUFS failed: %s", strerror(errno));
    return false;
  }
  memset(&req, 0, sizeof(req));
  req.count = 1;
  req.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  req.memory = V4L2_MEMORY_MMAP;
  if (ioctl(this->jpeg_fd_, VIDIOC_REQBUFS, &req) < 0) {
    ESP_LOGE(TAG, "JPEG CAPTURE REQBUFS failed: %s", strerror(errno));
    return false;
  }

  struct v4l2_buffer buf;
  memset(&buf, 0, sizeof(buf));
  buf.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  buf.memory = V4L2_MEMORY_MMAP;
  buf.index = 0;
  if (ioctl(this->jpeg_fd_, VIDIOC_QUERYBUF, &buf) < 0) {
    ESP_LOGE(TAG, "JPEG QUERYBUF failed: %s", strerror(errno));
    return false;
  }
  this->jpeg_out_buffer_.length = buf.length;
  this->jpeg_out_buffer_.start =
      mmap(nullptr, buf.length, PROT_READ | PROT_WRITE, MAP_SHARED, this->jpeg_fd_, buf.m.offset);
  if (this->jpeg_out_buffer_.start == MAP_FAILED) {
    this->jpeg_out_buffer_.start = nullptr;
    ESP_LOGE(TAG, "JPEG mmap failed: %s", strerror(errno));
    return false;
  }

  int otype = V4L2_BUF_TYPE_VIDEO_OUTPUT;
  if (ioctl(this->jpeg_fd_, VIDIOC_STREAMON, &otype) < 0) {
    ESP_LOGE(TAG, "JPEG OUTPUT STREAMON failed: %s", strerror(errno));
    return false;
  }
  this->jpeg_output_streaming_ = true;
  int jtype = V4L2_BUF_TYPE_VIDEO_CAPTURE;
  if (ioctl(this->jpeg_fd_, VIDIOC_STREAMON, &jtype) < 0) {
    ESP_LOGE(TAG, "JPEG CAPTURE STREAMON failed: %s", strerror(errno));
    return false;
  }
  this->jpeg_capture_streaming_ = true;
  return true;
}

bool ESPVideoCamera::stop_capture_() {
  if (!this->suspend_capture_()) {
    ESP_LOGE(TAG, "Camera teardown deferred: V4L2 queue still active");
    return false;
  }

  if (this->jpeg_fd_ >= 0) {
    if (this->jpeg_out_buffer_.start != nullptr) {
      if (munmap(this->jpeg_out_buffer_.start,
                 this->jpeg_out_buffer_.length) < 0) {
        ESP_LOGE(TAG, "JPEG munmap failed: %s", strerror(errno));
        return false;
      }
      this->jpeg_out_buffer_.start = nullptr;
      this->jpeg_out_buffer_.length = 0;
    }
    struct v4l2_requestbuffers release {};
    release.count = 0;
    release.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    release.memory = V4L2_MEMORY_MMAP;
    if (ioctl(this->jpeg_fd_, VIDIOC_REQBUFS, &release) < 0) {
      ESP_LOGE(TAG, "JPEG CAPTURE REQBUFS(0) failed: %s",
               strerror(errno));
      return false;
    }
    release.type = V4L2_BUF_TYPE_VIDEO_OUTPUT;
    release.memory = V4L2_MEMORY_USERPTR;
    if (ioctl(this->jpeg_fd_, VIDIOC_REQBUFS, &release) < 0) {
      ESP_LOGE(TAG, "JPEG OUTPUT REQBUFS(0) failed: %s",
               strerror(errno));
      return false;
    }
    if (close(this->jpeg_fd_) < 0)
      ESP_LOGE(TAG, "JPEG close failed: %s", strerror(errno));
    this->jpeg_fd_ = -1;
  }

  // Keep every DMA-visible buffer mapped if the driver unexpectedly reports
  // an unfinished blocking transaction. A later idempotent stop can retry.
  if (!this->release_rotation_()) {
    this->streaming_ = false;
    return false;
  }

  if (this->capture_fd_ >= 0) {
    for (int i = 0; i < this->num_capture_buffers_; i++) {
      if (this->capture_buffers_[i].start != nullptr) {
        if (munmap(this->capture_buffers_[i].start,
                   this->capture_buffers_[i].length) < 0) {
          ESP_LOGE(TAG, "capture munmap[%d] failed: %s", i,
                   strerror(errno));
          return false;
        }
        this->capture_buffers_[i].start = nullptr;
        this->capture_buffers_[i].length = 0;
      }
    }
    struct v4l2_requestbuffers release {};
    release.count = 0;
    release.type = V4L2_BUF_TYPE_VIDEO_CAPTURE;
    release.memory = V4L2_MEMORY_MMAP;
    if (ioctl(this->capture_fd_, VIDIOC_REQBUFS, &release) < 0) {
      ESP_LOGE(TAG, "capture REQBUFS(0) failed: %s", strerror(errno));
      return false;
    }
    if (close(this->capture_fd_) < 0)
      ESP_LOGE(TAG, "capture close failed: %s", strerror(errno));
    this->capture_fd_ = -1;
  }
  this->num_capture_buffers_ = 0;
  this->capture_prepared_ = false;
  this->streaming_ = false;
  this->capture_streaming_ = false;
  this->jpeg_output_streaming_ = false;
  this->jpeg_capture_streaming_ = false;
  return true;
}

void ESPVideoCamera::dump_config() {
  ESP_LOGCONFIG(TAG, "ESP-Video Camera:");
  ESP_LOGCONFIG(TAG, "  Name: %s", this->get_name().c_str());
  ESP_LOGCONFIG(TAG, "  Source: %s (%s)", this->device_.c_str(), this->resolved_device_.c_str());
  ESP_LOGCONFIG(TAG, "  Resolution: %s", this->resolution_.c_str());
  if (this->is_hw_jpeg_) {
    ESP_LOGCONFIG(TAG, "  JPEG quality: %d", this->jpeg_quality_);
    ESP_LOGCONFIG(TAG, "  Rotation: %u degrees",
                  (unsigned) this->rotation_degrees_);
  } else if (this->is_raw_csi_) {
    ESP_LOGCONFIG(TAG, "  Output: raw CSI only");
    ESP_LOGCONFIG(TAG, "  Rotation metadata: %u degrees",
                  (unsigned) this->rotation_degrees_);
  }
  ESP_LOGCONFIG(TAG, "  Max framerate: %.1f fps",
                this->max_framerate_.load(std::memory_order_acquire));
  if (this->is_failed()) {
    ESP_LOGCONFIG(TAG, "  State: FAILED");
  }
}

}  // namespace esphome::esp_video_camera

#endif  // USE_ESP_IDF
