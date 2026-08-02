#pragma once

#include "esphome/core/defines.h"

#ifdef USE_ESP_IDF

#include "esphome/core/component.h"
#include "esphome/components/camera/camera.h"
#include "esphome/components/i2c/i2c.h"

#include "driver/gpio.h"
#include "driver/ppa.h"

#include <atomic>
#include <memory>
#include <string>
#include <vector>

#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <freertos/semphr.h>

namespace esphome::esp_video_camera {

enum class RawVideoPixelFormat : uint8_t {
  RGB565_LE,
  YUV420_OUYY_EVYY,
};

/// A borrowed native frame produced by the camera's ISP path.
///
/// ESPVideoCamera owns the storage. It remains valid only for the synchronous
/// consume_raw_video_frame() call and must not be retained by the consumer.
/// rotation_degrees describes the configured clockwise display orientation;
/// encoded consumers apply it independently from the camera JPEG transform.
struct RawVideoFrame {
  const uint8_t *data{nullptr};
  size_t size{0};
  RawVideoPixelFormat pixel_format{RawVideoPixelFormat::RGB565_LE};
  uint16_t width{0};
  uint16_t height{0};
  uint16_t stride_bytes{0};
  uint32_t timestamp_90khz{0};
  uint16_t rotation_degrees{0};
};

/// Optional hardware-media tap for the native camera pipeline.
///
/// The consumer is registered once during setup and can independently enable
/// or disable delivery. This keeps capture ownership in ESPVideoCamera while a
/// codec adapter borrows the native ISP frame before camera-specific JPEG
/// rotation/scaling.
class RawVideoFrameConsumer {
 public:
  virtual ~RawVideoFrameConsumer() = default;
  virtual void consume_raw_video_frame(const RawVideoFrame &frame) = 0;
};

/// A borrowed JPEG access unit produced by the camera hardware.
///
/// ESPVideoCamera retains the V4L2 buffer. The payload is valid only for the
/// synchronous consume_jpeg_frame() call, allowing realtime consumers to copy
/// it directly into their own bounded queue without a PSRAM allocation or a
/// trip through the ESPHome main loop.
struct JpegFrame {
  const uint8_t *data{nullptr};
  size_t size{0};
  uint32_t timestamp_90khz{0};
};

class JpegFrameConsumer {
 public:
  virtual ~JpegFrameConsumer() = default;
  virtual void consume_jpeg_frame(const JpegFrame &frame) = 0;
};

/// An owned JPEG/MJPEG frame (copied into PSRAM) shared with the API.
///
/// The data is JPEG-encoded (required by the Home Assistant camera API). It is
/// copied out of the mapped V4L2 buffer so that buffer can be re-queued
/// immediately, while the API streams this copy out over the network.
class ESPVideoCameraImage : public camera::CameraImage {
 public:
  ESPVideoCameraImage(uint8_t *data, size_t length, uint8_t requesters);
  ~ESPVideoCameraImage() override;

  uint8_t *get_data_buffer() override { return this->data_; }
  size_t get_data_length() override { return this->length_; }
  bool was_requested_by(camera::CameraRequester requester) const override;

 protected:
  uint8_t *data_{nullptr};
  size_t length_{0};
  uint8_t requesters_{0};
};

/// Reader used by the API to stream the JPEG bytes out in chunks.
class ESPVideoCameraImageReader : public camera::CameraImageReader {
 public:
  void set_image(std::shared_ptr<camera::CameraImage> image) override;
  size_t available() const override;
  uint8_t *peek_data_buffer() override;
  void consume_data(size_t consumed) override;
  void return_image() override;

 protected:
  std::shared_ptr<camera::CameraImage> image_;
  size_t offset_{0};
};

/// Home Assistant camera backed by Espressif's esp_video (V4L2) pipeline.
///
/// This single component both initialises the camera pipeline (MIPI-CSI, with an
/// optional USB-UVC host) and publishes the stream as a native `camera` entity.
/// It captures JPEG/MJPEG frames from a V4L2 device:
///   - "jpeg": the hardware JPEG encoder (/dev/video10), compatible with every
///     auto-detected MIPI-CSI sensor (SC202CS, OV5647, SC2336, ...).
///   - "uvc":  a USB-UVC camera (/dev/video40+) that streams MJPEG.
///   - "/dev/videoN": an explicit V4L2 path.
class ESPVideoCamera : public camera::Camera {
 public:
  void setup() override;
  void on_shutdown() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::DATA; }

