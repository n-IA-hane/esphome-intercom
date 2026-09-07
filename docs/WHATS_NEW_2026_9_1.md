# 2026.9.1-dev pre-release: cleaner audio, real codecs and in-call DTMF

`2026.9.1-dev` starts the next active development cycle. The stable release
remains `2026.9.0`. These notes will grow as further features and fixes enter
the candidate.

This revision makes the phone system less interested in special cases and more
interested in behaving like a phone system. ESP endpoints publish their real
media capabilities, compatible legs avoid unnecessary transcoding, the HA
phone can operate an IVR, and browser audio follows its own clock instead of
hoping the UI thread remains in a generous mood.

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

<p align="center">
  <img src="https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/dev/docs/images/ha-softphone-in-call-keypad-2026-9-1.jpg" width="420" alt="In-call DTMF keypad in the Home Assistant phone"/>
</p>

The keypad replaces the normal call view only while it is needed. `Hangup`
remains available, `Contacts` returns to destination selection, and remote or
local hangup restores the ordinary terminal screen. It is a telephone keypad,
not a modal dungeon with no exit.

## Clearer controls and more reliable audio

During a video call, the keypad is now a small icon beside Options. Both
controls sit together without covering the call timer. The keypad remains
available for automated menus, and Hangup stays accessible.

Spotpear VoIP-only now uses the same round-screen dialer as the Full profile.
The backspace icon renders correctly, and the clock and status icons hide
while the dialer is open so they cannot overlap the call button.

The Send Camera control refreshes correctly after changes. Browser-to-browser
calls also update their video direction when a camera is enabled after the
call starts. Local browser calls now deliver keypad digits through the shared
DTMF event path.

Audio processing preserves samples when the processing library returns a
partial block. Stopping and restarting the audio pipeline is also more
reliable, including during firmware updates. Assist calls no longer fail at
audio setup when the media-route status is updated.

Cancelling a call while the other phone is still ringing now releases the
reserved media ports as well. Repeated unanswered calls no longer leave those
ports occupied and gradually reduce the resources available for new calls.

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
its existing PCM formats. Spotpear and WS3 VoIP-only
profiles use Opus by default. P4 and Full profiles remain PCM to preserve
resources for their other features. Each firmware uses its selected codec;
there is no silent fallback between PCM and Opus.
Incompatible direct peers can route through Home Assistant, which performs the
required transcoding.

Opus is intentionally optional and compile-time gated. Full profiles continue
using the proven PCM configuration because wake word detection, AFE, Voice
Assistant, media playback, TTS and Sendspin already consume their hardware
budget. Codec selection therefore follows the selected product profile instead
of silently adding compressed-codec dependencies to every firmware.

This is a current hardware-resource limit, not a SIP interoperability limit.
Full profiles keep PCM so audio processing, wake word detection, the user
interface and media playback can share the available CPU and memory.

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

## Browser audio follows RTP cadence without crackling

The browser receive path now separates network packet arrival from Web Audio
rendering. The page WebSocket sends negotiated PCM frames directly to the
playback AudioWorklet, removing the former Worker and MessageChannel hop. The
worklet converts RTP frame cadence into the exact render blocks requested by
the browser audio clock.

An adaptive, bounded jitter buffer absorbs packet bursts and short Android
WebView scheduling stalls. It starts with a conservative reserve, learns only
from delivery gaps that exceed the current protection, and releases surplus
one frame at a time after a stable interval. This prevents both failure modes:
draining to an unrealistically small buffer after a few quiet seconds, and
growing to the maximum because ordinary message batching kept resetting the
recovery timer.

The same path reports input, output, drop, buffer and underrun counters.
Underrun diagnostics distinguish network arrival gaps from delivery stalls
inside the WebView, so a bad scheduler is no longer framed for a crime
committed by RTP.

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
- Maintained YAMLs select the speaker adapter required by these profiles;
  no manual migration to a different media-player component is required.
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

## Development status

Audio/video calls, call cancellation and cleanup, DTMF, forwarding and browser
playback are covered by automated checks and selected device scenarios.
Compatibility still depends on the device profile, peer codecs and route.
Some Android, trunk and simultaneous-use combinations remain under evaluation.

Configured camera FPS is a limit, not a promise for every resolution and
workload. JPEG and H.264 are separate profiles and have different resource
requirements. Keep `2026.9.0` if you need the stable release.

## Upgrade notes

The Opus-only codec configurations require the matching
`esphome-voip-stack@dev` component. Maintained development YAMLs already point
to the coordinated `dev` branches.

## Known issue

High-refresh-rate operation on the OnePlus Nord 5 still needs separate
qualification, especially during touch and orientation changes. The current
physical validation used the stable 60 Hz mode. The adaptive buffer fixes the
independent periodic scheduling gaps observed at 60 Hz, but it is not evidence
that every 120 Hz WebView lifecycle path is now qualified.

## Installation and feedback

In HACS, open VoIP Stack, use the three-dot menu, select Redownload and choose
`2026.9.1-dev`. Restart Home Assistant and clear the browser or Companion app
cache so the updated card module is loaded.

When reporting a regression, include the exact test time, call direction,
endpoint models, negotiated codecs and sanitized logs from INVITE through
final cleanup.

