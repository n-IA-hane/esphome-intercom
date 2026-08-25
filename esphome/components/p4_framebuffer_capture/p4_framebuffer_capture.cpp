#include "p4_framebuffer_capture.h"

#include "esphome/core/log.h"

#include "esp_heap_caps.h"

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <lwip/inet.h>
#include <lwip/sockets.h>

#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

namespace esphome::p4_framebuffer_capture {

static const char *const TAG = "p4_framebuffer_capture";

static bool send_all(int socket, const uint8_t *data, size_t size) {
  while (size != 0) {
    const ssize_t sent = ::send(socket, data, size, 0);
    if (sent <= 0)
      return false;
    data += sent;
    size -= static_cast<size_t>(sent);
  }
  return true;
}

void P4FramebufferCapture::setup() {
  if (this->display_ == nullptr || this->display_->get_frame_buffer() == nullptr) {
    ESP_LOGE(TAG, "Display framebuffer is unavailable during setup");
    this->mark_failed();
    return;
  }
  this->snapshot_capacity_ = this->display_->get_frame_buffer_size();
  this->snapshot_ = static_cast<uint8_t *>(
      heap_caps_malloc(this->snapshot_capacity_, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (this->snapshot_ == nullptr) {
    ESP_LOGE(TAG, "Unable to preallocate %u-byte framebuffer snapshot",
             static_cast<unsigned>(this->snapshot_capacity_));
    this->mark_failed();
    return;
  }
  ESP_LOGI(TAG, "Preallocated %u-byte framebuffer snapshot",
           static_cast<unsigned>(this->snapshot_capacity_));
}

void P4FramebufferCapture::capture() {
  if (this->capture_active_.exchange(true)) {
    ESP_LOGW(TAG, "Framebuffer capture is already active");
    return;
  }
  if (this->display_ == nullptr ||
      this->display_->get_frame_buffer() == nullptr) {
    this->capture_active_.store(false);
    ESP_LOGE(TAG, "Display framebuffer is unavailable");
    return;
  }
  this->snapshot_width_ = this->display_->get_width_internal();
  this->snapshot_height_ = this->display_->get_height_internal();
  this->snapshot_size_ = this->display_->get_frame_buffer_size();
  if (this->snapshot_ == nullptr || this->snapshot_size_ > this->snapshot_capacity_) {
    this->capture_active_.store(false);
    ESP_LOGE(TAG, "Preallocated framebuffer snapshot is unavailable or too small");
    return;
  }
  // Button automations run on the ESPHome loop task. Copy here, before the
  // network task starts, so the capture is one coherent LVGL frame rather
  // than a 15-second mixture of later redraws.
  std::memcpy(this->snapshot_, this->display_->get_frame_buffer(),
              this->snapshot_size_);
  if (xTaskCreate(capture_task, "p4_fb_capture", 4096, this, 1, nullptr) !=
      pdPASS) {
    this->capture_active_.store(false);
    ESP_LOGE(TAG, "Unable to start framebuffer capture task");
  }
}

void P4FramebufferCapture::capture_task(void *parameter) {
  auto *capture = static_cast<P4FramebufferCapture *>(parameter);
  capture->capture_sync_();
  capture->capture_active_.store(false);
  vTaskDelete(nullptr);
}

void P4FramebufferCapture::capture_sync_() {
  if (this->snapshot_ == nullptr || this->snapshot_size_ == 0) {
    ESP_LOGE(TAG, "Framebuffer snapshot is unavailable");
    return;
  }
  const int socket = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (socket < 0) {
    ESP_LOGE(TAG, "Unable to create capture socket: errno=%d", errno);
    return;
  }
  timeval timeout{.tv_sec = 5, .tv_usec = 0};
  ::setsockopt(socket, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
  sockaddr_in destination{};
  destination.sin_family = AF_INET;
  destination.sin_port = htons(this->port_);
  if (::inet_pton(AF_INET, this->host_.c_str(), &destination.sin_addr) != 1 ||
      ::connect(socket, reinterpret_cast<sockaddr *>(&destination), sizeof(destination)) != 0) {
    ESP_LOGE(TAG, "Unable to connect capture receiver: errno=%d", errno);
    ::close(socket);
    return;
  }
  const int width = this->snapshot_width_;
  const int height = this->snapshot_height_;
  const size_t size = this->snapshot_size_;
  char header[64];
  const int header_size = std::snprintf(header, sizeof(header), "P4FB1 %d %d %u\n", width, height,
                                        static_cast<unsigned>(size));
  const bool sent = header_size > 0 &&
                    send_all(socket, reinterpret_cast<const uint8_t *>(header), header_size) &&
                    send_all(socket, this->snapshot_, size);
  ::shutdown(socket, SHUT_RDWR);
  ::close(socket);
  if (sent)
    ESP_LOGI(TAG, "Captured %ux%u framebuffer (%u bytes)", width, height, static_cast<unsigned>(size));
  else
    ESP_LOGE(TAG, "Framebuffer transfer failed: errno=%d", errno);
}

}  // namespace esphome::p4_framebuffer_capture
