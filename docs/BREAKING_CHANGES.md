# Breaking changes

Follow the entries between your installed version and the version you are
upgrading to. Each section describes the migration for that release.
For a fresh installation, use the [deployment guide](DEPLOYMENT_GUIDE.md).

> **ESP firmware migration: start from the current YAML for your exact board.**
> Device packages, feature composition and display/runtime bindings have changed
> substantially. Bring your device settings into the new profile instead of
> stacking the new packages onto an old assembled YAML. Rebuild and upload the
> firmware to install ESP-side changes; updating HACS only updates Home Assistant.
> Follow the [device-package migration](#development-firmware-new-device-packages)
> below.

## 2026.11.1 preview: browser media identity

**End active browser calls before the first dashboard refresh after updating.**
That refresh replaces the legacy shared media identity with an identity owned
by the browser window. This one-time change prevents a cloned window from
taking over another window's call audio. Subsequent page reloads reuse the
updated identity and can recover that window's active session.

## 2026.11.1 preview: 4 MB Lite flash layout

**Only the Generic Full Lite 4 MB profile changes its partition table.** Download
[`partitions_4mb_lite_dual_ota.csv`](../yamls/full-experience/single-bus/partitions_4mb_lite_dual_ota.csv)
and keep that filename beside your main YAML. The profile uses ESPHome's standard
`esp32.partitions` setting.

Each OTA application slot is now `0x1e0000` bytes. NVS retains `0x30000` bytes
(192 KiB); OTA metadata and PHY partitions remain present. This redistributes
the former NVS reserve while retaining the profile's voice, wake word, AEC,
VoIP, music, timers and diagnostics.

Migrating a device with the previous stock table requires a **full flash erase
and serial factory upload**. An ordinary OTA update keeps the old table and
cannot perform this migration. Save your YAML, wiring and calibrated control
values first, then restore the desired settings after flashing: the new NVS
offset resets stored preferences. Other device profiles retain their existing
partition layouts.

## 2026.11.1 preview: standard ESP phone services

Maintained telephone packages now enable automatic HA registration. Home
Assistant creates the account/password and delivers them through the existing
ESPHome API. Preserve `api.custom_services: true` and use coordinated `dev`
components when trying this preview. Generic standalone VoIP Stack keeps its
direct SIP default and explicit account configuration.

The LDAP directory replaces JSON pushes for managed phones. Standard SIP MWI
replaces the earlier native `set_message_waiting` API. Remove custom
`ha_integration.message_waiting` declarations; use `registration.message_waiting`
for standard SIP accounts. The lengthy Endpoint sensor is disabled by default;
legacy integrations can explicitly select `ha_integration.endpoint: true`.

Updated display packages replace the contact carousel with a scrolling BLF
list and reusable row pools. The idle mailbox envelope calls the PBX menu;
the in-call keypad sends DTMF. Select `directory`, `blf` and `mwi` independently
in `firmware_features`. Automatic HA registration alone does not enable MWI.
Start from the current board YAML when migrating a custom UI.

Custom call displays should read `get_active_peer_name()` for the current
dialog and use the optional `on_peer_changed` callback to refresh connected
names after pickup or other PBX identity updates. Keep connection setup in
`on_in_call`. Clear the editable dialer on idle and retain any call-ended
notice separately. Maintained packages already include these bindings.
See [connected names after pickup](ESP_PHONE_SERVICES.md#connected-names-after-pickup).

Deleting an originating ESPHome
device/integration now permanently removes its managed credentials and mailbox;
ordinary offline/reload cycles retain them. Read the
[phone-service migration](ESP_PHONE_SERVICES.md#updating-an-existing-installation).

## Development firmware: native audio providers

**Start from the updated YAML for your exact board when rebuilding this
preview.** Keep your device identity, credentials, wiring and wanted features.
The maintained device packages now use official ESPHome 2026.10 audio, mixer,
resampler, `speaker_source`, Voice Assistant, Micro Wake Word and OTA providers.
Audio Stack/AEC/AFE and VoIP Stack remain project-owned. FULL profiles also use
Runtime Controller; standalone VoIP-only profiles use VoIP Stack callbacks.

For a custom configuration:

1. Remove `audio`, `micro_wake_word`, `mixer`, `ota`, `resampler`,
   `speaker_source` and `voice_assistant` from Git external-component lists
   pointing to this project or Audio Stack. Keep the actual component blocks;
   ESPHome supplies those providers. Remove an empty external-source entry.
2. Remove the retired `audio_http_compat`, `sendspin_compat` and
   `audio_dsp_iram` component declarations and their external-source entries.
   Keep native `audio_http`, WAV/MP3/FLAC codecs, Sendspin, microphone and
   speaker configuration selected by the maintained packages.
3. Remove `tts_playback_start_timeout`, MWW `task_core`/`ring_buffer_duration`
   and `task_core`/`task_priority` on `platform: resampler` speakers. These
   were local options and are absent from the supported official schemas.
   Task settings on our AFE/Audio Stack components are separate and remain
   supported. Native scheduling and playback timeout behavior now apply.
4. Keep package and component revisions coordinated, validate with ESPHome
   2026.10.0 or newer, compile and upload. Updating HACS does not update the ESP.

Audio processing, microphone downmix/BSS, gain/reference settings, media and
announcement inputs, call ownership and the shared maintenance scripts remain
in their existing owners. The AFE GMF vendor pin and P4 display adaptation are
separate from this audio-provider retirement.

The Box FULL AFE baseline has been exercised with calls, overlapping HTTP
media/announcements, real HA announcements, ordinary Stop/restart, native
Sendspin and completed/interrupted OTA. This is bounded functional evidence,
not a long-uptime result for every supported board. The outstanding Spotpear
announcement report remains open; preserve its complete logs and exact firmware
revision if it recurs.

## Development firmware: processing modes and microphone gain

Maintained FULL AEC/AFE profiles now start in `sr_high_perf` and offer
`sr_low_cost` as the alternative. The separate VoIP-only profiles retain their
FD/VoIP choices. If a copied FULL YAML overrides the initial mode or selector
options with FD values, remove that override or replace it with the intended
SR mode when adopting the current preset. The DSP and selector use the same
initial setting, including a restored SR choice where restoration is enabled.

Audio Stack's positive **Mic Gain** is now a constant post-processor gain
instead of an automatic ALC. Existing saved values are retained, but weak
speech following quiet input can arrive at a higher level than in older
firmware. Check restored positive gains with speech and playback, and reduce
the value if the output clips. A 0-dB setting adds no software boost.
Hardware PGA and echo-reference calibration remain separate controls; keep the
values supplied for your board instead of copying another device's tuning.

See [Audio Stack tuning](https://github.com/n-IA-hane/esphome-audio-stack/blob/main/docs/AUDIO_TUNING.md)
and the [Box V2 calibration](WAVESHARE_V2_PROCESSING.md#one-physical-calibration-for-this-board).

## Development firmware: ESPHome 2026.10 and music pairing

Upgrade the builder to **ESPHome 2026.10.0 or newer** before compiling the current
device packages or Audio Stack. Support for the 2026.9 core is retired; do not
combine old and new copies of audio, mixer or speaker-source providers.

Sendspin now uses the protocol library selected by ESPHome 2026.10 (0.9.3).
Update Music Assistant to **2.10.7 or newer** first. After the firmware upgrade,
complete **Setup** for each speaker in Music Assistant and use its native PIN
pairing flow. The device exposes **Sendspin Confirm Pairing**, **Sendspin Cancel
Pairing** and **Sendspin Pairing Code** through HA. The default native unpaired
access policy is unchanged; pairing does not require disabling encryption or
using a shared hardcoded PIN. For paired-server-only access add
`sendspin: {unpaired_access: false}` to your configuration.

See [the pairing steps](../packages/README.md#pair-sendspin-with-music-assistant).
A normal reboot retains the native device identity and pairing. Flash erasure
or a factory reset can require a new pairing; updating HACS alone does not
replace ESP firmware.

## Development firmware: retired speaker fork

The local `speaker` provider, including its legacy media player, has been
removed. Maintained profiles use ESPHome's native speaker interface and the
source-based `speaker_source` player. The Spotpear phone-only ringtone also
uses the local-file source now, keeping its existing sound, volume and call
state controls.

For custom YAMLs:

1. Remove `speaker` from any `external_components` entry pointing to this
   repository. Keep other components in that entry, or remove the entry if
   its component list becomes empty. Keep the actual `speaker:` hardware,
   mixer and resampler configuration.
2. Remove the fork-only `pause_releases_pipeline` option. ESPHome's native
   `platform: speaker` media player remains available, but its normal pause
   behavior keeps the playback pipeline. The removed pause-release behavior
   is not silently reproduced by the native player.
3. To adopt the maintained source-based path, start from the current board
   profile and its player/source packages. Do not only rename the platform:
   local files are `audio_file` assets played through `audio-file://` URLs,
   and sources belong to the corresponding player pipeline.

This removes a copied ESPHome component, not ESP Audio Stack or echo
cancellation. Updating HA alone does not change installed ESP firmware.

## Development firmware: new device packages

**For this firmware update, start again from the current YAML for your exact
board and carry over your settings.** The shared packages have been reorganized:
maintained profiles now select one `packages/devices/` entrypoint and one
`firmware_features` list. Updating a handful of includes in an old FULL YAML can
leave duplicate players, controls or callbacks alongside the new composition.

This is a firmware configuration migration, separate from updating VoIP Stack
through HACS. It does not require deleting the HA integration, its phones or
your working device. The 2026.11.0 firmware uses coordinated packages and
component sources from `main`. Use the source references published with your
selected release.

### Choose FULL or standalone VoIP-only

Start from `yamls/full-experience/` for Assist, wake word, music and telephone
features coordinated by Runtime Controller. For a standalone telephone, use the
matching entrypoint under `yamls/voip-only/`. Those profiles use VoIP Stack's
native callbacks for phone state, optional ringtone, LEDs and display controls.
They do not include Runtime Controller.

Changing a FULL profile to `firmware_features: [voip]` changes its selected
features while retaining the FULL architecture. Choose a VoIP-only entrypoint
when you want the standalone phone architecture.

For generic ESP32-S3 devices with a native DVP JPEG camera:

| Profile | Entrypoint | Application lifecycle |
| --- | --- | --- |
| FULL Audio + Video | [`generic-s3-full-aec-camera.yaml`](../yamls/full-experience/single-bus/generic-s3-full-aec-camera.yaml) | Runtime Controller with the selected Assist, media and phone features |
| VoIP-only + camera | [`generic-s3-voip-camera.yaml`](../yamls/voip-only/single-bus/generic-s3-voip-camera.yaml) | VoIP Stack callbacks, without Runtime Controller |

Both examples expose board memory, microphone/amplifier wiring and camera GPIOs
through substitutions. The reference wiring is for Freenove FNK0085 N8R8; select
values for your actual board. S3 supports camera video transmission, without
rendering the remote video on an S3 display. Supported P4 video profiles retain
camera transmission and remote-video rendering. Audio is bidirectional when
the device includes both a microphone and a speaker.

### Migrate a copied device YAML

1. Keep your old YAML and secrets for comparison. Select the current equivalent
   from the [device catalog](../packages/DEVICE_CATALOG.md), including the correct
   board revision, AEC/AFE/native backend and, for P4 video, JPEG or H.264 profile.
2. Transfer your existing device `name`, friendly name, Wi-Fi/network settings,
   API encryption, OTA authentication and secret references. Keep credentials in
   your private `secrets.yaml`. Preserve custom GPIOs only where they match your
   actual wiring; do not copy a different board's pin map.
3. Edit the new YAML's **existing** `firmware_features` list. Preserve your intended
   choices, such as no Sendspin or no display. Do not add another list in an
   overlay: ESPHome concatenates package lists. Feature selection does not
   change the chosen AFE/AEC/native backend.
4. Transfer custom settings and automations individually, using substitutions
   or extensions of the existing IDs. Keep wanted call settings, static contacts,
   wake-word models, gain/volume preferences and UI customizations. Compare each
   with the new package before adding it; its equivalent may already be supplied.
5. Keep **one device package**. Do not copy the old FULL preset, entire player,
   mixer or repeated voice/call callbacks on top of it. Leaf includes remain for
   legacy/custom builds with their documented prerequisites, not as additional
   full presets for a migrated device.
6. Keep package, component and asset references coordinated. Validate and compile
   the result with ESPHome, check image size against the actual OTA slot, then
   install it. A changed partition layout needs an appropriate full serial flash;
   an ordinary OTA application update does not migrate partitions.
7. Check the device's selected functions: call in and out, hang up and redial,
   invoke Assist, play and stop media, and check the display/LED after each action.
   Test only functions your profile includes, then their intended combinations.

Use only the selectors exposed by the chosen example. If it provides
`audio_processor`, `mic_mix` or `voip_transport`, edit those substitutions rather
than adding another audio backend or transport package. Fixed-backend profiles
retain their stated backend. Spotpear and BOX V2 FULL displays now navigate
vertically through Assist, Contacts, Keypad and Options, omitting unavailable
pages and stopping at the ends; the Options body scrolls independently of page
navigation. The experimental first-generation round layout retains its own
navigation adapter.

To inspect the resolved configuration with its packages expanded, run:

```sh
esphome config your-device.yaml
```

This recommendation does not mean that every existing leaf include was removed.
It gives a clean starting point for the new composition while preserving your
device identity and preferences. The [progressive examples](../packages/README.md#work-through-a-full-profile)
show small feature changes before custom board or runtime work. The Generic
4 MB Full Lite default still excludes Sendspin; historical build sizes are not
guarantees for the new libraries.

## 2026.10.3-dev: PBX functions available by default

Global parking and voicemail enable switches have been removed. Reconfigure
now offers General settings, Call parking and Voicemail voice separately. Basic
installation needs no TTS provider. Existing custom numbers, timers, media,
voices, mailbox settings and recordings are preserved. No mailbox is created
and no forwarding rule is added by the migration. Per-phone mailbox removal
remains an explicit destructive operation.

This deliberately changes a previous global `enabled: false`: those application
switches are removed rather than preserved as hidden disable flags. Application
availability does not create mailboxes or start call recording. Review feature
codes and each phone's resource/routing settings when migrating an installation
that previously disabled parking or voicemail globally.

Parking defaults to access 700 and slots 701-720. Previously disabled feature
codes become available where they do not conflict with existing destinations.
Conflicting routes are preserved and reported in Repairs; correct the feature
configuration or the conflicting destination before using those codes. Existing
automation service names and entity identities are unchanged.

## 2026.10.3-dev: disabling a phone's mailbox deletes it

**Enable mailbox** now controls whether that phone has a mailbox. Explicitly
turning it off from the card, phone configuration or `set_voicemail_settings`
action permanently deletes its messages, files, PIN and mailbox settings.
Turning it on again creates an empty mailbox. Omitted action fields keep their
current values. Updating, restarting or taking a phone offline does not delete
mailboxes, including mailboxes disabled by an older build. Separate conversation
recordings and other phones' mailboxes are preserved.

## PBX development branch: separate SIP trunk entries

The updated `v2026.11.0` preview changes where trunks are configured.
Older builds with the same version label used the former single-trunk flow.

Previously, a single trunk was configured in the main VoIP Stack setup or
reconfiguration flow. Now open **Settings > Devices & services > VoIP Stack**,
then the integration's **three-dot menu > Add SIP trunk**. Each line has a named
subentry, a device and a readiness entity. Edit an existing line using the
configuration control beside that trunk. Do not create a template entity.

- An enabled legacy trunk is migrated automatically to a **SIP trunk** entry,
  retaining its connection settings, credentials, incoming routing and outgoing
  default. A disabled, unused legacy trunk is not turned into a new active line.
- Do not delete the integration or recreate the working trunk to migrate it.
- Check the migrated line's readiness entity and receiving destination. If your
  dashboards or automations refer to the former trunk status entity, select the
  current entity on the named trunk device; do not assume its entity ID.
- The first newly added line becomes the outgoing default. With several lines,
  a call's explicit choice takes precedence over the phone preference and then
  the integration default.
- Deleting the default leaves no default selected. HA Repairs asks you to choose
  one; the integration does not silently switch to another provider.
- Reload dashboard pages or reopen Companion after updating to load the matching
  card code. Updating HA does not update ESP firmware.

The [illustrated walkthrough](PBX_WORKFLOWS.md) explains voicemail, phone routing,
parking, pickup and recording alongside the new trunk setup.

## 2026.10.3-dev: implicit automation call selection

Outgoing Automation sequences no longer require response variables or call IDs.
Speak, Play audio and Hang up select the unique outgoing call owned by that HA
execution. Explicit call references remain supported.

For safety, Hang up without a phone or call now requires either a native VoIP
trigger or that execution's unique outgoing Automation call. It does not fall
back to the preferred phone. If an existing manual or unrelated automation
relied on that fallback, select its intended phone in the action.

## 2026.10.3-dev: native Companion prototype archived

The experimental dashboard-menu composer and native Companion phone endpoints
are no longer included. Existing experimental Companion phone subentries are
removed during migration. Official mobile_app registrations and notifications,
normal dashboard phones, SIP accounts and ESPHome devices are preserved.
Update any automations that explicitly targeted a retired native-app phone.

The prototype is preserved on the
[`archive/companion-native-calls-prototype-20261004` branch](https://github.com/n-IA-hane/esphome-intercom/tree/archive/companion-native-calls-prototype-20261004).
It is historical design material, not a supported installation. Future work
will depend on the official Core, Companion and frontend APIs. Reload the
dashboard or restart the Companion app after updating to discard the old menu.

## 2026.10.1: coordinated firmware sources

The HA update does not require an ESP firmware update. When adopting the new
maintained firmware YAMLs, update their component sources together: ESP VoIP
Stack 2026.10.1, Runtime Controller 2026.10.1 and Audio Stack 2026.10.2.
They are available from the corresponding `main` branches.

Shared packages use the new `voip_stack.is_active` condition, so combining them
with an older ESP VoIP component fails validation. The Waveshare 1.85C-BOX V2
FULL profile also requires the included experimental MWW core-affinity component
and the new AFE output path. Start from its maintained V2 YAML.

ESPHome 2026.9.0 remains the minimum; newer releases are not excluded.

## 2026.10.0: coordinated update and call automations

HA and ESP firmware do not need matching version numbers for this release.
Installing the HACS update does not by itself require reflashing working ESPs.
We recommend rebuilding from an updated maintained YAML, particularly for
Generic S3 devices, to receive the scheduling and package improvements.

When rebuilding the updated profiles, use ESP VoIP Stack 2026.10.0, Runtime
Controller 2026.10.0 and Audio Stack 2026.10.1. The new Audio Diagnostics button
requires the latter; Audio Stack 2026.10.0 does not expose that action. Update
component sources together when adopting new shared firmware packages.

Home Assistant 2026.7.0 or newer is required for the native automation interface.
ESPHome 2026.9.0 is the firmware minimum, not an exact-version pin.

Phonebook as dialplan remains the default. Automations as dialplan adds overrides;
users do not need automations to make ordinary calls. Existing event/state rules
remain available. When migrating a rule to a native VoIP trigger, disable its old
equivalent to avoid handling the same call twice. Use the
[cookbook migration instructions](AUTOMATION_DIALPLAN.md#move-existing-automations-gradually).

Integration setup migrates supported stored settings and contacts. It cannot
rewrite copied firmware YAMLs or user-authored automations. Back up those files
and your HA configuration before updating. A previous HACS archive alone does not
undo a config-entry migration; restore the matching HA backup if rolling back.

## 2026.10.0: modular runtime controller packages

Requires ESPHome 2026.9.0 or newer. Update the firmware packages from the Intercom
repository together with the Runtime Controller component, and review the
[package guide](../packages/README.md). This does not require matching HA and ESP
firmware version numbers.

- VA controls no longer include telephone buttons. Add `voip/call_buttons.yaml`
  when desired; the full preset includes them.
- MWW callbacks and controls are separate from VA. Full presets select both.
- Native media/Wi-Fi/mute listeners replace redundant state-forwarding callbacks.
  Unsupported `observe.voice_assistant` and `observe.micro_wake_word` keys must be
  removed; shared official callbacks provide those integrations.
- `runtime/ha_connectivity.yaml` no longer exposes diagnostic API actions. Add
  `diagnostics/runtime.yaml` explicitly when needed.
- Sendspin now uses the official ESPHome provider. Custom inline Sendspin
  configurations need no compatibility adapter; use `media_player/sendspin.yaml`
  for the shared configuration. The legacy `media_player/sendspin_compat.yaml`
  include remains harmless but can be removed.
- HTTP playback, local-file announcements and Sendspin can be selected separately.
  Existing full media presets still compose the complete feature set.
- Shared LVGL projection owns the voice phase and restores the mode preceding a
  call. Do not duplicate phase assignments in TTS content callbacks.

The reducer updates all bound state values before output callbacks run.
An event sent from inside a callback is processed after that callback returns;
custom code must not expect the second event's result immediately.

For a device without VoIP, LED or display, use the base runtime package and
only the adapters it needs. The full preset deliberately includes all its
bindings. See the [runtime package selection guide](https://github.com/n-IA-hane/esphome-runtime-controller/blob/main/MIGRATION.md#choose-packages).


Read every section newer than the stable version currently installed before
upgrading. VoIP Stack is maintained by one person and may deliberately replace
an earlier development contract instead of carrying two parallel APIs. The
config-entry migration preserves supported persisted settings, but copied card
YAML and automations cannot be migrated by Home Assistant automatically.

## 2026.10.0: Audio Stack dependency and TDM layout

Audio Stack now uses Espressif `esp_codec_dev` **2.0.0-beta5**. This is an
upstream prerelease selected by the component. If your custom build overrides
the codec library with a 1.x version, remove that override before rebuilding;
the backend now uses the 2.x API. Maintained YAMLs require no manual dependency
changes. Rebuild and upload firmware to receive the update.

The TDM memory optimization does **not** require renumbering YAML slots:
`tdm_mic_slots`, `tdm_ref_slot`, `tdm_tx_slot` and diagnostic slot-level sensors
still refer to physical bus positions. Keep `tdm_total_slots` matched to the
hardware frame, even when only some slots are selected for capture or playback.

An explicit AFE `input_format: MMNR` remains valid. Its `N` is processor
padding, not an extra microphone or a requirement to capture an unused bus
slot. Omitting the override selects `MR` for one microphone and `MMR` for two.
The public ESPHome microphone and speaker interfaces are unchanged.

See the [Audio Stack upgrade notes](https://github.com/n-IA-hane/esphome-audio-stack/blob/main/README.md#upgrading-to-2026100)
for details. These audio changes do not require adding VoIP or declaring an
unused speaker or microphone.

## 2026.10.0: native ESP phone integration

VoIP discovery, call actions and phonebook delivery are now built into the ESP
component. Start from an updated [maintained YAML](../yamls), or comment out
or remove the old VoIP HA package entries before compiling, and enable
`custom_services: true` in the existing `api:` block. Old package paths fail
configuration validation with an explanatory message.

Do not remove hardware, audio, display or ringtone packages. Full profiles
retain their runtime connectivity package. The [migration guide](ESP_ENTITY_SURFACE.md)
lists the retired packages, replacement configuration and how to preserve your
routing preference. Existing entity and action names are retained.

## 2026.10.0: ESPHome 2026.9.0 for maintained firmware profiles

Update ESPHome to at least **2026.9.0** before compiling the current project
YAMLs. Updating only the Home Assistant integration does not require an
immediate firmware rebuild.

For copied custom YAMLs, remove `spi` and `audio_http` from external-component
lists that point to this repository. Both now come from ESPHome. Keep
`persistent_ring_buffer` settings unchanged. Where the old SPI fork was used
for the Spotpear display, set `psram_dma: true` on the `mipi_spi` display itself.

Include `packages/audio/http_media_codecs.yaml` for HTTP playback. It selects
MP3/WAV codecs. Current development packages use the official HTTP source
and decoder behavior; the former `audio_http_compat` adapter is retired.
The retired `audio_http` directory is no longer an external component source.

## 2026.9.0: upgrade checklist

If you use only the standard HACS integration and its visual card editor, the
upgrade is mostly automatic. The manual steps below apply when you copied an
ESPHome YAML, wrote card YAML by hand, created VoIP Stack automations or built a
custom frontend against the development API.

1. Update VoIP Stack through HACS and restart Home Assistant.
2. Open Reconfigure once and review trunk, incoming-routing and video options.
3. Open every `ha_softphone` card and confirm its selected Home Assistant phone
   Device. Clear the browser or Companion app cache after the update.
4. Rebuild ESPHome phones from the current YAMLs. The maintained YAMLs now use
   the stable `main` branches of every coordinated component repository.
5. Apply the relevant migration sections below if upgrading directly from
   `2026.8.0`.

Home Assistant migrates supported integration settings automatically. It cannot
rewrite files and configuration that live outside the integration, including a
copied P4 YAML, hand-written dashboard YAML, automations and custom frontend
code. Those are the items described below.

## 2026.9.0: P4 full profile renamed for SIP video

The P4 full landscape profile now includes bidirectional SIP video over JPEG,
in addition to AFE, micro wake word, Voice Assistant, media playback and its
native Home Assistant camera entity. The filename now identifies it as a
videophone.

Replace:

```text
yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-landscape.yaml
```

with:

```text
yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml
```

The ESPHome node name and entity identities are unchanged. Only the example
YAML filename and references to it must be updated. If your own YAML imports
the old file, change that import to the new filename. You do not need to delete
the ESPHome device or recreate its Home Assistant entities.

## 2026.9.0: phone actions and cards use device identifiers only

This section matters only if you wrote card YAML, automations or custom frontend
code by hand. The visual card editor writes the new format for you.

Each Home Assistant phone is now selected by its real Home Assistant Device ID.
The old synthetic selector `__voip_stack_ha_softphone__` and the old card field
`endpoint_id` are no longer accepted as configuration.

For every `ha_softphone` card, open the card editor and select the Home
Assistant phone that the card represents. Saving the editor replaces the old
selector with `device_id`. If no phone is selected, VoIP Stack uses the
preferred Home Assistant phone configured in the integration, or the only
compatible phone when there is no ambiguity.

Update copied automations so `device_id` contains the real Home Assistant
Device Registry ID of the local phone. `endpoint_id` remains visible in call
state and WebSocket snapshots for internal session and media correlation, but
it is not accepted as user configuration.

If an automation only starts a call and does not read the action response, no
response change is required. If it reads that response, use schema version 2:

- read the selected phone from `phone.device_id`, `phone.kind` and
  `phone.name`;
- read the call from `call.call_id`, `call.state` and `call.destination`.

The duplicate top-level response fields `device_id`, `endpoint_id`, `call_id`
and `destination` were removed.

ESPHome phones controlled from Home Assistant must expose the explicit
`start_call`, `answer_call`, `decline_call` and `hangup_call` native API
actions provided by the current `packages/voip/ha_phone.yaml`. VoIP Stack no
longer treats call/decline buttons as remote-control actions and no longer
uses `decline_call` as a substitute for hangup. Rebuild and flash custom
firmware that included only the older entity package.

The remaining changes in this section apply only to custom frontends, not to
the bundled card. Custom frontends must use the current scoped
`voip_stack/subscribe_ha_softphone` WebSocket request. The card no longer
retries the older unscoped subscription when the server rejects that request.

Video WebSocket negotiation now exposes codec parameters only inside the
directional `send` and `receive` objects. Custom clients must stop reading the
former flat `codec`, `encoding`, `clock_rate`, `payload_type` and `fmtp`
aliases from the negotiation root.

Softphone state events must include `endpoint_id` or `device_id`. The frontend
no longer guesses a phone for an endpoint-less event from the subscription
that delivered it.

The historical Home Assistant phone now uses the same translated, Device-owned
call-state entity as every other browser phone. Existing installations retain
their Entity Registry ID, which may still be
`sensor.voip_stack_call_state`; new installations receive an ID from Home
Assistant. Select the `Call state` entity from the phone Device instead of
constructing or hardcoding its entity ID.

## 2026.8.0: upgrade checklist

1. Read the `2026.8.0` sections below and the
   [`2026.8.0` release notes](https://github.com/n-IA-hane/esphome-intercom/releases/tag/v2026.8.0).
2. Update VoIP Stack, restart Home Assistant and open **Reconfigure** once.
   Confirm the incoming-routing, Assist and SIP-video choices.
3. Open every additional phone under **VoIP Stack > Add phone** and verify its
   extension, groups, DND and video options.
4. Update copied automations to the service and routing contracts below.
5. Hard-refresh every browser or Companion WebView that loads the Lovelace
   card. An old JavaScript module cannot safely drive the new backend.

Do not delete and recreate the integration merely to perform this upgrade.
Config entry version 3 migrated the historical Home Assistant phone and its
persisted DND, extension and group settings into a phone subentry. Version 5
removes its special default behavior. A new installation creates one ordinary
browser phone named from the Home Assistant location, and that phone can be
renamed or removed like any other. Existing local SIP registrar accounts are
also converted into phone subentries.

## 2026.8.0: every logical phone is a separate Home Assistant device

The former single HA softphone model is now a collection of native config
subentries and Devices. Browser phones and standard SIP accounts are created with
**Settings > Devices & services > VoIP Stack > Add phone**.

Each phone owns its call state, DND, extension, groups and video settings.
Standard SIP accounts additionally own their unregistered-endpoint policy.
Consequently:

- bind each `ha_softphone` card to the intended phone Device;
- trigger room-specific automations from that phone's own call-state Sensor or
  call Event Entity;
- do not assume that the aggregate `event.voip_stack_call` identifies one
  particular room phone;
- do not use browser-card presence as the definition of whether a logical
  phone exists. An enabled browser phone remains available without an attached
  card and can ring logically so that timeout and forwarding automations run.

Every phone receives its Device-owned entity IDs from Home Assistant. Select
entities from the Device instead of constructing an ID from the phone name.

Card YAML contains only `device_id`. The backend supplies the internal
`endpoint_id` in runtime snapshots when the frontend needs to correlate media
with a call.

## 2026.8.0: one Home Assistant call-action vocabulary

Home Assistant actions now use `destination` as the only call destination
field. Replace `target:` or `call:` inside `voip_stack.call`,
`voip_stack.forward` and `voip_stack.route` action data with `destination:`.
Phone actions now use `device_id` as their only optional phone selector;
remove `endpoint_id`, `entity_id`, `source`, `source_device_id`, `source_name`,
`name` and `friendly_name` from action data. Internal endpoint/entity IDs
remain available in state and events for correlation.

Before:

```yaml
- action: voip_stack.call
  data:
    target: Kitchen
```

After:

```yaml
- action: voip_stack.call
  data:
    destination: Kitchen
```

`destination` is resolved by the central phonebook; no Device ID is needed to
identify Kitchen. The optional `device_id` selects the **local calling phone**,
not the remote destination. For example, add the Device ID of a Reception
browser phone when Reception must place the call:

```yaml
- action: voip_stack.call
  data:
    destination: Kitchen
    device_id: <reception_phone_device_id>
```

Omit `device_id` only when a preferred phone is configured or exactly one
compatible phone exists. An ambiguous request fails explicitly. This same
local-phone selector rule applies to `answer`, `decline`, `hangup`, `forward`,
`set_dnd` and `set_ha_softphone_settings`. `call_id` remains an optional flat
action field when one of several concurrent calls must be selected;
**Advanced options** is only how the automation editor groups that field
visually.

The duplicate `voip_stack.export_accounts` action was removed; use the
identical `voip_stack.list_accounts` response action. These development-only
aliases were removed before the stable release so the automation editor and
API expose one predictable path. The development-only
`voip_stack/ha_softphone_start` WebSocket command was also removed: cards and
clients must use the standard Home Assistant `call_service` command with the
`voip_stack.call` action.

## 2026.8.0: incoming trunk routing is explicit

Reconfigure the integration and choose one incoming-routing mode:

- **Route immediately** sends the call to the configured fallback destination
  without pre-answering it for digit collection.
- **Collect extension with DTMF** pre-answers the trunk leg and collects
  negotiated RFC 4733 `telephone-event` or compatible SIP INFO digits. An
  explicit valid extension is authoritative. An unknown explicit extension
  fails instead of silently ringing the fallback.

Existing entries migrate without changing their effective mode: an enabled,
non-zero legacy DTMF timeout becomes DTMF mode; every other configuration
becomes Direct mode. Experimental automation overrides are always disabled by
the migration and must be enabled deliberately.

When automation routing is enabled, `route_requested` is a bounded initial
decision point. Use:

```yaml
- action: voip_stack.select_inbound_destination
  data:
    destination: Home ring group
```

Do not use `voip_stack.forward` for that initial decision. `forward` moves an
already delivered ringing or connected HA-owned call. `voip_stack.route` is
the advanced low-level decision action and is not the ordinary phonebook
route.

In Direct mode the decision occurs before the fallback. In DTMF mode it occurs
only when no digits were entered. If no automation acts within the window, the
configured fallback remains authoritative.

Routing a group now re-enters the canonical PBX dispatcher: eligible members
ring in parallel, the first answer wins, losing legs are cancelled and the
originating endpoint is excluded when it belongs to the destination group.
Automations that previously expanded a group themselves should select the
group name instead.

## 2026.8.0: use phone-scoped state for phone-scoped automations

`event.voip_stack_call` is the aggregate PBX-wide Event Entity and is the
correct trigger for `route_requested`. Each integration-owned phone also has a
scoped call Event Entity and durable call-state Sensor. Use the scoped Sensor
for rules such as "Casa has been ringing for 30 seconds": the selected entity,
not the word `ringing`, determines which phone owns the automation.

Call state and event attributes now expose:

- `direction`: `incoming` or `outgoing` from the selected phone's perspective;
- `ingress` and `origin`: `trunk` for a provider/PBX call or `extension` for a
  call originating from a local ESP, browser phone or registered SIP endpoint;
- `scope`: internal state ownership, not the transport source.

A phone in `ringing` is already receiving a call, so an additional
`direction: incoming` condition is redundant. Use an `ingress: trunk`
condition when a no-answer rule must apply only to external PBX/provider
calls. Replace route-source filters based on `scope` or internal owner IDs with
`ingress`.

## 2026.8.0: SIP account secrets are returned, not broadcast

Local-account and phonebook export actions are administrator-only when invoked
by an authenticated user. Internal Home Assistant automations remain allowed
where documented.

`voip_stack.create_account` no longer publishes a generated password in a
persistent notification or call event. When Home Assistant generates the
password, it is returned once in that administrator action response. A
user-supplied password is preserved but never echoed. Likewise,
`voip_stack.rotate_account_password` returns the replacement once, and
`voip_stack.list_accounts` returns account metadata without passwords.

If an old script waited for `sip_account_created`,
`sip_account_password_rotated`, `list_accounts` or `export_accounts` call
events, replace that flow with an action response. Save a generated or rotated
password immediately; it cannot be recovered later and must instead be
rotated again.

## 2026.8.0: optional ESPHome entities are explicit platforms

The ESPHome `voip_stack` core no longer accepts `auto_entities`. ESPHome's
upstream contribution rules prohibit using `AUTO_LOAD` for primary entity
platforms such as `switch`, `number`, `button`, `text` and `text_sensor`.
Declare the required `platform: voip_stack` entities explicitly, or include
`packages/voip/auto_entities.yaml` in maintained full-duplex configurations.
Configurations that do not need those entities now compile without declaring
empty platform sections.

## 2026.7.1: contract updates

The stable `2026.7.0` migration below remains the main breaking change.
`2026.7.1` additionally makes several previously implicit
contracts explicit. Review them before upgrading an older custom YAML,
automation, SIP client or card fork.

- **ESP contact schema.** Structured ESP `static_contacts` and
  `voip_stack.add_contact` entries use `name`, optional `ip`, `port`,
  `rtp_port` and `transport`. `address`, `sip_uri`, `extension`, `number`,
  groups and media metadata are HA central-phonebook fields.
- **No inbound phonebook allowlist.** An unknown or unregistered SIP peer with
  network reachability can call HA or an ESP. Normal destination, busy/DND and
  SDP checks still apply. Enforce caller admission at a firewall, VLAN, VPN or
  SBC boundary when required.
- **Constrained in-dialog renegotiation.** ESP endpoints still return `488 Not
  Acceptable Here` for media-changing re-INVITEs. Direct HA browser dialogs
  accept compatible peer-initiated UPDATE/re-INVITE changes, including
  hold/resume, RTP endpoint changes and video add/remove. An audio-only
  SIP-to-SIP bridge may add video through a paired destination re-INVITE and a
  direct or transcoded relay. HA does not initiate an unsolicited media change.
  A rejected offer leaves the previous media/dialog active.
- **DTMF route input.** The trunk digit router accepts RTP `telephone-event`
  and compatible legacy SIP INFO DTMF. It does not decode acoustic in-band tones from
  the call audio.
- **Processor bypass semantics.** When the configured audio processor is
  enabled, unavailable processed output fails closed to silence. Disabling the
  parent AEC switch explicitly publishes converted raw microphone audio on the
  same public microphone surface.
- **Software-AEC reference lifecycle.** Maintained generic single- and dual-bus
  profiles now default to `aec_reference: previous_frame`. The optional
  `ring_buffer` mode remains supported, but its reference is session-scoped: it
  is cleared when the first microphone consumer starts and when the last one
  stops, and it is not filled while no microphone consumer exists. Custom code
  must not treat old speaker samples as a reference for a later call.
- **Bounded reentrant runtime events.** Runtime-controller events/actions
  created during a drain stay queued for the next main-loop turn. Custom
  automations must not depend on an unbounded synchronous self-trigger chain.
- **Plaintext local media boundary.** ESP SIP is UDP/TCP without TLS and RTP is
  UDP without SRTP. Do not expose an ESP listener directly to an untrusted
  network.

The full change and validation summary is in the
[`2026.7.1` release](https://github.com/n-IA-hane/esphome-intercom/releases/tag/v2026.7.1).

## 2026.7.0: ESPHome devices are SIP phones now

This release is the SIP/VoIP migration. It is not a small protocol tweak and it
is intentionally not backward compatible with the retired project-specific
call-control path.

The practical change:

- ESP `voip_stack` devices are now SIP phones.
- Home Assistant `voip_stack` is now a SIP softphone, dial-plan authority,
  SIP router/B2BUA, RTP bridge/resampler and optional SIP trunk client.
- The Home Assistant integration domain and services are `voip_stack.*`.
  Existing automations that used older development names must be updated.
- Older development installs used the Home Assistant domains `intercom_native`
  or `homeassistant_voip_stack`. The HACS repository now exposes only
  `custom_components/voip_stack`; remove old test config entries and stale
  old-domain folders before adding the current VoIP Stack integration.
- Home Assistant no longer exposes local SIP transport toggles. Configure ports
  and optional features, not listener modes.
- VoIP Stack supports Home Assistant's native Reconfigure action. You no longer
  need to remove and re-add the integration to change ports, debug mode,
  local SIP registrar support, Assist intents or optional trunk settings.
- Standard softphones can register to Home Assistant as local SIP accounts and
  become phonebook contacts.
- Local SIP account services now describe generic SIP endpoint accounts, not
  only "softphones". Real VoIP phones, ATAs, baresip, pjsua, Zoiper and Linphone
  all use the same account model.
- Home Assistant can register one provider/PBX trunk, so ESPs and the HA
  softphone can make and receive external calls without requiring Asterisk next
  to the integration.
- The shared phonebook is now a SIP dial plan: explicit `sip:name@host` /
  `name@host` routes go direct, known local endpoints can be bridged by HA, and
  external numbers can use the trunk when configured.
- Phonebook `extension` values are the internal dial-plan aliases. Static DTMF
  route maps are not a second routing table; inbound DTMF and numeric local
  calls resolve against roster extensions.
- SIP call reasons are surfaced to ESP displays, HA state and cards: busy, DND,
  declined, cancelled, timeout, media-incompatible, transport-unreachable and
  route errors are no longer hidden behind the old intercom FSM.
- The reusable ESP audio backend has moved out of this repository. If you are
  looking for `esp_audio_stack`, `esp_aec` or `esp_afe`, use the dedicated
  [`esphome-audio-stack`](https://github.com/n-IA-hane/esphome-audio-stack)
  repository. This repository now consumes it as the audio engine for full
  voice/VoIP products.

Migration impact:

- Rebuild ESP firmware from the maintained 2026.7.0 YAMLs or update custom
  YAMLs to the SIP `voip_stack` contract.
- Update custom `external_components` entries that previously pointed at this
  repository for `esp_audio_stack`, `esp_aec` or `esp_afe`; those components now
  resolve from `github://n-IA-hane/esphome-audio-stack@main`.
- `transport: udp|tcp` still exists in ESP YAML, but it means SIP signaling
  transport only. RTP media is UDP.
- ESP devices do not REGISTER to a provider/PBX and do not require SIP auth.
  Provider/PBX registration belongs to Home Assistant's optional trunk client.
- The old ESP-only network scanning/discovery path is gone. Use explicit SIP
  URIs, ESP `static_contacts` entries or the HA-managed roster.
- The old project-specific VoIP call-control protocol is not a fallback.
- ESP group membership is not packed into the endpoint sensor anymore. The
  endpoint sensor stays short and stable; group membership is read from optional
  sibling ESPHome entities (`voip_ring_groups`, `voip_conference_groups` and
  `voip_conference_ring`) when those entities are exposed by the HA integration
  package.
- `voip_stack:` alone is only the ESP SIP/RTP engine. HA discovery, mirror-card
  state, group membership and phonebook synchronization require the maintained
  `packages/voip/ha_integration.yaml` entity surface or equivalent manual
  `platform: voip_stack` entities.

## 2026.7.0: SIP behavior

Version 2026.7.0 introduced the SIP architecture and these migration requirements:

- ESP `voip_stack` is a SIP phone. `transport: udp|tcp` means SIP signaling
  transport only.
- HA `voip_stack` is a SIP softphone, dial-plan authority, SIP/RTP
  bridge and optional SIP trunk endpoint. Only trunk/provider transport remains
  configurable.
- The retired proprietary intercom protocol is not a compatibility layer.
- ESP discovery/scanning between standalone peers is not a functional
  primitive. Use explicit SIP URIs, ESP `static_contacts` entries or the HA
  roster.
- Cards mirror their owner. HA cards mirror HA softphone state. ESP cards mirror
  ESPHome entities and controls.
- Optional HA trunk support is disabled by default. If disabled, no provider
  registration, external route or DTMF routing path starts.
- Inbound trunk routing uses standard SIP plus RFC2833/telephone-event DTMF to
  select local phonebook targets.

## 2026.7.0: source-based full media path and native 48 kHz VoIP presets

`2026.7.0` introduces the new full-experience audio/media path and
Sendspin/Music Assistant integration.

Maintained full-experience YAMLs now use ESPHome's `speaker_source` media
player path. Media, announcements, timer sounds, local audio files and optional
Sendspin streams enter one media player and then flow through the existing
mixer. VoIP call audio still has its own mixer source and keeps priority through the
existing call-state logic. Custom YAMLs copied from older full-experience files
should migrate away from `platform: speaker` media-player blocks and local
`files:` entries toward `media_source` plus `media_player.play_media` with
`audio-file://...` URLs.

Maintained non-native full-experience YAMLs now use the generic `runtime_controller`
component for runtime state arbitration. Custom full-experience YAMLs copied
from older releases should migrate away from `update_status`,
`timer_ringing`, local VA pending flags and callback-local LED/display/ducking
decisions. The new pattern is: callbacks send `runtime_controller.event`, the reducer
sets activities, and policies drive LED/display/audio/timer outputs from one
committed snapshot.

Voice Assistant response state is now tied to TTS/media-player announcement
lifecycle callbacks through `runtime_controller`. Slow local TTS backends can
exceed ESPHome's historical 2-second playback-start watchdog, especially XTTS
running locally. Older development snapshots supplied a configurable local
watchdog. Current profiles use the official Voice Assistant provider and
its native behavior; remove the obsolete `tts_playback_start_timeout` option
when following the native-provider migration above.

Sendspin is included in maintained full-experience profiles as a Music Assistant
media source. It is not required for normal HA media, TTS, timer sounds,
ringtones, Voice Assistant or VoIP calls. WS3, Spotpear and P4 grouped
playback were validated with the shared `speaker_source` path and the
hardware-clocked ESP audio stack timing model.

Native ESPHome voip-only presets now use 48 kHz PCM where the actual
native I2S microphone or speaker path supports it. SIP/TCP profiles may use
larger packet times; SIP/UDP profiles use short packet times such as 10 ms so
48 kHz/s16/mono remains below the default 1200-byte UDP datagram limit.
AFE/AEC-backed profiles keep 16 kHz TX because that is the Espressif AFE/AEC
output format; this is a local branch constraint, not a global SIP media limit.

UDP custom formats are validated against `udp_max_payload` at build time and by
Home Assistant when publishing the phonebook. The default is intentionally
conservative at 1200 bytes per audio frame. If you deliberately run larger LAN
datagrams, set the same larger `udp_max_payload` in the ESPHome YAML and the
VoIP Stack integration options; otherwise use TCP for high-rate, stereo or
32-bit PCM.

ESP endpoint publication now waits for a valid IPv4 address from ESPHome's
network API and republishes on Wi-Fi or Ethernet IP events. Endpoint sensors
should no longer publish incomplete rows before the board has a usable address.

Inbound SIP calls are now treated consistently across SIP/TCP and SIP/UDP: the
callee does not require the caller to be present in its local phonebook. The
phonebook is the outbound dial plan; inbound INVITE already carries caller and
destination identity. This matters for HA SIP bridges, routed subnets, VPN
callers and direct SIP clients. If a custom automation previously assumed that
an unknown inbound caller would be rejected by missing phonebook state, replace
that policy with DND, routing rules or an explicit bridge/service check.

Call-ended UI now preserves the real incoming caller through the terminal
callback before clearing the caller sensor. Device displays and the HA card
should show the peer from the active call, not whichever phonebook contact is
currently selected for the next outbound call.

The HA softphone card exposes Auto Answer, DND, Send Camera and browser ringtone
behind an idle-only Options panel. Auto Answer, DND and Send Camera are
persistent logical-phone settings exposed through HA entities and services.
Browser ringtone remains a per-browser localStorage preference.

ESP mirror and HA-softphone Lovelace modes have intentionally separate
semantics. `esp_mirror` mirrors one ESP endpoint and exposes controls from that
ESP's perspective. `ha_softphone` represents Home Assistant itself. If you
previously depended on one card mixing both models, split it into one ESP mirror
card and one optional HA softphone card.

ESP mirror cards now have a keypad/manual target view. This still calls the
mirrored ESP's own `start_call` action; it is not a separate HA-side contact or
number path. The ESP uses its local synced phonebook first and sends unresolved
targets to HA for central dial-plan/trunk/group routing.

Browser audio setup now waits for the HA SIP softphone control response and
uses the negotiated TX/RX formats from that response before creating the AudioWorklets.
Custom frontend code that starts microphone/playback worklets before the
control reply should be updated; otherwise ESP -> HA answered calls can use the
wrong frame size when one direction negotiates 48 kHz.

ESP caller playback now applies the negotiated RX speaker format before the
call is activated and re-applies it when the SIP answer confirms the effective
direction formats. Custom ESP integrations that bypass the maintained
`voip_stack` speaker setup must do the same before feeding high-rate PCM to
the local speaker path.

The Lovelace frontend derives ringtone/worklet cache keys from the loaded card
module version instead of a manually bumped audio asset constant. During custom
frontend testing, clear the browser cache or change the card resource URL when
serving files outside the packaged release flow.

Older upgrade notes are kept in their original GitHub release pages. The
sections above track the supported upgrade contracts through `2026.8.0`.
