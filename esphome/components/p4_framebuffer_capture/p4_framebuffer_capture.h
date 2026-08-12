#pragma once

#include "esphome/components/mipi_dsi/mipi_dsi.h"
#include "esphome/core/component.h"

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <string>

namespace esphome::p4_framebuffer_capture {

class P4FramebufferCapture : public Component {
 public:
  void set_display(mipi_dsi::MipiDsi *display) { this->display_ = display; }
  void set_host(const std::string &host) { this->host_ = host; }
  void set_port(uint16_t port) { this->port_ = port; }
  void capture();

 protected:
  static void capture_task(void *parameter);
  void capture_sync_();

  mipi_dsi::MipiDsi *display_{nullptr};
  std::string host_;
  uint16_t port_{19090};
  std::atomic_bool capture_active_{false};
  uint8_t *snapshot_{nullptr};
  size_t snapshot_size_{0};
  int snapshot_width_{0};
  int snapshot_height_{0};
};

}  // namespace esphome::p4_framebuffer_capture
