# Troubleshooting

Collect evidence from the parts involved in the failing call. For an HA or
browser-card problem, start with HA integration diagnostics, the HA logs, call
direction and reproduction steps. Include the browser or Companion app version
and relevant browser console errors for card problems.

**If no ESP device is involved, skip the ESP section.** For an ESP problem,
include its firmware logs and applicable diagnostic dumps as described below.
Negotiated formats, RTP counters and a short audio recording can help with
audio faults. Use the SIP capture for signaling that passes through HA.

## Start with the failing step

| What you observe | First checks |
| --- | --- |
| The wrong phone places the call | Check Calling phone and the card's selected phone. Destination is the other party. |
| No outgoing trunk is selected | Choose a default line or an explicit phone/action preference; check HA Repairs. |
| The phone rings but audio is missing | State which direction is silent. Collect SIP negotiation and media diagnostics; ringing alone proves no audio path. |
| An ESP has no contacts although its API is connected | Check its VoIP endpoint discovery and phonebook diagnostics. Updating HACS does not update old firmware. |
| Turning Send Camera off still shows remote video | This disables your transmission, not the other party's. See the video direction guide. |
| A mailbox is enabled but callers never record a message | Set a no-answer voicemail rule, or route the call there explicitly. Enabling storage does not enable forwarding. |
| A message or call stays active unexpectedly | Preserve diagnostics before restarting and include how it was answered and ended. |

