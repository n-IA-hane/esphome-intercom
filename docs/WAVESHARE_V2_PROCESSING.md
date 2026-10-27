# Waveshare 1.85C BOX V2 processing choices

For other boards and microphone wiring, see Audio Stack's
[illustrated microphone guide](https://github.com/n-IA-hane/esphome-audio-stack/blob/dev/README.md#microphone-wiring-and-delivery-illustrated).

Use the [unified FULL YAML](../yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml)
for the V2 board (SKU 30684). V1 and the headless Waveshare S3 Audio are different
hardware profiles. ESPHome 2026.10.0 or newer is required.

## Select the microphone delivery and processor

The three root substitutions are independent compile-time choices:

```yaml
substitutions:
  audio_processor: aec
  mic_mix: average
  voip_transport: udp
```

| Choices | Audio Stack delivery | Processor |
| --- | --- | --- |
| `aec`, `average` | Mean of the two physical microphones | Standalone mono AEC, the unified entrypoint default |
| `aec`, `passthrough` | First microphone | Standalone mono AEC |
| `afe`, `passthrough` | Two separate microphone channels | Dual-microphone AFE with structural SE/BSS |
| `afe`, `average` | Mean of the two microphones | Mono AFE, no dual-microphone SE/BSS |

The package selects AFE's matching channel count automatically. A manual override
that combines downmix with `mic_num: 2` fails native ESPHome validation at
`mic_mix`, with an explanation of the required mono AFE settings. BSS is not an
independent switch in the dual-microphone profile. VAD remains available as
**Voice Activity Detector** and **Voice Detected**. **Echo Cancellation** and
**AEC Mode** keep their existing names; AFE also contains an AEC stage.

Maintained FULL presets start in **SR HIGH** (`sr_high_perf`) and expose only
SR HIGH and **SR LOW** (`sr_low_cost`). The separate VoIP-only presets retain
FD choices. The historical FD measurements below describe their identified
firmware, not the current FULL default.

SIP may use UDP or TCP; RTP audio remains UDP. The existing `transport.yaml`
sets this scalar directly, without separate duplicate transport packages.
Older `full-aec.yaml` and `full-afe.yaml` entrypoints remain compatible aliases.

Audio Stack owns capture, channel selection, conversion and optional downmix.
The processor receives its normal one/two-channel input, independently of
whether the microphones came from TDM or conventional I2S Left/Right slots.
See the [general microphone-delivery guide](https://github.com/n-IA-hane/esphome-audio-stack/blob/dev/docs/AUDIO_TUNING.md)
for codec-free wiring and actual channel-count limitations.

## One physical calibration for this board

Both processors include the same [board audio package](../packages/audio/profiles/waveshare-s3-box-v2-common.yaml):

- Microphones are in TDM slots 0 and 2; the playback reference is in slot 1.
- The microphone ADC gain is 0 dB for capture headroom. The separate physical
  ES7210 reference channel is configured at 24 dB.
- ES8311 uses the NS4150B amplifier calibration `pa_gain: 7.6`, derived from
  this board's 2.4 voltage gain. Its native hardware-volume curve is retained.
- Capture/playback remain at 48 kHz; AEC/AFE and the processed microphone use
  16 kHz. The existing post-processor **Mic Gain** remains saved-aware.

These values were already explicit in the standalone AEC profile. The previous
AFE package omitted the amplifier/reference calibration and used a different
microphone ADC gain. A real-parser regression failed on that mismatch and now
passes for both profiles. This is a physical board contract, not a new AFE DSP
algorithm or a universal gain prescription for other devices.

FULL AFE with the native 360x360 assets uses ESPHome's official `SIZE` compiler
optimization to fit the existing OTA slots. Standalone AEC retains `PERF`.
No animation pose, source image, display resolution or application feature was
removed. A root `v2_compiler_optimization` override remains possible, but every
custom build must pass its actual partition-size check.

## Physical buttons

The V2 has one programmable button on **GPIO0/BOOT**. The other button is
**RESET**, wired directly to the ESP32-S3 `CHIP_PU` reset input, so it cannot
be reassigned to Call/Answer in firmware. See the
[manufacturer's V2 schematic](https://files.waveshare.com/wiki/ESP32-S3-Touch-LCD-1.85C/ESP32-S3-Touch-LCD-1.85C_V2.pdf).

In the maintained FULL interface, a short press stops a ringing timer first.
Otherwise, an active or pending Assist interaction takes priority: pressing
the button cancels Assist while an established audio call remains active.
When Assist is inactive, the existing view-specific phone and voice controls
apply. Options uses a centered title and swipe navigation, without a Back
button; swipe down from its fixed header to return to the preceding view.

## Physical checks on 2026-10-22

The identified FULL AFE diagnostic image used official ESPHome 2026.10.0 and
ESP-IDF 5.5.6, dual-microphone SE/BSS, `fd_high_perf`, codec/master volume 50
percent, media volume 100 percent and the existing saved 20-dB Mic Gain.
The AEC/AFE DSP implementations were unchanged.

The Box itself played an Espressif speech fixture and a real SoundHelix music
excerpt while a SIP peer supplied continuous silent RTP and recorded the
processed microphone return. Each source ran with cancellation off/on/on/off.
After excluding the first six seconds of each window:

| Source | Cancellation off, returned PCM | Cancellation on, returned PCM |
| --- | --- | --- |
| Speech | -52.95 / -52.96 dBFS | -102.75 / -102.20 dBFS |
| Music | -46.58 / -46.56 dBFS | -111.92 / -110.22 dBFS |

These are returned-signal levels including the selected frontend and gains,
not calibrated ERLE, a percentage of cancellation or a near-end speech score.
Raw taps retained two distinct 48-kHz microphones and two 16-kHz processor
inputs while cancellation was enabled. Their similar off/on levels establish
that capture continued; the reference remained a separate mono stream. The
captured windows had no digital clipping, diagnostic queue gaps or missing,
duplicate or reordered RTP sequences. PCAP validation passed.

Two additional real SIP/media cycles exercised music at 48 kHz, announcements
at 48 and 16 kHz, overlapping playback/call audio, independent stops, restart,
both hangup sides and return to quiescence. VAD and wake word were enabled;
a switch state is not a recognition-accuracy measurement. No I2S failure or
watchdog reset occurred in these bounded checks.

The diagnostic image occupied 7,691,600 OTA bytes, with 434,864 bytes of slot
headroom and 186,111 bytes of linked static RAM. These are build measurements,
not free heap. The initial `PERF` version linked but exceeded the same OTA slot
by 20,000 bytes and was not flashed.
The normal image without capture/PGA bench controls is 7,685,568 OTA bytes,
with 440,896 bytes of headroom and 185,423 bytes of static RAM.

Local evidence retains image/config/source identities, PCM WAVs, PCAPs, native
regressions and separate build/runtime results. The codec-free I2S example also
compiled and linked completely; it was not flashed onto matching standalone
MEMS hardware. Natural wake-word recognition during playback, independent
near-end speech/double-talk, long uptime and physical LCD rendering are not
qualified by this session. The headless WS3 and P4 were not reflashed.
