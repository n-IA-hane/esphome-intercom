#pragma once
namespace esphome::testing { template<typename... Ts> void log(Ts...) {} }
#define ESP_LOGE(...) esphome::testing::log(__VA_ARGS__)
#define ESP_LOGW(...) esphome::testing::log(__VA_ARGS__)
#define ESP_LOGCONFIG(...) esphome::testing::log(__VA_ARGS__)
