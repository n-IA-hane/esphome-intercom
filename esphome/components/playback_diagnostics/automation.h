#pragma once
#include "esphome/core/automation.h"
#include "playback_diagnostics.h"

namespace esphome::playback_diagnostics {
template<typename... Ts> class DumpAction : public Action<Ts...>, public Parented<PlaybackDiagnostics> {
 public:
  TEMPLATABLE_VALUE(std::string, reason)
  void play(const Ts &...x) override { this->parent_->dump(this->reason_.value(x...)); }
};
template<typename... Ts> class WatchAction : public Action<Ts...>, public Parented<PlaybackDiagnostics> {
 public:
  TEMPLATABLE_VALUE(uint32_t, timeout)
  void play(const Ts &...x) override { this->parent_->watch_next_announcement(this->timeout_.value(x...)); }
};
template<typename... Ts> class CancelWatchAction : public Action<Ts...>, public Parented<PlaybackDiagnostics> {
 public:
  void play(const Ts &...x) override { this->parent_->cancel_watch(); }
};
}  // namespace esphome::playback_diagnostics
