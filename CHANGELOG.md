# Changelog

## 2026.10.3-dev

### Recorded audio in Automation calls

- Add `voip_stack.play_media` for [#138](https://github.com/n-IA-hane/esphome-intercom/issues/138).
  Select a file with Home Assistant's Media picker or provide an HTTP(S) audio
  URL. HA sends the recording through the existing VoIP call, without a browser.
  Combine Call, Play audio and Hang up for recorded reminders, notifications or
  sound effects. Playback waits for transmission to complete before the next
  action and shares the TTS call ownership, timeout and cancellation handling.


Changes since stable **2026.10.1**.

- Add opt-in native HA response templates for VoIP voice commands, preserving
  existing custom sentences. English, Italian and German examples include
  resolved contact names and localized errors. Based on
  [#137](https://github.com/n-IA-hane/esphome-intercom/pull/137).
- Reconcile every mixer source when forced shared-output stop discards accepted
  audio, and wait for the mixer writer to exit before stopping that output.
  Together with the Audio Stack pending-reset admission fix, this addresses two
  reproduced playback-stall paths from the investigation of
  [#129](https://github.com/n-IA-hane/esphome-intercom/issues/129). Three targeted
  Spotpear cycles passed; long-uptime validation remains open.


- Add an explicit Dahua symmetric-audio mode: after validated incoming audio,
  transmit with the same codec only when negotiated in that direction. Keep
  browser/application PCM stable, use the answer's TX payload mapping and
  preserve DTMF across changes. RTP clock changes use a new source identity.
  This provides a targeted hardware test for the remaining one-way audio in
  [#115](https://github.com/n-IA-hane/esphome-intercom/issues/115), without
  changing the default policy for other devices.

- Expose Dahua compatibility and audio-offer choices in contact creation,
  editing and `add_contact`. Static Dahua contacts use common 20 ms audio and
  the existing receive-only H.264 profile without pretending to be registered.
  An explicitly profiled destination retains that behavior through a trunk;
  ordinary trunk destinations remain unchanged. Includes a controlled PCM-offer
  comparison for the remaining transmit-audio report in
  [#115](https://github.com/n-IA-hane/esphome-intercom/issues/115). Physical
  confirmation remains pending; no blanket transmit-audio fix is claimed.

- Let ordinary HA automations originate backend audio calls from an Automation
  contact, wait for answer, speak through the existing TTS action and hang up
  using an execution-bound call reference. No browser or physical source phone
  is required. Includes a weekday wake-up example for
  [#136](https://github.com/n-IA-hane/esphome-intercom/issues/136).
- Resolve SIP transfers to local Automation contacts through HA's reachable
  listener and the contact's extension, rather than the remote phone's host.
  Explicit SIP/SIPS targets retain their existing behavior.

- Fix negotiated receive audio being restricted to the preferred transmit codec
  when a SIP answer accepts multiple codecs. Keep the receive payload mappings
  from the local offer, decode accepted alternatives into the existing PCM
  contract, and preserve compatible RTP passthrough. Covers Dahua PCM/16000
  received while HA transmits PCMU, reported in
  [#115](https://github.com/n-IA-hane/esphome-intercom/issues/115). Re-INVITE
  updates preserve a bounded transition window and reject conflicting payload
  reassignment before committing media. No ESP firmware update is required.

- Add **Set external call access** (`voip_stack.set_external_call_access`) to
  persistently allow or block new trunk calls per phone. All phones are allowed
  by default. Restrictions follow stable identity and apply to HA routing,
  forwards and transfers. Incoming/internal and established calls continue.
  Requested in [#119](https://github.com/n-IA-hane/esphome-intercom/issues/119).

- Add **Wait without answering** (`voip_stack.wait_unanswered`) for Automation
  contacts. Keep SIP ringing without accepting the call or starting media,
  then continue the automation or end unanswered at its completion. Caller
  cancellation stops the wait. Includes a provider no-answer routing recipe
  for [#117](https://github.com/n-IA-hane/esphome-intercom/issues/117).

- Add an experimental ESP32-S3-BOX-3 Full AFE profile using both ES7210
  microphones through standard I2S and a software echo reference. Reuses the
  existing dual-mic backend and includes per-slot level diagnostics. Firmware
  compilation passed; physical qualification is pending in
  [#116](https://github.com/n-IA-hane/esphome-intercom/issues/116).

- Fix outgoing browser calls to recognized Dahua SIP peers with the local
  camera off: offer receive-only H.264 mode 0 when SIP video is enabled, so a
  VTO response with video and no H.264 format parameters can establish audio
  and incoming video. Camera capture, other peer offers and strict SDP media
  ordering remain unchanged. Covers the reported negotiation in
  [#115](https://github.com/n-IA-hane/esphome-intercom/issues/115); physical
  device confirmation is still pending.
- Archive the experimental native Companion calling and dashboard-menu
  composer. Remove their runtime/API/UI code and migrate away their virtual
  phone subentries. Official mobile_app registrations and ordinary browser,
  SIP and ESPHome phones are preserved. Historical source and design notes
  remain on the dedicated archive branch.

## 2026.10.1

Changes since stable **2026.10.0**.

### Home Assistant backend and dashboard card

- Fix phonebook delivery to remote ESPHome devices. A remote test exposed an
  unnecessary lookup using the ESP's advertised IP address, which could differ
  from the address configured in HA. Delivery now uses the resolved ESPHome
  service identity and the existing API connection. This requires an integration
  update and HA restart, with no ESP firmware update.
- Keep audio calls established when a SIP peer answers a one-way video offer
  with the same one-way direction. The effective video direction is bounded by
  both peers, becoming inactive when neither can supply the requested stream;
  a later video change can restore it on the same call.
- Retry the original UDP route when a size-triggered TCP connection attempt is
  explicitly rejected before sending the INVITE. This allows large offers to
  reach UDP-only peers without overriding explicit TCP/TLS configuration.
- Add Call-ID, transport, destination, elapsed time and OS error details to
  outbound connection diagnostics, including failures absent from SIP captures.
- Fix direct inbound trunk routing: resolve the called phonebook destination
  before using the fallback, and preserve the original called number for routing
  automations. An Assist/group fallback no longer bypasses the automation window.
- Add a **Register the trunk** choice. Static trunks can place and receive calls
  without REGISTER, using an explicit list of permitted incoming SIP source IPs
  or CIDR networks. Credentials are optional in this mode; INVITE digest remains
  available when required by the peer.
- Show source-IP settings only for static trunks and registration expiry only
  for registered trunks. Diagnostics distinguish registration from route availability.
- Let circular call buttons grow to fit longer translated labels while preserving
  their shape and placement.

Static-trunk qualification covers real baresip calls over UDP and TCP, measured
bidirectional audio, termination from both sides, repeated calls, SIP captures
without REGISTER, and rejection of an untrusted source attempting external routing.
Tests against the requesting user's gateway and carrier-specific deployments are
still requested. Setup: [SIP trunk guide](docs/SIP_TRUNK.md#static-trunk-without-register).

### ESPHome configuration

#### Experimental FULL support: Waveshare 1.85C-BOX V2

Add a FULL profile for **Waveshare ESP32-S3-Touch-LCD-1.85C-BOX V2
(SKU 30684)**, combining dual-microphone AFE/AEC, micro wake word, Home Assistant
Voice Assistant, bidirectional VoIP, HTTP media, Sendspin and the circular
360x360 touch interface with animated avatar, contacts and dialpad.

It was a tough one, but the S3 BOX FULL is finally in testing!

Getting the complete workload running together required a temporary micro wake
word fork with explicit CPU core affinity. We plan to propose that option to
ESPHome; no upstream PR for it has been submitted yet. The profile separates
wake word inference from dual-microphone AFE processing and places audio tasks
deliberately across the two cores.

Audio work also uncovered processed samples waiting inside GMF for another input
iteration. The updated Audio Stack receives them directly from the existing AFE
fetch task. This path adds no task or audio buffer. Profiling then guided placement
of selected FFT and dot-product functions in internal instruction RAM, at a measured
cost of 1,556 internal bytes. QIO flash access and cache settings complete this
board's configuration. CPU scheduling and memory placement were measured with the
full workload rather than dropping microphones or other features to make it fit.

**Display performance:** animations can look uneven and show tearing, as also
observed with the supplied factory firmware. This is a rendering performance
limitation of this board with the FULL profile, rather than an outstanding bug
scheduled for correction. The complete profile remains experimental.

Hardware testing covered calls in both directions with the WS3 Audio, music
playing during calls, AEC switching during a call, and Assist/call transitions.
Support starts as experimental while broader everyday-use feedback is collected.
This profile targets the Waveshare V2 hardware specifically; the V1 and
Espressif ESP32-S3-BOX-3 require different configurations.

[FULL YAML](yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml)

| Avatar and status display | Touch dialpad |
| --- | --- |
| ![Animated-avatar screen on the Waveshare 1.85C-BOX V2](docs/images/waveshare-185c-box-v2/avatar.jpg) | ![VoIP touch dialpad on the Waveshare 1.85C-BOX V2](docs/images/waveshare-185c-box-v2/dialpad.jpg) |

![V2 model label identifying the supported Waveshare hardware](docs/images/waveshare-185c-box-v2/model-label.jpg)

#### Other ESPHome changes

- Backport [ESPHome's mixer restart fix](https://github.com/esphome/esphome/pull/19368): a start request received while
  the previous worker is stopping survives cleanup. No new worker or buffer is added.
- Playback queue-full errors now include queue and source/speaker state snapshots
  to help investigate persistent audio stalls. Long-uptime validation for #129
  remains in progress; updating HACS alone does not update an ESP firmware.

Use existing Audio Stack actions and conditions without fixed IDs. Headless
profiles also use automatic VoIP and runtime-observer binding. Replace simple
VoIP state lambdas with component conditions, preserving active-versus-idle
semantics. Display rendering keeps explicit IDs where it reads phone data.

## 2026.10.0: build call flows with Home Assistant automations

Changes since stable **2026.9.2**.

### Home Assistant backend and dashboard card

- **Greetings in the browser phone:** the card now signals when audio playback
  is ready, allowing an automation greeting to wait for the browser before starting.
- **Audio recovery in the card:** after a brief shortage of incoming audio, playback
  can resume with the samples needed for recovery instead of waiting for the full
  initial buffering target again. This reduces unnecessarily long recovery pauses.
- **External calls from registered SIP phones:** phones registered to HA and
  calls forwarded by automations now reuse the trunk's established signaling
  connection, extending the behavior previously used by the dashboard phone.
- **Duplicate contact names:** conflicting phonebook names now produce an error
  and a persistent HA notification. The message makes the naming problem visible
  instead of leaving users to diagnose an ambiguous call destination.
- **Calling Assist:** the advanced call-details option now controls whether an
  opening message containing caller information is sent to the assistant.
  Disable it to start listening without that introduction. The HA configuration
  also accepts longer extensions.
- **Adding or changing video during a call:** fixes address frozen or missing
  P4 video when a SIP peer enables video after answering, switches its source
  or changes camera direction. Turning off **Send video** stops the local camera
  transmission while allowing incoming video to continue.

### Automations as dialplan

You can now decide what happens to a call using Home Assistant's automation
editor: play a greeting, ask the caller to press a key, ring a selected phone,
forward to Assist or choose another destination when nobody answers.

Create an **Automation contact**, such as `Welcome`, with an optional extension.
Calling that contact starts your automation without creating another phone device.

- Choose VoIP call triggers, conditions and actions directly in the editor.
  Filter a trigger by destination, caller or call origin, instead of writing a
  generic event trigger followed by extra filtering conditions.
- Use **Speak to the caller** to choose a TTS provider and enter a message.
  Playback completes before the next action runs, so you can forward immediately
  after the greeting without adding an arbitrary delay.
- Forward by contact name or extension. Actions started by a native call trigger
  keep their association with that call across delays and scripts the automation
  waits for;
  you do not need to copy a changing Call-ID into the automation.
- Build an IVR, a spoken menu where the caller presses a DTMF key to choose a
  destination. Configure the choices, waiting time and what happens when no
  valid key is entered.
- Handle unanswered calls with another destination, a greeting or Assist.

These rules override phonebook routing for the calls they handle. The
[illustrated automation cookbook](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/AUTOMATION_DIALPLAN.md)
walks through the editor and provides examples for greetings, opening hours,
keypad menus, transfers and door controls.

![Filter incoming calls by destination in the Home Assistant automation editor](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/main/docs/images/automation-call-filter.png)

### Update Home Assistant

Requires **Home Assistant 2026.7.0 or newer**.

HA and ESP firmware do not need matching version numbers for this release.
Updating the HACS integration does not itself require reflashing your ESPs.

Update VoIP Stack through HACS, restart Home Assistant, then reload the dashboard
or clear the Companion app's frontend cache so the updated card is loaded.

When replacing an old event-based automation with a native VoIP trigger,
disable the old rule to avoid handling the same call twice. Follow the
[cookbook migration steps](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/AUTOMATION_DIALPLAN.md#move-existing-automations-gradually).

### ESP firmware: Generic validation and device improvements

- **Simpler YAML examples:** redundant `esp_audio_stack_id` and `voip_stack_id`
  references are omitted. Each component supports one instance per ESP, and its
  child platforms already resolve that instance automatically.
- **Simpler phone setup:** HA discovery, phonebook reception and call controls
  are built into the ESP VoIP component. Separate VoIP HA packages are retired;
  see the migration steps below.
- **P4 display:** received JPEG video uses more of the screen. The call screen
  shows **Direct** or **HA transcoding** below the call heading, and a subsequent
  audio-only call no longer inherits the video screen.
- **Generic S3 audio:** testing exposed CPU contention between microphone/AEC
  processing and VoIP work. Generic VoIP-only and single-bus Full/Full Lite
  profiles put capture and echo cancellation on core 1, separate from VoIP
  tasks on core 0. VoIP-only profiles also reduce the AEC filter length from 8 to 4. This addresses microphone
  audio falling behind and being discarded during bidirectional calls.
- **New Generic Full Lite for ESP32-S3 boards with 4 MB of flash:** leaving
  Sendspin out makes room for a voice and intercom profile that includes Voice
  Assistant, wake word, AEC, VoIP, TTS, HTTP music and timers. The single-bus Lite
  YAML disables Sendspin and uses compiler size optimization to fit the standard
  4 MB OTA layout. **PSRAM is still required.** The profile is experimental, with
  limited room for extra features; the full 8 MB profile includes Sendspin.
- **Voice and playback:** replies can finish correctly while music remains
  paused. Interrupted announcements report completion, and playback shutdown
  handles unrelated task notifications correctly.
- **Modular runtime packages:** voice controls, call buttons, media, Sendspin,
  timers and presentation can be selected separately. The updated controller
  excludes unused observer, LED and VoIP support, including their optional
  stored state.
- **Easier troubleshooting:** shared controls add Audio and VoIP diagnostic
  buttons for collecting the current device state without enabling packet tracing.
- **Upstream components:** maintained YAMLs now use ESPHome's SPI and HTTP audio
  components, and P4 profiles obtain the camera component from Psix-anp's repository.
- **Experimental Voice PE profile:** updated VoIP packet timing and center-button
  handling for answering incoming calls.

The Generic single-bus reference board was retested with VoIP-only, Full AEC
and Full Lite configurations. The checks included real calls, calls with music
playing, announcements over paused music, HA card audio capture and calls between
the Generic Lite and Waveshare S3 in both directions. The assistant's announcement
completion was also checked through HA, including its return to idle.

The Generic single-bus Full and Lite examples now set Wi-Fi transmit power to
15 dB and document how to adjust it or restore the ESP-IDF default. See the
[deployment guide](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/DEPLOYMENT_GUIDE.md#generic-s3-full-and-full-lite).

### Update ESP firmware

**We recommend rebuilding your ESP firmware from an updated maintained YAML,**
especially on Generic S3 devices, to receive the scheduling fixes and revised
package setup. This recommendation is separate from updating HA.

Use **ESPHome 2026.9.0 or newer**. The updated firmware packages use ESP VoIP
Stack **2026.10.0**, Runtime Controller **2026.10.0** and Audio Stack **2026.10.1**.
When adopting these packages, update their component sources together.

Start from an updated
[maintained YAML](https://github.com/n-IA-hane/esphome-intercom/tree/main/yamls)
and reapply your board settings and customizations.

**Alternatively, update your existing YAML:**

- Comment out or remove package entries for `voip/ha_phone.yaml`,
  `voip/ha_integration.yaml`, `voip/ha_actions.yaml`, `voip/ha_api.yaml` and
  `voip/phonebook_subscribe.yaml`. Leaving them enabled fails validation.
- In full profiles, replace `voip/ha_api_runtime.yaml` with
  `runtime/ha_connectivity.yaml`. Add `diagnostics/runtime.yaml` separately if
  you want its diagnostic API actions.
- Add `custom_services: true` to the existing `api:` block. Keep `voip_stack:`
  and your hardware, audio, display and ringtone packages.
- Remove `spi` and `audio_http` from external-component lists that point to
  this repository. Read the migration instructions for the replacement settings.

[Complete breaking changes](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/BREAKING_CHANGES.md)
| [ESP phone migration](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/ESP_ENTITY_SURFACE.md)
| [Modular package guide](https://github.com/n-IA-hane/esphome-intercom/blob/main/packages/README.md)

### Diagnostics and useful bug reports

Collect the information for the part of the system that is failing. **If you
use only VoIP Stack on HA, a browser card or SIP equipment without an ESP, skip
the ESP instructions below.** No ESP firmware or Audio Stack is required to
report an HA problem.

For HA issues, attach the integration diagnostics and HA logs. For a card issue,
also include the browser or Companion app version and any relevant console
errors. Use the built-in SIP capture when the failing call's SIP signaling
passes through HA.

1. Describe how to reproduce the fault, what you expected and what happened.
   Include HA and integration versions, caller, destination and SIP transport.
   For ESP-related faults, also include ESPHome/component versions, the exact
   board and relevant firmware YAML with credentials removed.
2. Download the VoIP Stack integration diagnostics from HA and include the HA
   logs around the failure. For an ESP, include its boot log and complete
   **VoIP Diagnostics** output during the fault and after hangup. Include
   **Audio Diagnostics** too if the device uses Audio Stack.
3. For call setup, hangup or video negotiation problems handled by HA, start
   this capture in **Developer Tools > Actions** before reproducing the issue:

```yaml
action: voip_stack.capture_sip
data:
  operation: start
  duration: 120
```

After the test, run the action with `operation: stop`. Download the PCAP using
`download_url` from the response and attach the file, not the temporary link.
This captures SIP signaling handled by HA, not RTP audio/video or direct
ESP-to-ESP signaling. The
[diagnostic guide](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/troubleshooting.md)
explains the limits and additional information to collect.

**Only if the problem involves an ESP device, run its diagnostic dumps:**

Open the device's ESPHome logs with `logger` at INFO or a more verbose level.
While the problem is present, press **VoIP Diagnostics** and, if the firmware
uses Audio Stack, **Audio Diagnostics**. Repeat after hangup and attach the
complete output from `BEGIN v=1` through `END v=1` for each component, plus the
boot log.

In Home Assistant, these are device buttons. You can press them on the device
page, or open **Developer Tools > Actions**, select **Button: Press**
(`button.press`) and choose the diagnostic button entities for that ESP.
The output appears in the **ESPHome device logs**, not in the HA action response.

Updated maintained profiles already include the applicable buttons. If they are
missing from a custom YAML, add the relevant definitions below, then rebuild and
upload with the updated components:

```yaml
button:
  - platform: template
    name: VoIP Diagnostics
    entity_category: diagnostic
    on_press:
      - voip_stack.dump_diagnostics:
          id: phone

  - platform: template
    name: Audio Diagnostics
    entity_category: diagnostic
    on_press:
      - esp_audio_stack.dump_diagnostics:
          id: audio_stack
```

Replace `phone` and `audio_stack` with your actual component IDs. Omit the Audio
Diagnostics button if the device does not use Audio Stack. Do not add duplicate
buttons when your packages already provide them. The two `dump_diagnostics`
entries are **ESPHome firmware actions**, not HA services to paste directly into
HA's Actions editor. These dumps do not require verbose audio tracing.

**Example report, replace the details with your own:**

```text
Title: ESP receives audio but the browser caller hears silence

Versions: [HA], [VoIP Stack HA], [ESPHome], [ESP VoIP Stack], [Audio Stack if used]
Hardware: [exact ESP board, microphone, speaker and codec]
Call path: HA browser card -> Generic S3, through HA, SIP UDP
Steps: call the ESP, answer, speak into each microphone, then hang up from HA
Expected: audio in both directions; both endpoints return to idle after hangup
Actual: the ESP plays browser audio, but the browser hears silence; hangup works
Frequency: reproduced in 3 consecutive calls after reboot
Test time and timezone: [when the call was made]
Attachments: sanitized YAML, HA diagnostics, HA/ESP logs,
             complete VoIP/Audio diagnostic blocks, SIP capture
```

Review attachments for passwords, tokens, authentication data and other private
information before sharing them.

If you ignore these instructions and open an "it does not work" issue without
reproduction details or supporting logs, I'll get pissed off like there's no
tomorrow and close it as incomplete.

Thanks to everyone who donated to support the project.

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
