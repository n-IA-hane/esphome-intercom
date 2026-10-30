# Device configurations

Start here to choose and build an ESPHome device. Use the profile for your exact
board before changing features or studying its internal packages. Maintained,
experimental and untested profiles are listed separately; appearing in this
directory does not by itself mean that a board has passed hardware testing.

The one-device-package layout and `firmware_features` examples describe the
current development tree. Use the YAML, packages and components from the matching
coordinated revision. Downloading an older stable YAML does not add this layout.

## Choose a starting point

| Your first goal | Start with | Next step |
| --- | --- | --- |
| An ESP telephone | Your board under [`voip-only/`](voip-only/) | Make and receive one call before changing SIP or audio settings |
| Assist, wake word, calls and music on one ESP | Your board under [`full-experience/`](full-experience/) | Keep its defaults for the first build, then edit its feature list |
| A display, camera or unusual board | The exact model in the [device catalog](../packages/DEVICE_CATALOG.md) | Read that profile's hardware and qualification notes |
| A board you wired yourself | A generic profile with the matching audio topology | Check GPIOs, flash, PSRAM and microphone/reference wiring first |

For example, the headless Waveshare S3 Audio uses
[`waveshare-s3-full-afe.yaml`](full-experience/single-bus/waveshare-s3-full-afe.yaml);
the round Spotpear Ball v2 uses
[`spotpear-ball-v2-full-afe.yaml`](full-experience/single-bus/spotpear-ball-v2-full-afe.yaml).
The Waveshare touch-screen models have separate profiles. A similar board name
is not enough to select the correct wiring.

