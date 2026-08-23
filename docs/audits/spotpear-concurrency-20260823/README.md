# Spotpear concurrency failure snapshot

This snapshot records the last read-only investigation before the remediation
work started on 2026-08-23. Raw captures remain under the gitignored directory
`test_captures/spotpear-concurrency-20260823T1345Z/`.

## Candidate identity

- ESPHome intercom: `fef583e3c37e9ee6fcd143017c47a2ebecec6c24`
- Audio stack: `07041f51b1b63929f84c75d4a216ea117f9b1b68`
- VoIP stack: `d1aff249c6323ca50d261e18ee8cb4a1b388b209`
- Runtime controller: `a41b84d871b4f6ef7a8bd8b02190ff647460b83f`
- ESPHome: 2026.7.4
- ESP-IDF: 5.5.5
- Profile: `yamls/full-experience/single-bus/spotpear-ball-v2-full-afe.yaml`
- OTA SHA256: `1ab9783224d6ed1879801c50c7db224ec0e808d9414640df1a44acb5156fa354`
- ELF SHA256: `2c1be0e61a935053f443e8e91690cbbd754520b7591251102a56564959d7bbbf`

The exact firmware running at the start of remediation was not independently
read back from flash. Historical build artifacts and old green captures are
not treated as proof of current deployment.

## Reproduced failure

1. Sendspin music played successfully.
2. A direct Spotpear to WS3 VoIP call established and carried L16 audio.
3. TTS started during music plus VoIP and failed to allocate its 65,536 byte
   decoder ring.
4. A later TTS attempt failed to allocate the decoder output buffer.
5. SPI DMA and LVGL allocations then failed and Spotpear became unavailable to
   ping, ESPHome API and Sendspin without rebooting.
6. JTAG showed both CPUs alive. `loopTask` remained in the same LVGL label draw
   path while Wi-Fi was suspended.

The immediate failure is certain: neither PSRAM nor internal RAM contained a
contiguous block large enough for the lazy 65,536 byte HTTP decoder ring. The
owner responsible for the preceding memory pressure was not yet established.

## Measured memory state

- Clean idle PSRAM: approximately 166 to 167 KiB free.
- Music plus call PSRAM: approximately 86 KiB free.
- After failed TTS cleanup: approximately 46 KiB free.
- Minimum internal heap in the failed run: 612 bytes.
- MWW minimum observed free stack: 584 bytes.
- VoIP TX peak observed stack use: 3,216 bytes of 12,288.
- VoIP RX peak observed stack use: 944 bytes of 12,288.

The current `origin/main` profile was rebuilt as a diagnostic control and also
failed at the first standalone announcement ring allocation. This rules out a
simple `dev` regression and makes a source revert an invalid fix.

## Ownership conclusions before remediation

- Keep the audio frame integrity, desynchronization and I2S lifecycle fixes.
- Keep the runtime controller single-owner fixed-capacity storage migration.
- Keep VoIP signaling ownership, RTP pacing and cleanup barriers.
- Investigate overprovisioned VoIP task stacks with matched hardware traces.
- Trace persistent setup allocations before changing the upstream decoder.
- Preserve MWW, Sendspin, TTS, VoIP, AFE and AEC throughout remediation.

## Qualification boundary

Spotpear is available over `/dev/ttyACM0` and WS3 is available over OTA. P4 is
not available for hardware qualification. Shared changes may be compile-gated
against P4 JPEG and H.264 profiles, but no P4 runtime claim may be made from
this work.
