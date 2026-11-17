# Paging: announcements on your phones

Your desk phones can announce a visitor, an alarm or a reminder in speakerphone
mode. A separate smart speaker is optional. VoIP Stack can synthesize a TTS
message once and deliver it to one phone, several phones or a saved paging group.

## Start with one phone

In the automation editor choose **VoIP Stack: Announce**. Select the phone,
the TTS entity and message. Use its VoIP endpoint ID or Home Assistant device ID
in `targets`; phone names are also accepted when unambiguous.

```yaml
action: voip_stack.announce
data:
  targets:
    - Reception
    - Warehouse
  tts_entity_id: tts.home_assistant_cloud
  message: A delivery is waiting at reception.
  answer_timeout: 5
  timeout: 120
  wait: true
response_variable: announcement
```

Use your installed TTS provider. Its normal language and options settings are
available. For a file, supply `media` instead of `message` and `tts_entity_id`:

```yaml
action: voip_stack.announce
data:
  targets: [Reception]
  media:
    media_content_id: media-source://media_source/local/door-chime.wav
    media_content_type: audio/wav
```

Busy, DND and unreachable phones are skipped. The result identifies each target
and its outcome. The answer window closes before the message starts, so a late
recipient does not join halfway through. With no available recipients there is
no successful delivery to report.

`wait: true` waits for completion. `wait: false` returns an `announcement_id` so
the automation can continue. Stop that announcement with:

```yaml
action: voip_stack.stop_announcement
data:
  announcement_id: "{{ announcement.announcement_id }}"
```

Stopping an already finished announcement is safe. It cannot stop an unrelated
call. Completing an announcement drains the audio before closing its calls.

## Saved groups and live announcements

Add a **Paging group** on the VoIP Stack integration, select its phones and
optionally give it an extension. Automations can target the group's name or
stable ID. Repeated targets are deduplicated.

Call that extension from a SIP phone to make a live one-way announcement.
The originating phone supplies the audio; recipients do not send microphone
audio back to it.

## Automatic answer and compatibility

VoIP Stack requests per-call automatic answer with the standard
`Answer-Mode: Auto` header. A phone must support and authorize this behavior.
This request does not change its persistent autoanswer setting.

For ESP phones, use firmware containing the paging feature. The managed HA
account authorizes this path; manually configured SIP accounts require
`allow_paging_auto_answer: true` on the intended account. Another registered
account does not inherit that consent. A busy ESP still rejects another call.
Direct peers require an explicit trusted-peer configuration.

A browser phone must have an active card/client and be able to start playback.
It receives the announcement without opening its microphone. Browser autoplay,
background suspension and mobile operating-system restrictions still apply;
a closed Companion app is not an always-listening paging speaker.

The default answer window is five seconds and the default total limit is
120 seconds. Limits and failures are returned to the automation. Test your
phone's speakerphone and paging behavior before relying on unattended delivery.

See [groups and queues](GROUPS_AND_QUEUES.md) for operator groups and
[automations as dialplan](AUTOMATION_DIALPLAN.md) for interactive call flows.
