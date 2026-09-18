# 2026.10.0-dev: simpler ESP phones and automations as dialplan

## ESP phones no longer need a VoIP HA package

The VoIP component now provides its Home Assistant discovery, call controls
and phonebook reception directly. A custom phone no longer needs a collection
of HA glue packages before it can appear in the central phonebook.

Compile with **ESPHome 2026.9.0 or newer** and enable `custom_services: true`
in your existing `api:` block. Keep your board's microphone, speaker and
network settings. Native ESPHome audio and Audio Stack are both supported,
including microphone-only and speaker-only devices.

**Breaking change:** remove the retired VoIP HA packages before rebuilding.
Do not remove hardware, display or ringtone packages. The
[migration guide](ESP_ENTITY_SURFACE.md) lists the exact changes. Old package
includes now produce a useful validation error instead of duplicate controls.

The existing entity and action names are retained. HA still owns the central
phonebook; the ESP still owns its calls and local contacts. Standalone SIP
remains possible, and discovery does not force every call through HA.

ESPHome 2026.9 includes the SPI PSRAM-DMA and persistent HTTP audio-buffer
changes we previously carried in local copies. Those copies have been removed;
the remaining narrow adapters are documented in the migration notes.

This preview also includes the correction for streamed Assist responses that
could leave a device waiting after audio had ended, and removes the HA
integration's eight-character extension restriction.


This preview adds telephone services controlled by Home Assistant automations and fixes the unwanted initial response when calling Assist. **2026.9.2 remains the stable release.**

The existing **Provide advanced call details to the assistant** option now controls the entire opening message:

- **Off:** Assist starts listening directly. No caller name, number or automatic text message is sent to the conversation agent.
- **On:** Assist receives the caller name and telephone details once at the beginning, then continues listening as before.

The option stays off by default. No additional setting is needed for this Assist behavior.

## Create call automations in the Home Assistant editor

Build a greeting, add an optional delay, then forward the same caller to a
phone, group or your voice assistant. Native VoIP call triggers select the
call automatically, so ordinary sequences need no Call-ID templates or
blueprint imports.

- **Add contact > Automation** creates a callable service with a name and an
  optional extension, without creating an extra phone device.
- Filter triggers by caller, destination or local/trunk origin directly in the
  editor. Normal phonebook routing remains the default.
- **Speak to the caller** selects your TTS provider and message. Browser
  greetings wait for the audio connection to be ready.
- **Wait for keypad input** and **Caller keypad input matches** build menus
  such as "press 1 for reception, 2 for the assistant".
- **VoIP call unanswered** handles a phone still ringing after your chosen
  duration. Answering or moving the call cancels that wait.
- Call actions keep their original call across delays and synchronous scripts.
  Finishing an information-only automation ends its call; a successful forward
  lets the new destination continue the conversation.
- Dedicated icons and a guide with cropped editor screenshots make the new
  triggers, conditions and actions easier to find.

