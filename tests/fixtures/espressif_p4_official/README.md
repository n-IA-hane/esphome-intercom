# Official P4 source contracts

These are unmodified test fixtures, never component overrides or firmware
providers. `PROVENANCE.json` records the exact commit, Git blob and SHA-256 of
each file, including its original license. IDF uses Apache-2.0; the esp_video
file uses the ESPRESSIF MIT license included alongside it.

- ESP-IDF `dd47ea3696b419fdb19cf747a6b96e2c4f6abcc5` supplies the rotated
  PPA SRM decision and resets SDMMC before the memory buses and other
  peripherals during restart.
- esp-video-components `79ae6117e58e0cf831c8e96820af72e877b5fb82` supplies
  the CSI input-format boundary at IDF 5.5.6 for pre-v3 P4 silicon. The tests
  also retain the independent conversion-capable branch.

The PPA host test extracts the real DIG-734 block and compares its decision
with rotated geometry, including 424x134. It preserves the official strict
`<` FIFO boundary. The [complete issue 19023 and its comments](https://github.com/espressif/esp-idf/issues/19023)
show why rotation changes the leftover size; they do not establish a fix
changing `<` to `<=`. FIFO equality remains a physical qualification gap.
The current P4 display path uses PPA scratch buffers with LVGL, rather than
PPA writes directly into the DPI framebuffer under scanout. These software
contracts do not qualify either hardware workload.
