#pragma once

// Host-only input data for the unchanged production status formatter.
// These are connection booleans, not network clients or servers.
#ifdef USE_HOST
namespace esphome::api {
struct StatusApiInput {
  bool connected{true};
  bool is_connected() const { return this->connected; }
};
inline StatusApiInput status_api_input;
inline StatusApiInput *global_api_server = &status_api_input;
}
namespace esphome::wifi {
struct StatusWifiInput {
  int wifi_rssi() const { return -40; }
};
inline StatusWifiInput status_wifi_input;
inline StatusWifiInput *global_wifi_component = &status_wifi_input;
}
#endif