The [illustrated Automations as dialplan cookbook](https://github.com/n-IA-hane/esphome-intercom/blob/dev/docs/AUTOMATION_DIALPLAN.md)
walks through a first greeting, optional delays, forwarding by name or
extension, keypad menus and persistent notifications for received keys.
Existing contacts are migrated to the native contact editor. Existing event
and state automations remain available; the guide explains how to migrate
rules gradually.

## A quick look at the editor

**Choose when the automation starts.** Search for VoIP, then pick the call event
that fits your rule, such as a received call or a call that has not been answered.

<img src="https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/dd3db31bc0232a3a01b0252564721ba49d43b362/docs/images/automation-trigger-picker.png" alt="Home Assistant picker showing the VoIP call triggers" width="900">

**Build the conversation one step at a time.** This example speaks a greeting,
shows an optional delay before forwarding. Omit Delay to connect the caller
immediately after the greeting. You can create these steps
in the normal automation editor.

<img src="https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/dd3db31bc0232a3a01b0252564721ba49d43b362/docs/images/automation-greeting-delay-forward.png" alt="Automation actions: speak to the caller, wait 20 seconds, then forward" width="960">

**Let the caller choose with the keypad.** After Wait for keypad input, this
condition matches key 1. Use it in a Choose block to send that caller to
reception, another phone or your assistant.

<img src="https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/dd3db31bc0232a3a01b0252564721ba49d43b362/docs/images/automation-keypad-condition.png" alt="Keypad condition configured to match received digit 1" width="500">

## Named telephone services driven by automations

Create a phonebook contact with `type: automation`, for example **Welcome**, and
optionally give it an extension such as **666**. It does not need a physical
device or an open browser. Calls to that contact trigger an HA automation.

Use **VoIP Stack: Speak to the caller** (`voip_stack.tts_say`) to select
a TTS entity such as Piper or xTTS and the message to say. The next action runs
after the greeting has been sent, so `voip_stack.forward` can then connect the
same caller to the configured Assist endpoint, another SIP phone or a ring group.

The contact can have an optional fallback if no automation handles the call.
Separate calls to the same contact remain independent. Hanging up cancels the
announcement. An automation can use HA's normal `continue_on_error` option to
skip a failed greeting and continue to the next action.

See the [complete automation examples](https://github.com/n-IA-hane/esphome-intercom/blob/dev/docs/AUTOMATION_DIALPLAN.md#automation-contacts)
and the [greeting and forwarding walkthrough](https://github.com/n-IA-hane/esphome-intercom/blob/dev/docs/AUTOMATION_DIALPLAN.md#forward-after-the-greeting).
Use the actual name or extension of your configured assistant as the forwarding
destination. This first version plays announcements on automation calls, not
inside an existing conversation between two people.

Real browser and SIP calls were used to check greetings, xTTS-to-Assist,
DTMF input and call cleanup. Additional device and provider feedback is still
welcome; this preview does not claim every cookbook combination is qualified.
The HA automation features can be used without adopting the new ESP firmware interface.

## More reliable greetings and forwarding

- A greeting can connect the caller to a browser phone, a registered SIP phone or a ring group.
- An unanswered browser call can move to another phone, a group or Assist. If forwarding fails and you selected Resume, the original phone can still answer.
- Successful transfers now finish the original call instead of leaving it active.
- Assist keeps the beginning of short spoken requests, and greetings continue cleanly into the next call step.
- Keypad events remain available to automations, while **Phonebook as dialplan** continues to work without automation rules. **Automations as dialplan** overrides the calls your rules handle.

This update includes real browser and SIP call checks, audio recordings and repeated keypad tests. Device and provider feedback is still welcome.

## Clear warnings for duplicate phonebook names

Phonebook names must be unique, including browser phones and Assist pipelines,
even if their extensions differ. Conflicting names now produce an error log and
a persistent Home Assistant notification identifying the destinations to rename.
The notification clears once the conflict is corrected.

## Consistent external calls from SIP phones

Registered SIP phones and automation forwards now reuse the trunk's registered
UDP connection in the same way as the dashboard phone. This addresses a path
difference that can cause a provider to reject a SIP phone's external call while
accepting calls from the card. Confirmation from affected providers is welcome.

The cookbook also explains how to combine a greeting, keypad input and choices
into an IVR, including no-input handling and forwarding to a real voicemail
service when one is available.

## Install and test

1. Open VoIP Stack in HACS, enable prerelease versions if necessary, choose **Redownload**, and select **2026.10.0-dev**.
2. Restart Home Assistant, then refresh the browser or reset the Companion app frontend cache. Redownload even if the same preview version is already installed to obtain this updated package.
3. In VoIP Stack's configuration, leave **Provide advanced call details to the assistant** off and call the Assist extension. Check that it waits for your speech without first replying to a caller-information message.
4. If you use the advanced details with a conversational agent, enable the option and check that your existing telephone behavior still works.

For the new native ESP integration, update ESPHome to at least 2026.9.0,
follow the [firmware migration guide](ESP_ENTITY_SURFACE.md), then compile and
upload your device. Installing the HACS archive does not update ESP firmware.
Existing firmware can remain installed while you migrate custom YAMLs.

Please report your conversation agent, the option state and the result in [issue #120](https://github.com/n-IA-hane/esphome-intercom/issues/120). The local automated checks pass; confirmation from the affected installation is welcome.
