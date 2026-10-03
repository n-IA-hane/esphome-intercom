# Internal instruction memory for ESP32-S3 audio

This opt-in build helper places selected FFT and resampler dot-product functions
in internal instruction RAM using ESP-IDF linker fragments. It changes where the
existing code executes, without changing sample rates, filters or algorithms.
It defines no task, entity, audio buffer or runtime initializer.

The Waveshare 1.85C V2 full profile uses it after JTAG profiling identified these
functions in the active audio workload. In the measured firmware, the placement
used 1,556 additional internal bytes. Check the map and memory budget when
changing library versions; other profiles do not enable it automatically.

```yaml
external_components:
  - source: github://n-IA-hane/esphome-intercom@dev
    components: [audio_dsp_iram]

audio_dsp_iram:
```

The configuration requires ESP32-S3 and `esp_afe`. The mappings use the already
linked ESP-SR FFT and ESPHome audio libraries. No code is copied or patched in
those libraries. Omitting the component omits the linker placement.
