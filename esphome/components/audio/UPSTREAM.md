# ESPHome audio fork upstream record

Upstream baseline: ESPHome `dev` commit
`5f6a910e2d6e41d3716668a66e5dff8cca25f2ea`, component path
`esphome/components/audio`.

Checked with:

```bash
git diff --no-index ../esphome-pr-work/esphome/components/audio esphome/components/audio
```

## Local patch

The host simulator uses the shared audio format, transfer buffer and decoder
interfaces without ESP-IDF. The local fork permits `audio:` on the host,
skips IDF component registration there and provides the narrow non-ESP32
speaker and error-code adapters needed by `audio_transfer_buffer`.

ESP32 dependency versions, decoder behavior and Kconfig names remain aligned
with the recorded upstream baseline. Re-run the diff after every ESPHome update.
