#include "runtime_diag.h"

#include <algorithm>
#include <cstdio>
#include <sstream>

#include <esp_heap_caps.h>
#include <esp_memory_utils.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include "esphome/core/application.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"

namespace esphome {
namespace runtime_diag {

static const char *const TAG = "runtime_diag";

namespace {

const char *task_state_to_str(eTaskState state) {
  switch (state) {
    case eRunning:
      return "running";
    case eReady:
      return "ready";
    case eBlocked:
      return "blocked";
    case eSuspended:
      return "suspended";
    case eDeleted:
      return "deleted";
    case eInvalid:
    default:
      return "invalid";
  }
}

const char *task_core_to_str(BaseType_t core, char *buf, size_t len) {
#if configTASKLIST_INCLUDE_COREID
  if (core == tskNO_AFFINITY) {
    return "any";
  }
  snprintf(buf, len, "%ld", static_cast<long>(core));
  return buf;
#else
  return "?";
#endif
}

void append_heap(std::ostringstream &out, const char *name, uint32_t caps) {
  multi_heap_info_t info{};
  heap_caps_get_info(&info, caps);
  out << "\"" << name << "\":{"
      << "\"free\":" << heap_caps_get_free_size(caps)
      << ",\"largest\":" << heap_caps_get_largest_free_block(caps)
      << ",\"min_free\":" << heap_caps_get_minimum_free_size(caps)
      << ",\"allocated\":" << info.total_allocated_bytes
      << ",\"blocks\":" << info.allocated_blocks
      << "}";
}

void append_json_string(std::ostringstream &out, const std::string &value) {
  out << "\"";
  for (char ch : value) {
    switch (ch) {
      case '\\':
        out << "\\\\";
        break;
      case '"':
        out << "\\\"";
        break;
      case '\n':
        out << "\\n";
        break;
      case '\r':
        out << "\\r";
        break;
      case '\t':
        out << "\\t";
        break;
      default:
        out << ch;
        break;
    }
  }
  out << "\"";
}

}  // namespace

void RuntimeDiag::setup() {
  this->last_loop_ms_ = millis();
}

void RuntimeDiag::loop() {
  const uint32_t now = App.get_loop_component_start_time();
  if (this->last_loop_ms_ != 0) {
    this->max_loop_gap_ms_ = std::max<uint32_t>(this->max_loop_gap_ms_, now - this->last_loop_ms_);
  }
  this->last_loop_ms_ = now;
  this->loop_count_++;
  if (this->burst_active_ && static_cast<int32_t>(now - this->burst_next_ms_) >= 0) {
    char reason[96];
    snprintf(reason, sizeof(reason), "%s_%u", this->burst_reason_.c_str(), static_cast<unsigned>(this->burst_index_));
    this->capture(reason);
    this->burst_index_++;
    if (this->burst_remaining_ > 0) {
      this->burst_remaining_--;
    }
    if (this->burst_remaining_ == 0) {
      this->burst_active_ = false;
      ESP_LOGI(TAG, "burst complete reason=%s samples=%u", this->burst_reason_.c_str(),
               static_cast<unsigned>(this->burst_index_));
    } else {
      this->burst_next_ms_ = App.get_loop_component_start_time() + this->burst_interval_ms_;
    }
  }
}

void RuntimeDiag::dump_config() {
  ESP_LOGCONFIG(TAG, "Runtime diagnostics:");
  ESP_LOGCONFIG(TAG, "  Snapshot output: logger only");
}

void RuntimeDiag::mark_api_connected() {
  this->last_api_connected_ms_ = millis();
}

void RuntimeDiag::mark_api_disconnected() {
  this->last_api_disconnected_ms_ = millis();
}

void RuntimeDiag::set_context(const char *key, const char *value) {
  if (key == nullptr || key[0] == '\0')
    return;
  const std::string k(key);
  const std::string v(value == nullptr ? "" : value);
  for (auto &item : this->context_) {
    if (item.first == k) {
      item.second = v;
      return;
    }
  }
  this->context_.emplace_back(k, v);
}

void RuntimeDiag::capture(const char *reason) {
  const char *safe_reason = reason == nullptr ? "" : reason;
  const std::string snapshot = this->build_snapshot_(safe_reason);
  constexpr size_t CHUNK_SIZE = 180;
  const size_t chunks = std::max<size_t>(1, (snapshot.size() + CHUNK_SIZE - 1) / CHUNK_SIZE);
  for (size_t offset = 0, index = 0; offset < snapshot.size(); offset += CHUNK_SIZE, index++) {
    const std::string chunk = snapshot.substr(offset, CHUNK_SIZE);
    ESP_LOGI(TAG, "snapshot[%u/%u] %s", static_cast<unsigned>(index + 1), static_cast<unsigned>(chunks),
             chunk.c_str());
  }
  this->max_loop_gap_ms_ = 0;
  this->loop_count_ = 0;
  this->last_loop_ms_ = millis();
}

void RuntimeDiag::capture_heap(const char *reason) {
  multi_heap_info_t internal{};
  multi_heap_info_t dma{};
  multi_heap_info_t psram{};
  heap_caps_get_info(&internal, MALLOC_CAP_INTERNAL);
  heap_caps_get_info(&dma, MALLOC_CAP_DMA);
  heap_caps_get_info(&psram, MALLOC_CAP_SPIRAM);
  ESP_LOGI(TAG,
           "heap_sample reason=%s internal_free=%u internal_largest=%u internal_min=%u internal_blocks=%u "
           "dma_free=%u dma_largest=%u dma_min=%u dma_blocks=%u "
           "psram_free=%u psram_largest=%u psram_min=%u psram_blocks=%u",
           reason == nullptr ? "" : reason,
           static_cast<unsigned>(internal.total_free_bytes),
           static_cast<unsigned>(internal.largest_free_block),
           static_cast<unsigned>(internal.minimum_free_bytes),
           static_cast<unsigned>(internal.allocated_blocks),
           static_cast<unsigned>(dma.total_free_bytes),
           static_cast<unsigned>(dma.largest_free_block),
           static_cast<unsigned>(dma.minimum_free_bytes),
           static_cast<unsigned>(dma.allocated_blocks),
           static_cast<unsigned>(psram.total_free_bytes),
           static_cast<unsigned>(psram.largest_free_block),
           static_cast<unsigned>(psram.minimum_free_bytes),
           static_cast<unsigned>(psram.allocated_blocks));
}

void RuntimeDiag::start_burst(const char *reason, uint32_t samples, uint32_t interval_ms) {
  this->burst_reason_ = reason == nullptr || reason[0] == '\0' ? "burst" : reason;
  this->burst_remaining_ = std::max<uint32_t>(1, std::min<uint32_t>(samples, 120));
  this->burst_interval_ms_ = std::max<uint32_t>(250, std::min<uint32_t>(interval_ms, 10000));
  this->burst_index_ = 0;
  this->burst_next_ms_ = App.get_loop_component_start_time();
  this->burst_active_ = true;
  ESP_LOGI(TAG, "burst start reason=%s samples=%u interval_ms=%u", this->burst_reason_.c_str(),
           static_cast<unsigned>(this->burst_remaining_), static_cast<unsigned>(this->burst_interval_ms_));
}

void RuntimeDiag::start_heap_trace(const char *reason) {
#if CONFIG_HEAP_TRACING_STANDALONE
  if (this->heap_trace_active_) {
    heap_trace_stop();
    this->heap_trace_active_ = false;
  }
  if (this->heap_trace_records_ != nullptr) {
    heap_trace_init_standalone(nullptr, 0);
    heap_caps_free(this->heap_trace_records_);
    this->heap_trace_records_ = nullptr;
  }
  this->heap_trace_reason_ = reason == nullptr ? "" : reason;
  this->heap_trace_records_ = static_cast<heap_trace_record_t *>(
      heap_caps_calloc(HEAP_TRACE_RECORDS, sizeof(heap_trace_record_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (this->heap_trace_records_ == nullptr) {
    ESP_LOGE(TAG, "heap_trace start failed reason=%s error=psram_allocation", this->heap_trace_reason_.c_str());
    return;
  }
  esp_err_t err = heap_trace_init_standalone(this->heap_trace_records_, HEAP_TRACE_RECORDS);
  if (err == ESP_OK) {
    ESP_LOGI(TAG, "heap_trace start reason=%s capacity=%u", this->heap_trace_reason_.c_str(),
             static_cast<unsigned>(HEAP_TRACE_RECORDS));
    err = heap_trace_start(HEAP_TRACE_LEAKS);
  }
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "heap_trace start failed reason=%s error=%s", this->heap_trace_reason_.c_str(),
             esp_err_to_name(err));
    heap_trace_init_standalone(nullptr, 0);
    heap_caps_free(this->heap_trace_records_);
    this->heap_trace_records_ = nullptr;
    return;
  }
  this->heap_trace_active_ = true;
#else
  ESP_LOGE(TAG, "heap_trace unavailable reason=%s", reason == nullptr ? "" : reason);
#endif
}

void RuntimeDiag::stop_heap_trace(const char *reason) {
#if CONFIG_HEAP_TRACING_STANDALONE
  if (!this->heap_trace_active_) {
    ESP_LOGW(TAG, "heap_trace stop ignored reason=%s active=false", reason == nullptr ? "" : reason);
    return;
  }
  const esp_err_t stop_err = heap_trace_stop();
  this->heap_trace_active_ = false;
  heap_trace_summary_t summary{};
  const esp_err_t summary_err = heap_trace_summary(&summary);
  ESP_LOGI(TAG,
           "heap_trace summary start_reason=%s stop_reason=%s stop_error=%s summary_error=%s records=%u capacity=%u "
           "high_water=%u overflow=%u allocations=%u frees=%u",
           this->heap_trace_reason_.c_str(), reason == nullptr ? "" : reason, esp_err_to_name(stop_err),
           esp_err_to_name(summary_err), static_cast<unsigned>(summary.count), static_cast<unsigned>(summary.capacity),
           static_cast<unsigned>(summary.high_water_mark), static_cast<unsigned>(summary.has_overflowed),
           static_cast<unsigned>(summary.total_allocations), static_cast<unsigned>(summary.total_frees));
  for (size_t i = 0; i < summary.count; i++) {
    heap_trace_record_t record{};
    if (heap_trace_get(i, &record) != ESP_OK || record.address == nullptr || record.freed) {
      continue;
    }
    char callers[192]{};
    size_t used = 0;
    for (size_t frame = 0; frame < CONFIG_HEAP_TRACING_STACK_DEPTH && record.alloced_by[frame] != nullptr; frame++) {
      const int written = snprintf(callers + used, sizeof(callers) - used, "%s%p", frame == 0 ? "" : ":",
                                   record.alloced_by[frame]);
      if (written <= 0 || static_cast<size_t>(written) >= sizeof(callers) - used) {
        break;
      }
      used += static_cast<size_t>(written);
    }
    ESP_LOGI(TAG, "heap_trace record=%u size=%u region=%s address=%p callers=%s", static_cast<unsigned>(i),
             static_cast<unsigned>(record.size), esp_ptr_external_ram(record.address) ? "psram" : "internal",
             record.address, callers);
  }
  heap_trace_init_standalone(nullptr, 0);
  heap_caps_free(this->heap_trace_records_);
  this->heap_trace_records_ = nullptr;
#else
  ESP_LOGE(TAG, "heap_trace unavailable reason=%s", reason == nullptr ? "" : reason);
#endif
}

std::string RuntimeDiag::build_snapshot_(const char *reason) {
  std::ostringstream out;
  out << "{"
      << "\"reason\":";
  append_json_string(out, reason == nullptr ? "" : std::string(reason));
  out << ",\"uptime_ms\":" << millis()
      << ",\"loop\":{\"count\":" << this->loop_count_
      << ",\"max_gap_ms\":" << this->max_loop_gap_ms_ << "}"
      << ",\"api\":{\"last_connected_ms\":" << this->last_api_connected_ms_
      << ",\"last_disconnected_ms\":" << this->last_api_disconnected_ms_ << "}"
      << ",\"context\":{";
  for (size_t i = 0; i < this->context_.size(); i++) {
    if (i != 0)
      out << ",";
    append_json_string(out, this->context_[i].first);
    out << ":";
    append_json_string(out, this->context_[i].second);
  }
  out << "}"
      << ",\"heap\":{";
  append_heap(out, "internal", MALLOC_CAP_INTERNAL);
  out << ",";
  append_heap(out, "dma", MALLOC_CAP_DMA);
#ifdef MALLOC_CAP_SPIRAM
  out << ",";
  append_heap(out, "psram", MALLOC_CAP_SPIRAM);
#endif
  out << "}";

#if configUSE_TRACE_FACILITY
  constexpr UBaseType_t MAX_TASKS = RuntimeDiag::MAX_TASK_SAMPLES;
  TaskStatus_t tasks[MAX_TASKS];
  uint32_t total_runtime = 0;
  const int64_t now_us = esp_timer_get_time();
  const UBaseType_t count = uxTaskGetSystemState(tasks, MAX_TASKS, &total_runtime);
  const bool have_delta = this->have_previous_tasks_ &&
                          total_runtime >= this->previous_total_runtime_ &&
                          now_us > this->previous_task_sample_us_;
  const uint32_t wall_us = have_delta
                               ? static_cast<uint32_t>(std::min<int64_t>(
                                     now_us - this->previous_task_sample_us_,
                                     static_cast<int64_t>(UINT32_MAX)))
                               : 0;
  const uint32_t total_delta = have_delta ? total_runtime - this->previous_total_runtime_ : 0;
  out << ",\"task_window\":{\"wall_us\":" << wall_us
      << ",\"total_runtime_delta\":" << total_delta
      << ",\"first\":" << (have_delta ? "false" : "true") << "}";
  out << ",\"tasks\":[";
  for (UBaseType_t i = 0; i < count; i++) {
    uint32_t previous_runtime = 0;
    bool found_previous = false;
    for (const auto &previous : this->previous_tasks_) {
      if (previous.used && previous.number == tasks[i].xTaskNumber) {
        previous_runtime = previous.runtime;
        found_previous = true;
        break;
      }
    }
    const uint32_t delta = have_delta && found_previous && tasks[i].ulRunTimeCounter >= previous_runtime
                               ? static_cast<uint32_t>(tasks[i].ulRunTimeCounter - previous_runtime)
                               : 0;
    const uint32_t pct_x10 = wall_us > 0
                                 ? static_cast<uint32_t>((static_cast<uint64_t>(delta) * 1000ULL +
                                                          (wall_us / 2)) /
                                                         wall_us)
                                 : 0;
    if (i != 0)
      out << ",";
    char core_buf[16];
    out << "{"
        << "\"name\":";
    append_json_string(out, tasks[i].pcTaskName == nullptr ? "" : std::string(tasks[i].pcTaskName));
    out << ",\"state\":\"" << task_state_to_str(tasks[i].eCurrentState) << "\""
        << ",\"core\":";
#if configTASKLIST_INCLUDE_COREID
    append_json_string(out, task_core_to_str(tasks[i].xCoreID, core_buf, sizeof(core_buf)));
#else
    append_json_string(out, "?");
#endif
    out << ",\"prio\":" << tasks[i].uxCurrentPriority
        << ",\"base_prio\":" << tasks[i].uxBasePriority
        << ",\"stack_hwm\":" << tasks[i].usStackHighWaterMark
        << ",\"runtime\":" << tasks[i].ulRunTimeCounter
        << ",\"delta\":" << delta
        << ",\"pct_x10\":" << pct_x10;
    out << "}";
  }
  out << "]";
  for (auto &previous : this->previous_tasks_) {
    previous = {};
  }
  for (UBaseType_t i = 0; i < count && i < MAX_TASKS; i++) {
    this->previous_tasks_[i].number = tasks[i].xTaskNumber;
    this->previous_tasks_[i].runtime = static_cast<uint32_t>(tasks[i].ulRunTimeCounter);
    this->previous_tasks_[i].used = true;
  }
  this->previous_total_runtime_ = total_runtime;
  this->previous_task_sample_us_ = now_us;
  this->have_previous_tasks_ = true;
#else
  out << ",\"tasks\":\"trace_facility_disabled\"";
#endif
  out << "}";
  return out.str();
}

}  // namespace runtime_diag
}  // namespace esphome
