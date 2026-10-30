# Documentation

These pages cover installation, protocols, routing, media and diagnostics
beyond the project overview in the [top-level README](../README.md).

## Learn in stages

1. **First call:** learn [phone, card and destination](CONCEPTS.md), then follow
   the [user guide](USER_GUIDE.md). Start with two phones and no external trunk.
2. **Everyday telephone system:** add [groups](GROUPS.md), a [provider line](SIP_TRUNK.md)
   and optional [phone defaults](PHONE_ROUTING.md). Use the GUI first.
3. **Messages and handoffs:** add [voicemail](VOICEMAIL.md), [parking/pickup](CALL_PARKING.md)
   or [recording](CALL_RECORDING.md) only where you need them.
4. **Context-dependent behavior:** use the [automation recipes](AUTOMATION_DIALPLAN.md)
   for schedules, presence, announcements and menus.
5. **Custom integrations or difficult faults:** continue with the protocol,
   service and testing references below. They do not replace the user walkthrough.

Historical release notes and migration entries describe their named versions.
Do not use an old release example as the current installation procedure.

For ESP firmware, use a separate progression: [choose and build your board](../yamls/README.md#how-to-use),
then [edit its feature selection](../packages/README.md#work-through-a-full-profile),
and only then explore the [board/audio/module map](../packages/README.md#find-the-implementation).
The [migration checklist](BREAKING_CHANGES.md#development-firmware-new-device-packages)
explains how to carry settings from an older YAML into the new device packages.

## Pick your path

| Need | Document |
|---|---|
| Set up the new PBX features with practical examples | [Illustrated PBX walkthrough](PBX_WORKFLOWS.md) |
| Learn the complete everyday workflow | [User guide](USER_GUIDE.md) |
| Build greetings, keypad menus and call routing in the HA editor | [Automation as dialplan cookbook](AUTOMATION_DIALPLAN.md) |
| Review the current release | [Release history](../CHANGELOG.md) |
| Choose a board and maintained YAML | [Deployment guide](DEPLOYMENT_GUIDE.md) |
| Start simple and add ESP voice, calls, music or presentation | [Package walkthrough](../packages/README.md#work-through-a-full-profile) |
| Complete the shortest supported setup | [Quick start](../README.md#fastest-start) |
| Update an existing installation | [Breaking changes](BREAKING_CHANGES.md) |
| Connect ESP firmware to a classic PBX | [Direct and registered SIP accounts](CLASSIC_PBX.md) |
| Set up managed ESP phone services | [Accounts, LDAP, BLF and IMAP voicemail](ESP_PHONE_SERVICES.md) |
| Configure ESP and HA options | [Configuration reference](reference.md) |
| Diagnose calls and media | [Testing and debug](TESTING_AND_DEBUG.md) |
| Resolve a specific failure | [Troubleshooting](troubleshooting.md) |

Published release notes and their artifacts are available in the
[GitHub releases](https://github.com/n-IA-hane/esphome-intercom/releases).
The PBX additions in these guides are included in the updated 2026.11.0
release; they were not in 2026.10.1.

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
| Persistent phone defaults and automation precedence | [Phone routing](PHONE_ROUTING.md) |
| Contextual routing recipes | [Automation cookbook](AUTOMATION_DIALPLAN.md) |
| Ring and conference groups | [Groups](GROUPS.md) |
| Phone mailboxes, SIP message indicators and optional transcription | [Voicemail](VOICEMAIL.md) |
| Parking, retrieval and directed pickup | [Call parking](CALL_PARKING.md) |
| Blind transfer from the card and consultation transfer from a SIP handset | [Transfer a call](USER_GUIDE.md#forward-or-transfer-a-call) |
| Play an ordinary ESPHome announcement or PCM sound | [ESPHome sound actions](ESPHOME_SOUND_ACTIONS.md) |
| Record an established conversation | [Conversation recording](CALL_RECORDING.md) |
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
- [`esp_video_camera`](https://github.com/Psix-anp/esphome-esp-video-camera), the
  ESP32-P4 Espressif V4L2 camera surface used by the native camera entity and
  SIP video sources.
- [`esp_jpeg_video_source`](https://github.com/n-IA-hane/esphome-voip-stack/tree/main/esphome/components/esp_jpeg_video_source),
  [`esp_h264_video_source`](https://github.com/n-IA-hane/esphome-voip-stack/tree/main/esphome/components/esp_h264_video_source)
  and [`p4_video_renderer`](https://github.com/n-IA-hane/esphome-voip-stack/tree/main/esphome/components/p4_video_renderer),
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
  The native ESPHome `speaker` base remains in use; the local legacy player
  fork has been removed. See the [migration instructions](BREAKING_CHANGES.md#development-firmware-retired-speaker-fork)
  for custom YAMLs that selected it explicitly.
- [`runtime_controller`](https://github.com/n-IA-hane/esphome-runtime-controller), a generic
  YAML-programmed reducer used by maintained full-experience profiles to derive
  LED, LVGL/display, audio ducking, ringtone and timer policies from one state
  snapshot. It is control-plane only and does not process audio samples.
- [`esp_aec`](https://github.com/n-IA-hane/esphome-audio-stack/tree/main/esphome/components/esp_aec), standalone ESP-SR echo cancellation.
- [`esp_afe`](https://github.com/n-IA-hane/esphome-audio-stack/tree/main/esphome/components/esp_afe), the full Espressif AFE pipeline (AEC + NS + VAD + AGC, optional dual-mic Speech Enhancement).
- [Playback diagnostics](playback_diagnostics.md), optional observation of the
  native announcement pipeline and hardware completion. Maintained firmware
  uses official ESPHome media and voice providers.
- `voip_simulator`, an internal test/simulation component used by the virtual
  device harness. It is not a production YAML component.

For an offline or browsable copy, [build the documentation site](DOCUMENTATION_SITE.md).
