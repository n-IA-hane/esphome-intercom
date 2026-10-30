# Voicemail

This guide describes the implementation on the `dev` branch. It is not available in the current stable release.

A mailbox belongs to a phone, not to a dashboard card. SIP phones and the HA
softphone call the same voicemail application. The card displays message
metadata; listening and deleting use the ordinary call and keypad commands.

## ESP display mailboxes in 2026.11.1-dev

The development firmware uses standard SIP MWI for the idle envelope and IMAP
for the message list, playback, read state and deletion. Tap the envelope or
open Voicemail from Contacts. The same HA-generated account authenticates SIP,
LDAP and IMAP. See [standard phone services](ESP_PHONE_SERVICES.md) for setup,
paging, standalone PBX settings and supported protocol boundaries.

## Configure the mailbox and menu voice

Voicemail is available without a global enable switch. Create only the phone
mailboxes you need. Depositing a message uses the optional recorded greeting
and recording tone; it does not require a TTS provider.

For spoken mailbox navigation, open **VoIP Stack > Reconfigure > Voicemail voice**
and choose an existing Home Assistant TTS entity and language. English, Italian,
German and Portuguese are supported when offered by that provider. When several
voices are available, select one or keep **TTS default voice**. HA installation
and ordinary calls do not depend on configuring this voice. A missing or
unavailable provider produces a configuration error for spoken-menu operations.

Voicemail feature codes and named prefixes share the phone/contact namespace.
Existing conflicting destinations are preserved and reported in HA Repairs;
resolve the conflict before using the mailbox feature addresses. Numeric
extension `97` remains distinct from feature code `*97`.

A mailbox is the message inbox belonging to one phone. For an ESP, use its
mirror card or the native mailbox action; messages stay on HA. Calls must pass
through HA for its no-answer rules to apply.

Enable the phone's mailbox in its VoIP Stack phone configuration, or with
**Enable this phone's mailbox** in the card's Options. The native
**VoIP Stack: Configure voicemail** action provides the same control and the
advanced options, including PIN, greeting and recording limit. Select its phone
with the device picker. The recording limit defaults to 180 seconds. A greeting
uses HA's media picker; without one, the caller hears a recording tone.

![Configure a phone mailbox using named GUI fields](images/voicemail-settings-action.png)

In this action, select the phone in **Phone mailbox**, enable the optional
**Enable mailbox** field and turn its switch on. The checkboxes beside optional
fields mean "change this setting"; leaving one unchecked preserves its current
value. Choose greeting media and a message duration only if you want to change
those settings. The screenshot is an unsubmitted form, not an active mailbox.

Turning **Enable mailbox** off permanently removes that phone's mailbox,
messages, files, PIN and mailbox settings. Turning it on again creates an empty
mailbox. This also applies when changing the setting through an HA automation.
It does not remove other phones' messages or separate conversation recordings.

Enabling a mailbox does not enable forwarding. In the phone card's
**Options > Call routing** section,
enable a ringing timeout and select the voicemail destination if unanswered
calls should go there. Leave the destination empty for this phone's own mailbox,
or choose another phone with an enabled mailbox. Leaving the timeout unset keeps the existing unlimited
application-level ringing behavior. An accepted call-control action from an
automation takes precedence over the phone's configured fallback.

## Access from a telephone

Configure a SIP handset's voicemail button to dial `*97`. This opens that
identified phone's own mailbox without a PIN. The same number works from the
HA softphone keypad. `mailbox` is an equivalent named destination.

To access another mailbox, dial `*98` and enter its extension, or call
`mailbox.<phone>` directly. That mailbox must have a configured PIN. No PIN
means access from other phones is disabled. These are PBX feature-code choices,
not numbers mandated by SIP.

| Key | Operation |
| --- | --- |
| `1` | Next message |
| `2` | Delete the selected message |
| `3` | Repeat the selected message |
| `0`, message number, `#` | Select a message, for example `01#` |
| `*` | Message count and menu |
| `#` outside number entry | Hang up |

A key immediately interrupts the spoken prompt and remains available to the
menu. Numbers stay fixed for the duration of a call. If another call deletes
message 1, selecting 1 does not silently play or delete the next message.
Caller numbers are announced digit by digit, including leading zeroes. A named
caller is announced by name; the stored caller identity remains unchanged.

The HA softphone card shows **Voicemail** when messages exist. Each row shows
the caller and date. Selecting a row calls `*97` and selects that message.
**Delete** uses the same call, waits for confirmation, then hangs up while
keeping the list open. It refuses to replace an unrelated active call.

Registered SIP phones can subscribe to `message-summary`. RFC 3842 MWI reports
new and old message counts through the existing SIP SUBSCRIBE/NOTIFY dialogs.
MWI does not carry a message catalog, recordings or transcriptions.

