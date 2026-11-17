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

## SDK selection and official build-local fixes

Maintained P4 profiles follow ESPHome's recommended ESP-IDF without a framework
source pin. At the recorded Core baseline, the default is official v5.5.6,
commit `880a203ae603d08a94d06aa01e0d6761958e5e9e`. That tag lacks the official
SDMMC reset-order and PPA task/interrupt coordination fixes.
`p4_sdmmc_reset` and `p4_ppa_backport` apply those corrections to checksummed
build-local copies on the original IDF targets, preserving target settings and
leaving the installed SDK untouched. The vendored PPA/CSI replacement components
remain retired.

The same bounded backports support the true v6.0.3 tag,
`76f5dedd9950a3012fee8fb7d5586df21fc67802`, which also lacks both fixes.
Already-fixed sources are recognized without another copy; unrecognized inputs
fail closed. A directory named after an SDK version does not establish its Git
identity. The actual tag6 build, archive and one matching JPEG digital-media
check have passed. Optical panel behaviour and other profiles remain separate
qualification items; public profiles still follow the recommended SDK.

The camera component selects official `esp_video` source containing its pre-v3
CSI correction. Check the resolved lock and compiled source rather than
assuming a registry release has the same contents.

## Camera and display

Set `esp_video_camera.resolution`, `jpeg_quality` and `max_framerate` in the
device YAML. Resolution controls outgoing capture and must be supported by the
attached sensor. Incoming resolution is negotiated with the sender. The panel
size, incoming stream size and outgoing camera size are separate settings.

Configured frame rate is a limit. Qualification reports captured, transmitted,
decoded and presented frames separately and compares the same resolutions and
concurrent workloads. JPEG and H.264 remain separate firmware profiles.

For normal on-demand snapshots, use the standalone [ESP Screenshot component](ESP_SCREENSHOT.md)
and the existing native button/API action. The independent P4 renderer decodes
JPEG or H.264 and commits through the compatible MIPI display's public pixel
transaction. The display owns RGB/YUV conversion, scale, rotation, framebuffer
and serialized UI/video writes. LVGL supplies the logical video rectangle and
page visibility; P4 video is not an `lv_image`. VoIP adapters remain in VoIP
Stack, outside the renderer.

The minimal MIPI adaptation is aligned with official ESPHome commit
`c6f330458a2da2f64788487486bd6d0f8749fafd`, whose base matches the recorded
ESPHome baseline. Its [upstream notes](../esphome/components/mipi_dsi/UPSTREAM.md)
record the remaining owner/API delta. With `lvgl_id`, screenshots automatically
select this owner's public composed-frame copy when advertised. They do not
force an LVGL redraw before copying. Account for the actual screenshot buffers.

The optional low-level [`p4_framebuffer_capture`](../esphome/components/p4_framebuffer_capture/README.md)
diagnostic requires the proposed public `copy_frame_buffer_rgb565` API and its
provider capability metadata. The declared MIPI adaptation supplies that API;
stock ESPHome at the recorded baseline does not. Ordinary S3 LVGL screenshots
remain independent of this P4 capability. The raw diagnostic neither exposes a
live framebuffer pointer nor creates another display owner.

A raw capture preallocates one full logical RGB565 frame: 800x1280 takes
2,048,000 bytes even with `downsample: 2` or `4`. Downsampling compacts that same
storage after the driver copy and reduces network bytes, not its preallocation.
Record the optional diagnostic allocation when comparing memory/performance.
Capture does not change the displayed image.

Two default-SDK digital-media checks used the same 320x240, 10 FPS JPEG source.
The approximately eight-second steady windows measured 9.696 and 9.941 P4
display-owner commits per second, excluding requested screenshots. These are
receive-video measurements, not a guarantee of 10 FPS camera transmission or
acoustic quality. The first strict browser probe passed; the repeat retained
24 startup audio-playout drops and failed that strict check despite the video
rate. Both released their call-scoped resources. These observations do not
establish that every visible-panel issue is resolved.

The true SDK 6.0.3 forward check on 16 November used identical MIPI, Renderer and
Screenshot source, with the same official build-local fixes. It measured 9.824
native commits per second in the same approximately eight-second window.
Completed JPEG streams were approximately 10 FPS in both directions; strict
browser audio reported zero playout drops and underruns, RTP had zero loss,
and all call-scoped resources were released. Master 0 and all recorded controls
were preserved. The device was then restored to the archived recommended-SDK
candidate. These are digital transport and framebuffer results; the absent user
could not confirm that transient physical white flashes are gone. No acoustic
quality or H.264 claim follows from this check.

Before changing C6 firmware after a reset, hang or Wi-Fi failure, preserve the
boot/reset log, P4 application identity and actual ESP-Hosted MCU version.
Compare those with the selected SDK's compatibility requirement. Use a supported
update or ROM recovery only when that identified mismatch requires it; an audio
problem alone is not evidence that the coprocessor needs reflashing.

## Camera source in the preview

Dev profiles and coordinated CI use the
[project camera fork on `dev`](https://github.com/n-IA-hane/esphome-esp-video-camera/tree/dev).
It includes the shared hardware-JPEG frame admission submitted in
[upstream PR #9](https://github.com/Psix-anp/esphome-esp-video-camera/pull/9).
The existing 14 November P4 JPEG firmware was built with that same C++ code;
changing the public profile reference does not itself update installed firmware.
Main/stable profiles continue to use the upstream component on `main`.

For `device: jpeg` with hardware JPEG encoding, the camera makes one admission
decision before RAW callbacks, PPA and encoding. Dedicated `device: csi` RAW
capture and already-compressed JPEG/MJPEG paths retain their existing policy.
This describes the code path, not a measured FPS or CPU gain. The October check
below records its original source revision and remains historical evidence.

## ESPHome 2026.10 physical check

The 2026-10-21 check used the FULL landscape JPEG profile on physical P4
rev1.3, official ESPHome 2026.10.0 and its default IDF 5.5.6, esp_video 2.4.1,
and the upstream camera source at `0814536988d946e0e697cccef5063b208f6df901`.
The updated firmware was retained on the device. No old firmware was restored.

That reference used the [historical CSI format adaptation](https://github.com/n-IA-hane/esphome-intercom/blob/dabf197a9cac57c6624441a937eb32e5a88d3903/idf_components/esp_video_csi_compat/README.md)
for its SDK/library combination. The current candidate selects the official
replacement described above. ISP performs the RAW-to-RGB conversion;
CSI must receive the resulting format as a passthrough. That historical
adaptation replaced one compilation unit in the build directory, preserving
the managed library and external camera sources. It added no media task or
second pipeline.

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

P4 uses PCM by default. Its speaker bus runs at 48 kHz and AFE microphone output at
16 kHz. The existing speaker resampler accepts compatible incoming PCM rates,
including 16 kHz for direct ESP-to-ESP calls; 48 kHz speaker output does not
require every peer to transmit 48 kHz RTP. The FULL defaults prefer TX16k/20 ms
and RX48k/10 ms, with declared alternatives. Transmit and receive durations are
independent. Optional G.722 provides a wideband PBX codec alongside PCM; see
[codec configuration](ESP_AUDIO_CODECS.md).

Send Video controls the next-call camera preference while idle and requests a
SIP direction update during an established video call. Incoming video remains
independent. The switch uses its configured restore mode after reboot.

Ring On Conference requires a conference group. Assign the group before
enabling the switch. Removing the group clears the ringing preference in the
existing device contract; an empty group is not a signaling or codec failure.
