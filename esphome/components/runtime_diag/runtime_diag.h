#pragma once

#include <string>
#include <vector>

#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#if CONFIG_HEAP_TRACING_STANDALONE
#include <esp_heap_trace.h>
#endif

#include "esphome/core/component.h"

namespace esphome {
namespace runtime_diag {

class RuntimeDiag : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;

  void mark_api_connected();
  void mark_api_disconnected();
  void set_context(const char *key, const char *value);
  void capture(const char *reason);
  void capture_heap(const char *reason);
  void start_burst(const char *reason, uint32_t samples, uint32_t interval_ms);
  void start_heap_trace(const char *reason);
  void stop_heap_trace(const char *reason);

 protected:
  std::string build_snapshot_(const char *reason);

  struct TaskSample {
    // TaskStatus_t::xTaskNumber is UBaseType_t in current ESP-IDF/FreeRTOS.
    // Using the public field type keeps the diagnostic component compatible
    // with IDF releases that do not expose the private TaskNumber_t alias.
    UBaseType_t number{0};
    uint32_t runtime{0};
    bool used{false};
  };

  static constexpr size_t MAX_TASK_SAMPLES = 48;

  uint32_t last_loop_ms_{0};
  uint32_t max_loop_gap_ms_{0};
  uint32_t loop_count_{0};
  uint32_t last_api_connected_ms_{0};
  uint32_t last_api_disconnected_ms_{0};
  bool burst_active_{false};
  uint32_t burst_remaining_{0};
  uint32_t burst_interval_ms_{1000};
  uint32_t burst_next_ms_{0};
  uint32_t burst_index_{0};
  std::string burst_reason_;
  TaskSample previous_tasks_[MAX_TASK_SAMPLES]{};
  uint32_t previous_total_runtime_{0};
  int64_t previous_task_sample_us_{0};
  bool have_previous_tasks_{false};
  std::vector<std::pair<std::string, std::string>> context_;

#if CONFIG_HEAP_TRACING_STANDALONE
  static constexpr size_t HEAP_TRACE_RECORDS = 2048;
  heap_trace_record_t *heap_trace_records_{nullptr};
  bool heap_trace_active_{false};
  std::string heap_trace_reason_;
#endif
};

}  // namespace runtime_diag
}  // namespace esphome