  // Pipeline configuration -----------------------------------------------------
  void set_i2c_bus(i2c::I2CBus *bus) { this->i2c_bus_ = bus; }
  void set_xclk_pin(gpio_num_t pin) { this->xclk_pin_ = pin; }
  void set_xclk_freq(uint32_t freq) { this->xclk_freq_ = freq; }
  void set_enable_xclk_init(bool enable) { this->enable_xclk_init_ = enable; }
  void set_enable_uvc(bool enable) { this->enable_uvc_ = enable; }

  // Camera platform configuration ----------------------------------------------
  void set_device(const std::string &device) { this->device_ = device; }
  void set_resolution(const std::string &resolution) { this->resolution_ = resolution; }
  void set_jpeg_quality(int quality) { this->jpeg_quality_ = quality; }
  void set_rotation(uint16_t rotation) { this->rotation_degrees_ = rotation; }
  void set_max_framerate(float fps) {
    this->max_framerate_.store(fps, std::memory_order_release);
    this->min_interval_ms_.store(
        (fps > 0.0f) ? (uint32_t) (1000.0f / fps) : 0,
        std::memory_order_release);
  }

  // Runtime settings from HA/web are stored atomically and applied by the
  // capture task on live descriptors. Never issue these ioctls from another
  // thread. A value of -1 leaves the driver default/automatic mode unchanged.
  void set_runtime_exposure(int v) {
    this->rt_exposure_.store(v);
    this->ctrls_dirty_.store(true);
  }
  void set_runtime_vflip(bool v) {
    this->rt_vflip_.store(v ? 1 : 0);
    this->ctrls_dirty_.store(true);
  }
  void set_runtime_hflip(bool v) {
    this->rt_hflip_.store(v ? 1 : 0);
    this->ctrls_dirty_.store(true);
  }
  void set_runtime_jpeg_quality(int q) {
    this->rt_quality_.store(q);
    this->ctrls_dirty_.store(true);
  }
  void set_runtime_max_fps(float fps) {
    this->max_framerate_.store(fps, std::memory_order_release);
    this->min_interval_ms_.store(
        (fps > 0.0f) ? (uint32_t) (1000.0f / fps) : 0,
        std::memory_order_release);
    // Runtime changes cannot reprogram an already-streaming CSI device here.
    // Use the frame-driven gate for the current session; resume_capture_()
    // reapplies S_PARM before the next STREAMON without reallocating buffers.
    this->hardware_framerate_active_.store(false,
                                           std::memory_order_release);
  }

  // camera::Camera -------------------------------------------------------------
  void add_listener(camera::CameraListener *listener) override { this->listeners_.push_back(listener); }
  camera::CameraImageReader *create_image_reader() override;
  void request_image(camera::CameraRequester requester) override;
  void start_stream(camera::CameraRequester requester) override;
  void stop_stream(camera::CameraRequester requester) override;

  bool register_raw_frame_consumer(RawVideoFrameConsumer *consumer,
                                   RawVideoPixelFormat pixel_format);
  bool start_raw_frame_consumer(RawVideoFrameConsumer *consumer);
  void stop_raw_frame_consumer(RawVideoFrameConsumer *consumer);
  bool register_jpeg_frame_consumer(JpegFrameConsumer *consumer);
  bool start_jpeg_frame_consumer(JpegFrameConsumer *consumer);
  void stop_jpeg_frame_consumer(JpegFrameConsumer *consumer);

 protected:
  bool init_pipeline_();
  bool start_capture_();
  bool resume_capture_();
  bool suspend_capture_();
  bool stop_capture_();
  void update_capture_state_();
  void schedule_capture_retry_();
  bool has_consumers_() const;

  // Capture runs in a dedicated FreeRTOS task because blocking DQBUF calls
  // previously stalled loopTask for more than five seconds and tripped
  // task_wdt. As in core esp32_camera, the task puts a completed JPEG into a
  // mutex-protected pending slot and loop() alone notifies API listeners.
  static void capture_task_trampoline(void *param);
  void capture_task_run_();
  // Copy a completed JPEG to PSRAM and publish it to the pending slot.
  void queue_frame_(const uint8_t *data, size_t length);
  // Deliver from loop(); CameraImage takes ownership of the allocation.
  void deliver_frame_owned_(uint8_t *data, size_t length,
                            uint8_t requesters);
  // Apply rt_* settings to live descriptors from the capture task only.
  void apply_runtime_ctrls_();
  bool configure_capture_format_(uint32_t pixelformat);
  bool configure_capture_framerate_();
  bool setup_capture_buffers_();
  bool setup_rotation_();
  bool release_rotation_();
  // Hardware-JPEG path: capture RGB565 (sensor/ISP) -> JPEG M2M encoder.
  bool start_jpeg_pipeline_();
  void loop_jpeg_pipeline_();
  void deliver_raw_frame_(const uint8_t *data, size_t size,
                          RawVideoPixelFormat pixel_format, uint16_t width,
                          uint16_t height, uint16_t stride_bytes,
                          uint32_t timestamp_90khz,
                          uint16_t rotation_degrees);
  void deliver_jpeg_frame_(const uint8_t *data, size_t size,
                           uint32_t timestamp_90khz);
  // Direct path: a source that already delivers JPEG/MJPEG (USB-UVC / device).
  bool start_direct_capture_();
  void loop_direct_capture_();

