# Changelog

## 2026.10.0-dev: modular full profiles and native ESP phones

- Voice Assistant returns to idle after its reply while keeping previously paused music paused.
- Interrupting TTS no longer leaves Home Assistant waiting for an announcement that has already stopped.
- Fixed a network lockup that could occur when interrupting HTTP audio playback.
- Ending a ringtone clears the player playlist as well as stopping the sound. A timer followed by a call no longer leaves the device showing an announcement after hangup.
- Shared packages can select HTTP playback, Sendspin, local announcements, call buttons, wake-word controls, timers and diagnostics separately. The full presets still assemble the complete feature set.
- P4 profiles now use the camera component directly from Psix-anp's repository. The component code matches our previous source.

Use **ESPHome 2026.9.0 or newer**. Review the [package guide](https://github.com/n-IA-hane/esphome-intercom/blob/dev/packages/README.md) and [breaking changes](https://github.com/n-IA-hane/esphome-intercom/blob/dev/docs/BREAKING_CHANGES.md) before rebuilding a custom YAML.

Waveshare S3 Audio and Spotpear were tested with direct calls, ringing, manual and automatic answer, Voice Assistant and overlapping media. P4 JPEG and H.264 were compiled with the upstream camera; this update does not claim a new P4 hardware qualification.

These changes require rebuilding and uploading ESP firmware. The existing Home Assistant features below remain included.

[Complete preview changes](https://github.com/n-IA-hane/esphome-intercom/releases/tag/v2026.10.0-dev).

Thank you to everyone supporting the project through GitHub Sponsors, including the latest donation, and to the contributors sharing fixes and hardware feedback.

---

## 2026.9.2: more reliable calls, audio fixes and built-in SIP capture

This stable release brings together call reliability fixes, improved ESP audio support, P4 display improvements and built-in SIP troubleshooting. Existing working configurations remain supported.

### Home Assistant calls

- **Forwarding and connecting calls:** improved cleanup when a call ends or fails while its audio/video connection is being prepared. This helps prevent an interrupted call from leaving resources occupied for the next one.
- **Hangup compatibility:** added support for providers that request authentication when ending an established call. The Swisscom capture confirmed this missing authentication retry; final confirmation on the affected account is still pending.
- **Repeated controls:** pressing Hangup or Decline again after a call has ended no longer produces an incorrect ownership error.
- **Phone menus:** the in-call keypad lets you interact with an IVR, for example to enter an extension or choose a department. It does not create a spoken IVR inside Home Assistant.

### Capture SIP directly from HA, including HA OS

The new **VoIP Stack: Capture SIP signaling** action helps diagnose calls without installing tcpdump or using SSH.

In **Developer Tools > Actions**, start a capture before the test call:

```yaml
action: voip_stack.capture_sip
data:
  operation: start
  duration: 120
```

After reproducing the problem, run the same action with `operation: stop`. Its response contains a complete `download_url`: copy it into your browser to download `voip-stack-sip.pcap` for Wireshark or a support report. If the link expires, `operation: status` provides a fresh one while the capture is retained.

The capture records this integration's SIP signaling, not conversation audio or all network traffic. Authentication header values are hidden, but phone numbers and network addresses can remain. Review the file before sharing it publicly. Recording stops automatically at the time or size limit; data is held in RAM and deleted 15 minutes after stopping, or immediately with `operation: clear`.

### ESP audio, firmware and P4 display

**ESP-side changes require rebuilding and uploading firmware. A HACS update alone does not update an ESP.**

- **P4 volume:** full landscape and VoIP-only profiles no longer force Master Volume to 1% on every boot. The saved setting can now be restored normally.
- **P4 date and time:** the full landscape display abbreviates the date when space is limited, keeping the complete clock on one line.
- **Announcements:** HTTP playback profiles include MP3 and WAV alongside FLAC. WAV announcements served without a filename extension are recognized, including the `audio/vnd.wave` content type.
- **Early microphone startup:** capture requested before component setup no longer depends on a semaphore that has not yet been created.
- **Microphone-only calls:** calls no longer time out simply because the device transmits audio without receiving it. Microphone gain controls also work without requiring a dummy speaker configuration.
- **Speaker playback:** incoming call audio can restart a speaker that has become idle.
- **Firmware updates:** shared OTA handling supports the larger Full images. The I2S interrupt fix addresses a P4 failure during flash writes that could cause an update to roll back.
- **Audio negotiation:** two-way calls select a compatible format. Explicit directional negotiation retains separate transmit and receive rates; no automatic PCM/Opus fallback has been added.

### Optional dual-microphone features

Standard I2S can feed two microphone slots into the existing dual-microphone processing path. Optional I2S left/right slot-level sensors can support sound-direction automations. The microphone output presented to applications remains mono.

Dual-microphone AFE configurations honor the requested VAD setting and can use optional output gain normalization when the underlying pipeline does not apply AGC. These features are optional and do not enable themselves in existing profiles. Changing AGC can rebuild the processing pipeline and briefly interrupt audio.

### Retesting and updating

Provider and board-specific reports, including Swisscom and some experimental ESP configurations, remain under investigation. These remaining reports are not declared resolved by this release. Original ESP32/A1S support and native locked-screen mobile calling have not been added.

1. Open VoIP Stack in HACS, choose **Redownload**, and select **2026.9.2**.
2. Restart Home Assistant.
3. Reload the browser or Companion app to load the matching card.
4. For ESP fixes, rebuild and upload the current `main` profile/components for your device.

When reporting a retest, include the integration version, board/profile and call direction. Check audio in both directions, hangup from both ends and a second call. Remove passwords and keys from shared configuration or logs.

### Thanks to the community

Thanks to @jharris4 for the P4 I2S interrupt fix ([Audio Stack PR #12](https://github.com/n-IA-hane/esphome-audio-stack/pull/12)), @benklop for standard I2S dual-microphone support ([PR #11](https://github.com/n-IA-hane/esphome-audio-stack/pull/11)), and @jyoushiki for the VAD/AGC contribution ([PR #8](https://github.com/n-IA-hane/esphome-audio-stack/pull/8)). Their authorship is retained in the incorporated changes.

Thanks also to @rvdv01, @MakaronaiVLN, @TheEris, @catthetech, @Frohnert59, @DunklerPhoenix and everyone providing configurations, captures and retest results.


---

## 2026.9.1: use automated phone menus, clearer controls and smoother calls

This release adds a keypad you can use during calls, improves audio playback,
and makes the phone controls easier to use.

### Use automated menus and extensions during a call

You can now interact with **IVRs, the automated phone menus that ask you to
"press 1 for support" or "enter an extension"**, directly from the Home Assistant
phone card.

Call the service or switchboard, open the keypad, and press the requested
numbers. The card sends the keypad signals, also called DTMF tones, to the
other phone system. This lets you choose a department or enter an extension
without leaving the call, when the receiving system supports those signals.

Before a call, the keypad still lets you enter the number you want to dial.
During a call, it controls the menu at the other end. The Hangup button remains
available while the keypad is open.

<p align="center">
  <img src="https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/v2026.9.1/docs/images/ha-softphone-in-call-keypad-2026-9-1.jpg" width="420" alt="Home Assistant phone keypad used during a call to select options in an automated phone menu"/>
</p>

### Easier controls on the Home Assistant card

- **Video calls:** open the keypad using the small icon beside Options. The two
  icons sit close together and no longer cover the call timer.
- **Incoming calls:** Answer and Decline appear even if you left the keypad or
  Options open during the previous call.
- **Send Camera:** the checkbox now reflects your selection correctly.
- **Turning video on later:** in calls between browser phones, you can enable
  the camera after starting with audio only, and the other person receives it.
- **Calling Assist:** fixed an issue that could stop the call while its audio
  was being set up.

### The same dialer on Spotpear Full and VoIP-only

Spotpear VoIP-only now has the same dialer layout as the Full profile, fitted
to its round screen. The delete key shows the correct icon. The clock and
status icons hide while the dialer is open, so they do not overlap the call
button, and return when you leave it.

This is the device's dialer for entering a destination. The in-call keypad for
interacting with automated menus described above is on the Home Assistant card.

### Smoother audio and more reliable call endings

Browser audio playback handles uneven delivery more smoothly, reducing
crackling and short gaps. Audio processing on ESP devices also preserves
samples that could previously be dropped, and stopping or restarting audio
is more reliable, including during firmware updates.

Cancelling a call before the other person answers now fully releases the
resources it was using. This prevents repeated unanswered calls from gradually
leaving fewer resources available for new calls.

Call controls also handle interrupted connections to Home Assistant more
reliably. Ending one call should not leave its screen or background work
interfering with the next call.

### Better compatibility with other SIP phones and switchboards

ESPHome phones now tell Home Assistant which audio formats they actually
support. Compatible devices can call each other directly. When two phones need
different audio formats, Home Assistant can convert the audio between them;
this conversion is called transcoding.

Connection and sign-in fixes improve compatibility with some door stations,
SIP phones and switchboards. Calls, forwarding and groups also handle SIP
addresses without an explicitly specified port correctly. These changes improve
interoperability, but do not mean every phone or provider has been tested.

### Opus on supported VoIP-only devices; PCM on Full profiles and P4

Opus and PCM are the two supported audio formats:

- **Spotpear and WS3 VoIP-only profiles use Opus by default.** These profiles
  focus on phone calls and have more resources available for audio compression.
- **Full profiles and P4 keep PCM.** This leaves resources available for features
  such as the voice assistant, wake word detection, music playback and video.

Each firmware uses the format selected in its configuration. If the other phone
cannot use that format, the call needs Home Assistant to convert the audio;
the ESP does not silently switch between Opus and PCM.

Use the maintained YAML for your device and profile so its component versions
and audio settings stay together. No manual change to a different speaker
component is required for these profiles.

### Starfleet: optional assistant theme

Give your assistant a Starfleet-inspired look, with an animated home screen
and matching listening, thinking, error and other assistant screens.

Choose it in your own configuration with `ai_avatar: starfleet`. It is entirely
optional: the maintained YAMLs keep their current default theme. A matching
ringtone is also available separately; choosing the avatar does not change
your ringtone automatically.

<p align="center">
  <img src="https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/v2026.9.1/assets/images/assistant/starfleet/idle_00.png" width="240" alt="Optional Starfleet assistant theme"/>
</p>

See the [theme preview and instructions](https://github.com/n-IA-hane/esphome-intercom/blob/v2026.9.1/assets/images/assistant/starfleet/README.md).

### Other improvements

- Improved discovery of devices with larger audio/video configurations,
  including P4 profiles.
- Reduced blocking work during Home Assistant setup.
- Updated ESP component integration and memory use for the maintained profiles.
- A clearer warning when the Home Assistant integration and ESP component
  versions do not match.

### Compatibility notes

Compatibility can vary between Android phones and external switchboards.
Not every device or combination of simultaneous features has been tested.

JPEG and H.264 remain separate video profiles. Camera speed depends on the
resolution, device and other features running at the same time; a configured
frame rate is a maximum, not a guaranteed result.

On the **OnePlus Nord 5**, the current workaround is to use the display's
**60 Hz mode**. Higher refresh rates can still affect calls in the Companion
app and are not yet fully validated.

### Community credits

Thank you to **[@rvdv01](https://github.com/rvdv01)** for creating and sharing
the Starfleet assistant artwork and optional ringtone in
[discussion #110](https://github.com/n-IA-hane/esphome-intercom/discussions/110).
It is great to see the shared avatar format used for a community contribution!

### Updating

1. In HACS, open VoIP Stack and install the **2026.9.1** update.
2. Restart Home Assistant, then reload the browser or Companion app so it loads
   the updated card. Clear its cache if the old controls still appear.
3. For ESP firmware updates, use the maintained stable YAML for your
   device. The YAMLs reference the matching `@main` components.

Updating the Home Assistant integration does not flash your ESP devices.

If something goes wrong, include the device models, who called whom, whether
video was enabled, and what you expected to happen. If you attach logs, remove
passwords and other private information first.

For setup instructions and technical details, see the
[user guide](https://github.com/n-IA-hane/esphome-intercom/blob/v2026.9.1/docs/USER_GUIDE.md)
and [P4 configuration guide](https://github.com/n-IA-hane/esphome-intercom/blob/v2026.9.1/docs/P4_HARDWARE_AND_MEDIA.md).
