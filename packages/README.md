# Composing device features

Start from the YAML for your board. It now selects one complete device package;
change its `firmware_features` list to choose what is compiled. You do not need to
find and remove a microphone callback, mixer input, button and timer in separate
files to remove a feature.

The package syntax requires ESPHome 2026.10.0 or newer. The current composition and
firmware checks use ESPHome 2026.10.0 with ESP-IDF 5.5.6. Validation, compilation and
physical testing are different checks; experimental boards remain experimental.

This guide describes the reorganized development packages. Start with the
[device setup guide](../yamls/README.md#how-to-use) and use a coordinated source
revision containing this layout. Updating an older YAML is covered in the
[migration checklist](../docs/BREAKING_CHANGES.md#development-firmware-new-device-packages).

## Select features in the device YAML

For example, the headless Waveshare S3 Audio FULL AFE profile contains:

```yaml
substitutions:
  firmware_features: [voice, wake_word, voip, music, sendspin, timers, status_led, diagnostics]
  # Keep the board's other substitutions, pins and component sources.

packages:
  device: github://n-IA-hane/esphome-intercom/packages/devices/waveshare-s3-full-afe.yaml@dev
```

Edit that existing list. These are examples of replacement values:

| Intended firmware | `firmware_features` |
| --- | --- |
| Voice Assistant and wake word | `[voice, wake_word]` |
| Voice Assistant and telephone | `[voice, wake_word, voip]` |
| Telephone | `[voip]` |
| HTTP music | `[music]` |
| HTTP music and Sendspin | `[music, sendspin]` |
| Full device without Sendspin | Keep the supplied list and remove `sendspin` |
| Full device without a screen or status LED | Remove `display` and `status_led` |

Keep the features offered by the chosen board/profile. The microphone-only and
speaker-only telephone profiles retain their intentional audio direction; adding
`voice` to one does not turn it into a FULL profile. Voice PE's list selects our
VoIP overlay only: its official firmware continues to own Voice Assistant and
its hardware audio.

A single feature list belongs in the user-facing device YAML. ESPHome merges
lists contributed by different packages by appending them. Do not declare a
second list in another package and expect it to subtract from the first one.
These are compile-time choices, so changing them requires a firmware rebuild.
The profile's chosen AFE, AEC or native audio backend remains selected;
`firmware_features` chooses its application consumers. Choose a different
maintained backend profile when changing the audio-processing architecture,
or use the processor selector where the board provides one, as below.
They do not disable Home Assistant's PBX services or change its feature policy.

## Choose processing and SIP on Waveshare Box V2

The [unified V2 YAML](../yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml)
keeps the choices at the top. The package supplies the board's GPIOs, calibrated
codec/reference, 360x360 interface and application bindings:

```yaml
substitutions:
  audio_processor: aec
  mic_mix: average
  voip_transport: udp
```

Edit these existing values and rebuild. Only the chosen processor is compiled;
there is no runtime switch between AEC and AFE. SIP can use UDP or TCP while
RTP audio remains UDP. Direct ESP calls require matching SIP transports;
Home Assistant can bridge separately negotiated UDP and TCP legs.

| Processing choice | Microphone delivery | Result |
| --- | --- | --- |
| `aec` + `average` | Both physical microphones averaged | One input to standalone AEC, the unified YAML's default |
| `aec` + `passthrough` | First microphone | Existing single-microphone standalone AEC |
| `afe` + `passthrough` | Two separate channels | Dual-microphone AFE with SE/BSS |
| `afe` + `average` | Both microphones averaged | Mono AFE, with `mic_num: 1` and SE/BSS disabled |

Audio Stack owns extraction and optional downmix. AFE receives one or two
microphone channels using its normal contract; it does not need to know whether
a mono signal came from one microphone or a two-microphone average. The playback
reference stays separate. The package configures the matching AFE channel count;
inconsistent manual overrides fail ESPHome's normal configuration validation.
Other application features still come from `firmware_features`.

The older V2 `full-aec.yaml` and `full-afe.yaml` filenames remain compatibility
entrypoints with their previous single-mic AEC and separate dual-mic AFE choices.
Both delegate to the same device implementation. Custom microphone IDs, GPIOs,
source refs and buffer settings can still be overridden in root substitutions.
These selectors currently belong to the V2 device package, not every board.
See the [V2 processing and hardware-check notes](../docs/WAVESHARE_V2_PROCESSING.md) for calibration, build optimization and measured limits.

To inspect all merged packages and substitutions without changing the source:

```sh
esphome config --no-defaults waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml > device-expanded.yaml
```

This is ESPHome's native configuration view. External components and assets
remain referenced; it is not a self-contained firmware distribution. Keep
editing the original YAML, and do not add `--show-secrets` to share a config.
For codec-free stereo microphones and the available acquisition layouts, see
[Audio Stack microphone delivery](https://github.com/n-IA-hane/esphome-audio-stack/blob/dev/docs/AUDIO_TUNING.md#1-choose-how-microphones-reach-the-processor).

## On-device Options

Spotpear Ball v2, Waveshare Box V2 and P4 FULL displays share one Options page.
It is composed with the selected firmware features; no extra includes are
needed in the main device YAML.

On the round displays, the vertical sequence is **Assist -> Contacts -> Dialer
-> Options**. Swipe **from bottom to top** to advance and **from top to bottom**
to go back. The first and last views stop there, without wrapping. Views for
features not compiled in are skipped: without VoIP, the sequence is Assist
and Options; with VoIP but no Voice Assistant, it starts at Contacts. A
display-only profile shows Options alone.

Inside Options, swipes in the body scroll the controls. Navigate from the
fixed header or an exposed area outside the scrolling body. The centered
**Options** header has no Back button. On P4, swipe up from the main screen
to open Options and swipe down to return to that screen.

Audio comes first: master volume, playback volume, microphone gain and mutes.
Master controls the complete speaker output, including calls. Playback volume
is shared by music and announcements. Processing controls follow the chosen
backend: AEC and processing mode, plus NS, AGC and VAD where available. The
dual-microphone AFE profile has no runtime BSS switch. The mode selector retains
the backend's existing restrictions during active Assist or VoIP sessions.

Maintained FULL AEC/AFE profiles start in **SR HIGH** (`sr_high_perf`) and offer
**SR LOW** (`sr_low_cost`). FD modes remain available in the separate VoIP-only
profiles. These processing choices do not replace board-specific microphone
and reference calibration; keep saved gains unless a level measurement calls
for a change. See the [Box V2 controls](../docs/WAVESHARE_V2_PROCESSING.md#physical-buttons)
for the distinction between its programmable button and hardware RESET.

Wake word and Sendspin pairing controls appear only with their features.
The final **VoIP** section contains auto answer, Do not disturb, conference
ringing and video sending where a video pipeline is present. Removing `voip`
also removes this section, its widgets and bindings. Extension and group
configuration remains in Home Assistant. Backend, microphone delivery and SIP
transport are still compile-time choices in the device YAML.

The page reads the existing controls and follows their state notifications.
Opening it does not reset volumes, gains or saved preferences. Options does
not add a periodic polling task or a second audio/telephone state owner.

## Work through a FULL profile

The examples below use the headless
[Waveshare S3 FULL AFE YAML](../yamls/full-experience/single-bus/waveshare-s3-full-afe.yaml).
Keep its board package and other substitutions. Each snippet is a **replacement
for the existing `firmware_features` value**, not another file to include or a
second `substitutions:` block to append. Rebuild and upload after making a choice.

### 1. Remove one optional feature

You use ordinary HTTP music and Assist, but do not use a Sendspin server:

```yaml
firmware_features: [voice, wake_word, voip, music, timers, status_led, diagnostics]
```

Only `sendspin` was removed. HTTP music, announcements, calls and the status ring
remain. Check that ordinary playback and a voice reply still work; Sendspin is
no longer instantiated. There is no separate player or callback to delete.

### 2. Build a voice satellite

For a device dedicated to Assist, start with:

```yaml
firmware_features: [voice, wake_word, status_led, diagnostics]
```

The device keeps microphone processing, wake word and spoken replies. It has no
SIP phone or ordinary music input. Configure its Assist pipeline in Home
Assistant, invoke the assistant and let its reply finish. The status indication
should return to idle and wake-word detection should be ready again.

Add `timers` if you also want the shared voice-timer alarm handling. If you want
a telephone instead, `[voip, status_led, diagnostics]` selects that application;
the separate VoIP-only board profile is the dedicated starting point when you
do not need the FULL runtime. Neither selection changes AFE into lightweight AEC.

### 3. Add telephone calls to the satellite

```yaml
firmware_features: [voice, wake_word, voip, status_led, diagnostics]
```

The package now includes the SIP endpoint, call controls and ringtone handling.
With the device connected to HA, call it from another phone, answer, hang up and
try Assist again. Then originate a call from the ESP. A normal shared phonebook
comes from VoIP Stack; [a conventional PBX](../docs/CLASSIC_PBX.md) needs its own
addressing configuration.

### 4. Add music, timers and supported presentation

```yaml
firmware_features: [voice, wake_word, voip, music, sendspin, timers, status_led, diagnostics]
```

This restores the headless profile's full selection. Music and announcements use
one player with separate pipelines; call audio has its own input to the shared
speaker mixer. Start music quietly, make and end a call, then try a voice reply.
Check the return to the remaining activity, not just the first successful start.
Sendspin also needs a compatible server; including the feature does not create one.

On a display board, begin with **that board's** YAML and keep its `display` entry.
Adding `display` to this headless Waveshare example does not add screen hardware.
Likewise, video needs a P4 video profile and its selected codec, not just a new
word in the feature list. Keep the selected board's flash and OTA limits in mind.

### 5. Customize one setting

First use the board's public substitutions. For example, with `status_led`
selected on the Waveshare profile, add this to the existing substitutions:

```yaml
status_led_name: Desk status light
```

The device still uses the same light and runtime renderer; only its displayed
name changes. The shared LED package supplies the default when you omit this
override. For settings without a public substitution, use normal ESPHome merging
or `!extend` with the existing component ID. Do not copy the whole light, player
or call controller just to change one field.

### 6. Follow a feature to its implementation

When changing behavior or adding hardware, follow the actual includes. For this
example:

1. [The board YAML](../yamls/full-experience/single-bus/waveshare-s3-full-afe.yaml)
   holds your feature list, identity, GPIO substitutions and source references.
2. [The device composition](devices/waveshare-s3-full-afe.yaml) selects the
   [physical board](board/waveshare_s3_audio.yaml) and
   [AFE audio backend](audio/profiles/waveshare_s3_afe.yaml). It binds consumers to
   the board's microphone and speaker inputs.
3. [The application composition](profiles/application.yaml) includes the selected
   features and their shared player/mixer dependencies.
4. [The voice feature](features/voice/main.yaml), for example, includes its
   operation-specific callbacks and controls. The
   [shared runtime](features/shared/runtime.yaml) coordinates overlapping
   activities; the audio and SIP components continue to own their streams/calls.

Use the same path for another board through the [catalog](DEVICE_CATALOG.md).
Only custom hardware needs a new board/audio binding. Custom priorities belong
in the [Runtime Controller guide](https://github.com/n-IA-hane/esphome-runtime-controller#four-terms-used-in-the-yaml);
changing a feature list does not require writing reducer rules.

### How the list becomes a configuration

This uses native ESPHome substitutions, conditional expressions and package
merging. There is no runtime feature dispatcher. For example, the application
composition contains these declarations:

```yaml
substitutions:
  _fw_music: ${ 'music' in firmware_features }
  _fw_sendspin: ${ _fw_music and 'sendspin' in firmware_features }
  _fw_music_package: !include ../features/music/main.yaml
  _fw_sendspin_package: !include ../features/sendspin/main.yaml

packages:
  music: ${ _fw_music_package if _fw_music else {} }
  sendspin: ${ _fw_sendspin_package if _fw_sendspin else {} }
```

These are excerpts from `profiles/application.yaml`; their include paths are
relative to that file. They explain the implementation, not an extra block to
paste into a board YAML. A selected package contributes its configuration;
an unselected one contributes the empty mapping `{}`. ESPHome then validates
the composed configuration and generates the C++ for it.

The music module creates its HTTP source and uses `!extend speaker_media_player`
to add a music pipeline to the shared player. The player may remain after music
is removed because Assist replies still use its announcement pipeline. Similarly,
two or more playback consumers select a mixer, while a single consumer uses the
direct resampler. Shared hardware and the chosen processor remain where needed;
removing one feature does not promise that every related library disappears.

## What each selection includes

| Feature | Included together | Dependency or boundary |
| --- | --- | --- |
| `voice` | Voice Assistant binding, response playback, stop/start coordination and voice controls | The board supplies its microphone and speaker |
| `wake_word` | MWW models, startup/mute handling and detector controls | Requires `voice` |
| `voip` | SIP binding, call controls, phone settings, media input, call callbacks and phone UI attachments | Preserves the selected audio directions and codec |
| `music` | HTTP music source, player input and playback coordination | Shares the physical speaker |
| `sendspin` | Sendspin source and its player binding, plus artwork where the display supports it | Requires `music`; client protocol stays at 0.7.2 |
| `timers` | Voice timer callbacks, alarm source, stop handling and timer UI | Requires `voice` |
| `display` | The board's display/touch/backlight and presentation | Only on profiles with a screen; common idle artwork may also serve music/clock views |
| `status_led` | The actual status light and projection | Only on boards with that hardware; BOX-3's legacy indication uses its display backlight |
| `diagnostics` | Native ESPHome diagnostic entities | Optional, independent of detailed debug logging |
| `sensor_dock` | BOX-3 dock bus and sensors | BOX-3 profiles only, independent of its screen |
| `video` | Camera transmission and the selected SIP video codec | Requires `voip`; P4 renderer profiles also require `display` |

If a dependency is absent, its dependent feature is not instantiated. For example,
`sendspin` without `music`, or `wake_word` without `voice`, contributes no consumer.
Validation prints the requested features and an explicit warning for each such
selection, for example `Firmware feature 'sendspin' is disabled: requires music`.
Add the dependency or remove the unused entry. Unknown names such as `voiip` are
configuration errors, with the supported choices shown by ESPHome.

This check is a Python schema used while ESPHome validates the YAML. It creates no
firmware component, task, entity or runtime state and never enables missing
dependencies. The native package expressions remain responsible for composition.
Use the documented names; the list is not a mechanism for loading arbitrary
component names. Hardware capabilities still come from the chosen board profile.

Announcement playback is a shared dependency of voice, timers and configured
ringtones. Removing music does not remove voice replies. One physical speaker
and one player are created when needed. Two or more playback consumers share the
mixer; a single consumer uses a direct resampler instead of a dummy mixer input.
The original mixer input order, formats, buffers and device IDs are preserved.

FULL profiles use Runtime Controller for competing voice, music, call and timer
activity. Standalone telephone profiles keep their existing direct call callbacks
and do not add a controller merely to operate one phone. Both use the same SIP
component, audio backends and appropriate shared lifecycle helpers.

## Find the implementation

The [device catalog](DEVICE_CATALOG.md) maps every supplied YAML to its entrypoint.
The public entrypoint is `devices/<profile>.yaml`. Its includes show the complete
composition, with these boundaries:

| Directory | Responsibility |
| --- | --- |
| `devices/` | Select a board, an audio backend, consumer parameters and application features |
| `board/` | Pins, buses, codec wiring, networking and optional physical display/dock |
| `audio/profiles/` | Physical capture/output and the AEC, AFE or native audio configuration |
| `profiles/application.yaml` | Resolve the FULL feature selection and shared dependencies |
| `features/voice/`, `wake_word/`, `voip/`, `music/`, `sendspin/`, `timers/` | Complete feature wiring, consumer policies and operation-specific adapters |
| `features/presentation/` | Display/LED projection, shared round content and geometry-specific layouts |
| `features/shared/` | Shared playback, mute, startup and one ordered OTA lifecycle |
| Existing `audio/`, `runtime/`, `lvgl/`, `voip/` and similar directories | Internal primitives and compatibility entrypoints |
| `debug/` | Opt-in development diagnostics, not production prerequisites |

For a new board, copy the closest **device composition**, then adapt its board and
audio binding. Do not copy the reducer, SIP state machine or all feature bodies.
Screen geometry remains with its layout; common round text/animation behavior is
shared. JPEG and H.264 remain separate P4 selections.

The first-generation experimental Waveshare 1.85C FULL file keeps its historical
`full-afe` filename, but its original implementation uses standalone `esp_aec`.
The package is named `waveshare-s3-box-v1-aec.yaml` to make that fact explicit.
Its custom factory-only partition CSV is still required beside the device YAML;
this migration does not silently change its flash layout or add dual-slot OTA.

## Extend and override

Use ordinary ESPHome package merging, `!extend` and `!remove`. Consumer bindings are kept with the device rather than spread across tiny files.
Keep public scalar
substitutions in the device YAML, for example `voip_transport`, the SIP/RTP ports,
processor mode choices and existing buffer settings. Supplied defaults inherit
those overrides; they do not shadow them or append duplicate mode choices.

Hardware IDs such as the board microphone and `hw_speaker` are defined once.
Features extend their selected inputs and controls. Omitting a root component
block is not removal: included packages can still contribute that block. Use the
feature list to remove a complete feature.

One restartable script owns OTA entry and recovery. It stops selected sources in
order, stops the speaker adapter before suspending its Audio Stack hardware,
and pauses LVGL before the asynchronous audio-idle wait. Recovery cancels
pending actions from an earlier phase, including after an interrupted update.
Audio Stack owns deferred hardware teardown and its restart barrier; a YAML wait
is not presented as a barrier inside ESPHome's blocking OTA network transfer. Music and announcements
are separate player pipelines and are stopped separately. Abort/error restores
available infrastructure and wake-word readiness; it does not originate a call
or restart an old music URL.

## Existing include paths

Existing leaf includes remain available for custom configurations. They are
internal building blocks with prerequisites, not another recommended menu of
public features. Do not combine a new device package with an old FULL preset.

| Existing path | Current role |
| --- | --- |
| `presets/full_voice_voip_runtime.yaml` | Legacy full callback bundle; keep its documented prerequisites |
| `presets/voice_runtime.yaml` | Portable legacy voice binding; no sibling checkout is required |
| `runtime/lvgl_ui_projection.yaml` | Compatibility facade for shared phase and call lifecycle projection |
| `lvgl/lvgl_settings_p4_unified_boot.yaml` | Facade for the canonical P4 settings projection |
| `voip/waveshare_p4_touch_videophone_base.yaml` | Legacy P4 telephone base; its caller still selects the video codec |
| Runtime Controller's `packages/runtime_controller/*.yaml` | Compatibility adapters to the canonical application bindings here |
| Retired `voip/ha_phone.yaml`, `ha_integration.yaml`, `ha_actions.yaml` and related paths | Continue to reject obsolete duplicate HA surfaces |

Runtime Controller's `features` setting selects **rules inside the coordinator**.
It is different from `firmware_features`, which composes the firmware's actual
components, controls and presentation. Ordinary device users only need the latter.

## Source identity and validation

Keep package, component and asset references on the intended coordinated revision.
A local development checkout can use `scripts/yaml_paths.sh`; inspect its help and
review the diff before publication. Do not publish machine-local paths. The CI
records component SHAs with `scripts/coordinated_sources.py`, and can replay a
previous manifest through the workflow's `component_manifest` input.

The native matrix in `tests/test_package_composition.py` loads and validates the
real packages, removes features and generates C++ for representative combinations.
It also resolves compatibility entrypoints in an isolated cache layout. The media
contract tests inspect the composed result, not just the shortened device file.
Builds and physical audio tests remain separate requirements.

## Pair Sendspin with Music Assistant

Use ESPHome **2026.10.0 or newer** and Music Assistant **2.10.7 or newer**. Select
both `music` and `sendspin` in `firmware_features`, compile, and upload. The native
ESPHome client handles discovery, encryption and pairing; no separate custom
player or Sendspin clock is installed.

1. Open Music Assistant's player settings and select **Setup** for the speaker.
2. Choose dynamic PIN pairing. If the flow requests confirmation, use the
   Sendspin pairing section in the device's **Options**, or press **Sendspin
   Confirm Pairing** on the ESP device in Home Assistant.
3. Read the temporary code in Options or **Sendspin Pairing Code** in Home
   Assistant and enter it in Music Assistant. It clears after the attempt.
   The cancel button in Options or **Sendspin Cancel Pairing** in Home
   Assistant cancels the pending request.
4. Play a track, stop it, then try a second track. A normal restart preserves
   pairing; it is not repeated for every playback.

The firmware preserves ESPHome's default unpaired-access policy. Music
Assistant also offers an explicit unpaired setup option; use PIN pairing when
associating your own server. To permit only paired servers, add this override:

```yaml
sendspin:
  unpaired_access: false
```

Keep TCP port 8928 and mDNS discovery reachable between the server and speaker.
If the speaker is absent, check network reachability before changing audio
settings. If Setup is missing after an upgrade, refresh Music Assistant and
check its server version. These controls are included only when Sendspin is
selected, including on boards without a display. Keep pairing codes and
credentials out of screenshots and issue logs.
