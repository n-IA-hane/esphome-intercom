# ESPHome upstream alignment

This component is based on ESPHome 2026.8.0 `spi`.

The local ESP-IDF adapter opts external transmit buffers into the ESP-IDF 5.5
PSRAM DMA path. Cache-line-aligned transfers use 4032-byte chunks and disable
the driver's automatic private bounce buffer. Non-aligned external tails keep
the IDF fallback in PSRAM instead of consuming scarce internal DMA memory.
