# 2026.9.1-dev: qualified source recovery, GMF audio and browser call fixes

Stable remains `2026.9.0`. This development prerelease now packages the September 7 HA candidate and coordinates the ESP component sources used during that work. The previous September 7 edit updated release notes but left the September 4 ZIP attached; this revision replaces that stale package.

## Included changes

- HA card: preserve continuous PCM resampling/playout work, refresh ESP Mirror camera controls, and fix Assist audio setup when projecting the committed media route.
- Local browser calls: refresh video send/receive permissions when a participant enables a camera after connection. Publish local browser DTMF through the existing canonical call-event path.
- Audio Stack: preserve fragmented GMF output in one existing byte-stream ring and abort inactive GMF input cooperatively during pause. The historical I2S completion experiment accidentally selected for dev is replaced with the qualified audio candidate.
- WS3 full: use the measured Audio Stack Core 1 placement, retaining AFE feed Core 0/fetch Core 1 and the existing priorities, rates and queue capacities. Include the production OTA adapter explicitly.
- P4: retain the validated full-profile video/MWW policy and include the qualified PPA DIG-734 FIFO equality repair as a remotely available IDF component. This does not deploy new firmware to any device.
- Spotpear VoIP-only: use the complete full-profile round dialer page and geometry, including the backspace glyph and status-overlay visibility. This is an idle dialer, not an in-call DTMF keypad.
- HA video call bar: show a dialpad icon immediately left of Options and reserve both buttons in the call bar so they cannot overlap the duration. Idle keypad labels and accessible names remain available.
- Keep PCM and Opus as the supported audio codec families. Maintained full profiles remain PCM; compatible S3 VoIP-only profiles default to Opus. P4 is excluded from Opus. Compatible endpoints can call directly; incompatible legs use the HA transcoding path when available.

The qualified source set retains the local speaker adapter. Do not follow the previous prerelease instruction to migrate these profiles to `speaker_source`; that instruction described a different source tree.

## Evidence and limits

Previously completed on the identified candidates:

- HA focused suites: 122 tests and 4 subtests. Browser A/B video and DTMF in both call directions, cleanup, temporary-account services, successful forwarding, decline, and busy-forward fallback/resume with subsequent answer and hangup.
- WS3 production firmware: ringing/manual answer while Sendspin was active, automatic answer, local and remote hangup, and return to idle. Received microphone PCM had continuous RTP sequence numbers and timestamps over 19.94 seconds.
- WS3 diagnostic predecessor: cooperative audio stop/restart and OTA completed without the earlier pause watchdog. Production removes the 512 KiB capture buffer and CPU profiling.
- P4's user-validated full firmware and its source baseline remain preserved.

After the user requested an additional verification, P4 full was compiled exclusively from online dev sources and uploaded. Its dependency versions and IDF configuration match the baseline (apart from the PPA source revision and a build-directory path). Of 70 captured component C/C++ files, 69 are byte-identical; only esp_afe.cpp contains the intended GMF changes. The new OTA binary is 2,288 bytes larger, so it is not byte-identical to the baseline.

On that P4 firmware, a bidirectional JPEG/audio call overlapped Sendspin, followed by GMF stop/restart and a second call terminated locally. Panel counters reported 177 presentations from 178 admitted frames in the first call. Received microphone PCM covered 19.94 seconds with no sequence/timestamp discontinuities. Wake word and VAD were restored and the device returned to idle. Physical-speaker audio and a framebuffer screenshot were not independently captured in this run.

The card update passed its focused runtime scenario and Chromium layout inspection at 320, 390 and 600 pixels. Spotpear dialer source is copied from the full profile; visual confirmation on the physical device remains pending. Android/trunk and remaining service combinations are still pending; historical broader test counts are not qualification of this archive.

Source custody and integration details: [publication recovery record](https://github.com/n-IA-hane/esphome-intercom/blob/dev/docs/PUBLICATION_RECOVERY_2026_09_07.md).

## Installation

Select `2026.9.1-dev` when redownloading VoIP Stack in HACS, restart Home Assistant, and refresh the card/browser assets. Development YAMLs use the coordinated `@dev` branches. This release operation itself does not update an installed Home Assistant instance or flash ESP devices.
