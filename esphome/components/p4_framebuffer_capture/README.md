# Optional P4 rendered-frame diagnostic

Normal screenshots use `esp_screenshot` with the LVGL owner. This optional raw
diagnostic is independent and is not enabled by maintained public profiles.

It requires the selected MIPI provider to expose
`copy_frame_buffer_rgb565(uint8_t *, size_t)` and advertise
`FRAMEBUFFER_RGB565_READBACK = True` in its Python display module. The required API is absent from the recorded official commit 373224 baseline.
Configuration fails with an actionable message when the capability is absent;
no private framebuffer getter or local display override is accepted.

Existing component/class IDs, `display_id`, `host`, `port` (default 19090),
`downsample` (1,2,4) and the `P4FB1 width height bytes` wire header are retained.
The driver copies a completed frame on its owner loop into caller-owned
RGB565 little-endian storage in logical display orientation. The diagnostic
never borrows a live framebuffer pointer or manages the driver's cache policy.

The snapshot is allocated once at the full logical frame size. An 800x1280
frame uses 2,048,000 bytes, including with downsample 2/4. Downsampling compacts
that snapshot in place before the existing sender task starts; only network
bytes decrease. Treat this optional allocation separately from normal firmware
and LVGL screenshots. Do not apply a second display rotation to the raw output.

The copy must run on the display owner loop after drawing is complete, with
no external framebuffer producer. An unavailable/busy/failed copy does not
start the network sender. No thread-safety or optical panel timing is claimed.
