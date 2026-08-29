# ESPHome upstream alignment

This component is identical to ESPHome `dev` merge commit
`e458a38f89e7ccfc3d186d6d2f41708168e6f492`, pull request `#18699`, path
`esphome/components/spi`.

The opt-in `psram_dma` setting enables the official ESP-IDF 5.5 PSRAM DMA path
for supported hardware SPI devices and TX-only transactions. The local copy is
temporary compatibility for ESPHome 2026.8.1 and can be removed once the
merged component is included in the required ESPHome release.
