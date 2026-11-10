# Optional coordinate touch and swipe

Include `packages/diagnostics/touch.yaml` only in firmware that needs this diagnostic. It adds the standard ESPHome API action `touch` with signed 32bit `x` and `y` arguments. Home Assistant exposes the usual node-prefixed action, for example:

```yaml
action: esphome.example_display_touch
data:
  x: 12
  y: 250
```

Coordinates refer to the current logical LVGL display, with origin at the upper left. The component binds the existing `LvglComponent`; use `lvgl_id` explicitly if the configuration has multiple LVGL instances. Bounds follow its current logical resolution. ESPHome hardware rotation is already reflected there. The inverse of LVGL's own display rotation is applied only when that separate rotation is nonzero.

The same package exposes `swipe(x1, y1, x2, y2, duration_ms)`. Both endpoints use those logical coordinates; duration must be between 80 and 2000ms. For example:

```yaml
action: esphome.example_display_swipe
data:
  x1: 120
  y1: 250
  x2: 120
  y2: 80
  duration_ms: 320
```

Choose coordinates from the current screen so the path avoids unintended controls. A swipe uses the same cooperative pointer input and read timer as a tap, interpolating its position until release. It adds no task or input queue.

One event-driven pointer input is allocated at setup. LVGL performs normal hit testing, pressed styling, release and click callbacks. Its existing read timer releases the tap on the next normal input cycle after 80ms, including taps on empty space, then pauses again. There is no direct widget event, phone stop, navigation, new task, stack or network server.

Requests are rejected while another tap/swipe/input, a screen transition or a paused/failed LVGL component is active. A physical input or screen/rotation change during a gesture cancels it through LVGL's normal `PRESS_LOST` path. The simulator does not disable or write another input device. Shutdown cancels an outstanding press. Detach or destruction also deletes the owned input/timer.

Historical measurements of the tap-only version with the P4 compiler and FULL09 headers were 32B for `TapInput` and 44B for the complete component. Those object sizes do not describe the current swipe-capable version, which adds two fixed points and a duration. The same historical measurement found 336B/28B for LVGL input/timer structures, plus two 8B list nodes, totaling 380B requested at setup. Heap metadata/alignment and generated API/automation objects are separate. LVGL keeps its existing PSRAM-preferred allocation policy and normal fallback. The simulator adds no allocation site per tap or swipe, affinity change or RAM/PSRAM setting. Real LVGL drawing, scrolling and callbacks can still allocate normally. The existing host allocation control compares a tap redraw against an independent normal pointer driver; it is not an allocation measurement of every swipe path.

The package is absent from existing default profiles. Without it, code generation emits no component, input or touch/swipe API action. This component enables a diagnostic interaction; it does not itself qualify a hardware call or prove the cause of a watchdog.

Primary contracts: [LVGL 9.5 input processing](https://github.com/lvgl/lvgl/blob/v9.5.0/src/indev/lv_indev.c), [pointer/display APIs](https://github.com/lvgl/lvgl/blob/v9.5.0/src/indev/lv_indev.h), and the installed ESPHome 2026.10.0 `LvglComponent::get_disp()`, API integer mapping and allocator. The implementation uses public LVGL APIs only.