Report the exact route, for example **HA card > trunk Work > door station**,
and whether the fault is signaling, received audio, transmitted audio or video.
Start with the [issue evidence checklist](TESTING_AND_DEBUG.md#before-opening-an-issue).
Use the ESP steps below only when an ESP is involved; use SIP capture for calls
that pass through HA. The [developer test guide](TESTING_AND_DEBUG.md) is for
maintainers reproducing a fault, not a requirement for opening a useful issue.

## Collect a runtime snapshot from an ESP

**Only if the problem involves an ESP device, run its diagnostic dumps:**

Open the device's ESPHome logs with `logger` at INFO or a more verbose level.
While the problem is present, press **VoIP Diagnostics** and, if the firmware
uses Audio Stack, **Audio Diagnostics**. Repeat after hangup and attach the
complete output from `BEGIN v=1` through `END v=1` for each component, plus the
boot log.

In Home Assistant, these are device buttons. You can press them on the device
page, or open **Developer Tools > Actions**, select **Button: Press**
(`button.press`) and choose the diagnostic button entities for that ESP.
The output appears in the **ESPHome device logs**, not in the HA action response.

Updated maintained profiles already include the applicable buttons. If they are
missing from a custom YAML, add the relevant definitions below, then rebuild and
upload with the updated components:

```yaml
button:
  - platform: template
    name: VoIP Diagnostics
    entity_category: diagnostic
    on_press:
      - voip_stack.dump_diagnostics:
          id: phone

  - platform: template
    name: Audio Diagnostics
    entity_category: diagnostic
    on_press:
      - esp_audio_stack.dump_diagnostics:
          id: audio_stack
```

Replace `phone` and `audio_stack` with your actual component IDs. Omit the Audio
Diagnostics button if the device does not use Audio Stack. Do not add duplicate
buttons when your packages already provide them. The two `dump_diagnostics`
entries are **ESPHome firmware actions**, not HA services to paste directly into
HA's Actions editor. These dumps do not require verbose audio tracing.

The dumps report component state, negotiated formats and existing counters.
Older firmware must be rebuilt to add them; installing a HACS update does not
change ESP firmware. The maintained preview packages provide these actions
through their coordinated component sources. Rebuild the matching board profile
when upgrading firmware that does not expose them.

For custom YAMLs, see the
[ESP VoIP diagnostic button](https://github.com/n-IA-hane/esphome-voip-stack/blob/main/README.md#before-opening-an-issue)
and [Audio Stack diagnostic button](https://github.com/n-IA-hane/esphome-audio-stack/blob/main/README.md#before-opening-an-issue).
For a Runtime Controller state problem, its detailed dump separately requires
`debug: true`; see the [runtime diagnostic guide](https://github.com/n-IA-hane/esphome-runtime-controller/blob/main/README.md#diagnosing-a-stuck-state).

Use the [report template](TESTING_AND_DEBUG.md#before-opening-an-issue) when opening an
issue. A diagnostic snapshot is a starting point; a packet or audio recording
may still be needed to establish why a fault occurs.

## Follow one call in Home Assistant

Open the account device's **Activity** to see who called and how the call
ended. For a PBX-wide view, open the **VoIP Stack** device (model **PBX**).

At the logger's INFO level, search the Home Assistant log for `PBX | Call`
to find the outcome and the parties. To see what made the routing decision:

1. Open **Settings > Devices & services > VoIP Stack**, then **Enable debug logging**.
2. Reproduce the call once. Disable debug logging to download the log.
3. Search for the called number or phone name. Copy the `call_id` at the end
   of a matching line and search for it to follow that call across components.

For an incoming number block, the trunk line shows the original number, the
number after normalization, and the SIP field that supplied it. Subsequent
lines show whether HA accepted a routing action or used the trunk fallback,
which phone policy applied, and which destination the PBX tried. Queue offer
results explain which operator rang and whether the offer was answered.
For a decision made by a traced HA automation, the log includes its name when
Core can identify the current run. Open that automation's native **Traces** to
inspect its conditions. A manual service call can appear as a Home Assistant
action. Transfers and parking records use the same call reference.

These decision messages use the standard integration logger
`custom_components.voip_stack`. The **SIP/RTP** diagnostic option provides
additional media details. Audio recording and SIP packet capture have their
own controls.

## Capture SIP signaling from Home Assistant (including HA OS)

Use the **VoIP Stack: Capture SIP signaling** action as an administrator. No
SSH, tcpdump, extra add-on or privileged network access is required. This
feature is available from VoIP Stack HA 2026.9.2.

In **Developer Tools > Actions**, start before making the failing call:

```yaml
action: voip_stack.capture_sip
data:
  operation: start
  duration: 120
```

Reproduce the problem, note the times of answering and hanging up, then run:

```yaml
action: voip_stack.capture_sip
data:
  operation: stop
```

The downloaded file is named **`voip-stack-sip.pcap`** and is saved by your
browser on the phone or computer used to open the link. On the HA server,
the capture exists only in RAM; there is no PCAP file to find with a file editor.

The action response contains `download_url`, `messages`, `dropped` and `reason`.
Open the complete `download_url` in your browser to download the PCAP.
The Actions response may display it as plain text; copy the entire URL into
your address bar if it is not clickable. `download_path` is also provided for
clients that already know their HA address. If HA cannot determine its URL,
the response explains how to configure it and still provides that path. Open the file in Wireshark, or
attach it to your support report. The signed link expires after five minutes;
`operation: status` returns a fresh link while the capture is still retained.
Do not share the download link itself.

Use the same action with these operations:

| `operation` | Effect |
| --- | --- |
| `start` | Begin capturing before the test call. `duration` defaults to 120 seconds. |
| `stop` | Stop recording and return the download link. |
| `status` | Check progress or obtain a fresh download link after stopping. |
| `clear` | Stop and delete the capture immediately, invalidating its download link. |

For example, to retrieve a new link after the original has expired:

```yaml
action: voip_stack.capture_sip
data:
  operation: status
```

If `messages` is zero, the capture did not observe SIP messages from this
integration. Check that it was started before the call and that HA actually
handles the call's SIP signaling. If the action does not appear, check that
your installed VoIP Stack release includes it and restart HA after updating.

Capture stops automatically after the selected duration (10 to 300 seconds),
4 MiB or 20,000 messages. It retains the beginning of the call rather than
overwriting it. `dropped` reports observations not saved; `size_limit` means
the capture ended early. After stopping, data stays in memory for 15 minutes,
then is deleted. `operation: clear` deletes it immediately. Starting another
capture replaces the previous stopped capture; attempting to start while one
is running is rejected. Restarting HA also discards it. Nothing is written to
`www`, logs or a persistent capture directory.

This is a **SIP application trace**, exported using Wireshark's Upper PDU PCAP
format. It records SIP UDP messages and complete TCP/TLS messages at the
integration's I/O boundary, with timestamps and socket endpoint metadata.
Authorization and Proxy-Authorization values are removed from the saved copy;
phone numbers, addresses, SDP and other SIP headers remain. Review the file
before sharing it publicly. Redaction never changes messages sent to peers.

The trace does not capture RTP audio/video, TCP segment retransmissions,
packets rejected before reaching the application, or SIP exchanged directly
between two ESP devices without passing through HA. TCP/TLS messages are
recorded before encryption/after decryption, not as fabricated TCP packets.
A TX record means the application handed the message to its socket transport,
not that the remote provider received it. A wildcard local socket address can
appear as `0.0.0.0` or `::`; it is not a measured on-wire source address. Router
or host packet capture may still be needed for NAT or network-delivery faults.

### A call fails before an outbound INVITE appears

Attach the integration's debug log alongside the SIP capture. In **Settings >
Devices & services**, enable debug logging for VoIP Stack, reproduce one call,
then disable debug logging and download the log. A refused TCP connection has
no SIP message to record, so it cannot appear in the application PCAP.

For outbound calls, the log identifies each transport attempt by Call-ID,
transport and destination address/port. A successful attempt reports the local
port and elapsed time. A failed attempt includes the exception type, operating
system error number and elapsed time, even when no INVITE was sent.

Calls to a current authenticated UDP registration keep UDP without probing
TCP first, including for large INVITEs. The registrar validates the selected
Contact before the client acquires its signaling flow. This transport policy
does not pin ordinary registrations to an observed source address: Contact and
Record-Route still control dialog routing. RFC 5626 Outbound registrations
retain their existing fixed-flow behavior. An arbitrary URI or a borrowed UDP
socket alone does not enable this policy.

Preserving registered UDP is an interoperability choice, similar to
[Asterisk's default `disable_tcp_switch=yes`](https://docs.asterisk.org/Latest_API/API_Documentation/Module_Configuration/res_pjsip/),
rather than an exception granted by
[RFC 3261 section 18.1.1](https://www.rfc-editor.org/rfc/rfc3261.html#section-18.1.1).
Large UDP messages can still require IP fragmentation; network
paths that discard fragments need a reachable TCP/TLS setup. The SDP, codec
preferences and UDP datagram size limit remain unchanged.

For other initially UDP requests, `reason=message_size` means the request
exceeded 1300 bytes and triggered a TCP attempt. This can happen when video
adds more SDP; it does not mean the video itself uses TCP. If every TCP connection
candidate is explicitly refused/reset or reports protocol-unreachable before
the request is sent, the client retries the original UDP route once, as described
in RFC 3261 section 18.1.1. The same compatibility recovery covers a confirmed
plain TCP connection-setup timeout toward a resolved IP address, such as a
firewall silently dropping SYN packets while UDP remains reachable. It uses
the existing setup budget and preserves the original SDP and dialog identity.
The log marks this with `reason=connection_failed_before_send`. DNS, writer
publication, unrelated errors, cancellation and SIP response timeouts do not
enable that retry. A request already queued or sent over TCP is never replayed
over UDP by this recovery. Explicit TCP/TLS choices remain unchanged, and TLS
is never downgraded.

These entries describe application-level attempts and outcomes, not captured
TCP SYN/RST packets. For an on-wire handshake or firewall diagnosis, capture
traffic on the HA host or router. Review addresses and call identifiers before
sharing logs; authentication headers are not added by these diagnostic entries.

## ESP does not ring

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

## HA softphone does not ring

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

## Unknown or unregistered caller is rejected

Check admission separately from the destination phonebook. HA requires a
configured trunk, an existing locally owned SIP leg or a Digest-authenticated
account for a new call. A `401` followed by a new authenticated INVITE can be
the normal account challenge; a repeating rejection needs investigation of
the account and the exact Request-URI.

An ESP accepts calls through its recognized configured PBX bindings. Direct
calls require `registration.accept_direct_calls: true`; use this explicitly
for a standalone direct-call installation. A phonebook entry alone does not
grant incoming-call access. See [registered ESP accounts](ESP_REGISTRATION.md).

After admission, check DND, busy state, destination routing and SDP
compatibility. Keep unencrypted SIP/RTP on the intended LAN or VPN. Network
access and SIP account admission address different parts of that boundary.

## Call fails with `media_incompatible`

The SDP offer/answer did not produce a usable PCM RTP format, or HA could not
build the required bridge conversion. Use explicit supported PCM profiles such
as `16000:s16le:1:16`, `16000:s16le:1:32`, `16000:s16le:1:20`, or
`48000:s16le:1:10`. Standard SDP negotiates accepted mappings for both
directions; TX and RX may use different rates and packet times. Maintained PCM
profiles can send 16 kHz/20 ms while receiving 48 kHz/10 ms. The peer must accept
the declared alternatives, and packets must fit the receive envelope.

Check the ESP firmware codec: FULL and P4 profiles use PCM; ESP32-S3 VoIP-only
profiles can use Opus. HA softphone/trunk legs can negotiate additional codecs,
but the bridge must support conversion to the format actually selected by the
ESP. PCM and Opus are separate firmware choices.

## HA cannot route a name

- Ensure `sensor.voip_phonebook` contains the target.
- For an ESP, first identify the phonebook provider. With compatible managed
  LDAP firmware, verify enrollment and the managed account, delivered directory
  configuration, listener/network reachability, a successful refresh/query for
  the current generation and the Native snapshot commit. Manual contacts must
  survive refresh. An online API or JSON push alone proves none of those steps.
- LDAP is the sole dynamic ESP provider. If the required directory actions are
  missing, rebuild and upload matching ESP firmware and update maintained HA
  packages. `voip_stack.push_phonebook` requests a central directory rebuild;
  it does not send JSON rows to firmware. Follow the
  [phonebook guide](PHONEBOOK_PROTOCOL.md#standard-ldap-directory).
- For local ESP-only routing, declare the contact in
  `voip_stack.static_contacts`.
- Use a direct SIP URI (`sip:name@host:5060`) when bypassing HA.
- Use `ha_bridge: true` when HA must bridge a logical target.
- For external numbers, confirm the selected trunk is enabled and ready.
  Registered trunks need a valid binding; static trunks need an active runtime
  and configured trusted networks. Check [trunk selection](SIP_TRUNK.md), rather
  than assuming REGISTER is required for every trunk.
- Check whether the entry is disabled. Disabled entries reject instead of
  routing through HA.
- Check `extension` aliases for local/internal targets. Numeric targets from
  ESP always go to HA; HA resolves `extension` as an internal target and
  `number` as an external trunk target.

## Registered softphone cannot register to HA

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

## Dahua VTO registers but calls fail

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
- For a destination identified by the existing Dahua SIP profile, an outgoing
  browser call with the local camera off now offers receive-only H.264 mode 0.
  This accommodates VTO answers that omit H.264 `fmtp` parameters and add video
  even when called without a local camera. Enable SIP video globally and for
  the calling/receiving phone; the camera-send permission can remain disabled.
  The offer does not enable browser camera capture. Other SIP peers and calls
  with camera transmission requested keep their existing offers. This specific
  path is covered by simulated SIP exchanges; physical VTO retesting is needed.
- If HA-to-VTO immediately returns `486 Busy Here`, give the originating HA
  phone its own extension/name instead of making it appear to call from the
  VTO's own account. Some firmware rejects an apparent self-call.
- UTF-8 `text/plain` SIP `MESSAGE` requests from an authenticated, actively
  registered account are published as `voip_stack_sip_message` events. Use an
  automation to validate the `sender`, `recipient` and `message` fields before
  operating a relay. Other content types and unregistered signaling flows are
  rejected.

Current compatibility is capture- and simulator-qualified, not a claim that
every Dahua firmware revision has been tested on physical hardware. When
reporting a variant, attach sanitized REGISTER, INVITE/answer SDP, final status
and RTP payload-size evidence.

## Browser or companion app cannot answer a call

The card verifies browser audio support and microphone access before it sends
Answer. If that check fails, the SIP call remains ringing and the card reports
the local media problem. It must not answer first and then send BYE merely
because `navigator.mediaDevices` or `getUserMedia` is unavailable.

- Use Home Assistant through HTTPS or another secure browser context.
- Grant microphone permission to the browser or Companion app.
- After updating VoIP Stack, restart HA and reset the frontend cache. In the
  Companion app use **Settings > Companion App > Troubleshooting > Reset
  frontend cache**.
- If the problem remains, report the browser or app version, operating system,
  `window.isSecureContext`, whether `navigator.mediaDevices` exists and the
  exact card error. Do not include tokens or authenticated dashboard URLs.

## Busy or DND

DND and active-call contention should produce `486 Busy Here` or a terminal
reason of `busy`. Decline should produce `603 Decline` or a configured SIP
final response.

## Hold, UPDATE or re-INVITE

Compare the offer with the
[in-dialog compatibility table](ARCHITECTURE.md#in-dialog-media-compatibility).
ESP Native rejects established audio-contract changes, but compatible video
add/remove and direction changes can use its existing media owner. Active
video retargets and incompatible formats remain rejected. A rejected request
must preserve the original dialog and media, including a later normal BYE.

HA-owned dialogs accept compatible peer-initiated UPDATE or re-INVITE offers.
If HA returns `488`, inspect the exact incompatible format or direction,
established video codec change, unavailable converter/resources, or rejection
by the other bridge leg. Video add/remove is not inherently invalid. An
offerless re-INVITE is a supported delayed offer: HA sends the offer in
`200 OK` and requires a compatible answer in ACK. Diagnose an invalid/missing
ACK answer separately from the absence of SDP in the request.
For an audio-only SIP bridge, an accepted video addition produces a second
re-INVITE toward the destination before HA answers the source. A rejected offer
must not replace the previous audio media. For an accepted update, inspect
`media_renegotiations`, both dialog CSeq values, the current directional formats
and the video relay counters. A re-INVITE 2xx also requires ACK; HA terminates
the dialog if that ACK never arrives.

## No audio

- Confirm RTP ports are reachable in both directions.
- Check selected TX/RX formats in the SIP snapshot.
- Check RTP packet/byte counters on both HA and ESP.
- For HA bridge calls, inspect relay logs for conversion/drop messages.
- Capture WAV from the HA websocket probe or a SIP softphone. Counters that
  increase do not prove audible audio.
- If audio is rhythmic, choppy or "machine gun" style, compare the negotiated
  `ptime`/frame size against the actual RTP payload byte size.
- A peer may advertise one `ptime` and still send shorter aligned packets.
  Record both SDP and several RTP payload lengths. VoIP Stack can accumulate
  matching-rate PCMA, PCMU or PCM packets, but conversion paths still reject
  malformed sizes rather than guessing.
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

## Trunk does not register

- Confirm `trunk_enabled` is on; when off, no trunk runtime is created.
- Check `sip_trunk.trunk_status_code`, `trunk_status_reason` and
  `trunk_last_sip_event` in the HA softphone snapshot.
- Confirm provider transport, server, port, username/auth username and password.
- If the provider requires an outbound proxy, set `trunk_outbound_proxy`.
- INFO logs should show REGISTER, challenge if present, and final registration
  status. DEBUG logs include the detailed SIP flow.

## Inbound trunk call routes to the wrong target

- Check the received called number and any matching DID rule, then the named
  trunk's configured inbound destination.
- Check any matching native routing automation and its selected destination.
- For a spoken menu, route to the intended Automation contact and verify that
  its call-received automation runs. Trunks no longer collect extensions
  automatically.
- Verify the receiving account or contact has the extension entered by the
  caller. The [Welcome menu](AUTOMATION_DIALPLAN.md#build-a-small-keypad-menu)
  forwards non-empty digits even when the wait returns `timeout` after a gap.
- Menu input requires negotiated RTP `telephone-event` or compatible SIP INFO.
  Acoustic in-band tones are not decoded. A valid initial route is separate
  from whether a peer sends usable in-call digits.

## Card state looks wrong

- ESP mirror cards should follow ESPHome entity state and ESP buttons. They do
  not own RTP counters for the HA softphone leg.
- HA softphone cards should follow `voip_stack` softphone snapshots/events.
  The card must not infer terminal state locally.
- Hard-refresh the dashboard after upgrading the frontend resource.
- If multiple browsers are open, only the browser that attached the HA
  softphone media WebSocket owns live browser audio for the active HA call.
