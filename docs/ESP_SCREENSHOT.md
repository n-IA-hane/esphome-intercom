# ESP screenshot

`esp_screenshot` is maintained in the standalone
[ESP Screenshot repository](https://github.com/n-IA-hane/esphome-screenshot/tree/dev).
Its [canonical guide](https://github.com/n-IA-hane/esphome-screenshot/blob/dev/README.md)
covers standalone LVGL capture and coherent P4 display readback, HTTP access,
Home Assistant image helpers and resource settings.

Intercom keeps the optional [diagnostic controls](../packages/diagnostics/screenshot.yaml).
Load `esp_screenshot` from its repository and use the display provider declared
by the coordinated configuration.
Use the selected
[coordinated source channel](DEPLOYMENT_GUIDE.md#development-preview-firmware).

Configure `lvgl_id` with the existing LVGL owner. For the compatible P4 MIPI
display, capability metadata automatically selects public
`copy_frame_buffer_rgb565` readback of the driver's composed UI and direct
video. Capture copies the last completed composition under the display owner;
it does not force a redraw that could repaint over video. A busy owner preserves
the previous JPEG and ID. No framebuffer pointer or private getter is exposed.

Other displays, including the normal S3 path, keep complete LVGL-layer capture.
The deprecated `mipi_dsi_id` selector remains accepted and preserves the logical
output orientation. Explicit screenshot rotation is additional and applied
once; it does not repeat the display owner's rotation. Component, renderer and
native action IDs are unchanged.

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

The optional [coordinate touch and swipe](../esphome/components/lvgl_touch_diagnostic/README.md)
provide separate native `touch(x, y)` and `swipe(x1, y1, x2, y2, duration_ms)`
actions that use normal LVGL hit testing and cooperative input processing.
They neither capture pixels nor directly invoke a phone's hangup implementation.