  // Pipeline
  i2c::I2CBus *i2c_bus_{nullptr};
  gpio_num_t xclk_pin_{GPIO_NUM_36};
  uint32_t xclk_freq_{24000000};
  bool enable_xclk_init_{false};
  bool enable_uvc_{false};
  bool pipeline_ready_{false};

  // Camera platform
  std::string device_{"jpeg"};
  std::string resolved_device_;
  bool is_hw_jpeg_{false};
  bool is_raw_csi_{false};
  std::string resolution_{"auto"};
  int jpeg_quality_{10};
  uint16_t rotation_degrees_{0};
  std::atomic<float> max_framerate_{10.0f};
  std::atomic<uint32_t> min_interval_ms_{100};
  uint32_t native_capture_fps_{0};
  uint32_t last_frame_ms_{0};
  std::atomic<bool> hardware_framerate_active_{false};

  // Consumers (bit masks indexed by camera::CameraRequester)
  std::vector<camera::CameraListener *> listeners_;
  std::shared_ptr<ESPVideoCameraImage> current_image_;
  // Camera API callbacks run on the ESPHome loop while the capture task reads
  // these masks to decide whether hardware JPEG output is needed. Keep the
  // cross-task ownership explicit instead of relying on byte-sized accesses
  // being incidentally atomic on the P4.
  std::atomic<uint8_t> stream_requesters_{0};
  std::atomic<uint8_t> single_requesters_{0};
  std::atomic<RawVideoFrameConsumer *> raw_frame_consumer_{nullptr};
  std::atomic<RawVideoPixelFormat> raw_frame_pixel_format_{
      RawVideoPixelFormat::RGB565_LE};
  std::atomic<bool> raw_frame_consumer_active_{false};
  std::atomic<JpegFrameConsumer *> jpeg_frame_consumer_{nullptr};
  std::atomic<bool> jpeg_frame_consumer_active_{false};
#ifdef USE_ESPHOME_VOIP_STACK_VIDEO_DEBUG
  std::atomic<uint32_t> jpeg_debug_generation_{0};
  uint32_t jpeg_debug_observed_generation_{0};
  uint32_t jpeg_debug_frames_{0};
  uint64_t jpeg_debug_ppa_total_us_{0};
  uint64_t jpeg_debug_encode_total_us_{0};
  uint32_t jpeg_debug_ppa_max_us_{0};
  uint32_t jpeg_debug_encode_max_us_{0};
  uint32_t jpeg_debug_last_log_ms_{0};
#endif

  // V4L2 state.
  //
  // A direct source (USB-UVC, or an explicit /dev/videoN already producing
  // JPEG/MJPEG) only uses capture_fd_ + capture_buffers_.
  //
  // The hardware-JPEG source spans two devices: capture_fd_ is the MIPI-CSI/ISP
  // device producing RGB565 frames, jpeg_fd_ is the JPEG hardware encoder (an
  // M2M device) fed RGB565 on its OUTPUT queue and read as JPEG from its CAPTURE
  // queue (jpeg_out_buffer_).
  int capture_fd_{-1};
  int jpeg_fd_{-1};
  bool streaming_{false};
  // Track each V4L2 queue independently. A failed STREAMOFF must retain every
  // DMA-visible mapping until a later bounded recovery attempt confirms that
  // all queues stopped.
  bool capture_streaming_{false};
  bool jpeg_output_streaming_{false};
  bool jpeg_capture_streaming_{false};
  // V4L2 queue memory is prepared once and reused with STREAMON/STREAMOFF.
  // This follows Espressif's own repeated-stream test and lets the capture
  // task sleep indefinitely without freeing/reallocating large DMA extents.
  bool capture_prepared_{false};
  std::atomic<bool> capture_faulted_{false};
  uint32_t capture_width_{0};
  uint32_t capture_height_{0};
  uint32_t capture_stride_bytes_{0};
  size_t capture_frame_bytes_{0};
  RawVideoPixelFormat capture_pixel_format_{
      RawVideoPixelFormat::RGB565_LE};
  uint32_t jpeg_width_{0};
  uint32_t jpeg_height_{0};
  static constexpr int MAX_BUFFERS = 3;
  struct MappedBuffer {
    void *start{nullptr};
    size_t length{0};
  };
  MappedBuffer capture_buffers_[MAX_BUFFERS];
  int num_capture_buffers_{0};
  MappedBuffer jpeg_out_buffer_;

