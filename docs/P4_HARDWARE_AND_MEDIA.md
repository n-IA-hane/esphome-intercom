# P4 hardware, resolution and media settings

`packages/platform/esp32p4_base.yaml` selects the engineering-sample P4 board
used by the physical rev1.3 test device. For production rev3 silicon, remove
`board: esp32-p4-evboard`, retain `variant: esp32p4`, and set
`engineering_sample: false`. ESPHome and component dependencies choose the
corresponding target libraries when compiling. Firmware already built for an
earlier revision does not change its linked libraries when moved to another
board.

Audio Stack selects esp_audio_effects 1.3 for pre-v3 P4; newer library variants
require instructions absent from that silicon. Other dependency upgrades and
explicit ESP-IDF overrides require their own boot, OTA and media qualification.
Without an explicit framework version, the installed ESPHome release selects
its default SDK. Updating the P4 SDK does not by itself prove that C6 firmware
must be replaced; check the applicable ESP-Hosted MCU compatibility contract.

## Camera and display

Set `esp_video_camera.resolution`, `jpeg_quality` and `max_framerate` in the
device YAML. Resolution controls outgoing capture and must be supported by the
attached sensor. Incoming resolution is negotiated with the sender. The panel
size, incoming stream size and outgoing camera size are separate settings.

Configured frame rate is a limit. Qualification reports captured, transmitted,
decoded and presented frames separately and compares the same resolutions and
concurrent workloads. JPEG and H.264 remain separate firmware profiles.

For normal on-demand snapshots, use the standalone [ESP Screenshot component](ESP_SCREENSHOT.md)
and the existing native button/API action. Choose its synchronized P4 backend
when direct video draws outside LVGL; account for its actual buffer policy.

The low-level `p4_framebuffer_capture` bench diagnostic remains separate. It
accepts `downsample: 2` or `4`
to reduce its preallocated RGB565 snapshot while retaining a coherent panel
image. The default `1` preserves native capture. A full 800x1280 snapshot takes
2,048,000 bytes; factor 2 takes 512,000 bytes. Record this diagnostic allocation
when comparing memory and performance. It does not change the displayed image.

Before changing C6 firmware after a reset, hang or Wi-Fi failure, preserve the
boot/reset log, P4 application identity and actual ESP-Hosted MCU version.
Compare those with the selected SDK's compatibility requirement. Use a supported
update or ROM recovery only when that identified mismatch requires it; an audio
problem alone is not evidence that the coprocessor needs reflashing.

## ESPHome 2026.10 physical check

The 2026-10-21 check used the FULL landscape JPEG profile on physical P4
rev1.3, official ESPHome 2026.10.0 and its default IDF 5.5.6, esp_video 2.4.1,
and the upstream camera source at `0814536988d946e0e697cccef5063b208f6df901`.
The updated firmware was retained on the device. No old firmware was restored.

Pre-v3 P4 needs the [CSI format adaptation](../idf_components/esp_video_csi_compat/README.md)
for this SDK/library combination. ISP performs the RAW-to-RGB conversion;
CSI must receive the resulting format as a passthrough. This adaptation
replaces one compilation unit in the build directory, preserving the managed
library and external camera sources. It adds no media task or second pipeline.

Physical checks covered audio-first video added, removed and re-added by
re-INVITE, initial sendrecv JPEG, local and remote hangup, consecutive calls,
UDP/TCP audio with music and announcements, native Sendspin through Music
Assistant 2.10.7, pairing persistence after reboot, and an interrupted OTA
with real PCM queued followed by playback and panel recovery. PCAP analysis
found no audio sequence/timestamp gaps or clipped samples in the JPEG calls.
Returned 800x800 JPEG streams decoded independently without errors. Repeated
framebuffer captures retained the call controls and returned to the dashboard.

These checks used master/media volume at 1 percent and a diagnostic firmware
with a 512,000-byte one-shot snapshot allocation. They do not qualify acoustic
echo rejection, wake-word recognition, production rev3 silicon or H.264.
The profile's video limit was 10 FPS, not a guaranteed delivered rate. Measured
camera TX was approximately 3.8-5.0 FPS with simultaneous incoming video,
FULL audio and diagnostic captures. Incoming synthetic video used a 320x180
source (320x184 dimensions in its RTP/JPEG headers).
This is functional verification, not a claim of 10 FPS bidirectional video.

The capture tool invalidates CPU cache before reading the DMA-written panel
buffer. Earlier snapshots without that synchronization could show stale or
missing controls even though the current DMA framebuffer was correct.

## Audio and controls

P4 remains PCM. Its speaker bus runs at 48 kHz and AFE microphone output at
16 kHz. The existing speaker resampler accepts compatible incoming PCM rates,
including 16 kHz for direct ESP-to-ESP calls; 48 kHz speaker output does not
require every peer to transmit 48 kHz RTP. Prefer 10 ms packetization when both
endpoints support it.

Send Video controls the next-call camera preference while idle and requests a
SIP direction update during an established video call. Incoming video remains
independent. The switch uses its configured restore mode after reboot.

Ring On Conference requires a conference group. Assign the group before
enabling the switch. Removing the group clears the ringing preference in the
existing device contract; an empty group is not a signaling or codec failure.
