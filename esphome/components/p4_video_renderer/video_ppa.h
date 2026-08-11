#pragma once

#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

namespace esphome::p4_video_ppa {

inline SemaphoreHandle_t mutex() {
  static StaticSemaphore_t storage{};
  static SemaphoreHandle_t handle = xSemaphoreCreateMutexStatic(&storage);
  return handle;
}

class Guard {
 public:
  Guard() { xSemaphoreTake(mutex(), portMAX_DELAY); }
  ~Guard() { xSemaphoreGive(mutex()); }

  Guard(const Guard &) = delete;
  Guard &operator=(const Guard &) = delete;
};

}  // namespace esphome::p4_video_ppa
