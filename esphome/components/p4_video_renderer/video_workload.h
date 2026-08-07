#pragma once

#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

#include <cstddef>
#include <cstdint>

namespace esphome::p4_video_workload {

// The P4 encoder, decoder and PPA share PSRAM bandwidth. Serializing complete
// frame workloads prevents bidirectional video from starving realtime audio.
enum class Role : uint8_t { TX = 0, RX = 1 };

class Scheduler {
 public:
  Scheduler() {
    this->lock_ = xSemaphoreCreateMutexStatic(&this->lock_storage_);
    for (size_t index = 0; index < 2; index++)
      this->turn_[index] =
          xSemaphoreCreateBinaryStatic(&this->turn_storage_[index]);
  }

  void acquire(Role role) {
    const size_t index = static_cast<size_t>(role);
    xSemaphoreTake(this->lock_, portMAX_DELAY);
    if (!this->active_) {
      this->active_ = true;
      xSemaphoreGive(this->lock_);
      return;
    }
    this->waiting_[index] = true;
    xSemaphoreGive(this->lock_);
    xSemaphoreTake(this->turn_[index], portMAX_DELAY);
  }

  void release(Role role) {
    const size_t current = static_cast<size_t>(role);
    const size_t opposite = 1U - current;
    xSemaphoreTake(this->lock_, portMAX_DELAY);
    size_t next = 2;
    if (this->waiting_[opposite])
      next = opposite;
    else if (this->waiting_[current])
      next = current;
    if (next < 2)
      this->waiting_[next] = false;
    else
      this->active_ = false;
    xSemaphoreGive(this->lock_);
    if (next < 2)
      xSemaphoreGive(this->turn_[next]);
  }

 protected:
  SemaphoreHandle_t lock_{nullptr};
  StaticSemaphore_t lock_storage_{};
  SemaphoreHandle_t turn_[2]{nullptr, nullptr};
  StaticSemaphore_t turn_storage_[2]{};
  bool waiting_[2]{false, false};
  bool active_{false};
};

inline Scheduler &scheduler() {
  static Scheduler instance;
  return instance;
}

class Guard {
 public:
  explicit Guard(Role role) : role_(role) { scheduler().acquire(role); }
  ~Guard() { scheduler().release(this->role_); }

  Guard(const Guard &) = delete;
  Guard &operator=(const Guard &) = delete;

 protected:
  Role role_;
};

}  // namespace esphome::p4_video_workload
