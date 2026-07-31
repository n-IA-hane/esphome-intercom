# SIP video

VoIP Stack can optionally turn the Home Assistant softphone card into a SIP
video phone for standard SIP phones, softphones and door stations. SIP video
is a supported `2026.8.0` capability. Standard ESPHome profiles remain
audio-only. Qualified ESP32-P4 profiles can send and receive RTP/JPEG or H.264
video without changing the audio contract.

Video is disabled by default and does not alter an audio-only installation.
Open the VoIP Stack integration, choose **Reconfigure**, then enable
**SIP video for the Home Assistant softphone**. A second step
offers two independent capabilities:

- **Enable video transcoding** lets the HA softphone receive H.263,
  H.263-1998 or H.265 through the FFmpeg binary already available to Home
  Assistant.
- **Allow browser camera transmission** exposes a **Send Camera** control in
  the card. The logical Home Assistant phone stores that choice in its config
  subentry and exposes it as a native switch; each attached browser still asks
  for its own camera permission. Receiving video never requires camera access.

If video setup, decoding, camera permission or transcoding fails, the SIP
dialog and browser audio remain active whenever audio negotiation succeeded.

## Capability matrix

The direct browser path does not decode and re-encode video on the HA server:

| Negotiated SIP codec | Receive in card | Send browser camera | Server transcode |
| --- | --- | --- | --- |
| H.264 Baseline, Main or High | Direct when supported by the browser | Direct for packetization mode 1 | No |
| VP8 | Direct | Direct | No |
| JPEG over RTP | Direct | No | No |
| H.263 | Optional | No | Receive-only to VP8 |
| H.263-1998 / H.263-2000 | Optional | No | Receive-only to VP8 |
| H.265 / HEVC | Optional | No | Receive-only to VP8 |
| Audio-only or unsupported video | Audio continues | No | No |

## ESP32-P4 video endpoints

P4 video is compile-gated. A firmware includes exactly one SIP video codec:

- the VoIP-only JPEG profile uses the hardware JPEG camera path and the P4 JPEG
  decoder;
- the VoIP-only H.264 profile uses the P4 hardware encoder and the maintained
  receive decoder path;
- the full-experience SIP profile uses JPEG so video can coexist with AFE,
  Voice Assistant, Micro Wake Word and the landscape UI.

The native-camera full profile is a separate one-way case. It publishes the
standard ESPHome camera entity to Home Assistant while its SIP call remains
audio-only. Home Assistant renders that camera through the normal camera
platform rather than treating it as negotiated SIP video.

Do not enable both JPEG and H.264 in one P4 firmware. The YAML codec choice
gates the unused source, decoder, buffers and managed libraries at compile
time.

The profile supports:

- incoming and outgoing calls owned by the Home Assistant softphone;
- `sendrecv`, `sendonly`, `recvonly` and `inactive` SDP directions;
- one negotiated video stream, using the first accepted `m=video` section;
- H.264 single NAL units, STAP-A and FU-A;
- VP8 RTP payloads and RFC 2435 JPEG reassembly;
- sequence reordering with bounded queues and damaged-access-unit rejection;
- RTP/AVP by default and RTP/AVPF when offered by the remote endpoint;
- compound RTCP receiver reports, plus negotiated AVPF PLI or FIR key-frame
  requests;
- symmetric RTP source latching for ordinary SIP devices behind NAT;
- one authenticated browser media owner for the active HA call;
- exact-codec RTP and RTCP relay between two HA-owned standard SIP legs.

The exact-codec bridge changes only the leg-local RTP payload type. Encoded
payload, timestamp, sequence, marker, SSRC and RTP extensions remain intact.
H.264 packetization mode, profile and RTP transport profile must match, and
other codecs must have matching normalized format parameters. Different
codecs are not transcoded between two SIP endpoints.

## Direct, trunk and PBX calls

SIP video is not limited to LAN or VPN SIP URIs. The HA softphone
offers and accepts the same video profile when a call uses a configured SIP
trunk or a video-capable PBX. An authenticated `401` or `407` retry preserves
the complete audio/video SDP offer, including media direction and codec
parameters.

