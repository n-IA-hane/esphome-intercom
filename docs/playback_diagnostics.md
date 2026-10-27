# Playback diagnostics

Playback Diagnostics observes the existing announcement path without replacing
its sources, speakers, decoder or lifecycle. It provides snapshots and a finite
watch session. It never starts audio, changes volume, stops playback, restarts a
component or reboots the device.

The component targets the official ESPHome 2026.10 audio interfaces and Audio
Stack. It is optional: firmware that does not include it gains no observers,
timers, buffers or code from this component.

## Enable on a full profile

Add the maintained package alongside the existing full WS3 or Spotpear profile:

```yaml
packages:
  playback_diagnostics: !include packages/diagnostics/playback.yaml
```

Resolve the include relative to your configuration, using the same repository
package mechanism as the device profile. The component source follows
`ext_components_source`. For local development, set
`playback_diagnostics_source` to the local `esphome/components` directory.
Use a revision that actually contains this component when selecting a Git source.
Rebuild and upload the firmware to expose its button and API actions.

For [issue 129](https://github.com/n-IA-hane/esphome-intercom/issues/129), use the
**Spotpear Ball V2 FULL AFE** configuration in
`yamls/full-experience/single-bus/spotpear-ball-v2-full-afe.yaml`. Its board
package supplies the ES8311 codec, one microphone and the hardware playback
reference in the stereo RX frame. Preserve those board settings and add the
diagnostic package to that configuration.

The reported workload is ordinary `assist_satellite.announce`, with alternating
preannouncement sounds and wake-word recognition switched off. Reproduce that
path when collecting evidence for this issue. A WS3 laboratory run qualifies
only the interfaces exercised on that device, not the Spotpear failure.

The package defaults to these existing IDs:

| Binding | Default | Override |
|---|---|---|
| Media player | `speaker_media_player` | `playback_diagnostics_player_id` |
| Announcement HTTP source | `announcement_http_source` | `playback_diagnostics_source_id` |
| Background HTTP source | `media_http_source` | `playback_diagnostics_media_source_id` |
| TTS resampler speaker | `tts_speaker_input` | `playback_diagnostics_tts_id` |
| Announcement mixer input | `tts_mixer_input` | `playback_diagnostics_mixer_input_id` |
| Hardware speaker | `hw_speaker` | `playback_diagnostics_hardware_id` |
| Mixer | `audio_mixer` | `playback_diagnostics_mixer_id` |
| Voice Assistant | `va` | `playback_diagnostics_voice_id` |

Audio Stack resolves automatically when there is one instance. A configuration
with multiple instances must set `playback_diagnostics.audio_stack` explicitly.
The optional package adds a **Playback Diagnostics** button and three
ESPHome API actions:

| API action | Arguments | Effect |
|---|---|---|
| `playback_dump` | `reason`, string | Playback snapshot and the existing Audio Stack report |
| `playback_watch_next_announcement` | `timeout_ms`, integer | Watch the next ordinary announcement for a bounded window |
| `playback_cancel_watch` | None | Disarm observation and report the cancellation |

Use logger level INFO or more verbose. `reason` accepts `manual`, `before`,
`stalled` and `after`; other values become `manual` rather than exposing arbitrary
API input in logs. A watch window is clamped to 1 through 300 seconds and includes
the time waiting for the next announcement. Arming another watch replaces the
previous deadline. It does not repeat automatically.

The package appends ordinary YAML actions to the existing VA automations. It
does not attach a second automation parent to a VA trigger. Existing UI and
Runtime Controller actions remain in the combined chain.

## Capture a failure

1. Record the firmware build identity, exact component revisions, ESPHome
   version and target HA instance. A moving `dev` branch does not identify
   deployed firmware.
2. Request `playback_dump` with `reason: before`.
3. Arm a suitable watch window, then use the existing Home Assistant
   `assist_satellite.announce` path and the normal preannouncement sound.
   The diagnostic service itself does not generate an announcement.
4. If playback stalls, press **Playback Diagnostics** before issuing STOP or
   rebooting. Preserve complete `BEGIN` through `END` blocks and the separate
   Audio Stack report.

For an already stuck device, a dump works without arming a watch. Neither action
tries to recover playback. Do not substitute manual Assist, a synthetic server
or a different media route when reproducing an announcement failure.

`on_tts_start` captures a baseline before VA submits its URLs. `on_tts_end` is
reported as `tts_url_submitted`: it does not prove audible playback. `on_end`
also does not prove that the speaker has drained. If no TTS-start callback is
received, the watched player's first ANNOUNCING transition supplies the baseline.
An earlier VA baseline is preserved. A watched player leaving
ANNOUNCING produces `watch_player_finished`; the wording describes the public
state transition, not acoustic success. A deadline is a diagnostic observation,
not proof of a fault or permission to recover automatically.

## Read the report

Each report records its snapshot time and latest TTS-request sequence (zero
when no TTS-start callback has been received). Reports emit
one short line per scheduler turn; their values belong to the original snapshot,
not to the later printing time. The queue holds at most four reports. Excess
requests increment `coalesced`, and the latest sanitized error remains available
to the next explicit dump.

Each line carries a `report_id` and a `part=N/7` index. A complete report has
parts 1 through 7 exactly once, with matching `BEGIN` and `END` report IDs and
`parts=7`. Missing parts identify an incomplete log block. IDs increase for
every requested snapshot, including requests counted as coalesced, and wrap
modulo 2^32. A gap can therefore indicate coalescing or missing complete blocks;
compare the `coalesced` counter before drawing a conclusion.

Part 2 identifies the running firmware: the full 64-character application ELF
SHA-256, application build date/time, ESP-IDF version and generated ESPHome
version. These fields come from `esp_app_get_description()` and ESPHome's
generated version header. They are read and formatted only during the main-loop
report phase, using a fixed buffer. The identity line belongs to its numbered
report and must be retained when comparing failures or timeout behavior across
firmware builds.

`high` is the TTS resampler's existing output-completion callback. Its frames are
in the resampler input sample rate, shown as `rate_hz`. `low` is the hardware
speaker's output-completion callback, at its own sample rate. Neither observer
sees or stores PCM. Callbacks only update lock-free 32-bit counters and the
provided completion timestamp; there is no logging, allocation, mutex or
scheduler call from those callbacks. Each stage has one serialized writer in
the Audio Stack completion task. Main-loop readers never reset its counters.
The observer uses relaxed word-sized loads and stores, not atomic read-modify-write
operations. With the supported Xtensa compiler these are native word accesses;
the target assembly gate checks this independently of the host tests. The
general `std::atomic<uint32_t>::is_always_lock_free` property is intentionally
not assumed: IDF's PSRAM workaround can make it false for read-modify-write
operations even when these loads and stores remain native accesses.

Compare changes over the same time window. While rates remain unchanged,
16,000 high frames and 48,000 low frames can represent the same second. A
preannouncement and its message can use different rates: cumulative frame counts
across that change cannot be converted to duration using the latest rate. The
counters are progress observations, not a duration or acoustic oracle.
Counters wrap modulo 2^32;
subtract consecutive values as unsigned 32-bit values. `age_ms=4294967295` means
no nonzero completion has been observed. An age is relative to snapshot time.
`watch_frames` is a counter delta from the watched announcement baseline, or
from arming while waiting for that announcement.

Hardware output is shared. `media_active` is 1 when the optional background
source is not IDLE, 0 when it is IDLE, and -1 when it is not bound. A background
stream or VoIP can explain hardware progress while TTS does not advance.
`hardware_shared=YES` therefore applies even when the HTTP media source is idle.
Hardware progress alone is not proof that the announcement reached the speaker.

| Field | Meaning |
|---|---|
| `watch` | 0 inactive, 1 waiting for a request, 2 observing a request |
| `va_running` | Public VA pipeline-running flag, or -1 when VA is absent |
| `player` | ESPHome MediaPlayerState numeric value |
| `source` | MediaSourceState: 0 IDLE, 1 PLAYING, 2 PAUSED, 3 ERROR |
| Stage `flags` | Bit 0 stopped, bit 1 running, bit 2 paused, bit 3 buffered |
| Stack `flags` | Bits 0 running, 1 starting, 2 idle, 3 failed session, 4 I2S error, 5 microphone consumers, 6 speaker running, 7 speaker paused |
| `queued_bytes` / `capacity_bytes` | Audio Stack speaker ring buffer occupancy and capacity, in bytes |
| `pending_tx` | Audio Stack reports real output awaiting completion |
| `mixer_frames` | Mixed output frames written but not yet reported played; valid only when `mixer_available=1` |

`has_buffered_data()` is the speaker's public predicate. The resampler predicate
also includes its downstream speaker; it is not a measurement of only the local
resampler buffer. The snapshots are observations, not an atomic transaction
across all audio tasks. Completion counters and timestamps are independent
atomic values and may straddle one callback while sampled.
Completion timestamps arrive in microseconds and are stored as milliseconds.
The millisecond timestamp and age use unsigned 32-bit wrap, approximately
49.7 days, rather than the approximately 71-minute wrap of 32-bit microseconds.

Queue depth, the decoder's internal state, per-source playback credits and the
hardware owner's internal enum are explicitly marked unavailable. The separate
Audio Stack report supplies its supported hardware snapshot. Do not infer those
unavailable values from a public IDLE state or a pending frame counter.

## Error privacy and version differences

The VA error callback supplies both `code` and `message`. Diagnostics retains
only exact, fixed allowlisted error labels and static messages from ESPHome and
HA; unknown values become `redacted` with their original byte lengths. It does
not retain error input, text, media URLs or audio. TTS text contributes only its
byte length. Other firmware components can still print URLs at verbose logger
levels, so inspect and redact a complete log before sharing it externally.

Historical Intercom firmware used a configurable playback-start watchdog and
could report `tts-start-timeout` after 35 seconds. Official ESPHome 2026.10 has
different timeout behavior. The disappearance of that historical error after an
update does not prove that audio has recovered. Capture the actual code/message
and completion observations for the identified firmware.

## Other profiles

The component itself has no Runtime Controller dependency. Configuration
validation requires the actual player announcement source and the native
resampler -> mixer input -> Audio Stack speaker chain, all bound to the same
objects. This establishes the serialized completion writer for each observer;
arbitrary custom speaker implementations are not supported by the counters.
Official ESPHome 2026.10 Speaker Source state publication runs in its main loop
or main-loop control/deferred actions. The diagnostic event handling follows
that contract; audio callbacks publish only their progress words.

The `announcement_source` binding is required. `voice_assistant`, `mixer` and the
background `media_source` binding are optional. The full-profile package assumes
the full profile's player, HTTP sources and mixer IDs. For a full profile with VA
deliberately excluded, set `playback_diagnostics_voice_enabled: false`; this
omits its VA hooks and does not create VA.

For this chain with different IDs, configure the component directly with its
existing objects and expose the three diagnostic actions as needed. When using VA, wire
its existing YAML triggers to `on_tts_start(x.size())`, `on_tts_end()`,
`on_va_end()` and `on_error(code, message)` as the maintained package does. A
VoIP-only profile without an announcement source does not need this component
and must not include the full-profile package merely for a general health check.

## Software checks

Run `python -m pytest -q tests/test_playback_diagnostics.py` in the repository's
normal Python environment. The host tests compile the maintained C++ with public
fake audio objects and check observation, privacy and owner validation.

To repeat the target assembly guard, set
`PLAYBACK_DIAGNOSTICS_COMPILE_COMMANDS` to an existing, identified ESPHome
Xtensa `compile_commands.json` with Playback Diagnostics enabled and run the
same test file. The guard reuses that
SDK's real response file and precompiled header. It emits only temporary
assembly from this component, checks the actual progress function and its
compiler-generated parts for native
word accesses and rejects atomic helpers, CAS and unexpected calls. The
timestamp conversion's arithmetic helper is allowed. It creates no firmware
object, link or upload and does not modify the existing generated build.
