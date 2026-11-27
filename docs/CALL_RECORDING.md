# Record a conversation

Recording is optional. Nothing is recorded until an action or the phone's card
starts a recording. Home Assistant must handle the call's audio, and the call
must already be answered. Direct calls between devices that bypass HA cannot
be recorded by this integration.

The recorder receives samples from the call's existing audio path. It does not
create another SIP call or another RTP receiver. Audio storage runs outside the
HA event loop. Disk, quota or recording errors stop the recording while the
conversation continues.

## Use the card

In the Home Assistant softphone card editor, enable **Show recording controls**.
The default is off. During a call, **Record call** starts recording and
**Stop recording** finishes the file without hanging up.

The equivalent card option is:

```yaml
type: custom:voip-stack-card
mode: ha_softphone
show_recording_controls: true
```

Showing the control does not start automatic recording. The card displays the
backend's recording state. If another phone started the recording, that phone
owns its stop control and saved file.

## Use an automation

In the automation editor:

1. Add the native **VoIP call connected** trigger and filter its destination to
   the phone you want, for example Kitchen.
2. Add **VoIP Stack: Start call recording**.
3. Select Kitchen in the action's **Phone** picker and set the desired limit,
   for example 1800 seconds.

The trigger supplies the current call and generation. The phone picker saves
its device reference; you do not need to find or copy a Call-ID or a long device
ID. Outside a native call trigger, selecting a phone uses its active call.
An explicit `call_id` must belong to that phone. See the
[action reference](SERVICES.md) for fields used by advanced scripts.

For a browser phone, the connected event can arrive before its audio WebSocket
opens. Recording attaches when that existing media adapter starts, without
polling or starting a substitute browser.

`voip_stack.stop_recording` accepts the same phone selector. It finishes the
recording and can return its file reference while the call continues. Stopping
again on the same live call returns `status: not_recording`; it does not select
an older recording. Hangup and media handoff stop recording automatically.
Omitting `max_seconds` uses the configured PBX recording limit, initially 3600
seconds. Set a positive duration up to 3600, or
explicitly set `0` to record without a duration deadline. Zero does not disable
the recorder's storage quota or error handling. Stop recording, hangup or a
media handoff still finalizes the recording. Existing positive limits and the
finite action-editor prefill remain unchanged.

## Process the saved file

`voip_stack.recording_finished` is a normal HA event. Its data identifies the
phone, call and recording, with `status` and `error`. A completed or usable
partial recording also includes `file_path` and `media_source_id`.

```yaml
alias: Get a finished call recording
mode: parallel
triggers:
  - trigger: event
    event_type: voip_stack.recording_finished
conditions:
  - condition: template
    value_template: "{{ trigger.event.data.status in ['complete', 'partial'] }}"
actions:
  - action: voip_stack.get_recording
    data:
      device_id: "{{ trigger.event.data.device_id }}"
      recording_id: "{{ trigger.event.data.recording_id }}"
    response_variable: recording
```

Subsequent actions can use `recording.file_path` for a file attachment or
`recording.media_source_id` for HA media playback. These refer to the original
saved file, not an exported copy. The recording ID identifies this one file;
it is returned by `start_recording` and included in the completion event.

Files use the existing `voip_stack` directory in Home Assistant's configured
Media directory. A conversation becomes one WAV containing both directions,
mixed on their recorded timeline. Finalization retains the highest recorded
sample rate, sample width and channel count. Temporary native-format segments
are removed after the final file and metadata commit successfully.

The shared archive has a 1 GiB quota. Recordings reserve enough space to finish
their WAV before accepting more audio, so quota exhaustion can preserve a
usable partial recording. The integration does not delete old recordings to
make room. Use `voip_stack.delete_recording` with the phone and recording ID to
remove a saved recording together with its metadata. Conversation recordings do not increase voicemail counts or MWI.
Removing the associated phone also removes its saved recordings.

## Scope and verification

This implementation covers ordinary two-party SIP calls, browser-to-SIP calls
and local browser calls. Conference recording is not exposed by these actions.
The recording stops when its media owner is handed off, including a transfer or
parking operation; a new conversation can start its own recording explicitly.

Tests use real baresip SIP/RTP calls and inspect both tones in the saved WAV.
They also cover concurrent start requests, remote hangup, duration limits,
recording faults, disk failure with a clean retry, wrong-phone access and hangup
while storage is being prepared. Browser adapter and local-bridge tests verify
that recording leaves the ordinary audio frames available to the participants.

The media-tap approach follows the same separation used by
[Asterisk MixMonitor](https://docs.asterisk.org/Latest_API/API_Documentation/Dialplan_Applications/MixMonitor/)
and [FreeSWITCH session recording](https://developer.signalwire.com/freeswitch/media-and-codecs/audio-files/):
recording observes an existing conversation; it does not become its signaling
or media-routing owner.
