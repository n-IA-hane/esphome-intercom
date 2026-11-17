# Upstream alignment

Base: official ESPHome development commit
`c6f330458a2da2f64788487486bd6d0f8749fafd`,
`esphome/components/mipi_dsi`. Its runtime and schema match the official
`373224c141dceadbb67ace263040cbfc802c5968` baseline. Original and local hashes
are recorded in `esphome/upstream_sources.json`. Model tables, clocks, timings,
AXI priorities and upstream IDF 5/6 setup are retained.

## Bounded display-owner additions

- `draw_rgb565_transformed` accepts a public PPA operation and a bounded source
  buffer. It composes the display rotation once, scales and converts directly
  into the DPI-owned RGB565 framebuffer, then submits that rectangle. The
  rectangle is logical and the supplied operation has rotation zero. Input
  pixel modes include packed YUV420; no codec or renderer state lives here.
- Normal little-endian RGB565 bulk rotation uses the same display owner.
  A single mutex covers transformation, compatibility-canvas mirroring and
  panel transfer completion. The producer retains its source until return.
  Admission is checked after acquiring ownership and again after completion.
- Completion supports both an ISR callback and the synchronous task callback
  used by IDF's no-copy submission. A stale binary token is drained before
  each submit. A rejected submit never waits for a missing callback.
- `copy_frame_buffer_rgb565` copies the completed, composed framebuffer into
  caller storage, without exposing a framebuffer pointer or forcing LVGL to
  redraw. It returns false when busy or when an uncommitted scalar UI update
  exists. Default output is logical owner orientation. Its optional rotation
  is additional clockwise rotation, independent of LVGL configuration.
- A compatibility canvas is seeded from the current DMA framebuffer under the
  same owner and M2C synchronization, and subsequent direct writes mirror into
  it. This prevents a later scalar flush from restoring stale background.

Schema capability IDs live under `CORE.data["mipi_dsi"]` as
`rgb565_transform_ids` and `framebuffer_readback_ids`. Hardware-rotation metadata
is declared only for the little-endian RGB565 transform path. The provider does
not configure LVGL acceleration, choose an SDK version, change DMA priorities,
or own a media lifecycle. There is no optional `use_ppa` presentation path.

## Evidence and limits

The complete driver is compiled by ESPHome's canonical C++ host runner with
public SDK-boundary doubles for IDF 5 and 6. Cases compare pixels with the
ordinary scalar Display implementation, exercise UI/video ownership and
readback, and check transfer-error and completion boundaries. The doubles do
not qualify hardware colour conversion, panel behaviour, speed or PSRAM margin.
Those require the same firmware and physical workload before promotion.

The remaining delta is intended for an explicit upstream proposal. Remove it
only after the official provider supplies and qualifies these ownership,
transform and readback contracts together. C++ runtime files retain ESPHome's
GPLv3 license; Python and other files retain MIT. See
`licenses/ESPHOME-LICENSE.txt` and `THIRD_PARTY_NOTICES.md`.
