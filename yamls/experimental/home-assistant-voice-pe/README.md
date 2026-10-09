# Voice PE experimental VoIP overlay

This profile adds VoIP Stack to the official Voice PE firmware. XMOS microphone
processing, the DAC, media playback, timers, volume dial and idle button gestures
remain provided by the official package. It does not add ESP Audio Stack or a
second software echo canceller.

## Call controls

- Press the center button while ringing to answer.
- Press it during an established or outgoing call to hang up or cancel.
- The answering/hangup press and release are consumed so they do not also start
  Assist. Initialization, disabled-button and dial/color gesture guards remain.
- A call pauses wake word and stops an active Assist interaction. When the SIP
  lifecycle returns to idle, wake word resumes only if it was running before
  the call and HA is connected. A terminated Assist conversation is not restarted.

The interaction was compared with
[eigger's Voice PE SIP profile](https://github.com/eigger/espcomponents/blob/master/packages/sip/voice_pe/voice_pe.yaml)
(MIT). This overlay implements those call controls against VoIP Stack's existing
state and keeps the official base, rather than importing a separate SIP engine
or a copied version of the complete Voice PE firmware.

## Volume and remaining work

The reference profile retains the official media-player volume bounds of 0.4
and 0.85. Its `gain_factor: 4` is for Micro Wake Word, not received call audio.
No microphone amplification, DAC limit or PCM scaling has been changed here.
The low-volume report still needs measurements on a physical Voice PE.

An audible incoming ringtone is not included yet. The reference implementation
reuses the announcement player and its repeat/stop commands. Those commands also
control timer and TTS playback, so they cannot be copied as an independently
owned ringtone without defining their interruption behavior first.

## Validation

The current changes have C++ host tests for the actual button filter, plus
ESPHome configuration validation and C++ generation against the official base.
These checks do not constitute a full firmware build or hardware qualification.

On a Voice PE, test answer, local hangup, remote hangup, outgoing cancellation
and an immediate second call. Confirm the release of the button never starts
Assist during a call; after the call, verify wake word, normal button gestures,
the volume dial and timers. Report call audio in each direction separately.
