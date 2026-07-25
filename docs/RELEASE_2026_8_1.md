# 2026.8.1 Pre-release: SIP Interoperability And Runtime Consolidation

<!-- Canonical source for the v2026.8.1 GitHub pre-release body. -->

`2026.8.1` builds on the stable multi-phone and SIP video foundation from
`2026.8.0`. This pre-release concentrates on peer interoperability, lower media
cost on Home Assistant and making the call-routing code easier to maintain
without introducing a second call engine or lifecycle authority.

## Media And Device Interoperability

- Home Assistant SIP legs can negotiate G.722 when the local runtime exposes a
  compatible codec adapter. ESPHome endpoints continue using their existing
  native PCM profiles; no ESP firmware downgrade or codec change is required.
- Matching Dahua `Dahua UAC/...` registrations automatically enable the
  vendor-specific dynamic `PCM/16000` mono profile. VoIP Stack treats this as
  little-endian signed PCM, not as standard network-byte-order RFC L16.
- The Dahua profile is carried through direct calls, forwarding, trunk routing,
  ring groups and in-dialog media renegotiation.
- G.711 conversion now uses vectorized lookup tables instead of a Python loop
  for every sample.
- The RTP envelope accepts the exact 20 ms, 16 kHz, 16-bit mono frame required
  by the Dahua profile while continuing to reject oversized payloads.

The detailed codec and video contract is documented in
[SIP Video](https://github.com/n-IA-hane/esphome-intercom/blob/v2026.8.1/docs/SIP_VIDEO.md).

## SIP And Registration Robustness

- Digest registration distinguishes invalid credentials from a stale or
  replayed nonce. A stale nonce receives a fresh challenge with `stale=true`;
  a wrong password remains a normal authentication failure.
- Registered TCP contacts retain and reuse their live inbound flow for reverse
  calls from Home Assistant.
- Registrar rows carry the selected contact's User-Agent and peer profile so
  outbound negotiation follows the actual registered device rather than a
  guessed global setting.
- Existing transaction, dialog, route-set and media-update ownership remains
  unchanged.

## Runtime Consolidation

The largest endpoint runtime paths were split into explicit domain
orchestrators for:

- inbound INVITE routing;
- trunk inbound routing and DTMF selection;
- forwarding;
- ring-group dialing;
- endpoint route binding.

This is a structural cleanup, not a replacement PBX. `CallRegistry`,
generation-current session ownership and the common commit/lifetime primitives
remain authoritative.

## Automations And Operations

- The automation cookbook now explains, in plain language, when to use
  `route_requested`, `select_inbound_destination`, phone-scoped `ringing` and
  `forward`.
- Copyable recipes cover office hours, presence routing, no-answer
  notifications, mobile-number fallback through a trunk, initial DTMF routing
  and in-call DTMF actions.
- Repository checks validate the documented YAML and the runtime schemas of
  the services used by those recipes.
- HA deployment now requires an explicit target, creates a backup before
  copying and verifies the deployed component content.
- Maintained ESPHome YAMLs use the supported remote component sources and have
  schema checks for their audio-stack contracts.

Advanced automation routing remains a preview surface and its semantics may
still evolve from real installation feedback.

## Qualification

The current tree passes the full Python, JavaScript, lint and virtual
qualification gates. Exact simulated Dahua audio/video calls were also
qualified in the isolated HA lab and on a deployed Home Assistant instance,
including initial audio, video negotiation, audio-to-video re-INVITE, TCP
registered-flow behavior, remote termination and complete call-resource
cleanup.

Physical Dahua models and firmware revisions still need field feedback. Please
include the model, firmware, transport, sanitized SDP and a short signaling
capture when reporting an interoperability issue.

## Installation

This is a pre-release. In HACS, enable pre-release versions for VoIP Stack and
select `2026.8.1`, then restart Home Assistant. Review
[Breaking Changes](https://github.com/n-IA-hane/esphome-intercom/blob/v2026.8.1/docs/BREAKING_CHANGES.md)
and reconfigure the integration before testing existing automations.

The GitHub release includes the flat `voip_stack.zip` archive used by HACS.
