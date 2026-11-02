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
#endif
