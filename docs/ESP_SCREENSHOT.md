# ESP Screenshot

`esp_screenshot` is maintained in the standalone
[ESP Screenshot repository](https://github.com/n-IA-hane/esphome-screenshot/tree/dev).
Its [canonical guide](https://github.com/n-IA-hane/esphome-screenshot/blob/dev/README.md)
covers standalone LVGL and P4 MIPI DSI configurations, HTTP capture,
Home Assistant image helpers and resource settings.

Intercom keeps the optional [diagnostic controls](../packages/diagnostics/screenshot.yaml)
and its [maintained MIPI DSI provider](../esphome/components/mipi_dsi/UPSTREAM.md).
For P4 snapshots, load `esp_screenshot` from its repository and `mipi_dsi`
from Intercom as separate external component sources.