  // Optional producer-side PPA transform for the hardware JPEG path. The same
  // SRM transaction performs rotation and, when the sensor coerces a requested
  // smaller resolution upward, downscaling. A single RGB565 buffer is
  // sufficient because the capture task waits for JPEG OUTPUT DQBUF before
  // submitting the next frame.
  ppa_client_handle_t ppa_srm_{nullptr};
  bool ppa_transform_required_{false};
  float ppa_scale_x_{1.0f};
  float ppa_scale_y_{1.0f};
  uint8_t *rotated_rgb565_{nullptr};
  size_t rotated_rgb565_bytes_{0};
  size_t rotated_rgb565_alloc_size_{0};
  // The P4 display/video stack owns several large PSRAM blocks. Reallocating
  // this contiguous DMA buffer for every short camera session eventually
  // fragments PSRAM even when the total free size remains ample. Retain the
  // largest successful allocation for the component lifetime and only
  // register/unregister the PPA client per capture session.
  size_t rotated_rgb565_capacity_{0};

  // Capture task and its single-slot handoff to loop().
  TaskHandle_t capture_task_{nullptr};
  std::atomic<bool> capture_task_running_{false};
  SemaphoreHandle_t capture_task_done_{nullptr};
  StaticSemaphore_t capture_task_done_storage_{};
  SemaphoreHandle_t frame_mutex_{nullptr};
  // Ownership passes to CameraImage when loop() removes this pointer. That
  // contract prevents safe reuse without changing ESPHome's camera API.
  uint8_t *pending_jpeg_{nullptr};
  size_t pending_jpeg_len_{0};
  uint8_t pending_requesters_{0};
  std::atomic<bool> capture_wanted_{false};
  std::atomic<bool> capture_retry_requested_{false};
  std::atomic<uint8_t> capture_retry_attempts_{0};
  // Main-loop-owned guard for the one-shot linger timeout. Ready frames may
  // wake loop() while the camera is lingering; they must not postpone the
  // original shutdown deadline by re-arming the timeout every frame.
  bool capture_linger_armed_{false};
  bool capture_retry_armed_{false};
  uint32_t last_alloc_warning_ms_{0};

  // Espressif's V4L2 examples dequeue and requeue the first two CSI buffers
  // after every STREAMON. Apply that event-driven warmup to the hardware JPEG
  // path so its first published frame already has stable ISP color.
  uint8_t startup_frames_remaining_{0};  // capture task only
  static constexpr uint8_t STARTUP_FRAME_COUNT = 2;
  // Preserve the qualified time gate for the independent raw H.264 path.
  uint32_t raw_warmup_until_ms_{0};  // capture task only
  static constexpr uint32_t RAW_WARMUP_MS = 250;
  // Linger keeps capture warm for five seconds after the last request so a
  // short burst of related events avoids pipeline churn.
  static constexpr uint32_t LINGER_MS = 5000;
  static constexpr uint32_t CAPTURE_RETRY_MS = 1000;
  static constexpr uint8_t MAX_CAPTURE_RETRIES = 3;
  static constexpr uint32_t CAPTURE_STOP_TIMEOUT_MS = 3000;

  // Runtime HA/web controls; -1 means unspecified.
  std::atomic<int> rt_exposure_{-1};  // V4L2_CID_EXPOSURE, 2-235 on OV5647
  std::atomic<int> rt_vflip_{-1};    // V4L2_CID_VFLIP 0/1
  std::atomic<int> rt_hflip_{-1};    // V4L2_CID_HFLIP 0/1
  std::atomic<int> rt_quality_{-1};  // V4L2_CID_JPEG_COMPRESSION_QUALITY 1-63
  std::atomic<bool> ctrls_dirty_{false};
};

}  // namespace esphome::esp_video_camera

#endif  // USE_ESP_IDF