### ESP display indicator

The maintained Spotpear, S3 LCD and P4 display packages show an unread-message
envelope centered above the status area on the Assist idle and VoIP idle views.
Mailbox events and page changes update its visibility; reading the messages
clears the indicator.

For a Home Assistant mailbox, enable the phone's mailbox as described above.
The device packages enable `ha_integration.message_waiting`, and VoIP Stack
delivers the counts through the authenticated ESPHome API. SIP registration
is optional. Registered PBX accounts can instead enable
`registration.message_waiting` for standard SIP MWI. When both sources are
configured, the envelope represents unread messages in either source.

See the [ESP message-waiting guide](https://github.com/n-IA-hane/esphome-voip-stack/blob/dev/docs/SIP_MWI.md)
for component configuration and YAML triggers.

## Send a call to voicemail

Call `voicemail.<phone>` or `*60<extension>` to leave a message directly.
Automation dialplans can use `voip_stack.send_to_voicemail`, selecting the
destination phone with the **Mailbox** picker. It uses the current call from
the native VoIP trigger or the preceding Automation `call` action.

For example, a call automation can speak with `tts_say`, then invoke
`send_to_voicemail` with `skip_greeting: true`. Do not append `hangup`: the
voicemail application now owns recording and termination. It saves the message
when the caller hangs up or the recording limit is reached. Empty recordings
do not create messages.

## Process a received message in an automation

The native **Voicemail received** trigger runs after the recording is saved.
Its optional **Mailbox** device picker limits it to one phone. With no filter,
it matches all mailboxes. `trigger.voicemail` contains the caller, mailbox ID,
message ID, duration and partial-recording indicator.

Actions in that execution select the received message automatically, including
when two callers leave messages at the same time. An action run independently
must supply both `mailbox` and `message_id`. These identify a saved message,
not an active call.

### Optional transcription

Choose an existing HA STT entity, such as a configured Wyoming Whisper entity,
and a language it supports. No STT provider is enabled automatically. This
example uses a normal HA notification so it needs no email or messaging account:

```yaml
alias: Transcribe incoming voicemail
mode: parallel
triggers:
  - trigger: voip_stack.voicemail_received
actions:
  - action: voip_stack.transcribe_voicemail
    data:
      stt_entity_id: stt.whisper
      language: en
    response_variable: transcript
  - action: persistent_notification.create
    data:
      title: "Voicemail from {{ transcript.caller }}"
      message: "{{ transcript.text }}"
```

Replace `stt.whisper` with your installed entity. The action streams 16 kHz mono
PCM through HA's STT API and returns `text`, `language`, `caller`, `mailbox_id`
and `message_id`. It leaves the original recording and unread status unchanged.
Its timeout defaults to 120 seconds. An unavailable provider, unsupported
language, provider error or timeout fails the action without deleting the
message. Cancelling the automation releases the reader and transcription.

### Use the recorded file in another action

The trigger already supplies `trigger.voicemail.file_path` and
`trigger.voicemail.media_source_id` for the saved WAV in HA's media directory.
Pass these to an email, Telegram or notification action that accepts local files
or media-source items. No export or copy is necessary.

For an independently selected message, `voip_stack.get_voicemail` returns the
same references. In a voicemail-triggered automation it can omit both selectors:

```yaml
alias: New voicemail details
mode: parallel
triggers:
  - trigger: voip_stack.voicemail_received
actions:
  - action: voip_stack.get_voicemail
    response_variable: recording
  - action: persistent_notification.create
    data:
      title: "Voicemail from {{ recording.caller }}"
      message: "Recorded audio: {{ recording.media_source_id }}"
```

Messages are stored directly under `voip_stack/` in HA's `local` media directory
(or its first configured media directory when `local` is absent). The normal HA
media source resolves these files using its existing authentication. VoIP Stack
does not add a separate download server or a second audio player to the card.

If a call changes audio format while recording, finalization combines its
working fragments into one WAV, preserving gaps and the highest recorded sample
rate, channel count and bit depth. The working fragments are then removed.
Listening, transcription and notification actions all use that same saved file.
Deleting the message or permanently removing its phone removes the file.

## Storage and deletion

The archive in HA media has a combined 1 GiB quota, including active recordings.
Disk writes use a bounded queue. A storage failure ends the deposit through the
existing call cleanup; interrupted recordings are recovered as partial messages
where possible. There is no automatic deletion of old messages.

Permanently removing a phone removes its mailbox, PIN, settings and messages,
including a recording in progress. Temporary loss of connectivity, registration
expiry, a reboot or integration reload does not delete its mailbox. Renaming a
phone preserves the mailbox's identity.

Permanently removing the VoIP Stack integration also deletes its voicemail
messages after its calls have stopped. Unrelated files in HA media and separate
conversation recordings are preserved.
