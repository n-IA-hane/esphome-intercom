# Device Configurations

Ready-to-flash ESPHome YAML configurations for tested hardware. ESP32-S3
presets are the compact reference targets; P4 YAMLs are hardware-specific full
display/audio targets and should be tested with the intended panel and hosted
Wi-Fi firmware.

## How to use

1. Download the YAML file for your device
2. Create a `secrets.yaml` with your WiFi credentials:

   ```yaml
   wifi_ssid: "your_network"
   wifi_password: "your_password"
   ```

3. Compile with ESPHome. Public YAMLs point at the GitHub copy of this repository, so components, packages, and assets are fetched automatically. Stable releases point at `main`; opt-in test YAMLs on `dev` point at `dev`.

## Structure

```text
yamls/
  voip-only/         VoIP without Voice Assistant or Wake Word
    single-bus/          Devices using esp_audio_stack (mic+speaker on same I2S bus)
    dual-bus/            Devices using esp_audio_stack rx_bus + tx_bus
    esphome-native/      Native ESPHome mic/speaker examples

  full-experience/       VA + MWW + VoIP (complete voice assistant hub)
    single-bus/          esp_audio_stack full profiles
    dual-bus/            esp_audio_stack full profiles with separate RX/TX buses
    esphome-native/      Native ESPHome audio profiles for processed/separate paths

  experimental/          Untested topologies (compile-only, contributions welcome)
```

Debug should be enabled through component options such as `debug: true`,
`audio_debug: true`, or component-specific telemetry flags, then disabled again
for release builds. The repository does not publish separate debug device YAMLs;
reusable opt-in debug packages can live under `packages/debug/`.

## Single-bus vs Dual-bus

- **Single-bus**: mic and speaker share one I2S peripheral via `esp_audio_stack`. Used by devices with audio codecs (ES8311, ES7210+ES8311). Enables stereo AEC reference, TDM multi-mic, and 48kHz bus rate with Espressif `esp_ae_rate_cvt` conversion to 16kHz.

- **Dual-bus**: mic and speaker on separate I2S peripherals using `esp_audio_stack`
  `rx_bus` and `tx_bus` with official ESP-IDF I2S simplex channels. Simpler setup
  for MEMS mic + class-D amp boards (SPH0645 + MAX98357A).

- **Native ESPHome**: `voip_stack` binds directly to ESPHome `microphone`
  and/or `speaker` components. Use it for mic-only/speaker-only endpoints,
  hardware/DSP-processed audio, or independent mic/speaker paths that do not
  need software AEC. Use `esp_audio_stack` instead for shared-bus or
  software-reference builds.

## Audio processor: esp_aec vs esp_afe

- **esp_aec**: Lightweight echo cancellation only. Recommended for voip-only
  and Generic full-experience 4 MB targets.
