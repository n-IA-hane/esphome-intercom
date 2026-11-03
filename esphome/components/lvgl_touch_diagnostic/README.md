# Optional coordinate tap

Include `packages/diagnostics/touch.yaml` only in firmware that needs this diagnostic. It adds the standard ESPHome API action `touch` with signed 32bit `x` and `y` arguments. Home Assistant exposes the usual node-prefixed action, for example:

```yaml
action: esphome.example_display_touch
data:
  x: 12
  y: 250
```

Coordinates refer to the current logical LVGL display, with origin at the upper left. The component binds the existing `LvglComponent`; use `lvgl_id` explicitly if the configuration has multiple LVGL instances. Bounds follow its current logical resolution. ESPHome hardware rotation is already reflected there. The inverse of LVGL's own display rotation is applied only when that separate rotation is nonzero.

One event-driven pointer input is allocated at setup. LVGL performs normal hit testing, pressed styling, release and click callbacks. Its existing read timer releases the tap on the next normal input cycle after 80ms, including taps on empty space, then pauses again. There is no direct widget event, phone stop, navigation, new task, stack or network server.

Requests are rejected while another tap/input, a screen transition or a paused/failed LVGL component is active. A physical input or screen/rotation change during a tap cancels it through LVGL's normal `PRESS_LOST` path. The simulator does not disable or write another input device. Shutdown cancels an outstanding press. Detach or destruction also deletes the owned input/timer.

With the frozen P4 compiler and real FULL09 headers, `TapInput` is 32B and the complete ESPHome component 44B. LVGL input/timer structures are 336B/28B, plus two 8B list nodes: 380B requested at setup. Heap metadata/alignment and standard generated API/automation objects are separate. LVGL keeps its existing PSRAM-preferred allocation policy and normal fallback. No per-tap simulator allocation, affinity or RAM/PSRAM setting is added. Real LVGL drawing and callbacks can still allocate normally. The host control compares the same redraw against an independent normal pointer driver.

The package is absent from existing default profiles. Without it, code generation emits no component, input or API action. This component enables a diagnostic interaction; it does not itself qualify a hardware call or prove the cause of a watchdog.

Primary contracts: [LVGL 9.5 input processing](https://github.com/lvgl/lvgl/blob/v9.5.0/src/indev/lv_indev.c), [pointer/display APIs](https://github.com/lvgl/lvgl/blob/v9.5.0/src/indev/lv_indev.h), and the installed ESPHome 2026.10.0 `LvglComponent::get_disp()`, API integer mapping and allocator. The implementation uses public LVGL APIs only.
