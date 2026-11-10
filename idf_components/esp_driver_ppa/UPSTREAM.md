Source: Espressif ESP-IDF 5.5.5, isolated commit b2c4f7f.
Local changes:
- Retain the qualified DIG-734 FIFO equality boundary repair.
- Apply the rotated DMA macroblock axes from official ESP-IDF fix
  469aa16c3502c6860dbc70511dcb2f922bc6c57d. Quarter-turn operations use
  the rotated dimensions and corresponding scales when deciding FIFO bypass.
No timing, allocation, task or completion changes.
This adapter targets the ESP-IDF 5.5.x API; re-evaluate before upgrading SDK.
License: Apache-2.0, see LICENSE and source headers.
Upstream: https://github.com/espressif/esp-idf/commit/469aa16c3502c6860dbc70511dcb2f922bc6c57d