- **esp_afe**: Full Espressif AFE pipeline (AEC + NS + VAD + AGC + optional
  dual-mic Speech Enhancement). Higher flash/RAM cost, but adds the full
  frontend and runtime diagnostics. Generic full AFE is intended for app slots
  larger than the default 4 MB OTA layout, so 8 MB or 16 MB flash is the
  practical target. See the [esp_afe component README](https://github.com/n-IA-hane/esphome-audio-stack/tree/main/esphome/components/esp_afe) for details.

## Product mode

Each ESP flashed with these YAMLs is an independent SIP phone on the local
fabric. Same-transport devices can call each other directly when the HA-managed
roster provides complete direct SIP endpoint data. Names, numbers and
cross-transport routes go to HA, which is the stable roster authority and SIP
bridge/B2BUA.

There is one product mode: SIP-only phone mode. Phonebook, contacts,
destination and caller entities are always exposed. `transport: udp` selects
SIP/UDP signaling and `transport: tcp` selects SIP/TCP signaling; audio remains
RTP/UDP in the current profile. Static contacts are optional local entries for
offline/custom installs; normal shared routing comes from `sensor.voip_phonebook`.

## Optional packages

`packages/voice_assistant/timers_runtime.yaml` adds Home Assistant timer alarm
support on top of the runtime_controller full VA/VoIP package. Include it only
on devices that should expose timer behavior.

## Package map

Package names are meant to describe ownership, not implementation history:

- `packages/presets/full_voice_voip_runtime.yaml`: high-level preset for full
  VA + MWW + media + VoIP devices using runtime_controller. It wires runtime
  facts and callbacks; board audio, display and codec choices stay outside.
- `packages/voip_only.yaml`: high-level preset for simple VoIP endpoints. It
  uses voip_stack callbacks directly and does not include runtime_controller.
- `packages/audio/shared_mono_mixer_48k.yaml`: shared speaker mixer/resampler
  graph for full devices. It only creates audio plumbing and does not own UI or
  policy state.
- `packages/media_player/runtime_controller_mono_media_player_48k.yaml`: media,
  announcement, Sendspin and ringtone sources for runtime_controller devices.
  It emits media lifecycle facts instead of rendering state directly.
- `packages/voice_assistant/runtime_events.yaml`: Voice Assistant and Micro Wake
  Word components plus runtime_controller event forwarding.
- `packages/voice_assistant/runtime_controls.yaml`: user-facing VA/MWW controls
  such as mic mute and Wake Word.
- `packages/voip/simple_led_status_*.yaml`: direct LED mappings for VoIP-only
  devices with no runtime_controller.
- `packages/voip/*_runtime.yaml`: VoIP helper packages that additionally emit
  runtime_controller connectivity or mute events.
- `packages/debug/*.yaml`: opt-in diagnostic overlays. Do not include them in
  release YAMLs unless you are actively collecting logs or traces.

Prefer native ESPHome actions, conditions and scripts in packages. Use lambdas
only where ESPHome has no native primitive: dynamic trigger/API values, LVGL
widget synchronization, direct hardware register access, or FreeRTOS/task
operations.

## Production logging

Public YAMLs ship with `logger.level: INFO`. INFO covers all user-visible call-lifecycle, mic-consumer attach/detach and AFE/AEC mode-switch milestones. Flip to `DEBUG` only while developing; deep audio paths stay behind component options such as `audio_debug: true` or `esp_audio_stack.telemetry: true`. Local debug packages and generated host YAMLs are intentionally ignored by git.

## P4 status

Waveshare P4 Touch YAMLs build and boot with the maintained audio/LVGL state
model, FD high-perf AFE defaults and the current ESPHome/ESP-Hosted baseline.
The landscape full profile has been field-tested with hosted Wi-Fi, phonebook
sync and VoIP calls, but audio playback still needs follow-up tuning for
occasional glitches. Treat P4 as a hardware-specific target: hosted Wi-Fi/SDIO
firmware, LVGL/PPA, media/TTS transport behavior and task scheduling matter
more on P4 than on compact S3 boards.

The experimental
[`waveshare-p4-touch-full-afe-landscape-sip-jpeg.yaml`](full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-sip-jpeg.yaml)
profile combines SIP JPEG video, dual-microphone AFE and the full runtime.
Those workloads are difficult to run concurrently with Micro Wake Word, so the
profile suspends an enabled Wake Word switch only while negotiated video media
is active. It restores the user's previous switch state when video ends.
Audio-only calls leave Wake Word unchanged.

If a P4 target resets, hangs, or loses Wi-Fi under media/TTS streaming, update
the on-board ESP32-C6 hosted Wi-Fi firmware before chasing audio bugs. The
root README's P4 section documents the validated recovery path: use a P4
`esp-serial-flasher` SDIO-ROM recovery flasher, short the C6 `IO9` pad to `GND` while
the flasher boots, program the C6 `network_adapter` firmware, then reflash the
normal ESPHome P4 YAML. Current P4 packages also enable ESPHome's native
`esp32_hosted` update entity for future C6 updates once the coprocessor is on a
modern firmware.

## Local development vs release mode

The public YAMLs are stored in **remote mode** so they compile straight from
GitHub. Release YAMLs must point at `main`, so downloaded YAMLs, packages,
components and assets all resolve from the same public branch.

If you are working inside a local clone and want them to point back at your
checkout, run:

```bash
./scripts/yaml_paths.sh local
```

When you are ready to switch them back to the published form:

```bash
./scripts/yaml_paths.sh remote --intercom main --audio main --runtime main
./scripts/yaml_paths.sh check
```

## Not sure which one to pick?

See [../docs/DEPLOYMENT_GUIDE.md](../docs/DEPLOYMENT_GUIDE.md) for a decision tree that maps hardware and requirements to the right preset.
