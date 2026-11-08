#pragma once

#include <cstdint>
#include <vector>

namespace esphome::micro_wake_word::compat {

// Both callback APIs lend PCM only for the callback duration. Never make a copy.
inline const std::vector<uint8_t> &audio_bytes(const std::vector<uint8_t> &data) { return data; }
template<typename Capture> auto audio_bytes(const Capture &capture) -> decltype((capture.data)) {
  return capture.data;
}

// The old RingBufferAudioSource hides the one-argument base overload; the new
// owner exposes fill(ticks) and keeps the two-argument form only as deprecated.
template<typename Source, typename Timeout> auto fill(Source &source, Timeout ticks, int) -> decltype(source.fill(ticks)) {
  return source.fill(ticks);
}
template<typename Source, typename Timeout>
auto fill(Source &source, Timeout ticks, long) -> decltype(source.fill(ticks, false)) {
  return source.fill(ticks, false);
}

template<typename Component>
auto momentary_error(Component &component, const char *, uint32_t length, int)
    -> decltype(component.status_momentary_error(length)) {
  return component.status_momentary_error(length);
}
template<typename Component>
auto momentary_error(Component &component, const char *name, uint32_t length, long)
    -> decltype(component.status_momentary_error(name, length)) {
  return component.status_momentary_error(name, length);
}

}  // namespace esphome::micro_wake_word::compat
