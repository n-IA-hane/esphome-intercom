# Composing device features

Packages are the wiring between components. Runtime Controller resolves priorities;
Voice Assistant, the media player and VoIP remain responsible for their own work.
A display presents the controller's decision instead of creating another call or
voice lifecycle.

ESPHome 2026.9.0 or newer is required. Start from a maintained device YAML for a
complete device, or select the modules below for a smaller composition. Hardware
pins and audio-backend choices remain in the device configuration.

## Choose only the features you need

| Package | Provides | Requires |
| --- | --- | --- |
| `audio/mono_mixer_48k.yaml` | One hardware mixer | `hw_speaker` and at least one selected input |
| `audio/mixer_media_input.yaml` | Media port and resampler | `audio_mixer` |
| `audio/mixer_announcement_input.yaml` | Announcement port and resampler | `audio_mixer` |
| `audio/mixer_voip_input.yaml` | VoIP port and resampler | `audio_mixer` |
| `audio/shared_mono_mixer_48k.yaml` | All three inputs | `hw_speaker` |
| `media_player/mono_media_player_48k.yaml` | HTTP media and announcement player | Media and announcement speaker inputs; PSRAM; device friendly name |
| `media_player/sendspin.yaml` | Sendspin hub, source and player binding | `speaker_media_player`; PSRAM |
| `media_player/file_announcements.yaml` | Local-file announcement source | `speaker_media_player` and at least one `audio_file` asset |
| `media_player/sendspin_artwork.yaml` | Optional artwork | Sendspin and the board's artwork UI |
| `voip/ringtone_asset.yaml` | Bundled ringtone | `assets_base` |
| `voip/call_buttons.yaml` | Call controls | `phone` |
| `voice_assistant/runtime_controls.yaml` | Microphone mute control | `va` and `${mic_id}`; no VoIP or MWW dependency |
| `runtime/wake_word_events.yaml` | Official MWW callback and guarded startup | VA, MWW, lifecycle globals and mute switch |
| `runtime/wake_word_controls.yaml` | Wake-word switch and mute integration | MWW, `mute`, guarded startup |
| `voice_assistant/timers_runtime.yaml` | Timer callbacks, sound and shared runner | VA, runtime, announcement player/file source |
| `runtime/ha_connectivity.yaml` | Filtered HA presence | API, runtime and presence globals |
| `diagnostics/runtime.yaml` | Runtime inspection API actions | Runtime; enable explicitly for diagnosis |
| `runtime/lvgl_ui_projection.yaml` | Shared phase and call-page lifecycle | Full LVGL board UI contract |

The complete `presets/full_voice_voip_runtime.yaml` and media preset remain useful
shortcuts. They deliberately select a full set of features. A smaller device
selects the underlying modules instead of deleting arbitrary actions from a full
preset. `presets/voice_runtime.yaml` supplies voice/runtime integration without
instantiating VoIP, MWW, a display or a LED. Add the actual microphone, player and
speaker paths separately.

Sendspin artwork is a separate choice. Removing Sendspin also means omitting its
artwork module and any board widgets that explicitly use it. Similarly, telephone
widgets require a phone; the generic controller and voice preset do not.

## Controller adapters

In the Runtime Controller repository, `packages/runtime_controller/base.yaml`
creates the reducer without hardware bindings. Optional `voice.yaml`, `media.yaml`,
`ringtone.yaml`, `timers.yaml`, `display.yaml` and `led_state.yaml` add their own
bindings. Full LED and no-LED presets compose those modules.

The `features` list selects the built-in voice, media and timer rules. Packages
contribute their feature automatically. It does not instantiate a component or
replace the hardware configuration. The base selects no optional feature; the
full preset selects all three. Custom controllers can still declare activities,
priorities, events and actions directly.

## Native listeners and official callbacks

`runtime_controller.observe` supports `voip_stack`, `media_player`, `wifi`,
`microphone_mute` and `speaker_mute`. These use public component listeners and
replay initial state. Remove YAML callbacks that only forward those same facts.
Keep callbacks that actually control hardware or update content.

Voice Assistant and Micro Wake Word use their existing official automation
triggers through shared packages. MWW remains the upstream component. There is no
private observer API requirement and no replacement of the user's trigger
parent. The shared wake-word module reconciles startup and the existing voice, mute and OTA lifecycle hooks. It does not invent an upstream stopped event or periodically poll the detector. Do not declare `observe.voice_assistant` or `observe.micro_wake_word`:
those previously accepted entries did not provide native observation.

Pipeline completion is not the same as the last audio sample being played.
The runtime combines the existing pipeline and player notifications. Returning
to PAUSED completes the voice response just like returning to IDLE or PLAYING,
while leaving the underlying music paused. An explicit stop is acknowledged only
when VA is actually stopped; a deadline expiry is reported as an error.

## Extending a device

Define each component ID once and extend it with standard ESPHome package merging.
Keep board layouts, pins and user actions at the device boundary. Share event
forwarding and lifecycle logic in packages, rather than copying it into each
board. Text received from TTS updates response content; it does not independently
force the screen into replying.

Device headers show capture, echo reference, mixer ports and output paths. A
configuration/code-generation pass only validates wiring. A firmware build and
real audio/call tests are separate qualification steps.
