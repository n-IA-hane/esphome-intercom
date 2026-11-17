# Waveshare 1.85C-BOX V2 AEC compatibility and historical checks

For new configurations, use the [unified V2 FULL YAML](../../full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml).
It selects AEC/AFE, separate/averaged microphones and SIP UDP/TCP through root
substitutions. This older filename remains a single-microphone AEC compatibility
entrypoint. See the [current processing and hardware-check notes](../../../docs/WAVESHARE_V2_PROCESSING.md);
the dated measurements below retain their original scope.

The task placement and local resampler fixes described below belong to those
recorded builds. Current profiles use the official ESPHome audio providers and
their scheduling options; see the
[native-provider migration](../../../docs/BREAKING_CHANGES.md#development-firmware-native-audio-providers).

The measurements below describe the identified October candidate, not the
current unified profile's support status or DSP defaults. Its historical
configuration offered standalone echo cancellation on the Waveshare
ESP32-S3-Touch-LCD-1.85C-BOX V2 (SKU 30684). It keeps Assist, calling, music and
the circular interface. Use the maintained FULL AFE profile when you need its
dual-microphone processing. The AEC profile does not replace it.

## Processing changes

The recorded candidate used standalone `esp_aec` instead of `esp_afe`, with
`fd_low_cost` as its then-current default. Current FULL presets use SR modes
as described in the canonical processing guide.
The processed input is the primary microphone plus the existing hardware
playback reference. The board still has two microphones, but this profile
does not provide the dual-microphone processing, noise suppression or VAD
provided by AFE. Assist, MWW, VoIP, Sendspin, HTTP playback and the display
remain configured.

In the recorded candidate, Audio Stack stayed on core 1 and MWW ran on core 0. The AFE-specific
`audio_dsp_iram` linker helper is omitted. Removing the AFE feed/fetch pipeline
changes the competing workload; it does not by itself prove better wake-word
recognition or barge-in.

## Microphone headroom and call volume

This AEC profile uses 0 dB analog microphone gain. The earlier 24 dB setting
overloaded the microphone input during loud speaker playback: the clipped input
produced strong high-frequency artifacts after AEC, also present in the audio
returned to the caller. Low-volume tests had not exposed this failure.

The hardware playback reference keeps its separate 24 dB gain on physical
ES7210 MIC3 (channel index 2, TDM slot 1). Reference routing, sample rates and
the AEC algorithm are unchanged. An isolated capture with analog gain at 0 dB,
before the output calibration below, found no clipped microphone samples and
approximately 6 dB of peak headroom at maximum speaker volume. This is a
measurement for that recorded stimulus, not an acoustic-overload specification.

The ES8311 output now declares the external NS4150B amplifier's nominal
7.6 dB gain through Espressif's `pa_gain` calibration. The value follows the
board's 100 kOhm input resistors and the manufacturer's gain formula. The
profile also uses the SDK's native volume curve instead of the earlier
custom -30 dB floor. **The same Master Volume percentage is quieter than
before.** This corrects the output gain staging; it is not a new AEC algorithm
or a guarantee of distortion-free playback at every volume.

For a new installation, Mic Gain starts at +24 dB **after** AEC to provide the
nominal gain previously applied before processing. Existing saved Mic Gain
settings are retained. After upgrading an existing installation, adjust this
control with someone speaking near the device; background hiss is not a voice
level reference. Increasing post-processor gain cannot repair an overloaded ADC.
Update Audio Stack together with the packages for `initial_value` and `pa_gain`.

The regular October 17 build at 20:23:15 UTC was checked with real stereo music
at 48 kHz playing during a SIP call, Master Volume 85%, media volume 100% and
saved Mic Gain +18 dB. The existing call policy's 20 dB music ducking remained
enabled in every phase. Switching AEC on/off/on produced returned PCM levels
of -69.9, -31.2 and -69.8 dBFS RMS over consecutive 12-second windows. Neither
microphone nor speaker was muted. The return had continuous RTP sequence and
timestamps, no clipped PCM samples, and the call and playback drained normally.

The approximately 38.6 dB difference measures this music comparison; it is
not calibrated ERLE, a subjective listening test or a near-end speech test.
Separate loud SIP playback checks retained a low-frequency residual. Normal
speech and double-talk were not validated by that session; that candidate
remained experimental at the time.

The original AFE YAML assigns MWW, Audio Stack, AFE fetch and Wi-Fi to core 1,
with AFE feed on core 0. This is a reason to investigate scheduling, not proof
that scheduling alone causes missed wake words. Echo cancellation can also
affect the near-end voice during simultaneous playback.

## Concurrent calls, music and announcements

The shared V2 board package enables ESP-IDF's `CONFIG_LWIP_L2_TO_L3_COPY`.
Received data is copied to lwIP-owned memory so the Wi-Fi driver's finite RX
buffers can be released promptly. With simultaneous HTTP music and announcements,
slow TCP readers otherwise retain these buffers and can starve incoming VoIP
RTP, even when the audio tasks have CPU time available.

On this board, temporary instrumentation measured 64 outstanding driver buffers
during the failing workload. Enabling the copy policy reduced that peak to one.
Three consecutive runs received all 1,933 source RTP packets, with no jitter
buffer losses or overflows, and completed the HTTP announcements. The existing
lwIP allocation policy prefers PSRAM. Copying has a memory and CPU cost; it
changes buffer ownership rather than increasing driver buffer limits.

The recorded candidate also used a local resampler fix: it accepted bounded
prefixes of decoder blocks larger than its input buffer and waited for space
when full. This addressed a separate busy-retry problem with 16 kHz announcements
while music used 48 kHz. Current profiles use ESPHome's standard resampler instead.

These changes preserve the normal codec rates, AEC processing, Wi-Fi affinity
and packet queue sizes. No access-point tuning is required for the verified
scenario. The network setting is scoped to the V2 board; other boards and
networks have not been qualified by these measurements.

## Animation and flash budget

The animation uses the same eight original poses as the maintained AFE profile:
00, 03, 05, 08, 11, 14, 16 and 19. Resolution remains native 360x360 RGB565.
Source PNGs are unchanged and are not deleted. Forward and reverse animation
durations remain 2300 ms each, with the existing reverse delay and loop timing.

The eight idle images and ten other images require 4,665,600 bytes of raw RGB565
data. ESPHome 2026.9.1's default 16 MB partition layout provides an 8,126,464-byte
application slot, leaving 3,460,864 bytes for code, models, fonts and other data.
This is an asset-budget calculation, not a measured firmware binary size.
The local ESPHome 2026.9.1 / ESP-IDF 5.5.5 development build on October 17, 2026
passed with an OTA binary of 7,565,056 bytes and 561,408 bytes of
application-slot headroom. Static DIRAM usage is 170,383 of 341,760 bytes
(49.85%). This does not
measure free heap after startup or peak runtime memory use.

## Hardware evidence and remaining checks

ESPHome 2026.9.1 configuration validation, C++ generation and full compilation
passed using local component checkouts. The V2 was subsequently tested on real
hardware with standalone AEC and the full application set configured. The local
Audio Stack changes in that build included the bounded resampler input fix described above.

The investigation reproduced two separate failures: a resampler busy retry when
the decoder's block exceeded its input buffer, and packet loss when concurrent
HTTP readers retained Wi-Fi RX buffers. Both have measured before/after evidence.
The instrumented network tests described above preserve packet counts at each
stage from driver ingress to the RTP socket. Those temporary probes are not part
of the normal profile.

The earlier October 17 regular image was also tested without those probes, with normal
Wi-Fi core affinity and UDP queue size. Concurrent SIP audio, HTTP music at
48 kHz and an HTTP announcement at 48 kHz passed with either peer or device
hangup. Each call received all 634 source RTP packets, with no reported queue
losses, and the complete announcement arrived before playback was stopped.
This verifies packet delivery and lifecycle for that workload, not subjective
sound quality or all supported codecs.

The same regular image passed music at 48 kHz with announcements at both 48
and 16 kHz: stopping only the music left the announcement running to completion,
and music could then restart. No watchdog reset occurred in these checks.
These network and announcement measurements predate the gain calibration above;
the calibrated image's separate music/AEC test is described in that section.

Separate speaker-to-microphone recordings confirmed echo suppression with AEC
enabled versus disabled on the same hardware. This was an echo-only stimulus,
not a calibrated ERLE measurement or a near-end speech/double-talk test.

The dated session left these physical checks open; current support must not be
inferred from those measurements alone:

- Wake-word recognition during repeatable playback, with and without speech.
- Near-end speech before and after AEC, reference alignment and gain.
- MWW overruns, audio continuity, task scheduling and internal-memory headroom.
- Natural speech in both directions, Assist transitions and playback barge-in.
- Display responsiveness with the full animation and concurrent audio.

Use the same stimulus and firmware identity for comparisons. Do not infer
working barge-in from a successful build or from RTP packet counts.

## References

- [Espressif AFE resource benchmarks](https://docs.espressif.com/projects/esp-sr/en/latest/esp32s3/benchmark/README.html)
- [Espressif acoustic echo cancellation](https://docs.espressif.com/projects/esp-sr/en/latest/esp32s3/acoustic_echo_cancellation/README.html)
- [Waveshare V2 schematic](https://files.waveshare.com/wiki/ESP32-S3-Touch-LCD-1.85C/ESP32-S3-Touch-LCD-1.85C_V2.pdf)
- [NS4150B manufacturer documentation](https://nsiway.com.cn/list_38/82.html)
- [ESP-IDF 5.5.5 L2-to-L3 copy configuration](https://github.com/espressif/esp-idf/blob/v5.5.5/components/lwip/Kconfig)

Espressif benchmarks describe their tested processing chains. They are not
measurements of this firmware's combined AEC, MWW, display and VoIP workload.
