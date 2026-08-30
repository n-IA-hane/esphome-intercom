# ESPHome audio fork upstream record

Upstream baseline: ESPHome `dev` commit
`cd28a8a03e1fd00cde1e94e65ad089f3823ef274`, component path
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
