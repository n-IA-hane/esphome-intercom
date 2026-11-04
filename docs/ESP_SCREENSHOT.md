# ESP screenshot

`esp_screenshot` is maintained in the standalone
[ESP Screenshot repository](https://github.com/n-IA-hane/esphome-screenshot/tree/dev).
Its [canonical guide](https://github.com/n-IA-hane/esphome-screenshot/blob/dev/README.md)
covers standalone LVGL and P4 MIPI DSI configurations, HTTP capture,
Home Assistant image helpers and resource settings.

Intercom keeps the optional [diagnostic controls](../packages/diagnostics/screenshot.yaml)
and its [maintained MIPI DSI provider](../esphome/components/mipi_dsi/UPSTREAM.md).
For P4 snapshots, load `esp_screenshot` from its repository and `mipi_dsi`
from Intercom as separate external component sources. Use the selected
[coordinated source channel](DEPLOYMENT_GUIDE.md#development-preview-firmware).

Choose exactly one existing backend: `lvgl_id` captures LVGL layers, while
`mipi_dsi_id` copies the synchronized P4 driver framebuffer, including direct
video. The latter is not a readback of the physical panel. For the maintained
P4 FULL landscape UI, screenshot `rotation: 90` makes the native portrait buffer
upright without changing display, touch or video settings.

After including the diagnostic package, use **Take screenshot** or the ordinary
node-prefixed `esphome.<node>_take_screenshot` action. A successful capture
changes **Screenshot ID**; **Busy** or an error preserves the previous image.
Read the JPEG through the component's existing
`/esp_screenshot/screen/image.jpg` endpoint. The canonical guide shows HA's
native Template Image/Picture Glance setup and authenticated HTTP alternative.
No custom HA screenshot integration or separate image server is required.

Resource choices remain on `esp_screenshot`: `max_jpeg_size`,
`buffer_in_psram`, `task_stack_in_psram` and `preallocate_buffers`. The default
preallocation reserves a raw RGB565 image plus two bounded JPEG slots.
`preallocate_buffers: false` admits image buffers on demand and can refuse a
capture when memory is insufficient, preserving the last successful image.
Use the canonical resource formulas for the actual screen size rather than
copying a small S3 buffer budget to an 800x1280 P4.

The optional [coordinate tap](../esphome/components/lvgl_touch_diagnostic/README.md)
is a separate native `touch(x, y)` action that uses normal LVGL hit testing.
It neither captures pixels nor directly invokes a phone's hangup implementation.
