# Documentation

Welcome. These pages cover everything beyond the project pitch on the [top-level README](../README.md).

## Pick your path

- 🚀 **Start here**: [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) is the decision
  tree that maps hardware and features (VoIP only, VA + MWW, touch display,
  2-mic Speech Enhancement) to the right ready-to-flash config under
  [`yamls/`](../yamls/).

- 🧭 **Quick start**: the top-level [README](../README.md#fastest-start)
  gives the shortest supported path from HACS and one maintained YAML to a
  working call.

- 🧾 **Release / upgrade notes**: [BREAKING_CHANGES.md](BREAKING_CHANGES.md)
  starts from the current SIP/VoIP breaking migration. The current
  [2026.8.1 pre-release notes](RELEASE_2026_8_1.md) describe the latest
  interoperability and consolidation delta. The stable
  [2026.8.0 release notes](RELEASE_2026_8_0.md) remain the complete illustrated
  feature overview. Older published notes remain available for
  [2026.7.1](RELEASE_2026_7_1.md) and [2026.7.0](RELEASE_2026_7_0.md).

- 📚 **Configuration reference**: [reference.md](reference.md) covers the ESP
  `voip_stack` options, triggers, actions and conditions plus the Home Assistant
  services, setup options, events and state vocabulary. Audio processor details
  live in the linked `esphome-audio-stack` component references below.

- 🔌 **Wire protocol**: [INTERCOM_PROTOCOL.md](INTERCOM_PROTOCOL.md) is a
  tombstone for the retired proprietary protocol. Current call control is SIP,
  SDP and RTP.

- ☎️ **Optional SIP trunk**: [SIP_TRUNK.md](SIP_TRUNK.md) documents provider
  registration, outbound external routing and inbound DTMF target selection.

- 📒 **Phonebook protocol**: [PHONEBOOK_PROTOCOL.md](PHONEBOOK_PROTOCOL.md)
  documents canonical endpoint rows, `audio_mode`, `tx_formats`/`rx_formats`
  and how HA shapes direct SIP or HA-bridged routes for each ESP.

- 🧩 **ESP entity surface**: [ESP_ENTITY_SURFACE.md](ESP_ENTITY_SURFACE.md)
  explains which `voip_stack` entities enable HA discovery, ESP mirror cards,
  dynamic groups and debug.

- 🧭 **Dial plan / resolver**: [DIALPLAN_RESOLVER.md](DIALPLAN_RESOLVER.md)
  explains how HA resolves names, extensions, groups, registered SIP endpoints
  and trunk numbers.

- 📞 **Call flows**: [CALL_FLOWS.md](CALL_FLOWS.md) explains the expected
  signaling/media path for ESP, HA, registered endpoint, group and trunk calls.

- 🎥 **SIP video**:
  [SIP_VIDEO.md](SIP_VIDEO.md) defines the opt-in
  SIP video-phone profile for the HA softphone, its direct and optional
  transcoded codec matrix, browser privacy controls, requirements and
  deliberate limits. ESPHome endpoints remain audio-only.

- 🧰 **HA services**: [SERVICES.md](SERVICES.md) documents every
  `voip_stack.*` service and the expected side effects.

- 🧭 **Automation cookbook**:
  [AUTOMATION_DIALPLAN.md](AUTOMATION_DIALPLAN.md) contains copyable native HA
  recipes for presence routing, ring groups, actionable notifications,
  no-answer forwarding to Assist and connected-call DTMF.

- 👥 **Groups**: [GROUPS.md](GROUPS.md) documents ring group and conference
  group semantics, including `conference_ring`.

- 🧪 **Testing and debug**: [TESTING_AND_DEBUG.md](TESTING_AND_DEBUG.md)
  collects local pytest commands, real SIP matrix expectations, service-matrix
  checks, log filters and audio-debug capture paths.

- 🧱 **Architecture**: [ARCHITECTURE.md](ARCHITECTURE.md) describes component
  ownership, SIP transactions/dialogs, routing and media boundaries, governed
  SIP/TCP backpressure, frontend state projection and runtime diagnostics.

- 🧯 **Troubleshooting**: [troubleshooting.md](troubleshooting.md) covers SIP
  ringing, media negotiation, phonebook/routing, registration, trunk, audio and
  card-state failures with concrete checks.

- 🖼️ **Media catalogue**: [MEDIA_SHOT_LIST.md](MEDIA_SHOT_LIST.md) lists the
  screenshots, photos, GIFs, diagrams and repeatable demo scenes currently
  used to explain the project.

- 📐 **Qualification model**: [voip_test_matrix.md](voip_test_matrix.md) and
  [architecture/phase_00v_virtual_device.md](architecture/phase_00v_virtual_device.md)
  separate deterministic protocol coverage from real ESP timing, browser
  media and hardware-in-the-loop evidence.

- 📡 **ESP SIP/RTP profile**: [voip_profile.md](voip_profile.md) defines the
  lightweight standards-based endpoint contract; [ESPRESSIF_COMPONENTS.md](ESPRESSIF_COMPONENTS.md)
  records the Espressif component and licensing boundary.

## Per-component docs

Each ESPHome component ships its own README with the full option list, YAML snippets and component-specific notes:

- [`voip_stack`](https://github.com/n-IA-hane/esphome-voip-stack), the ESP SIP
  phone component.
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
