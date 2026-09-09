# 2026.9.2-dev: audio fixes and a new round of device testing

This development preview brings the latest fixes together for testing through HACS. **2026.9.1 remains the stable release.** Please report both successful retests and any remaining problems on your existing issue.

## Home Assistant call controls

Repeated Hangup or Decline requests now recognize a call that has already ended on that phone. This avoids an incorrect ownership error when a delayed or repeated command arrives after cleanup.

This does **not** establish that the reported Swisscom problem, where the remote phone stays connected after hanging up from the card, is fixed. We still need the provider-facing SIP logs for that case.

The in-call keypad remains available for interacting with automated phone menus, such as entering an extension or choosing a department. Hosting a spoken menu inside Home Assistant is a separate feature and is not added by this preview.

## ESP audio and firmware updates

These changes require rebuilding and uploading ESP firmware from the current development profiles/components. Installing the HACS update alone does not update an ESP.

- **Announcements:** HTTP playback profiles include MP3 and WAV alongside FLAC. WAV announcements served without a filename extension are recognized correctly, including the `audio/vnd.wave` content type.
- **Early microphone capture:** starting capture before component setup no longer depends on a semaphore that has not been created yet. This addresses the reported startup crash; it does not by itself qualify every EchoS3R audio configuration.
- **Microphone-only calls:** the receive-audio timeout no longer ends a call simply because the device only transmits audio. Shared SIP memory fixes are also included in the current development component.
- **Firmware updates:** shared OTA handling covers the larger Full images, and the I2S interrupt correction addresses a flash-write failure reported on P4. Boot and update checks passed on the available P4/S3 devices; confirmation on other boards is still welcome.
- **SIP audio negotiation:** ordinary two-way SDP answers select an audio format supported in both directions. Explicit directional negotiation keeps its separate transmit/receive rates. No automatic PCM/Opus codec fallback was added.

## Optional dual-microphone features

Standard I2S can now feed two microphone slots into the existing dual-mic processing path. Optional left/right level sensors can support sound-direction automations. The public microphone remains mono.

The dual-mic AFE changes honor the requested VAD state and provide optional output gain normalization when the underlying two-mic pipeline does not apply AGC. Unused optional support is excluded from the firmware. Existing profiles do not need to enable it.

## What still needs confirmation

Please do not treat this preview as confirmation that every open report is resolved. In particular, Swisscom hangup, some 1.85C and EchoS3R configurations, and audio under weak Wi-Fi still need device-specific evidence. Original ESP32/A1S support and native locked-screen mobile calling have not been added.

When reporting a retest, include the installed integration version, device/profile, call direction and whether audio works in each direction. Please also test hangup and a second call. Remove passwords and keys from shared logs or YAML.

## Updating through HACS

1. Open VoIP Stack in HACS, select **Redownload**, and choose **2026.9.2-dev**. Enable prerelease versions if necessary.
2. Restart Home Assistant.
3. Reload the browser or Companion app to load the matching card.
4. For ESP-side fixes, separately rebuild and upload the current `dev` profile/components for your device.

## Thanks to the community

Thanks to @jharris4 for the P4 I2S interrupt investigation and fix ([PR #12](https://github.com/n-IA-hane/esphome-audio-stack/pull/12)), @benklop for standard I2S dual-mic support ([PR #11](https://github.com/n-IA-hane/esphome-audio-stack/pull/11)), and @jyoushiki for the VAD/AGC contribution ([PR #8](https://github.com/n-IA-hane/esphome-audio-stack/pull/8)). Their authorship is retained in the incorporated changes.

Thanks also to @rvdv01, @MakaronaiVLN, @TheEris, @catthetech and everyone providing configurations, captures and retest results. Those reports help distinguish working calls from cases that still need attention.
