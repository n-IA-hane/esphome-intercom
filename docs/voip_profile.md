# voip-pcm/1

`voip-pcm/1` is the VoIP Stack profile.

It is a SIP/SDP/RTP profile, not a proprietary intercom protocol. ESP devices
act as SIP user agents and exchange RTP PCM media. Home Assistant may provide
roster distribution, routing, bridging and optional provider trunk registration,
but direct ESP-to-ESP calls must work when a SIP URI is known.

## Security and registration

The ESP profile intentionally does not implement SIP authentication or
registration.

- ESP does not require `Authorization`.
- ESP does not send `WWW-Authenticate`.
- ESP does not implement SIP `REGISTER`.
- If a remote endpoint returns `401` or `407`, the call fails with
  `auth_required_unsupported` or `proxy_auth_required_unsupported`.

ESP trust is provided by the existing ESPHome/Home Assistant management channel
for roster delivery. SIP signaling and RTP media remain unencrypted and
unauthenticated on the local network.

Home Assistant may register one optional provider/PBX trunk and may separately
act as a Digest-authenticating registrar for standard SIP endpoints. Both
features belong to HA and do not create registrations for ESP devices.

Phonebook membership and HA registration are not inbound caller admission
rules. Any peer that can reach an ESP or HA SIP listener may send an INVITE;
normal routing, busy/DND and SDP checks then decide the result. Deploy SIP/RTP
on a trusted LAN/VPN and enforce stricter admission with network controls or an
SBC. This inbound openness does not make HA an anonymous PSTN gateway:
external-trunk routes require a registered, roster-known, HA-local or trusted
trunk origin.

Configured local SIP accounts are retained by the PBX as logical endpoints so
their group membership and settings survive disconnects. Cards expose such an
account as a callable contact only while the registrar has at least one live
Contact binding; expiry or explicit deregistration removes it from the card.

Outbound audio/video offers remain video-capable. A standards-compliant answer
retains rejected media sections with port zero. For PSTN interoperability, the
outbound client also accepts a gateway answer that keeps the leading compatible
audio section but omits only trailing video sections; those omitted sections
are treated as rejected without disabling video offers or later video
re-INVITE negotiation.

## SIP core

Required SIP methods:

- `INVITE`
- `ACK`
- `CANCEL`
- `BYE`
- `OPTIONS`
- `REGISTER` on the HA trunk client and HA local registrar only
- `INFO` is acknowledged by HA, but its body is not used as a digit source;
  DTMF routing is based on RTP `telephone-event`

Required responses:

- `100 Trying`
- `180 Ringing`
- `200 OK`
- `405 Method Not Allowed`
- `486 Busy Here`
- `487 Request Terminated`
- `488 Not Acceptable Here`
- `500 Server Internal Error`
- `501 Not Implemented`
- `603 Decline`

Unsupported methods are rejected explicitly. Unsupported media is rejected with
`488 Not Acceptable Here`.

ESP endpoints do not support session-modifying in-dialog INVITE. They answer
hold or codec renegotiation with `488` while keeping the established dialog and
its selected media unchanged; the original dialog can still be terminated
normally with BYE.

HA-owned dialogs accept compatible peer-initiated UPDATE or re-INVITE offers.
They may update audio direction, supported audio format and RTP endpoint, and
may hold/resume an already negotiated compatible video stream. HA does not add,
remove or change the codec of video in-dialog and does not originate the offer.

SIP signaling transports:

- UDP on the configured SIP listen port
- TCP on the same configured SIP listen port

RTP audio remains UDP in the current profile, even when SIP signaling is TCP.

The HA softphone has an optional SIP video extension to this profile.
It does not change the ESP media contract and is disabled by default. When
enabled, standard SIP endpoints may negotiate direct H.264, VP8 or JPEG over
RTP/AVP, or RTP/AVPF when the remote offer selects feedback. A separate
opt-in can receive H.263, H.263-1998 or H.265 through the FFmpeg binary already
available to Home Assistant. HA-owned SIP bridges first preserve exact-codec
RTP and RTCP. When the two legs have no direct match, the same bounded opt-in
can convert an incompatible active direction to the H.264 or JPEG contract
negotiated by its receiver. See [SIP Video](SIP_VIDEO.md) for the exact
capability and security boundaries.

## SDP and RTP

ESP media is PCM-only.

- Mandatory: RTP `L16`
- Optional: RTP `L24`, only when packed as RTP L24
- Dynamic payload types: `96..127`
- SDP uses `a=rtpmap`, `a=ptime`, `a=maxptime`
- SDP must not use `a=fmtp` for packet time
- Default maximum RTP payload is 1200 bytes

ESP must convert at the RTP boundary:

- internal `S16LE` to RTP `L16` network byte order
- internal `S24LE_IN_S32` to packed RTP `L24` network byte order

Compressed codecs are not implemented on ESP. If a SIP softphone offers only
compressed codecs such as PCMU, PCMA, Opus, Speex, GSM or G.722, ESP returns
`488 Not Acceptable Here`.

ESP devices never advertise or decode video. The optional HA softphone video
path is not routed into an ESP, ring group, conference room or Assist pipeline.

## Roster

The public phonebook contract is canonical JSON. It resolves user-facing names,
extensions and phone numbers into either direct SIP URIs or Home Assistant
routes.

Direct calls use SIP URIs such as:

- `sip:Kitchen@192.168.1.51:5060;transport=tcp`
- `sip:Salotto@192.168.1.52:5060;transport=udp`

Phone numbers and unresolved names route through the Home Assistant SIP bridge when
configured. If an optional HA trunk is configured and registered, unresolved
external targets can route through that trunk. Missing endpoint metadata must
not invent a direct SIP route.

Inbound trunk calls can use RFC2833/telephone-event DTMF digits as local
extension selectors. Digits are mapped by HA to central phonebook `extension`
values; ESP devices do not need provider-side extensions or registrations.

## State

SIP events are the source of truth for VoIP phone state.

- outgoing `INVITE` -> calling
- incoming `INVITE` accepted -> ringing
- `180` -> remote ringing
- `200` + `ACK` -> connecting/in_call
- `CANCEL` before answer -> cancelled, never in_call
- `BYE` -> disconnected
- `486` -> busy
- `488` -> media incompatible
- `401`/`407` -> authentication unsupported

Cards render the state of their owner only:

- HA softphone card mirrors the HA softphone.
- ESP mirror card mirrors one ESP.

HA trunk registration status is observability data on the HA softphone snapshot;
it is not an ESP phone state.