For inbound trunk video, **Direct to default destination** reaches the selected
phone without digit-collection delay. **DTMF extension selection** can also
preserve video when the initial trunk offer contains a compatible video stream:
HA pre-binds and advertises the audio/video ports while collecting digits, then
hands those exact sockets to the selected HA browser phone. Selecting an ESP,
Assist or another audio-only target releases the unused video resources.

With RTP/AVP there may be no negotiated key-frame feedback. A browser that
attaches after DTMF collection can therefore wait until the remote sender's
next natural keyframe. VoIP Stack deliberately does not send unnegotiated PLI,
FIR or proprietary SIP picture-update messages.

End-to-end success still depends on every provider or PBX in the route keeping
the `m=video` section and forwarding the negotiated RTP/RTCP ports. A normal
PSTN leg or an audio-only ITSP may remove video while leaving audio usable.
When a PBX bridges two HA instances, exact-codec H.264, VP8 or JPEG can remain
passthrough; this integration does not require both homes to share one HA
instance.

### Dahua door-station interoperability

Dahua UAC firmware commonly advertises `PCM/16000` for audio. This is not the
network-byte-order `L16` payload defined by the RTP standards: captures and
existing integrations show signed 16-bit **little-endian**, mono, 20 ms frames
on a dynamic payload type. VoIP Stack recognizes the narrow `Dahua UAC/...`
User-Agent profile and applies that contract only to that peer. It never treats
generic `PCM` as an alias for standard `L16`.

The same profile is used in both directions:

- a Dahua INVITE can negotiate its exact PCM audio alongside compatible SIP
  video;
- an HA call to a Dahua endpoint registered over TCP reuses the live REGISTER
  connection and offers the vendor PCM format first;
- a repeated digest refresh with a stale nonce receives a fresh challenge
  with `stale=true`, without weakening replay protection;
- a compatible audio call may add video later with an in-dialog re-INVITE.

There is no public Dahua switch and no ESPHome firmware change. Detection,
offer/answer and RTP conversion remain internal to the HA SIP leg. The profile
has been qualified against public wire captures and an exact simulated peer,
including bidirectional audio/video, re-INVITE and teardown; real-device
firmware variants still need user feedback.

## Card behavior and privacy

Received video becomes the background of the in-call card. The caller,
duration and call state move into a full-width hang-up bar at the bottom so the
picture remains the primary content. The layout follows Home Assistant
Sections sizing from compact 6-column cards through wide and tall cards.
Long caller names are truncated inside the bar rather than widening the page.

Detailed codec, packet and frame counters appear only when VoIP Stack debug
mode is enabled. The normal card does not cover the picture with diagnostics.

Camera transmission has two gates. The integration-level option must first be
enabled, then **Send Camera** must be enabled on that logical HA phone. The
preference survives cache clearing and is shared by every card bound to the
same phone; camera permission remains local to the browser that owns media.
Permission denial stops only the outgoing camera track. Incoming video, audio
and call controls continue independently. Reloading the dashboard during
ringing or an established call transfers media ownership to the new card and
releases the old WebSocket deterministically.

## Direct and transcoded media paths

Direct receive path:

```text
SIP peer RTP -> bounded RTP reorder/depacketizer -> authenticated HA WebSocket
             -> browser WebCodecs or JPEG decoder -> card canvas
```

Direct camera path:

```text
browser camera -> WebCodecs H.264/VP8 encoder -> authenticated HA WebSocket
               -> RTP packetizer -> SIP peer
```

Optional legacy-codec receive path:

```text
SIP H.263/H.263-1998/H.265 RTP -> local FFmpeg subprocess -> VP8 RTP
                                -> authenticated HA WebSocket -> browser
```

There is no intermediate recording or complete video file. FFmpeg receives
and emits RTP continuously on loopback. The transcode path is intentionally
bounded to one active call, one FFmpeg thread, at most 1280 pixels of width,
15 frames per second, 700 kbit/s target and 900 kbit/s maximum output. A second
simultaneous transcode remains audio-only instead of starting unbounded codec
workers.

