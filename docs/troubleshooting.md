# Troubleshooting

Start from evidence, not from a single UI symptom. For every failing call,
collect:

- HA logs around the call;
- ESP log/monitor around the call;
- HA softphone or ESP `SipPhoneState` snapshot;
- selected TX/RX formats;
- RTP packet/byte counters;
- a short WAV capture when audio quality is in question.

## ESP Does Not Ring

- Confirm the peer sends a SIP `INVITE` to the ESP `sip_port`.
- Check ESP `voip_stack` `transport` matches the peer signaling transport.
- Verify SDP offers at least one compatible PCM format.
- Inspect `sensor.*_voip_sip_snapshot` for `last_sip_event`,
  `sip_status_code`, and `terminal_reason`.
- If HA is the caller or bridge, verify HA logs show the route decision and the
  outbound INVITE to the ESP.
- If the target was a name/number, confirm whether HA or the ESP resolved it.
  Direct SIP only happens when the phonebook contains complete direct route
  data.

## HA Softphone Does Not Ring

- Confirm HA reports both implicit SIP listeners ready in logs:
  `SIP UDP listener ready`, `SIP TCP listener ready` and
  `SIP endpoint enabled on UDP+TCP/<port>`.
- Confirm the INVITE Request-URI reaches HA's advertised host and SIP port.
- Check HA softphone DND and active-call state. A second inbound call while HA
  is ringing or in-call should receive busy.
- For trunk calls, no route hint means the configured inbound default target;
  an explicit unresolved route hint terminates as `route_not_found`.
- For local registered SIP endpoints, confirm the REGISTER Contact is present in
  HA logs and the phonebook includes the registered SIP endpoint contact.

## Unknown Or Unregistered Caller Is Rejected

The phonebook is an outbound dial plan, not an inbound caller allowlist. An ESP
or HA may therefore receive a compatible SIP INVITE from any peer that can
reach its listener, even when the caller is absent from the phonebook and has
not registered to HA. The optional HA registrar authenticates `REGISTER`; it
does not require every inbound caller to own an account.

Unknown callers may reach local HA, ESP, registered-phone and group targets.
They cannot use HA as an unauthenticated gateway to the configured external
trunk; outbound trunk routes require a registered, roster-known, HA-local or
trusted-trunk origin.

- Check DND, busy state, Request-URI routing and SDP compatibility before
  treating an unknown caller as unauthorized.
- Keep SIP/RTP on a trusted LAN or VPN. Use firewall, VLAN, VPN or an SBC when
  caller admission policy is required; the ESP profile does not provide
  SIP/TLS, SRTP or an inbound caller allowlist.

## Call Fails With `media_incompatible`

The SDP offer/answer did not produce a usable PCM RTP format, or HA could not
build the required bridge conversion. Use explicit supported PCM profiles such
as `16000:s16le:1:16`, `16000:s16le:1:32`, `16000:s16le:1:20`, or
`48000:s16le:1:10`. A call also needs one common packet time across the selected
TX and RX directions; rates may differ, but `frame_ms`/`ptime` must match.

ESP devices are PCM-only. HA softphone/trunk legs can negotiate common VoIP
codecs where supported, but the bridge must still be able to convert the ESP
leg to a compatible PCM format.

## HA Cannot Route A Name

- Ensure `sensor.voip_phonebook` contains the target.
- If an ESP has just rebooted or been reflashed, check
  `sensor.<device>_voip_contacts`. If it is `unknown` or empty while the device
  is otherwise online, wait for HA to see the ESPHome
  `esphome.<slug>_set_roster_json` service or call `voip_stack.push_phonebook`
  for diagnostics. Current builds automatically refresh when that service is
  registered.
- For local ESP-only routing, declare the contact in
  `voip_stack.static_contacts`.
- Use a direct SIP URI (`sip:name@host:5060`) when bypassing HA.
- Use `ha_bridge: true` when HA must bridge a logical target.
- For external numbers, confirm the optional trunk is configured and registered.
- Check whether the entry is disabled. Disabled entries reject instead of
  routing through HA.
- Check `extension` aliases for local/internal targets. Numeric targets from
  ESP always go to HA; HA resolves `extension` as an internal target and
  `number` as an external trunk target.

## Registered Softphone Cannot Register To HA

- Confirm the always-on HA SIP UDP/TCP listener is reachable on the configured
  SIP port and that the client's selected transport matches.
- Enable the local registrar in VoIP Stack setup.
- Create an account with `voip_stack.create_account`.
- If no password is supplied, copy it from the administrator-only action
  response or capture that response with `response_variable`. The generated
  password is returned only once.
- Configure the softphone with HA advertised host, SIP port, username and
  password. Do not configure an external PBX/outbound proxy for local HA
  registration.
- Confirm HA logs show REGISTER and a dynamic phonebook contact for the
  registered SIP endpoint.

## Dahua VTO Registers But Calls Fail

- Use the same VTO account/number configured on the door station when creating
  its local SIP account in VoIP Stack. The dynamic phonebook entry must show
  the current registered Contact.
