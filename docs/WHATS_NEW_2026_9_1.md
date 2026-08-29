# 2026.9.1-dev pre-release: standard SIP codec negotiation

`2026.9.1-dev` starts the next active development cycle. The stable release
remains `2026.9.0`. These notes will grow as further features and fixes enter
the candidate.

## ESPHome phones now publish their real codec capabilities

An ESPHome endpoint can advertise ordered transmit and receive RTP formats,
including codec, sample rate, channels and packet time. Home Assistant uses
that information in the same way it uses the capabilities of another SIP
phone. The endpoint is no longer routed through a special hard-coded media
profile merely because it is an ESP device.

When both call legs negotiate a compatible compressed format, HA relays the
payload without decoding and encoding it again. Payload type, RTP sequence,
timestamp and SSRC are still rewritten for the destination dialog. When the
legs are not compatible, the existing bounded media relay transcodes each
direction independently.

Debug diagnostics report a transcoding path only when conversion is actually
required. The normal production path does not enable media capture or verbose
codec diagnostics automatically.

## Opus is optional in compact ESP profiles

The standalone VoIP component can now negotiate RFC 7587 Opus in addition to
its existing PCM formats. The Spotpear VoIP-only profile prefers mono Opus at
48 kHz RTP clock and 20 ms packet time, retains a 10 ms Opus alternative and
keeps L16 PCM as a fallback for direct calls to existing ESP phones.

Opus is intentionally optional and compile-time gated. Full profiles continue
using the proven PCM configuration because wake word detection, AFE, Voice
Assistant, media playback, TTS and Sendspin already consume their hardware
budget. Codec selection therefore follows the selected product profile instead
of silently adding compressed-codec dependencies to every firmware.

This is a current hardware-resource limit, not a SIP interoperability limit.
Real-device testing of an Opus-only Spotpear Full build showed that incoming RTP
kept its required 20 ms cadence with no packet loss, while the device could only
encode about 15 frames per second and decode about 20 frames per second instead
of the required 50. The encoder and decoder each need a heavily accessed Opus
pseudostack. Keeping those working sets in PSRAM is too slow under the complete
AFE, wake word, LVGL and media workload, while moving them to internal memory
leaves too little DMA-capable RAM for the display and audio hardware.

For now, maintained Full profiles therefore remain PCM-only. The Spotpear
VoIP-only profile has enough remaining resources for bidirectional Opus and did
not show the same runtime limitation. A future Full Opus profile remains
possible if the codec working-memory contract or the available hardware budget
improves, but it will require complete real-device concurrency qualification.

## Packet time and SDP offers are smaller and more interoperable

Endpoints may support different packet times for the same wire codec. Offers
now publish one payload for each actual RTP encoding and preserve the preferred
packet time instead of repeating the same codec several times. The answerer
selects a packet time supported by both transmit and receive paths.

This keeps ordinary UDP INVITE requests below the LAN MTU in the validated
profiles without treating TCP as the primary solution. TCP remains a normal
standards-based transport or fallback when selected by the peer or route.

SIP URIs without an explicit remote port now use the scheme default, 5060 for
`sip` and 5061 for `sips`. HA's local listener port is no longer reused as an
unrelated destination port in direct calls, forwarding, groups, conferences or
inbound bridges.

## Home Assistant and ESP component cleanup

- DNS resolver imports and resolver initialization run outside the HA event
  loop, removing blocking-import warnings without changing RFC 3263 routing.
- The SPI PSRAM DMA adapter now matches the implementation merged upstream in
  ESPHome pull request 18699.
- Maintained YAMLs use ESPHome's official speaker interface and
  `speaker_source` media player. The obsolete local speaker fork has been
  removed.
- Spotpear and WS3 profiles keep their large audio and signaling allocations
  reusable instead of rebuilding them for every call.

## Qualification completed for the initial candidate

- 1673 software tests, 4 intentionally deselected tests, 140 parameterized
  subtests and 90 Home Assistant runtime tests passed.
- The ESP VoIP component passed 128 focused behavior and contract tests.
- Real Spotpear and WS3 calls passed in both directions with clean hangup and
  final idle state.
- Opus passed directly through Asterisk and through Home Assistant at 48 kHz
  RTP clock and 20 ms packet time with zero RTP sequence loss in the captured
  calls.
- A PCM client and an Opus Spotpear completed bidirectional HA transcoding with
  zero relay drops.
- SIP INFO and RFC 4733 DTMF passed in both directions, including the complete
  `0-9*#` keypad sequence.

## Upgrade notes

The former local `speaker` fork is no longer shipped. Custom YAMLs that still
request it from this repository must migrate to the official ESPHome speaker
interface and `speaker_source` media player before rebuilding.

The Spotpear VoIP-only codec configuration requires the matching
`esphome-voip-stack@dev` component. Maintained development YAMLs already point
to the coordinated `dev` branches.

## Known issue

On a OnePlus Nord 5, browser softphone receive audio can develop gaps when the
display uses a high refresh rate, especially during touch or orientation
changes. The same phone is stable at 60 Hz. This remains a post-release device
and Chromium scheduling investigation, and the current workaround is to use
the 60 Hz display mode for Chrome or the Home Assistant Companion app.

## Installation and feedback

In HACS, open VoIP Stack, use the three-dot menu, select Redownload and choose
`2026.9.1-dev`. Restart Home Assistant and clear the browser or Companion app
cache so the updated card module is loaded.

When reporting a regression, include the exact test time, call direction,
endpoint models, negotiated codecs and sanitized logs from INVITE through
final cleanup.
