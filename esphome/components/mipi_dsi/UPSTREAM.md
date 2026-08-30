# ESPHome MIPI DSI fork upstream record

Upstream baseline: ESPHome `dev` commit
`cd28a8a03e1fd00cde1e94e65ad089f3823ef274`, component path
`esphome/components/mipi_dsi`.

The local fork preserves the validated P4 panel models and initialization,
exposes the immutable framebuffer to the narrow video adapter, and serializes
display submissions so LVGL and direct video cannot consume each other's DMA
completion event. Optional video diagnostics remain compile-time gated.

Checked with:

```bash
git diff --no-index ../esphome-pr-work/esphome/components/mipi_dsi esphome/components/mipi_dsi
```

Re-run the comparison after every ESPHome update and keep unrelated upstream
model definitions intact.