For the **Waveshare 1.85C BOX V2**, the
[unified FULL YAML](full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml)
provides `audio_processor`, `mic_mix` and `voip_transport` substitutions. Start
there to choose AEC/AFE, separate/averaged microphones and SIP UDP/TCP without
copying hardware or application wiring. Read the [choice table](../packages/README.md#choose-processing-and-sip-on-waveshare-box-v2)
before changing processing; V1 is a different board.

## How to use

1. Download the YAML for your device and any companion files explicitly required
   by it, such as a custom partition CSV. Keep companion files beside the YAML.
2. Set `name` and `friendly_name` in its existing substitutions. Keep `name` when
   updating the same device. On generic boards, adapt the pin and memory settings
   to your hardware; board-specific GPIOs normally stay as supplied.
3. Create a `secrets.yaml` beside it with your Wi-Fi credentials:

   ```yaml
   wifi_ssid: "your_network"
   wifi_password: "your_password"
   ```

4. Keep any existing API encryption, OTA authentication and network settings
   when migrating a device. Read all `!secret` references in the chosen profile
   and its configuration errors; some overlays have additional prerequisites.
5. Validate, compile and install using ESPHome Device Builder, or use the CLI
   below from the folder containing the YAML. The first installation normally
   uses USB; later updates can use OTA after the device joins your network.
6. Add the device through Home Assistant's ESPHome integration. With VoIP Stack
   installed and `voip` selected, check its phonebook entry and make a call. For
   FULL voice features, select the device's Assist pipeline as well. An existing
   PBX instead uses the [classic-PBX setup](../docs/CLASSIC_PBX.md).

For the headless Waveshare example, the CLI commands are:

```bash
esphome config waveshare-s3-full-afe.yaml
esphome run waveshare-s3-full-afe.yaml
```

Use your actual filename for another board. `config` validates the composed
configuration; `run` builds and offers an upload. A valid configuration does not
prove physical audio quality. Start with low volume and check capture, playback,
hangup and a second call before adding custom behavior.

Public YAMLs fetch packages, components and assets automatically. Keep their
references coordinated as described in [source selection](#local-development-vs-release-mode).
When updating an old copy, start from the current board YAML and transfer your
settings using the [migration checklist](../docs/BREAKING_CHANGES.md#development-firmware-new-device-packages).

## Continue from simple to advanced

1. **One small change:** remove `sendspin` from the existing feature list, keeping
   ordinary music playback. [Example](../packages/README.md#1-remove-one-optional-feature).
2. **Choose the device's job:** keep just voice and wake word, then optionally add
   telephone and music. [Examples](../packages/README.md#2-build-a-voice-satellite).
3. **Customize a supported board:** adjust a substitution or extend an existing
   component by ID. [Example](../packages/README.md#5-customize-one-setting).
4. **Build for different hardware:** follow the device composition to the board
   and audio backend, then validate each layer. [Walkthrough](../packages/README.md#6-follow-a-feature-to-its-implementation).

The bus and processor sections below explain hardware choices. They are not
additional steps that every owner of a supported board must perform.

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
  untested/              Reference configurations awaiting hardware qualification
```

Debug should be enabled through component options such as `debug: true`,
`audio_debug: true`, or component-specific telemetry flags, then disabled again
for release builds. The repository does not publish separate debug device YAMLs;
reusable opt-in debug packages can live under `packages/debug/`.

## Single-bus vs dual-bus

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
  and constrained custom voice targets. The maintained Generic FULL reference
  uses 8 MB flash; choose the profile's actual OTA layout when budgeting size.
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
offline/custom installs. The `directory` feature reads the shared phonebook
through standard LDAP; manual contacts remain available without it.

## Select firmware features

Each supplied profile includes one complete device package. Edit its existing
`firmware_features` substitution to select voice, wake word, telephone, music,
Sendspin, timers, the independent `directory`, `blf`, `mwi` services,
and the hardware options offered by that profile. The matching
callbacks, controls and presentation follow that selection.

See the [package guide](../packages/README.md) for the feature table, dependencies,
examples and migration of older includes. These firmware choices do not gate
Home Assistant's PBX services. Standalone phones keep their lightweight callback
path; FULL devices use the shared runtime coordinator.

## Package map

Follow `packages/devices/<profile>.yaml` from the chosen YAML. It links the board,
audio backend and complete feature modules. Screen geometry is in presentation
layouts; runtime priorities and call/audio ownership stay in their shared owners.
The [directory map](../packages/README.md#find-the-implementation) explains where
to find each part. A package file is not a second audio or SIP engine.

## Production logging

Public YAMLs ship with `logger.level: INFO`. INFO covers all user-visible call-lifecycle, mic-consumer attach/detach and AFE/AEC mode-switch milestones. Flip to `DEBUG` only while developing; deep audio paths stay behind component options such as `audio_debug: true` or `esp_audio_stack.telemetry: true`. Local debug packages and generated host YAMLs are intentionally ignored by git.

## P4 status

Waveshare P4 Touch YAMLs build and boot with the maintained audio/LVGL state
model, FD high-perf AFE defaults and the current ESPHome/ESP-Hosted baseline.
The following profiles have distinct, qualified media roles:

- [`waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml`](full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml)
  combines bidirectional RTP/JPEG, the native Home Assistant camera,
  dual-microphone AFE and the full Voice Assistant runtime;
- [`waveshare-p4-touch-videophone-jpeg.yaml`](voip-only/single-bus/waveshare-p4-touch-videophone-jpeg.yaml)
  is the lightweight bidirectional RTP/JPEG videophone;
- [`waveshare-p4-touch-videophone-h264.yaml`](voip-only/single-bus/waveshare-p4-touch-videophone-h264.yaml)
  is the lightweight bidirectional H.264 videophone.

The full SIP/JPEG profile suspends an enabled Wake Word switch only while
negotiated video media is active, then restores the user's previous state.
Audio-only calls leave Wake Word unchanged. Treat P4 as a hardware-specific
target: hosted Wi-Fi/SDIO firmware, LVGL/PPA, media transport and task
scheduling matter more on P4 than on compact S3 boards.

If a P4 target resets, hangs, or loses Wi-Fi under media/TTS streaming, update
the on-board ESP32-C6 hosted Wi-Fi firmware before chasing audio bugs. The
root README's P4 section documents the validated recovery path: use a P4
`esp-serial-flasher` SDIO-ROM recovery flasher, short the C6 `IO9` pad to `GND` while
the flasher boots, program the C6 `network_adapter` firmware, then reflash the
normal ESPHome P4 YAML. Current P4 packages also enable ESPHome's native
`esp32_hosted` update entity for future C6 updates once the coprocessor is on a
modern firmware.

## Local development vs release mode

Public YAMLs use **remote mode** to fetch their dependencies from GitHub.
Follow the release's source references: changing only the device-package URL
can combine new YAML with old component APIs. An unpublished development package
is not made available by leaving its URL on `main`.

If you are working inside a local clone and want them to point back at your
checkout, run:

```bash
./scripts/yaml_paths.sh local
```

When you are ready to switch them back to the published form:

```bash
./scripts/yaml_paths.sh main
./scripts/yaml_paths.sh check --expect remote
```

Use `dev`, a tag or a temporary branch name instead of `main` only when that ref
exists in every managed project repository. The script applies the same ref;
it does not create branches or tags. The camera repository keeps its upstream
`main`, and the official Voice PE base keeps its own pinned revision. In local
mode, managed component sources use sibling checkouts. Review `git diff` after
switching, and follow the release's coordinated source manifest when provided.

## Not sure which one to pick?

See [../docs/DEPLOYMENT_GUIDE.md](../docs/DEPLOYMENT_GUIDE.md) for a decision tree that maps hardware and requirements to the right preset.
