# Media negotiation contract

VoIP Stack uses ordinary SIP offer/answer and RTP. Every phone is negotiated from
its enabled codecs, real implementation capabilities and the descriptions it sends.
An ESP, a desk phone and a trunk use the same rules. Home Assistant controls and
hardware provisioning do not supply a second media protocol.

## Ownership and data flow

`core/media` is the sole owner of codec, sample-rate, channel, packet-time,
payload-number and telephone-event decisions. Its codec registry describes the
implemented wire formats. Packet-time arithmetic and payload history are shared
primitives. SIP clients and listeners parse messages, run transactions and commit
the resulting contract; they do not select another format after negotiation.

The engine handles creating an offer, answering an offer, applying an answer and
renegotiating an established leg. A leg contract records transmit and receive
separately, including accepted alternatives. The bridge planner decides whether
each direction needs payload relay, reframing, PCM conversion or transcoding.
The media owner executes that plan.

A codec is usable only when enabled and its actual encoder or decoder is
available in the required direction. Codec availability is not inferred from a
library package name alone. Unsupported descriptions receive an explicit refusal.

## Standard descriptions and directional preferences

An SDP `sendrecv` description must contain formats that can genuinely be sent and
received. Transmit and receive preferences can differ within the negotiated set.
An answer can retain several acceptable primary mappings when needed for that
contract. The receiving side accepts the mappings it declared; it does not assume
that every packet uses the first mapping. See [RFC 3264 sections 5.1, 6.1 and 7](https://www.rfc-editor.org/rfc/rfc3264.html#section-6.1).

The maintained full profiles prefer 16 kHz speech in 20 ms packets for microphone
transmission and 48 kHz in 10 ms packets for speaker reception. They also implement
the alternatives they advertise. The receive envelope and its buffer capacity
are independent of the transmitter's preferred packet-size budget.

These are endpoint capabilities and preferences, not a PBX exception for ESPHome.
Compatible ordinary phones may choose G.711, G.722, Opus or another mutually
supported format. Different calls and directions may use different contracts.

`ptime` describes the local receiver's preferred packet duration. The value in an
offer and the value in its answer can differ. `maxptime` is a hard receive bound
for the media section. A fixed implementation packet set remains binding. An
advisory minimum does not force a codec downgrade when a valid shorter duration
exists. See [RFC 3264](https://www.rfc-editor.org/rfc/rfc3264.html#section-5.1) and
[RFC 8866](https://www.rfc-editor.org/rfc/rfc8866.html#section-6.5).

Packet durations use exact sample arithmetic. A candidate must have an integral
sample count, fit the applicable transmit budget and be producible by the local
implementation. The receiver separately validates wire size, framing and actual
duration. Preferred packet time is not a reason to discard a longer valid packet
inside the declared envelope.

## Capability evidence

SIP OPTIONS and negotiated SDP provide evidence for the individual Contact and
its signaling flow. Codec geometry, receive preferences and fixed packet-time
support are separate facts. A receiver's `ptime` does not prove the sender's packet
set. Incomplete evidence is not promoted into a complete declaration.

Contact replacement, credential changes and expiry invalidate the associated
evidence. One registered client must not inherit another client's capabilities
merely because both use the same account.

The HA card is a local client of an account using the integration's existing
WebSocket media transport. Its actual PCM edge supports 48 kHz. That local edge
has its own real capability declaration; the SIP leg still follows the same
negotiator and bridge planner. Voice applications can keep their natural 16 kHz
format without reducing a browser or telephone stream unnecessarily.

## Payload mappings and renegotiation

Answers use the offer's payload mappings where required by offer/answer. Sending
uses the mapping declared by the receiver. Static assignments remain fixed;
dynamic allocation never evicts a live or previously used meaning.

Payload history is retained for each receiving namespace through re-offers and
offerer changes. Reusing a number for a different meaning is refused while the
old media contract remains intact. Invalid media inside an already accepted 2xx
is handled by the dialog owner with the required ACK and recovery or teardown,
not by sending a new 488 response.

An established primary mapping is preferred when still legal. New packet-time
or format limits are applied by the engine before commit; keeping a mapping does
not authorize retaining an obsolete `ptime` above a newly offered `maxptime`.
Media allocation, signaling acceptance, commit and rollback have one owner.

## Codec-specific rules

- G.722 has an 8 kHz RTP clock and 16 kHz decoded audio.
- L16 and L24 use network byte order. Vendor PCM byte-order differences require
  an explicit interoperability profile derived from SIP/configuration evidence.
- Opus uses `opus/48000/2` on the wire. Its sender hints, receiver preferences and
  hard limits have different meanings. An answer describes our receiver; the
  peer's receive limits constrain our encoder. See [RFC 7587](https://www.rfc-editor.org/rfc/rfc7587.html).
- Opus payload compatibility is directional. Unequal hint strings alone do not
  require transcoding, but a relay must satisfy the destination's receive bounds.
- Opus validation reads the packet framing and actual duration, including the
  hard 120 ms packet limit. Byte count alone cannot enforce `maxptime`.
- Optional codec implementations remain optional and are advertised only for
  directions supported by working adapters.

## RTP reception and forwarding

The receiver validates source address and negotiated payload before accepting a
stream. It tracks sequence numbers, duplicates, ordering and guarded SSRC changes.
A legitimate new SSRC requires validation; an arbitrary packet cannot replace a
current source. Decoder, converter and timestamp state change coherently.

A payload relay preserves sequence gaps and timestamp progression with a stable
mapping. It does not turn a duplicate into a fresh packet. It owns no audio decoder
or periodic playout timer. A decoding path uses bounded reordering and a one-shot
deadline only while an incomplete ordering window exists. The deadline is canceled
on close or reconfiguration. Recording observes native decoded PCM once.

PCM conversion preserves streaming phase and sample accounting across blocks.
Downsampling filter length follows the rate ratio. Quality is qualified from the
production converter using independent signals, not a copy of its filter kernel.

Audio RTCP is explicitly disabled in descriptions where the implementation does
not provide it. Video RTCP is handled separately. Reports and feedback must refer
to the RTP stream and SSRC actually presented to the peer.

## Telephone events

Transmit and receive each negotiate a payload, event set and clock. Prefer the
active audio clock when mutually offered; use the 8 kHz alternative only when
negotiated. SIP INFO is a fallback only on a leg that can carry it.

A bridged press retains progress, duration and end semantics. Long events use
RFC 4733 segments instead of clamping at 65535 clock ticks. IVR continues to receive
one logical digit per press. Duplicates and terminal repeats do not create new
digits. See [RFC 4733](https://www.rfc-editor.org/rfc/rfc4733.html).

## Video intent and feedback

Audio-only origination does not add video because a peer happens to support it.
Receiving video and sending a camera are separate call intents. A receive-only
video call does not require permission to capture the caller's camera. Incoming
answers respect the offer and the selected receive/send preferences.

Adding or removing video uses normal renegotiation and preserves unaffected audio.
H.264 packetization mode and profile constraints remain binding. Feedback is routed
per stream and SSRC, including when only one direction is transcoded. A transcoder
without a working dynamic keyframe control must not claim that control; periodic
recovery remains distinct from a successfully executed request.

## Verification

Qualification identifies exact coordinated source revisions, actual call route,
registration mode, negotiated descriptions and independent media evidence.
Compilation is not hardware evidence. A unit test is not an acoustic observation.

Coverage includes offer/answer in both roles, renegotiation, shared-account
forking, transfer, loss, duplicates, ordering, source changes, packet-time bounds,
DTMF, video intent, teardown and a subsequent call. Browser, SIP, firmware and
physical-panel observations are reported separately. Structural tests protect
composition and packaging; they do not prove a protocol or media result.
