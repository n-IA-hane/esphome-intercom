# Qualified source publication recovery

This publication replaces the accidentally selected historical development trees with the source candidates used in the September 7 qualification. The user subsequently requested an online-dev P4 build and hardware verification; see the evidence addendum below.

- HA integration and frontend: source tree a2770b46 plus the requested video-bar keypad icon/layout change, including Assist media-route projection, ESP camera checkbox refresh, local video direction updates and local browser DTMF. The matching tests are retained.
- Audio: exact source tree 49a8d48c96, based on b4dd40b4d9, with one GMF output byte stream and cooperative inactive-input interruption. Historical I2S completion experiments are not part of this candidate.
- VoIP: published dev 2741e27 contains qualified jitter-state commit172e703 and the H.264 dependency adapter.
- WS3 full: qualified Audio Stack Core1/priority19, AFE feedCore0/fetchCore1. The production OTA adapter is included explicitly. No audio capture or CPU profiling enabled.
- P4: original full-profile video/MWW ownership from e383e144 is retained. The qualified PPA DIG-734 equality repair is included as a shared IDF component, replacing the private local build path. Camera/runtime refs remain on dev at the qualified source state.
- Spotpear: preserve the already published numeric dialer UI only, alongside qualified Opus configuration and DMA memory placement. This is an idle dialer, not a newly qualified in-call DTMF keypad.

Existing real-device evidence: WS3 production build September7 18:42:15 UTC completed ringing/manual answer with Sendspin, automatic answer, local and peer termination. Network microphone PCM had continuous sequence/timestamps. Physical speaker output on the final production binary was not independently captured. P4 full build September7 07:37:25 UTC remains the user-validated baseline. Archived binaries and source snapshots were preserved.

The release is a development prerelease. The initial publication used preserved evidence; the user then authorized the focused verification below. Do not generalize those cases to every profile or route. Android/trunk and remaining service combinations are still pending. Do not generalize prior green cases to all paths.

## Subsequent requested verification

P4 full build 2026-09-07 22:43:17 UTC fetched intercom a893138b, audio d8de513da5, VoIP2741e27, camera47d4710 and runtimeeba7ee3 from online dev. OTA SHA256726cfff47714c23b4c2ad45797019c19cb401a1eb6cbd3609dd8af35175d02a4. Dependencies match the baseline; PPA is the same driver source exposed through a remote component. IDF configuration differs only by camera JSON build path. Core/rate/buffer/priority setters match. Binary size14,634,608 versus14,632,320 bytes.

Hardware: Sendspin overlapped an audio/JPEG call for20.31 seconds; admitted178, rendered178, presented177, refresh_done177. Microphone network PCM:19.94 seconds, no sequence/timestamp discontinuity, longest exact-zero run0.1875ms. GMF suspension/restart observed via API logs with switch acknowledgements0.10-0.12s. A second audio/JPEG call after restart terminated locally and returned idle. Physical speaker waveform and framebuffer image are not captured by this production firmware. Serial is attached to WS3, so this run explicitly uses P4 API logs, not stale serial.

Spotpear VoIP-only now copies the full dialer page and round-band geometry verbatim, with backspace glyph and full-profile status visibility. HA video bar uses the existing keypad control with an icon and reserves both control widths. Focused card runtime passed; Chromium rectangles do not overlap at320/390/600px.