- Prefer TCP when the device registers over TCP. HA reuses the observed
  REGISTER connection, including when the Contact advertises an address that
  would not be reachable directly through NAT.
- Enable debug logging and look for `SIP TCP connection reuse enabled`. If the
  log instead says no live registered flow was found, confirm the registration
  has not expired and that the phonebook route selects the current Contact.
- Dahua `PCM/16000` is a vendor little-endian format, not RFC `L16`. It is
  enabled automatically only for a `Dahua UAC/...` User-Agent. A generic peer
  offering that token is rejected rather than globally changing PCM semantics.
- If HA-to-VTO immediately returns `486 Busy Here`, give the originating HA
  phone its own extension/name instead of making it appear to call from the
  VTO's own account. Some firmware rejects an apparent self-call.
- SIP `MESSAGE` door-control commands are not implemented. Use the VTO's
  supported DTMF/relay mechanism or a Home Assistant automation instead.

Current compatibility is capture- and simulator-qualified, not a claim that
every Dahua firmware revision has been tested on physical hardware. When
reporting a variant, attach sanitized REGISTER, INVITE/answer SDP, final status
and RTP payload-size evidence.

## Busy Or DND

DND and active-call contention should produce `486 Busy Here` or a terminal
reason of `busy`. Decline should produce `603 Decline` or a configured SIP
final response.

## Hold, UPDATE Or Re-INVITE

ESP endpoints do not renegotiate established media. A hold or media-changing
re-INVITE receives `488 Not Acceptable Here`; the original dialog and media
remain active and a later BYE must still end the call normally.

HA-owned dialogs accept compatible peer-initiated UPDATE or re-INVITE offers.
If HA returns `488`, inspect whether the peer tried to add/remove video, change
the established video codec, supplied an unsupported audio shape or sent an
offerless re-INVITE. A rejected offer must not replace the previous media. For
an accepted update, inspect `media_renegotiations`, the current directional
formats and the WebSocket `media_update` notification. A re-INVITE 2xx also
requires ACK; HA terminates the dialog if that ACK never arrives.

## No Audio

- Confirm RTP ports are reachable in both directions.
- Check selected TX/RX formats in the SIP snapshot.
- Check RTP packet/byte counters on both HA and ESP.
- For HA bridge calls, inspect relay logs for conversion/drop messages.
- Capture WAV from the HA websocket probe or a SIP softphone. Counters that
  increase do not prove audible audio.
- If audio is rhythmic, choppy or "machine gun" style, compare the negotiated
  `ptime`/frame size against the actual RTP payload byte size.
- If one direction is silent but counters increase, inspect the source device:
  mic-only/speaker-only mode, muted switch, low analog gain, AFE/AEC output
  surface, or silence in the room.
- For generic ESPHome-native YAMLs, verify the hardware matches the YAML. The
  reference native full/speaker examples are written around INMP441 plus a
  MAX98357A-style I2S amplifier. A PCM5102 is a line-level DAC, not a speaker
  amplifier, and may need a powered speaker or amplifier plus correct mute/XSMT
  wiring. INMP441 boards also depend on the L/R strap; if the selected
  `channels: [1]` path is silent, test `channels: [0]`.
- `speaker_only` profiles intentionally have no microphone path. They can play
  remote audio but cannot send local microphone audio back.
- If one browser works and another does not, check which browser owns the HA
  softphone media WebSocket for that active call.

## Trunk Does Not Register

- Confirm `trunk_enabled` is on; when off, no trunk runtime is created.
- Check `sip_trunk.trunk_status_code`, `trunk_status_reason` and
  `trunk_last_sip_event` in the HA softphone snapshot.
- Confirm provider transport, server, port, username/auth username and password.
- If the provider requires an outbound proxy, set `trunk_outbound_proxy`.
- INFO logs should show REGISTER, challenge if present, and final registration
  status. DEBUG logs include the detailed SIP flow.

## Inbound Trunk Call Routes To The Wrong Target

- Confirm the provider offers RFC2833/telephone-event DTMF in SDP.
- Prefer negotiated RTP `telephone-event`. The widely deployed legacy SIP INFO
  DTMF representation is also accepted; acoustic in-band tones are not.
- Check that the target exists in the central phonebook and has the matching
  `extension` value.
- Keep the inbound DTMF timeout short, normally 3 seconds. Set it to `0` when you do not want trunk pre-answer/DTMF and want inbound calls to follow the normal dialplan immediately.
- If no digits arrive, HA uses `trunk_inbound_default_target`.
- If digits arrive but do not resolve, HA logs them and terminates the answered
  trunk leg as `route_not_found`.

## Card State Looks Wrong

- ESP mirror cards should follow ESPHome entity state and ESP buttons. They do
  not own RTP counters for the HA softphone leg.
- HA softphone cards should follow `voip_stack` softphone snapshots/events.
  The card must not infer terminal state locally.
- Hard-refresh the dashboard after upgrading the frontend resource.
- If multiple browsers are open, only the browser that attached the HA
  softphone media WebSocket owns live browser audio for the active HA call.
