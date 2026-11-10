# Diagnostics packages

Select only the diagnostics needed by the identified firmware. These packages
reuse existing component owners and the ordinary ESPHome API; they do not create
another SIP/media engine or a temporary capture server.

| Package | Purpose | Boundary |
| --- | --- | --- |
| [`native.yaml`](native.yaml) | Standard diagnostic entities: heap, reset reason, device info, loop timing and Wi-Fi signal | Uses ESPHome's native debug/diagnostic surfaces |
| [`native_no_wifi_signal.yaml`](native_no_wifi_signal.yaml) | Same diagnostics without RSSI polling | Recommended for P4/C6 hosted Wi-Fi, where polling crosses the RPC link |
| [`playback.yaml`](playback.yaml) | Snapshot/watch of the existing announcement path | Observes only; does not start, stop or recover playback |
| [`screenshot.yaml`](screenshot.yaml) | Take screenshot button and native `take_screenshot` action | Configure the standalone capture component/backend separately |
| [`touch.yaml`](touch.yaml) | Optional native `touch(x, y)` action | Sends one normal LVGL coordinate tap; it is an interaction, not passive monitoring |
| [`runtime.yaml`](runtime.yaml) | Native `runtime_event`, `runtime_set_activity` and `runtime_dump` actions | Event meanings belong to the selected YAML/runtime profile |

Home Assistant exposes native actions with the node prefix. Use the existing
button or action for the actual device; a package is not available until its
firmware has been built and uploaded. Keep API encryption and other existing
settings when adding a diagnostic package.

See [Playback diagnostics](../../docs/playback_diagnostics.md),
[ESP Screenshot](../../docs/ESP_SCREENSHOT.md) and the
[coordinate-tap guide](../../esphome/components/lvgl_touch_diagnostic/README.md).
For physical panel claims, distinguish a synchronized framebuffer/LVGL capture
from independently observed panel presentation.

Low-level debug packages live under `packages/debug/`. Periodic runtime dumps
are disabled by default; enable them only for a focused investigation. Preserve
complete report blocks and the running firmware identity before attempting a fix.
