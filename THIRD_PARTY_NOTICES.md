# Third-party notices

Project-authored code in this repository is MIT-licensed unless a file states
otherwise. ESPHome-derived runtime files retain ESPHome's GPLv3 terms; Python
and non-runtime files in those component families retain MIT terms.

This file only records the third-party code or build-time dependencies that
matter for normal source and firmware redistribution.

## ESPHome-derived code

ESPHome's license is included in `licenses/ESPHOME-LICENSE.txt`.

| Local path | Upstream | Notes |
|---|---|---|
| `esphome/components/mipi_dsi/` | ESPHome community component lineage | Local P4 display support and panel models. |
| `esphome/components/voice_assistant/` | ESPHome 2026.10.0, `704551a6` | Noncontinuous idle completion and callback ordering. |
| `esphome/components/speaker_source/` | ESPHome 2026.10.0, `704551a6` | Terminal source error cleanup through the existing player owner. |
| `esphome/components/audio_http/` | ESPHome 2026.10.0, `704551a6` | Release completed HTTP workers before publishing idle. |

The three lifecycle adaptations above are loaded through the maintained
`ext_components_source` includes. Their `UPSTREAM.md` files describe the bounded
deltas. Other audio, MWW, mixer, resampler, speaker and OTA providers come from
the installed official ESPHome distribution. Their upstream licenses apply to
generated firmware. The existing ESPHome split license applies to all copied
runtime and Python files.

The host test entrypoint `tests/fixtures/assist_media/main.cpp` is derived from
the same ESPHome revision and retains GPLv3 terms. Host-only micro-decoder and
micro-flac sources are fetched as pinned CI dependencies and are not vendored.

## Contributed camera component

| Build source | Origin | License |
|---|---|---|
| `Psix-anp/esphome-esp-video-camera` external component | Repository maintained by GitHub user `Psix-anp`, derived from ESPHome PR 16944 and retaining `@youkorr` as code owner | ESPHome split license, GPLv3 for C/C++ runtime files and MIT for Python and other files. The external repository includes its complete `LICENSE` and `NOTICE`; this repository also includes the ESPHome license in `licenses/ESPHOME-LICENSE.txt`. |

## Runtime Python dependency

| Dependency | Used by | License |
|---|---|---|
| `numpy>=2.0.0` | Home Assistant audio conversion paths | BSD-3-Clause |

`numpy` is installed by Home Assistant from the package index; it is not
vendored in this repository.

## IDF Component Manager dependencies

ESP firmware builds may resolve these dependencies through ESPHome/ESP-IDF
Component Manager. They are not vendored here; their upstream licenses apply.

| Component | Used by |
|---|---|
| `esphome/esp-audio-libs`, `esphome/micro-decoder`, `esphome/micro-flac`, `esphome/micro-mp3`, `esphome/micro-opus`, `esphome/micro-wav` | ESPHome audio/media decoder paths. |
| `espressif/esp_audio_effects` | ESP Audio Stack rate/format/channel conversion. |
| `espressif/esp_codec_dev` | ESP Audio Stack codec-backed I2S paths. |
| `espressif/esp-dsp`, `espressif/esp-sr`, `espressif/gmf_ai_audio` | ESP AEC/AFE profiles. |
| `espressif/esp_video` 2.4.1, `esp_ipa` 2.3.x | P4 V4L2 camera, ISP and image-processing pipeline. Espressif MIT license; see `licenses/ESPRESSIF-MIT.txt`. |
| `esp_cam_sensor` 2.4.x, compatible `esp_sccb_intf` | P4 camera-sensor and SCCB drivers. Apache-2.0; see `licenses/APACHE-2.0.txt`. |
| `espressif/esp_h264` 1.3.6 | Optional P4 hardware H.264 encoder and software decoder. Apache-2.0; see `licenses/APACHE-2.0.txt`. |
| `espressif/esp_image_effects` 1.1.0 | Optional P4 H.264 I420-to-PPA color-layout conversion. Espressif Modified MIT; see `licenses/ESPRESSIF-MODIFIED-MIT.txt`. |

Firmware using Espressif-restricted components is intended for Espressif
products/SoCs.

## Documentation assets

Images and videos under `docs/images/` are project documentation assets unless
a file-specific notice says otherwise.

## Community assistant assets

The Starfleet-inspired artwork under `assets/images/assistant/starfleet/` and
optional ringtone under `assets/sounds/starfleet/` were contributed by GitHub
user [rvdv01](https://github.com/rvdv01) for inclusion in this project through
[discussion #110](https://github.com/n-IA-hane/esphome-intercom/discussions/110).
The original asset files are retained unchanged; provenance and usage are
recorded in the theme's README. This attribution concerns the submitted
artwork and audio, not ownership of the depicted Star Trek identifiers.
