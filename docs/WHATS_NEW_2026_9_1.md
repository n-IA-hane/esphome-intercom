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
48 kHz RTP clock and 20 ms packet time, with a 10 ms Opus alternative. The
firmware advertises Opus only. Incompatible direct peers can route through Home
Assistant, which performs the required transcoding.

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

## The HA phone now has an in-call keypad

The Home Assistant card reuses its normal keypad during an established call.
Digits entered while idle still build a destination. Digits entered during a
call are sent through the negotiated telephone-event payload, with SIP INFO
available for compatible peers. Hangup remains visible in the keypad view and
the card returns to its ordinary terminal screen when either side ends the
call.

Each new dialog also resets temporary card surfaces. An Options or keypad view
left open by the previous call can no longer hide Answer and Decline on the
next incoming call.

The same operation is available as the `voip_stack.send_dtmf` Home Assistant
action. ESP phones advertise DTMF only when their firmware contains the new
RFC 4733 implementation, so audio-only firmware does not gain a fictional
capability.

## Browser audio follows RTP cadence without crackling

The browser receive path now separates network packet arrival from Web Audio
rendering. A bounded worker reassembles incoming PCM into the exact blocks
requested by the audio clock. Packet bursts, WebSocket message boundaries and
RTP packet time therefore no longer become audible gaps or crackling.

The worker is paced by the browser audio clock, keeps bounded carry state and
reports real input, output and underrun counters. It does not guess a larger
timeout or grow a buffer until the symptom disappears.

## More resilient embedded registration and call control

The SIP registrar accepts the equivalent Digest URI form used by some embedded
door stations when the request omits port 5060 but the signed Digest URI states
it explicitly. The exception is deliberately narrow: the SIP user, host, URI
parameters and effective port must still match, and the Digest response is
verified against the exact URI signed by the client. Different ports or URI
parameters remain rejected.

During an active browser call, the shared softphone engine now checks the Home
Assistant connection and recovers through the official connection API after a
confirmed half-open WebSocket. Hangup and Decline use the same engine-owned
terminal operation, reconnect once when transport failure is confirmed, then
read the authoritative call state before deciding whether a retry is needed.
The monitor is suspended while an Android page is hidden, so normal Companion
app lifecycle transitions do not cause a false reconnect.

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
- HA and ESP log one warning when their coordinated VoIP Stack versions do not
  match. The warning is deliberately emitted once per mismatch instead of on
  every state update.

ESP discovery now keeps the stable SIP route separate from the complete media
contract. The route state retains a backward-compatible PCM format, while a
bounded diagnostic state carries directional RTP codecs, DTMF support, video
codec and component version. Large P4 profiles therefore remain discoverable
instead of exceeding Home Assistant's 255-character entity-state limit.

## Call ownership and cleanup are now centralized

Home Assistant now keeps one authoritative owner for each call generation.
Routing, forwarding, browser phones, registered SIP clients, media bridges and
termination all commit through the same lifecycle primitives instead of
maintaining partially independent state paths.

The shared cleanup barrier closes call legs, RTP relays, video transcoders,
timers and port reservations before the endpoint becomes reusable. Delayed
callbacks from an older call generation cannot alter a newer call that reused
the same public identity.

Dial targets, phonebook entries and service aliases now use canonical parsers
and tables. Registered contacts also preserve the signaling transport observed
during REGISTER. This prevents a large video INVITE from being changed to TCP
when the selected registered endpoint is explicitly reachable only through
its UDP binding.

## Current candidate qualification

- 1700 software tests passed, with 4 intentional deselections and 133
  parameterized subtests.
- 95 Home Assistant runtime tests passed.
- The complete local SIP laboratory passed caller and callee hangup, CANCEL,
  manual answer and decline, auto answer, forwarding, two browser subscribers,
  registered SIP clients, video added in-dialog and final resource cleanup.
- Browser playback consumed 49 measured input frames and produced 338 audio
  render blocks with zero underruns in the registered video call witness.
- SIP INFO and RFC 4733 DTMF passed in both directions, including the complete
  `0-9*#` keypad sequence.
- An Android call through the deployed Home Assistant instance exchanged 775
  receive and 762 transmit RTP audio packets with WS3, with zero PLC, late
  discard, queue drop or terminal resource leak.
- The current P4 JPEG profile completed an Android audio and video call with
  sendrecv media. Browser diagnostics reported 1526 receive and 1504 transmit
  audio packets, zero audio PLC or queue drops, and zero video loss, reorder or
  access-unit queue drops. P4 presented 74 of 75 admitted JPEG frames and
  returned every call-scoped resource to zero after hangup.

The P4 camera is configured for 10 FPS, but this Android witness produced about
4.6 encoded and presented frames per second. The result proves stable media and
physical presentation for this call, not achievement of the configured maximum
frame rate.

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
