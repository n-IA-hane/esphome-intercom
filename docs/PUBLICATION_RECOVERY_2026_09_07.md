# Qualified source publication recovery

This publication replaces the accidentally selected historical development trees with the source candidates used in the September 7 qualification. It does not rebuild or deploy firmware.

- HA integration and frontend: exact source tree a2770b46, including Assist media-route projection, ESP camera checkbox refresh, local video direction updates and local browser DTMF. The matching tests are retained.
- Audio: exact source tree 49a8d48c96, based on b4dd40b4d9, with one GMF output byte stream and cooperative inactive-input interruption. Historical I2S completion experiments are not part of this candidate.
- VoIP: published dev 2741e27 contains qualified jitter-state commit172e703 and the H.264 dependency adapter.
- WS3 full: qualified Audio Stack Core1/priority19, AFE feedCore0/fetchCore1. The production OTA adapter is included explicitly. No audio capture or CPU profiling enabled.
- P4: original full-profile video/MWW ownership from e383e144 is retained. The qualified PPA DIG-734 equality repair is included as a shared IDF component, replacing the private local build path. Camera/runtime refs remain on dev at the qualified source state.
- Spotpear: preserve the already published numeric dialer UI only, alongside qualified Opus configuration and DMA memory placement. This is an idle dialer, not a newly qualified in-call DTMF keypad.

Existing real-device evidence: WS3 production build September7 18:42:15 UTC completed ringing/manual answer with Sendspin, automatic answer, local and peer termination. Network microphone PCM had continuous sequence/timestamps. Physical speaker output on the final production binary was not independently captured. P4 full build September7 07:37:25 UTC remains the user-validated baseline. Archived binaries and source snapshots were preserved.

The release is a development prerelease. No new tests, firmware builds or hardware uploads were run during publication, per user instruction. Remote YAML packaging and the combined distribution have not been freshly hardware-qualified. Android/trunk and remaining service combinations are still pending. Do not generalize prior green cases to all paths.
