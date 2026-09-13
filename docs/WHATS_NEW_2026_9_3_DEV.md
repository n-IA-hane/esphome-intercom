# 2026.9.3-dev: automation contacts, call greetings and Assist listening

This preview adds telephone services controlled by Home Assistant automations and fixes the unwanted initial response when calling Assist. **2026.9.2 remains the stable release.**

The existing **Provide advanced call details to the assistant** option now controls the entire opening message:

- **Off:** Assist starts listening directly. No caller name, number or automatic text message is sent to the conversation agent.
- **On:** Assist receives the caller name and telephone details once at the beginning, then continues listening as before.

The option stays off by default. No additional setting is needed for this Assist behavior.

## Named telephone services driven by automations

Create a phonebook contact with `type: automation`, for example **Welcome**, and
optionally give it an extension such as **666**. It does not need a physical
device or an open browser. Calls to that contact trigger an HA automation.

Use **VoIP Stack: Speak to an automation call** (`voip_stack.tts_say`) to select
a TTS entity such as Piper or xTTS and the message to say. The next action runs
after the greeting has been sent, so `voip_stack.forward` can then connect the
same caller to the configured Assist endpoint, another SIP phone or a ring group.

The contact can have an optional fallback if no automation handles the call.
Separate calls to the same contact remain independent. Hanging up cancels the
announcement. An automation can use HA's normal `continue_on_error` option to
skip a failed greeting and continue to the next action.

See the [complete automation examples](https://github.com/n-IA-hane/esphome-intercom/blob/dev/docs/AUTOMATION_DIALPLAN.md#automation-contacts)
and the [greeting and forwarding blueprint](https://github.com/n-IA-hane/esphome-intercom/blob/dev/blueprints/automation/voip_greeting_then_forward.yaml).
Use the actual name or extension of your configured assistant as the forwarding
destination. This first version plays announcements on automation calls, not
inside an existing conversation between two people.

The complete sequence was exercised with Piper and xTTS, a browser phone,
registered SIP callers, Waveshare S3 Audio and a controlled SIP trunk selecting
666 through DTMF. Provider-specific feedback is still welcome. This update does
not require new ESP firmware.

## Install and test

1. Open VoIP Stack in HACS, enable prerelease versions if necessary, choose **Redownload**, and select **2026.9.3-dev**.
2. Restart Home Assistant.
3. In VoIP Stack's configuration, leave **Provide advanced call details to the assistant** off and call the Assist extension. Check that it waits for your speech without first replying to a caller-information message.
4. If you use the advanced details with a conversational agent, enable the option and check that your existing telephone behavior still works.

This is a Home Assistant integration update. ESP firmware does not need to be rebuilt.

Please report your conversation agent, the option state and the result in [issue #120](https://github.com/n-IA-hane/esphome-intercom/issues/120). The local automated checks pass; confirmation from the affected installation is welcome.