VoIP Stack first uses the FFmpeg binary configured by Home Assistant and then
falls back to `ffmpeg` on the host path. Home Assistant OS and Home Assistant
Container already include FFmpeg. Home Assistant Core installations must make
it available separately, as described by the
[Home Assistant FFmpeg integration](https://www.home-assistant.io/integrations/ffmpeg/).

VoIP Stack does not bundle, modify or redistribute FFmpeg or codec libraries.
It starts the installation's existing executable only when transcoding was
explicitly enabled and is required by a call. FFmpeg builds can be LGPL or GPL
depending on their enabled components; distributors of a custom binary remain
responsible for that binary under the
[FFmpeg licensing guidance](https://ffmpeg.org/legal.html). This paragraph is
informational, not legal advice.

## Browser and network requirements

- Use Home Assistant through HTTPS or a browser-recognized secure local
  context. WebCodecs and camera capture are secure-context features.
- Use a current browser. Codec availability is checked at runtime because it
  can depend on the browser, operating system and installed media support.
- Permit camera access only when HA must transmit video. A remote `sendonly`
  door station needs no HA camera permission.
- Keep SIP and RTP on a trusted LAN or VPN. This profile uses unencrypted
  SIP/RTP and does not add SRTP, ICE, STUN or TURN.
- Allow the configured HA RTP range through local firewalls. A video call
  reserves an additional even RTP/odd RTCP pair alongside audio media.

## Deliberate limits

This profile does not claim support for:

- video on ESPHome endpoints;
- video through Assist, conference rooms or ring-group legs that traverse
  standard SIP/RTP endpoints (a local HA browser caller can retain direct
  browser video when another local browser phone wins the ring group);
- cross-codec transcoding between two standard SIP endpoints;
- VP9, AV1, MxPEG or other proprietary payloads;
- more than one simultaneous server-side transcode;
- more than one browser owning the same active HA video stream;
- SRTP, DTLS, ICE, STUN or TURN;
- RTCP multiplexing, generic NACK retransmission or bandwidth adaptation;
- recording, snapshots or a camera entity;
- adding/removing video on an established SIP-to-SIP relay bridge, or changing
  the codec contract of an established video stream;
- locally initiated media renegotiation or REFER/NOTIFY transfer;
- IPv6 RTP media.

Unsupported video is rejected in the SDP answer with a zero media port while
a compatible audio section remains active. A direct HA-browser dialog can
stage and commit a compatible peer-initiated video add/remove; rejected or
stale updates preserve the previous media contract.

## Qualification

The `2026.8.0` release passes **1102 tests plus 99 subtests**. SIP video has
also been exercised on real browser-to-browser, HA-to-HA and video-capable
trunk calls, including bidirectional media, re-INVITE and cleanup.
Compatibility with a particular third-party phone or door station still
depends on its exact codec, SDP and RTP behavior.

The protocol work follows
[RFC 3264 offer/answer](https://www.rfc-editor.org/rfc/rfc3264),
[RFC 3550 RTP/RTCP](https://www.rfc-editor.org/rfc/rfc3550),
[RFC 4585 RTP/AVPF](https://www.rfc-editor.org/rfc/rfc4585),
[RFC 6184 H.264 RTP](https://www.rfc-editor.org/rfc/rfc6184),
[RFC 7741 VP8 RTP](https://www.rfc-editor.org/rfc/rfc7741),
[RFC 2435 JPEG RTP](https://www.rfc-editor.org/rfc/rfc2435),
[RFC 4629 H.263 RTP](https://www.rfc-editor.org/rfc/rfc4629),
[RFC 7798 HEVC RTP](https://www.rfc-editor.org/rfc/rfc7798) and
[RFC 4961 symmetric RTP](https://www.rfc-editor.org/rfc/rfc4961).
Browser media uses the
[W3C WebCodecs API](https://www.w3.org/TR/webcodecs/) and the
[AVC Annex B registration](https://www.w3.org/TR/webcodecs-avc-codec-registration/).
