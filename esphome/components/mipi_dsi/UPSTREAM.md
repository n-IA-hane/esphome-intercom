# ESPHome MIPI DSI fork upstream record

Upstream baseline: ESPHome `dev` commit
`5f6a910e2d6e41d3716668a66e5dff8cca25f2ea`, component path
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
