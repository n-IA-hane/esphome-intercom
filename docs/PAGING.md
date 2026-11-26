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

Targets are logical accounts. VoIP Stack offers an announcement to every
eligible registered SIP client and the account's active HA browser audio
adapter. Every client that accepts and has a ready output receives the message.
For example, Reception's HA card and its registered desk phone can both play it. Several open cards are views of one HA audio owner, rather than
additional phone devices. Ordinary conversations still use first-answer routing.

Each ready output starts without waiting for another client to answer. TTS is
synthesized once. For a finite TTS message or recorded file, a later-answering
client receives the complete message from the beginning, provided it can finish
within the announcement's total time limit. A ringing SIP client has not yet
accepted an announcement and receives no audio until it answers.

Several distinct target accounts receive the same shared announcement as well.
Ending one client's output does not stop the other accepted outputs. Ending one
recipient account's call does not stop announcements to other accounts.

Busy, DND and unreachable phones are skipped. Results distinguish completed,
skipped and failed clients. When only some requested clients receive the full
message, the result is `partial`, with per-account `clients` details identifying
the HA or SIP output and its reason. Already completed deliveries remain
recorded when another client does not answer or reaches a deadline. No ready
outputs means no successful delivery; a truncated source is not completion.

`wait: true` waits for completion. `wait: false` returns an `announcement_id` so
the automation can continue. Stop that announcement with:

```yaml
action: voip_stack.stop_announcement
data:
  announcement_id: "{{ announcement.announcement_id }}"
```

Stopping an already finished announcement is safe. It cannot stop an unrelated
call. Completing an announcement drains each output before closing it. Stop
closes delivery consumers and the job-owned file/URL decoder; a finite TTS
request already started remains owned by Home Assistant's TTS engine.

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
audio back to it. Live speech uses the existing bounded live fanout without
history. A late live recipient hears current speech, not a replay of earlier
microphone audio.

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
and playback. Omit it to use the configured PBX announcement duration; new or
migrated configuration starts at 120 seconds. An action can override it from
1 to 600 seconds. Even with an unlimited answer window, the job ends at that
total limit. A SIP transaction with no provisional
response still has its normal protocol timeout. Limits and failures are
returned to the automation. Test your phone's speakerphone and paging behavior
before relying on unattended delivery.


Finite file and URL audio is decoded once. Its decoded PCM budget follows the
time remaining in the announcement's existing total limit. Exceeding that byte
or time budget closes the source and reports failure, even if some audio was
already transmitted. Native HA replay retains finite audio; it is not a bounded
live fanout. Each output feeds its existing encoder in small sample-aligned
chunks.

See [groups and queues](GROUPS_AND_QUEUES.md) for operator groups and
[automations as dialplan](AUTOMATION_DIALPLAN.md) for interactive call flows.
