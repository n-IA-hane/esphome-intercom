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

The optional `p4_framebuffer_capture` diagnostic accepts `downsample: 2` or `4`
to reduce its preallocated RGB565 snapshot while retaining a coherent panel
image. The default `1` preserves native capture. A full 800x1280 snapshot takes
2,048,000 bytes; factor 2 takes 512,000 bytes. Record this diagnostic allocation
when comparing memory and performance. It does not change the displayed image.

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
