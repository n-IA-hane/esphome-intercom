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
| `esphome/components/speaker/` | ESPHome `speaker` component | Fork with local pause/release and decoder-source scheduling patches. See `esphome/components/speaker/UPSTREAM.md`. |
| `esphome/components/voice_assistant/` | ESPHome `voice_assistant` component | Fork with configurable TTS playback-start timeout. See `esphome/components/voice_assistant/UPSTREAM.md`. |
| `esphome/components/audio/` | ESPHome audio component family | Local copy/adaptation used by maintained media profiles; resolves ESPHome audio codec libraries at build time. |
| `esphome/components/mipi_dsi/` | ESPHome community component lineage | Local P4 display support and panel models. |

When rebasing a fork, update its `UPSTREAM.md` and keep the ESPHome license
available.

## Contributed camera component

| Local path | Origin | License |
|---|---|---|
| `esphome/components/esp_video_camera/` | Snapshot shared by GitHub user `Psix-anp`, derived from ESPHome PR 16944 and retaining `@youkorr` as code owner | ESPHome split license, GPLv3 for C/C++ runtime files and MIT for Python and other files. The published upstream `LICENSE` and `NOTICE` confirm the origin and terms. The complete ESPHome license is included in `licenses/ESPHOME-LICENSE.txt`; see `PROVENANCE.md`. |

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
| `espressif/esp_video` 2.3.0, `esp_ipa` 2.2.0~1 | P4 V4L2 camera, ISP and image-processing pipeline. Espressif MIT license; see `licenses/ESPRESSIF-MIT.txt`. |
| `esp_cam_sensor` 2.3.0, `esp_sccb_intf` 0.0.8 | P4 camera-sensor and SCCB drivers. Apache-2.0; see `licenses/APACHE-2.0.txt`. |
| `espressif/esp_h264` 1.3.6 | Optional P4 hardware H.264 encoder and software decoder. Apache-2.0; see `licenses/APACHE-2.0.txt`. |
| `espressif/esp_image_effects` 1.1.0 | Optional P4 H.264 I420-to-PPA color-layout conversion. Espressif Modified MIT; see `licenses/ESPRESSIF-MODIFIED-MIT.txt`. |

Firmware using Espressif-restricted components is intended for Espressif
products/SoCs.

## Documentation assets

Images and videos under `docs/images/` are project documentation assets unless
a file-specific notice says otherwise.
