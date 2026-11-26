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

Targets are logical accounts. If an account has a card and several registered
clients, paging offers the call through its eligible clients using the account's
ringing policy. The first client that accepts and has a ready audio output
receives that account's announcement. Other client offers are closed before the
message source starts. Sharing an account does not make every handset on that
account play at once.

Several distinct target accounts still receive the same announcement. For
example, targeting Reception and Warehouse sends the shared message to the
selected output of each available account; it does not choose only one account
from that list.

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

An administrator can also open the phone card's **Options > Paging groups**
and select the existing local groups that should announce on that phone.
The same memberships appear in the integration's paging group editor. Removing
the last phone keeps the group's extension and settings for future use.

When the paging service lives on another PBX, dial that PBX's paging extension
through its trunk. An **Automation contact** used as the source of an outgoing
announcement is a different dialplan destination. Calling that contact starts
its incoming automation and waits for a routing decision. The originating
card shows outgoing progress and offers **Hang up**.

Call that extension from a SIP phone to make a live one-way announcement to
the available target accounts.
The originating phone supplies the audio; recipients do not send microphone
audio back to it.

## Automatic answer and compatibility

VoIP Stack requests per-call automatic answer with the standard
`Answer-Mode: Auto` header. A phone must support and authorize this behavior.
This request does not change its persistent autoanswer setting. A physical
phone must permit trusted intercom or paging auto-answer from the configured
PBX. If that policy is off, the phone can ring without playing automatically;
a SIP 180 response proves ringing, not delivery. Use the phone's scoped trusted
paging policy rather than enabling ordinary automatic answer for every call.

For ESP phones, use firmware containing the paging feature. The managed HA
account authorizes this path; manually configured SIP accounts require
`allow_paging_auto_answer: true` on the intended account. Another registered
account does not inherit that consent. A busy ESP still rejects another call.
Direct peers require an explicit trusted-peer configuration.

A browser phone must have a valid active receiving card/client and be able to
start audio playback. Its ordinary **Auto Answer** setting can remain off:
paging uses the accepted playback-only path. It receives the announcement
without opening its microphone. Browser autoplay,
background suspension and mobile operating-system restrictions still apply;
a closed Companion app is not an always-listening paging speaker.

`answer_timeout` is an optional per-recipient ringing limit from 0 to 3600
seconds. Omit it to inherit each account's limit or the stored PBX setting;
`0` means no answer deadline. The explicit five seconds in the example is a
choice for that announcement, not another runtime default. A live call to a
saved paging group keeps its existing explicit answer window. New groups inherit until you
choose an override in the native form.

`timeout` is a separate limit on the entire announcement, including admission
and playback, and retains its 120-second default. Even with an unlimited answer
window, the job ends at that total limit. A SIP transaction with no provisional
response still has its normal protocol timeout. Limits and failures are
returned to the automation. Test your phone's speakerphone and paging behavior
before relying on unattended delivery.

See [groups and queues](GROUPS_AND_QUEUES.md) for operator groups and
[automations as dialplan](AUTOMATION_DIALPLAN.md) for interactive call flows.
