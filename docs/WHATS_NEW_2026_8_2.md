# What is new in 2026.8.2-dev

This pre-release consolidates call ownership, strengthens real-device video and
makes qualification belong to one exact multi-repository candidate.

## One call lifetime

Direct, trunk, forwarded, grouped, conference, browser and Assist calls now use
the same generation-owned `EndpointCallSession`. Common primitives commit an
answer, activate a bridge, settle losing legs, publish state and cross the
cleanup barrier. A stale callback cannot regain ownership of a newer call.

Public idle means the previous generation has released its call-scoped tasks,
media owners, relays, sockets and RTP reservations. Immediate redial therefore
uses the same lifecycle contract as an ordinary call rather than a retry or
delay workaround.

## Standards-based SIP behavior

The shared SIP core covers reliable provisional responses and PRACK, delayed
offers, RFC 4028 refresh, remote forks and late 2xx settlement, REFER/NOTIFY,
Digest algorithms, RFC 3263 discovery, TLS and IPv6. In-dialog media changes
commit atomically, so a rejected re-INVITE preserves the last accepted audio or
video contract.

The same mechanics are used by registered clients, trunks and local endpoints.
Route policy remains separate from transaction, dialog and media ownership.

## P4 bidirectional video

Maintained P4 profiles keep JPEG and H.264 as separate compile-time choices.
The video path supports initial video and audio-first add/remove video through
UPDATE or re-INVITE while preserving bidirectional audio. Presentation work is
bounded and paced by actual media events so camera capture, decode, PPA, MIPI
DSI, LVGL and audio do not create parallel polling loops.

Panel presentation is validated separately from signaling and RTP. The
diagnostic one-shot framebuffer capture can record the physical idle, ringing,
established, hangup and redial screens without changing upstream LVGL.

## Full-experience ESP profiles

The ESP voice assistant remains an independent optional device capability. It
is not created or controlled by a SIP call to Home Assistant Assist. Full
profiles may combine the local voice assistant with VoIP, MWW, media player,
TTS and Sendspin, while VoIP-only profiles remain smaller.

## Qualification and release custody

Local qualification records exact commits for intercom, VoIP stack, audio
stack and runtime controller together with tool versions. Its tools cover
software, HA runtime, coverage, mutation, firmware, browser, external SIP peer
and HIL jobs without executing automatically on GitHub pushes.

Every maintained firmware build can record its config hash, binary hash and
size metadata. The release ZIP is built deterministically, validated locally
and uploaded explicitly to the GitHub release for HACS.

Real hardware evidence remains explicit. A green model, unit suite or firmware
compile is never described as proof of physical audio, video or panel output.

## Known issues

- On a OnePlus Nord 5, browser softphone receive audio can develop audible gaps
  and increment the playback underrun counter when the display uses a high
  refresh rate, especially during touch interaction or orientation changes.
  The same call path is stable on that phone at 60 Hz and has not reproduced on
  a Samsung S20 or desktop browser. The current workaround is to select the
  standard 60 Hz display mode, either globally or for Chrome and the Home
  Assistant app. Initial evidence points to device-side Chromium or OxygenOS
  scheduling under high-refresh rendering load, but the exact cause is not yet
  proven. Deeper instrumentation and any narrowly scoped mitigation are planned
  after this release.

## Upgrade checklist

1. Read [Breaking changes](BREAKING_CHANGES.md).
2. Update the integration, restart Home Assistant and run Reconfigure.
3. Rebuild ESPHome firmware after clearing stale build caches.
4. Check phone-scoped automations and card caches.
5. Test inbound and outbound hangup, audio and any enabled video path before
   relying on forwarding, groups or conference routing.
