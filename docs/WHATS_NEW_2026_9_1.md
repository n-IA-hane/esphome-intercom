# 2026.9.1-dev: use automated phone menus, clearer controls and smoother calls

This preview adds a keypad you can use during calls, improves audio playback,
and makes the phone controls easier to use. `2026.9.0` remains the stable release.

## Use automated menus and extensions during a call

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
  <img src="https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/dev/docs/images/ha-softphone-in-call-keypad-2026-9-1.jpg" width="420" alt="Home Assistant phone keypad used during a call to select options in an automated phone menu"/>
</p>

## Easier controls on the Home Assistant card

- **Video calls:** open the keypad using the small icon beside Options. The two
  icons sit close together and no longer cover the call timer.
- **Incoming calls:** Answer and Decline appear even if you left the keypad or
  Options open during the previous call.
- **Send Camera:** the checkbox now reflects your selection correctly.
- **Turning video on later:** in calls between browser phones, you can enable
  the camera after starting with audio only, and the other person receives it.
- **Calling Assist:** fixed an issue that could stop the call while its audio
  was being set up.

## The same dialer on Spotpear Full and VoIP-only

Spotpear VoIP-only now has the same dialer layout as the Full profile, fitted
to its round screen. The delete key shows the correct icon. The clock and
status icons hide while the dialer is open, so they do not overlap the call
button, and return when you leave it.

This is the device's dialer for entering a destination. The in-call keypad for
interacting with automated menus described above is on the Home Assistant card.

## Smoother audio and more reliable call endings

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

## Better compatibility with other SIP phones and switchboards

ESPHome phones now tell Home Assistant which audio formats they actually
support. Compatible devices can call each other directly. When two phones need
different audio formats, Home Assistant can convert the audio between them;
this conversion is called transcoding.

Connection and sign-in fixes improve compatibility with some door stations,
SIP phones and switchboards. Calls, forwarding and groups also handle SIP
addresses without an explicitly specified port correctly. These changes improve
interoperability, but do not mean every phone or provider has been tested.

## Opus on supported VoIP-only devices; PCM on Full profiles and P4

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

## Other improvements

- Improved discovery of devices with larger audio/video configurations,
  including P4 profiles.
- Reduced blocking work during Home Assistant setup.
- Updated ESP component integration and memory use for the maintained profiles.
- A clearer warning when the Home Assistant integration and ESP component
  versions do not match.

## What is still being checked

This is a development preview. Some Android phones, external switchboards and
combinations of simultaneous features still need further testing.

JPEG and H.264 remain separate video profiles. Camera speed depends on the
resolution, device and other features running at the same time; a configured
frame rate is a maximum, not a guaranteed result.

On the **OnePlus Nord 5**, the current workaround is to use the display's
**60 Hz mode**. Higher refresh rates can still affect calls in the Companion
app and are not yet fully validated.

## Updating

1. In HACS, open VoIP Stack, select **Redownload**, and choose **2026.9.1-dev**.
2. Restart Home Assistant, then reload the browser or Companion app so it loads
   the updated card. Clear its cache if the old controls still appear.
3. For ESP firmware updates, use the maintained development YAML for your
   device. The YAMLs reference the matching `@dev` components.

Updating the Home Assistant integration does not flash your ESP devices.

If something goes wrong, include the device models, who called whom, whether
video was enabled, and what you expected to happen. If you attach logs, remove
passwords and other private information first.

For setup instructions and technical details, see the
[user guide](https://github.com/n-IA-hane/esphome-intercom/blob/dev/docs/USER_GUIDE.md)
and [P4 configuration guide](https://github.com/n-IA-hane/esphome-intercom/blob/dev/docs/P4_HARDWARE_AND_MEDIA.md).
