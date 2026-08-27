# Documentation

These pages cover installation, protocols, routing, media and diagnostics
beyond the project overview in the [top-level README](../README.md).

## Pick your path

| Need | Document |
|---|---|
| Learn the complete everyday workflow | [User guide](USER_GUIDE.md) |
| Use redirects and advanced automations | [Automation cookbook](AUTOMATION_DIALPLAN.md) |
| Review the new development features | [What is new in 2026.8.2-dev](WHATS_NEW_2026_8_2.md) |
| Choose a board and maintained YAML | [Deployment guide](DEPLOYMENT_GUIDE.md) |
| Complete the shortest supported setup | [Quick start](../README.md#fastest-start) |
| Upgrade without breaking automations | [Breaking changes](BREAKING_CHANGES.md) |
| Configure ESP and HA options | [Configuration reference](reference.md) |
| Diagnose calls and media | [Testing and debug](TESTING_AND_DEBUG.md) |
| Resolve a specific failure | [Troubleshooting](troubleshooting.md) |

Published release notes live with their immutable artifacts on GitHub:
[2026.8.2-dev](https://github.com/n-IA-hane/esphome-intercom/releases/tag/v2026.8.2-dev),
[2026.8.1-dev](https://github.com/n-IA-hane/esphome-intercom/releases/tag/v2026.8.1-dev),
[2026.8.0](https://github.com/n-IA-hane/esphome-intercom/releases/tag/v2026.8.0),
[2026.7.1](https://github.com/n-IA-hane/esphome-intercom/releases/tag/v2026.7.1)
and
[2026.7.0](https://github.com/n-IA-hane/esphome-intercom/releases/tag/v2026.7.0).
The repository keeps only the current upgrade contract.

## Architecture and protocols

| Topic | Document |
|---|---|
| Component and lifecycle ownership | [Architecture](ARCHITECTURE.md) |
| Canonical end-to-end paths | [Call flows](CALL_FLOWS.md) |
| SIP, SDP and RTP endpoint contract | [ESP VoIP profile](voip_profile.md) |
| Retired proprietary protocol boundary | [Intercom protocol](INTERCOM_PROTOCOL.md) |
| Phonebook rows and media capabilities | [Phonebook protocol](PHONEBOOK_PROTOCOL.md) |
| Name, extension, group and trunk resolution | [Dial-plan resolver](DIALPLAN_RESOLVER.md) |
| ESP entities published to HA | [ESP entity surface](ESP_ENTITY_SURFACE.md) |

## Features

| Topic | Document |
|---|---|
| Home Assistant actions and side effects | [Services](SERVICES.md) |
| Contextual routing recipes | [Automation cookbook](AUTOMATION_DIALPLAN.md) |
| Ring and conference groups | [Groups](GROUPS.md) |
| Provider registration and external routing | [SIP trunk](SIP_TRUNK.md) |
| Browser and P4 video codec paths | [SIP video](SIP_VIDEO.md) |

## Qualification

[The test matrix](voip_test_matrix.md) separates deterministic protocol checks
from real ESP timing, browser media and hardware-in-the-loop evidence.
[Espressif components](ESPRESSIF_COMPONENTS.md) records component versions,
local modifications and licensing boundaries.

## Per-component docs

Each ESPHome component ships its own README with the full option list, YAML snippets and component-specific notes:

- [`voip_stack`](https://github.com/n-IA-hane/esphome-voip-stack), the ESP SIP
  phone component.
- [`esp_video_camera`](https://github.com/n-IA-hane/esphome-esp-video-camera), the
  ESP32-P4 Espressif V4L2 camera surface used by the native camera entity and
  SIP video sources.
- [`esp_jpeg_video_source`](https://github.com/n-IA-hane/esphome-voip-stack/tree/dev/esphome/components/esp_jpeg_video_source),
  [`esp_h264_video_source`](https://github.com/n-IA-hane/esphome-voip-stack/tree/dev/esphome/components/esp_h264_video_source)
  and [`p4_video_renderer`](https://github.com/n-IA-hane/esphome-voip-stack/tree/dev/esphome/components/p4_video_renderer),
  the compile-gated P4 SIP video TX and RX paths.
- [`esp_audio_stack`](https://github.com/n-IA-hane/esphome-audio-stack/tree/main/esphome/components/esp_audio_stack), the
  coordinated full-duplex audio backend for shared codec buses, dual I2S
  MEMS/amp boards that need software reference handling, Espressif
  rate/layout conversion, AEC reference capture, PSRAM placement and
  post-processor mic output. On AEC/AFE profiles, its standard ESPHome
  microphone facade is the cleaned stream consumed by MWW, Voice Assistant and
  VoIP while media/TTS keeps playing through the speaker.
- Full-experience media now uses ESPHome's source-based `speaker_source` path:
  HA media, announcements, local files and optional Sendspin streams feed one
  media player before the mixer arbitrates with VoIP and Voice Assistant.
  The local [`speaker`](../esphome/components/speaker/README.md) fork remains
  documented for custom YAMLs that still use `platform: speaker`.
- [`runtime_controller`](https://github.com/n-IA-hane/esphome-runtime-controller), a generic
  YAML-programmed reducer used by maintained full-experience profiles to derive
  LED, LVGL/display, audio ducking, ringtone and timer policies from one state
  snapshot. It is control-plane only and does not process audio samples.
- [`esp_aec`](https://github.com/n-IA-hane/esphome-audio-stack/tree/main/esphome/components/esp_aec), standalone ESP-SR echo cancellation.
- [`esp_afe`](https://github.com/n-IA-hane/esphome-audio-stack/tree/main/esphome/components/esp_afe), the full Espressif AFE pipeline (AEC + NS + VAD + AGC, optional dual-mic Speech Enhancement).
- internal shared audio primitives used privately by the local media/voice
  component forks and by the split audio-stack repository.
- `voip_simulator`, an internal test/simulation component used by the virtual
  device harness. It is not a production YAML component.
