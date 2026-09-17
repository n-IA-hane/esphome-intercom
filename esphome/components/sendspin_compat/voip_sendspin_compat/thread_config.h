// Copyright 2026 Sendspin Contributors
// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <cstddef>

#ifdef ESP_PLATFORM
#include <esp_heap_caps.h>
#include <esp_pthread.h>

namespace sendspin {

// The pthread configuration belongs to the creating task. Keep the requested
// worker settings scoped to its creation rather than leaking them to siblings.
class PlatformThreadConfig {
public:
    PlatformThreadConfig(const char* name, size_t stack_size, int priority, bool stack_in_psram) {
        if (esp_pthread_get_cfg(&this->previous_) != ESP_OK) {
            this->previous_ = esp_pthread_get_default_config();
        }
        esp_pthread_cfg_t cfg = esp_pthread_get_default_config();
        cfg.stack_size = stack_size;
        cfg.prio = priority;
        cfg.thread_name = name;
        if (stack_in_psram) {
            cfg.stack_alloc_caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;
        }
        this->valid_ = esp_pthread_set_cfg(&cfg) == ESP_OK;
    }

    ~PlatformThreadConfig() {
        if (this->valid_) {
            esp_pthread_set_cfg(&this->previous_);
        }
    }

    PlatformThreadConfig(const PlatformThreadConfig&) = delete;
    PlatformThreadConfig& operator=(const PlatformThreadConfig&) = delete;
    bool valid() const { return this->valid_; }

private:
    esp_pthread_cfg_t previous_{};
    bool valid_{false};
};

}  // namespace sendspin
#else
namespace sendspin {
class PlatformThreadConfig {
public:
    PlatformThreadConfig(const char*, size_t, int, bool) {}
    bool valid() const { return true; }
};
}  // namespace sendspin
#endif
