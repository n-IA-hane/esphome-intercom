# Media negotiation contract

Status: implementation in progress after two adversarial reviews (section 9). The pure leg
engine is covered by the corpus; production entry points still use the legacy negotiator.
CC-12 is implemented in the shared codec registry. Once migration is complete this document
replaces `AUDIO_NEGOTIATION.md`.

This is the single written contract for how VoIP Stack negotiates audio codecs, sample rates,
packet times, payload numbers, DTMF and media direction on every SIP leg, in the Home Assistant
integration and in the ESP firmware. Every rule has an identifier. Tests cite the identifier they
defend. A behaviour that is not written here is not part of the contract.

The cases in `tests/media_corpus` state what this contract requires for real descriptions.
`tests/media_corpus/ledger.json` lists, field by field, where the engine in production differs
and which declared change excuses each difference.

## 1. Terms

- Leg: one SIP dialog between VoIP Stack and one peer (phone, door station, trunk, ESP, browser
  bridge, local application).
- Offer, answer: as in RFC 3264. Each side describes what it wants to receive.
- Capability: what a leg can do. For each codec and PCM geometry: the set of packet times it can
  send, the packet time it prefers to receive, its hard receive envelope, and its processing
  quantum. A capability lists only combinations that exist. It has no order of preference.
- Fixed set: a capability whose sender can only produce an explicit list of packet times (an ESP
  configured in YAML). A fixed set is a hard constraint, never a hint.
- Policy: what is allowed and preferred toward a peer. An ordered codec list plus defaults.
- Contract: the result of one offer and answer exchange on one leg. Per direction: codec, payload
  number, packet time, format parameters. Plus what is accepted on receive, DTMF, direction, and
  the video contract.
- Profile: a named, isolated deviation from the standards for a known device family.
- Budget: the largest RTP audio payload this side sends in one datagram. 1200 bytes in Home
  Assistant. On an ESP it is the configured `udp_max_payload` (576 to 1488, default 1200).
- Receive envelope: the largest packet a receiver holds. 12 KiB in Home Assistant, bounded per
  codec (Opus: 120 ms). On an ESP: its compiled packet, decoder and buffer limits.

## 2. Normative references and the clauses relied upon

| Reference | Clause | What this contract takes from it |
|---|---|---|
| RFC 3264 | 5.1, 6.1, 7 | Each side lists what it accepts. `ptime` states the packet time that side would like to receive. The two directions need not use the same packet time. The answerer should use the payload numbers of the offer. The sender must use the payload number its receiver declared. |
| RFC 3264 | 8, 8.3.2 | Modifying a session. A payload number must not be remapped to another codec within a session. |
| RFC 8866 | 6.4, 6.5 | `ptime` is a recommendation and is not needed to decode. `maxptime` is the most media a receiver accepts in one packet. |
| RFC 3551 | 4.2, 6 | Default packet time is 20 ms or one frame if longer. Accepting 0 to 200 ms is an interoperability target, not an upper bound. Static payload numbers. |
| RFC 7587 | 4, 6.1, 7.1 | Opus is always `opus/48000/2`. Frame durations 2.5, 5, 10, 20, 40, 60 ms, up to 120 ms per packet. Receiver parameters limit the sender; sender parameters are hints. |
| RFC 4733 | 2 | Telephone events share the timestamp base of the audio stream. |
| RFC 6184 | 8.2.2 | H.264 parameters in offer and answer. |
| RFC 3261 | 12.2.1.1, 13.2.1, 14.2, 18.1.1 | Dialog routing. The first description in a response to an INVITE is the answer of that dialog. A re-INVITE without a description is answered with an offer. Requests above 1300 bytes, or within 200 bytes of a known path MTU, use a congestion controlled transport; the retry over UDP is limited to failures while opening that connection. |
| RFC 5626 | 7 | A request to a binding registered with SIP Outbound stays on its flow. |
| RFC 6337 | 3.1.1, 5.2.5, 5.3 | Offer and answer placement in SIP messages. A media line slot persists. Hold survives an offerless re-INVITE. |
| RFC 6141 | 3.2 to 3.4 | What a failed re-INVITE leaves in place, and when a change is already committed. |
| RFC 4028, RFC 3311 | | Session refresh and UPDATE. |

`minptime` as a media attribute is not defined by RFC 8866. Opus stacks also send it inside the
format parameters of one payload. Section 4.2 treats the two separately.

## 3. Principles

1. Standards first. A rule that deviates from a reference says so and lives in a named profile.
2. One owner per decision: codec facts, packet time, payload numbers, the contract of a leg, the
   plan of a bridge.
3. Capabilities, not enumerated preferences. An order of preference is policy and is never
   inferred from the order of a capability. A capability is never widened by combining its rates,
   channels and durations independently.
4. Legs negotiate independently. The bridge adapts and passes media through untouched when both
   sides match.
5. Liberal in what is accepted within the receive envelope, conservative in what is sent.
6. A call is refused only when no feasible media contract exists: no codec with a usable
   direction, packet time, compatible parameters and transport. The refusal carries a reason. An
   optional stream that cannot be served is refused with port 0 while the rest of the session
   stays.
7. The shape of a call follows the request. An audio call stays an audio call.
8. Every decision is explainable from a per call trace.
9. The old engine is a comparison point, not an oracle. A defect that both engines share is still
   a defect.

## 4. Rules

### 4.1 Codec selection

- S1. Candidates are the offered codecs that the policy enables, that have a working RTP adapter
  in this installation for the needed direction, and whose format parameters are compatible.
- S2. The default order is the local policy order ("quality"). A policy may select the order of
  the offer instead.
- S3. In a bridged call the bridge planner may reorder the candidates of S1. It never adds one.
- S4. An answer written by this side lists one primary audio codec plus telephone events.
  Preserved exceptions: profile `esp_directional_v1`, and receive alternatives that were
  genuinely negotiated. S4 limits what is written.
- S5. When a peer's answer lists several codecs, this side receives only the mappings that are
  compatible, common, in the negotiated direction and allowed by the policy of the offer that was
  actually sent. A listed extra capability does not by itself authorize incoming RTP.
- S6. An answer is applied to the exact offer and policy that were sent.

### 4.2 Transmit packet time

Inputs: the codec, the receiver's `ptime`, `maxptime`, any minimum, the set of packet times the
local sender can produce for that codec and geometry, and the budget.

- T1. Use the receiver's `ptime` when the local sender can produce it, it is within the
  receiver's `maxptime`, and the payload fits the budget.
- T2. Otherwise use the default of the codec for that leg. If it is not feasible: rank the
  durations the local sender can really produce (including discrete values such as 16 and 32 ms
  where they exist) within the receiver's `maxptime` and the budget, longest standard value
  first (60, 40, 30, 20, 10, 5), then the longest whole sample duration.
- T3. `maxptime` and a fixed set are hard. When no duration satisfies them and the budget, the
  codec is unusable on that leg and selection moves to the next candidate. When no candidate is
  left the offer is refused.
- T4. A minimum is a wish with this precedence: (a) keep the duration chosen by T1 or T2 when it
  already meets the minimum; (b) otherwise use another feasible duration of the same mapping
  that meets it; (c) otherwise send below it and record in the trace which preference lost. A
  bare media minimum never demotes the codec that policy and B2 chose: with L16 at 32 kHz and
  PCMA offered and a minimum of 20 ms, L16 at 10 ms is sent. A minimum inside the Opus format
  parameters is scoped to that Opus payload. Only a hard constraint (`maxptime`, a fixed set,
  administrator policy) can eliminate a mapping.
- T5. Frame based codecs use whole multiples of their frame.

### 4.3 Receive packet time

- R1. The packet time this side advertises comes from the policy of the leg, never from the
  peer's own `ptime`. A media section has one advertised `ptime` and one `maxptime` that span all
  its payloads, so both must hold for every listed payload.
- R2. The advertised `maxptime` is the hard receive envelope for the receivable primary audio
  mappings of the section, and the receiver enforces exactly that, by duration and by size. A
  fixed set keeps its holes below the maximum. A redundant `maxptime` stays omitted where it is
  omitted today (`esp_directional_v1` without independent packet times). A packet longer than the advertised preference and within the
  envelope is accepted and reframed (issues 161 and 165). A packet above the envelope is dropped.
- R3. Toward a fixed set peer the advertised value is chosen from the durations that peer can
  send. For `esp_directional_v1` without independent packet times the existing common duration
  rule stays.

### 4.4 Payload numbers and format parameters

- P1. Answers use the payload numbers of the offer.
- P2. Offers use the static number of a static codec and dynamic numbers from 96. 101 is used for
  telephone events at 8000 Hz when free. Exhaustion never evicts a live or retired meaning.
- P3. Media is sent with the number the receiver declared. An answer may renumber an offered
  codec: the answer's number is used to send, the offer's number to receive. No receive alias is
  installed when it would be ambiguous.
- P4. Every used number keeps a history per receiving direction and RTP stream, retired mappings
  included. A remote re-offer that remaps a used number to another codec is refused and the
  existing media stays. An invalid answer inside a 2xx is handled by the dialog (ACK, then BYE
  or recovery), never by a 488.
- P5. Telephone events: per direction, a mutually negotiated payload, event set and clock.
  Prefer the clock of the active audio codec; use 8000 Hz only when it was offered and accepted
  for the digit; fall back to SIP INFO only on a leg with a working INFO path. Method, clock,
  SSRC and timestamp base are frozen across one digit and its terminal repeats. If no method can
  carry a digit the result is reported, not silently dropped.
- P6. Format parameters written in an answer describe this side's receiver and are generated
  from the local capability. Parameters that a codec defines as symmetric keep their rule. The peer's receiver parameters are applied to the local encoder.
  The peer's sender parameters are hints. Nothing is copied from the offer.

### 4.5 Bridged calls

- B1. Each leg negotiates on its own.
- B2. The callee is offered the codecs its policy enables. Tie break, per direction: prefer the
  candidate that preserves the decoded sample rate the caller already has; then pass through of
  the same codec; then fewer coding steps. An uncompressed caller rate is preserved even when a
  candidate changes the encoding. Decoded rate, RTP clock, effective band and parameters are
  tracked separately: upsampling does not create band.
- B3. The description sent to the caller is frozen for its dialog and its offer and answer
  exchange once it was actually sent upstream. A response without a description leaves the
  caller free. A free caller is answered after the callee answered, by the rule of B2.
  An early description is sent upstream only for an intended service with a feasible direction:
  an announcement needs a branch that can send and a caller that can receive, with ports,
  destination and conversion ready (one way audio is enough); digit collection needs a route
  that asks for it, a caller that can send the negotiated events and a collector that is ready.
  An inactive stream serves neither. The first RTP packet is not awaited. A description queued
  behind a reliable response that awaits its PRACK is not yet sent and can still be withdrawn. A provisional
  description of a fork branch freezes the caller only if it was relayed upstream. The winning
  branch is bridged to the contract the caller already has.
- B4. The caller is not renegotiated only to save a conversion.
- B5. A leg is answered only when the bridge can serve both active directions of the chosen
  route. A local application that answers first is admitted on its own; a later bridge is
  admitted separately.
- B6. An INVITE without a description is answered with a local offer before routing, as today.
  Deferring that offer until a callee is known is a separate, undeclared change.

### 4.6 Shape of the call and renegotiation

- V1. An audio call offers no video to the callee. V1 wins over V2 by default.
- V2. Video is offered to the callee when the caller offers it. Any other source of video (a
  door station preview, a camera entity) is a named trigger in the policy.
- V3. Video added or removed in a dialog is propagated to the other leg. When only the optional
  video stream fails it is answered with port 0. Media line count and order, session level
  attributes, RTCP and the committed media of both legs are preserved. The audio section is byte
  stable when audio did not change.
- N1. A re-offer keeps the established mapping only while the new offer allows it for that
  direction: payload number, parameters, packet bounds and direction. New remote limits are
  applied to the sender. Local receive parameters are derived independently.
- N2. A retransmission replays the original response. The same description in a new transaction
  may get a new answer when local state changed. Identity is dialog, origin and version, and
  body. Changed content under a reused version, in a new offer of a new transaction, is treated
  as changed content.
- N3. A re-INVITE without a description is answered with an offer that lists the codec in use
  first and preserves the hold state.
- N4. A hold or resume started by this side changes the direction only; the peer may change its
  ports in the same exchange. An inactive stream, a zero address hold and a
  removed stream (port 0) are three different states.
- N5. A renegotiation is prepared beside the active contract. Three things are tracked
  separately: completion of the offer and answer exchange, the state of the SIP transaction, and
  the media already applied on each leg. The exchange can complete before the final response
  (an answer in a reliable provisional response; for an INVITE without a description, the answer
  in the PRACK or in the ACK). A failure before completion discards the prepared state; ports
  and local hold state survive. A change already applied on the far leg is recovered by the
  protocol, not undone locally.
- N6. Glare (491) is handled by the transaction layer.

### 4.7 Transport and message size

- X1. A registration identifies a contact binding. The binding, the route set and the
  transaction decide where a request goes. A SIP Outbound flow is kept while it is alive. An
  ordinary contact follows its Contact address.
- X2. The size rule applies to the complete serialized request, after authentication and Route
  headers. Before changing transport, the offer is reduced within the contract: no video unless
  requested, only enabled codecs.
- X3. A request that is still too large for UDP uses TCP as RFC 3261 says (above 1300 bytes
  when the path MTU is unknown, within 200 bytes of it when known). The retry over UDP after
  that automatic change is limited to the failures RFC 3261 names while opening the connection;
  a timeout or uncertain delivery is not one of them and nothing is replayed on the other
  transport. Ordinary SIP retransmissions are unaffected. Exception: for a selected, live,
  authenticated SIP Outbound binding whose UDP flow is the only permitted path, the reduced
  request is sent on that flow within the real datagram limit, and a delivery failure is
  reported. Ordinary Contact and route set behaviour and a trunk's use of TCP stay as they are.
- X4. Discovery results (OPTIONS, the last successful contract, firmware declarations) rank
  formats. They narrow the candidates only when the evidence is complete, current and specific
  to that binding. After a 488 to a genuinely reduced offer, one retry with the larger permitted
  offer is allowed when ownership, forking, cancellation and deadline still permit it, in a fresh
  transaction and within the administrator's policy.

## 5. Compatibility profiles

| Profile | Trigger | Deviation |
|---|---|---|
| `dahua_pcm` | User-Agent `Dahua UAC/...` or contact setting | Accepts and offers `PCM/16000` on a dynamic payload number with little endian samples. Standard `L16` stays big endian. |
| `esp_directional_v1` | Peer sends `a=x-voip-stack-flow` | One description carries two payloads with different roles, so the phone receives 48 kHz and sends 16 kHz. The answer lists both. `a=x-voip-stack-independent-ptime:1` states RFC 3264 packet time independence; without it both directions share one duration. |

Receiver tolerances kept as they are: omitted trailing video lines and inactive streams with a
port.

## 6. Declared contract changes

Each change alters behaviour that an existing test defends today. The test is updated in the
same commit that implements the rule and cites the identifier. A change excuses only the fields
and preconditions it names. Old and new expectations are both pinned.

| Id | Change | Preconditions | Rule | Tests that keep or change |
|---|---|---|---|---|
| CC-1 | A minimum that cannot be met within the budget no longer refuses the call | Media level or Opus parameter minimum; not a fixed set; `maxptime` still satisfiable | T4 | Change: `test_sip_negotiation_capabilities_runtime.py` (two cases). Keep: stereo 48 kHz hard minimum arithmetic, 11025 Hz cases, refusal when the maximum cannot be met, `test_sdp_receiver_hint.py`, `test_audio_capability_quantum.py` |
| CC-2 | The advertised receive packet time is an explicit policy value per leg | Not a fixed set peer | R1 | Change: `test_media_audio_quantum.py` (preference source). Keep: 48 kHz at 10 ms to an ESP, 16 kHz at 20 ms from it, peers that only do 10 ms |
| CC-4 | The telephone event clock follows the selected codec | The peer offered that clock | P5 | Keep: `test_symmetric_bridge_dtmf.py`, Zoiper renumbering, event set intersection, INFO fallback |
| CC-5 | Opus parameters are generated locally and remote receiver limits reach the encoder | | P6 | Keep: `test_opus_receive_preferences.py`. A change of letter case on the wire needs its own golden delta |
| CC-6 | Offers are reduced toward peers with complete, current, binding specific evidence | X4 | X2, X4 | Keep: `test_inbound_factory_flow_audio_ha.py`, `test_sip_capabilities_ha.py` |
| CC-7 | Malformed input is handled per attribute | An unknown optional hint is ignored. A malformed known limit is ignored unless a valid one for the same attribute exists, which stays. Conflicting limits: the stricter. An invalid codec mapping removes that payload only | T3 | parser tests |
| CC-8 | An answer to a local offer passes through the policy of that offer | | S6 | none known |
| CC-9 | A renumbered video answer is accepted for sending when unambiguous | No receive alias | P3 | Keep: H264/120 control, static mappings, packetization mode, profile and level |
| CC-11 | Default transmit packet time when no feasible peer request or local policy chooses another legal value: L16 32 kHz mono 10 ms, L16 48 kHz stereo 5 ms (also when the peer asks for 10 ms, which needs 1920 bytes), a trunk leg at 48 kHz 10 ms, Opus 20 ms | An explicit feasible `a=ptime` still wins (Opus with `a=ptime:10` sends 10 ms and asks for 20 ms). 16 ms and 6.25 ms stay available as capabilities. The 11025 Hz arithmetic and the stereo budget tests stay | T2 | `test_bridge_local_tx_capabilities_ha.py`, matrix |
| CC-12 | Static payload numbers 10 and 11 (L16 at 44.1 kHz, RFC 3551 section 6) are recognized, bare and with an explicit rtpmap | Later phase, with its own vectors. The 44.1 kHz capture is refused today because of this, not because of its minimum | S1 | `core/sdp_audio.py` static mapping filter |
| CC-13 | The receiver checks the real duration of an Opus packet against the hard maximum, not only its size | Receiver and relay phase. Longer than preferred and within the envelope stays accepted | R2 | receiver tests |

Withdrawn after review: CC-3 (accepting above the advertised maximum) and CC-10 (tolerating a
remote payload remap). The existing bounds and refusals stay.

When a policy leaves no authorized codec (for example the only enabled codec is unavailable in
the installed media library), the leg has no feasible contract and a repair issue names the
reason. No codec the administrator excluded is granted as a fallback.

## 7. Codec registry

One entry per codec. Every other module reads codec facts from here. The first extraction keeps
exactly the domain the current backend accepts, byte for byte.

| Codec | Static number | RTP clock | Decoded rate | Kind | Notes |
|---|---|---|---|---|---|
| PCMU, PCMA | 0, 8 | 8000 | 8000 | sample | |
| G722 | 9 | 8000 (160 ticks per 20 ms) | 16000 | sample | 16 and 32 ms work in the Home Assistant adapter |
| L16, L24 | none recognized today | rate | rate | sample | bare static 10 and 11 are a later, separate change |
| opus | none | 48000 | 8000 to 48000 | variable | 2.5 ms frames are written as 3 in SDP |
| PCM (profile `dahua_pcm`) | none | 16000 | 16000 | sample | little endian |
| telephone-event | none | follows the audio clock | | | |

Later, each as its own declared change with independent wire vectors: G726 (two bit packings),
G723, comfort noise, static 10 and 11. Availability is probed per direction and requires a
working RTP adapter, not only a codec in the media library. A codec that can only be decoded is
usable only on a receive only stream. No codec depends on a system package.

## 8. Out of scope

SRTP, ICE, a TLS listener, T.38, AAC, music on hold. A description that requires one of them is
refused with an explicit reason instead of a generic 488.

## 9. Review log

First review: `CODEX_REVIEW_MEDIA_CONTRACT_20261121.md`, against draft one.

| Point | Reviewer's decision | Resolution in this draft |
|---|---|---|
| S4 | Approve one primary codec; a listed extra does not authorize RTP | S4, S5, S6, principle 6 |
| CC-1 | Approve as soft, with explicit precedence and three kinds of minimum | T4, CC-1 preconditions and kept tests |
| CC-2 | Approve explicit receive policy; no invented combinations | Terms (capability, fixed set), R1, R3, principle 3 |
| CC-3 | Reject blanket acceptance above a hard maximum | Withdrawn. R2 states the envelope |
| B3 | Freeze per dialog and exchange; bodyless progress leaves the caller free; gate early descriptions | B3, B6 |
| CC-10 | Keep the remap refusal | Withdrawn. P4 |
| CC-9 | Directional renumbering without ambiguous alias | P3 |
| X1 | Binding, route set and transaction decide | X1 to X4 |
| CC-6 | Discovery ranks; narrowing needs complete current evidence; bounded retry | X4 |
| N1, P7 | Retention needs the full per direction mapping; remote limits reach the sender | N1, P6 |
| N2 to N5, V | Replay per transaction; three hold states; V1 before V2 | N2, N4, N5, V1 to V3 |
| B2 | Directional tie break; preserve an uncompressed caller rate | B2 |
| CC-4, CC-5, CC-7, CC-8 | Details per direction and per attribute; no fallback to an excluded codec | P5, P6, CC-7, CC-8, section 6 |
| Registry | Keep the working domain; probes per direction; later additions declared | Section 7 |
| Gate | Differential ledger; shadow owns no side effects; switch at a dialog boundary | `tests/media_corpus/ledger.json`; shadow rules in the plan |

Second review: `CODEX_REVIEW_MEDIA_CONTRACT_20261121_R2.md`, against draft two.

| Point | Reviewer's decision | Resolution |
|---|---|---|
| Ledger | Rows must be verified against the engine and the case, with negative controls | `ledger_problems` and its tests in `tests/test_media_corpus_ha.py` |
| Golden | Byte level oracle for complete descriptions and for the offer writer; writing separate from asserting | `answer_bytes` and `offer:*` entries; `python -m tests.media_corpus_support --write-golden` |
| 44.1 kHz capture | Refused because of static payload 11, not the minimum | CC-12; CC-1 is witnessed by the 32 kHz capture |
| Opus duration | Enforcing the maximum by duration is a real receiver change | CC-13 |
| Defaults | Opus default and the infeasible stereo request need preconditions | CC-11 |
| Question 1 | A bare minimum never demotes the chosen codec | T4 |
| Question 2 | Accept the exception for a live authenticated Outbound binding | X3 |
| Question 3 | Intended service plus feasible direction | B3 |
| Question 4 | Keep issue 94 as a reconstruction; it does not prove the payload 120 path | case note; `test_fritzbox_pcma_packets_are_reframed_to_assist_duration` stays |
| N2, N4, N5, P6 | Distinct commit points; symmetric parameters | N2, N4, N5, P6 |

Coverage of the corpus today: 33 inbound initial offers and 26 answers to local offers, in Home
Assistant only. No step applies a re-offer, drives the bridge, carries RTP or runs the ESP
engine. Each of those witnesses is added before its first functional change.

Answered in the second review (kept for the record):

1. T4 (b): is trying another codec before sending below a bare media minimum right, or should a
   bare media minimum never change the codec?
2. X3: the qualified exception for an oversized request on a UDP only Outbound binding.
3. B3: the exact condition for "usable early media path that is intended".
4. Issue 94 has no original description in the report. The corpus case is a reconstruction and
   claims only that the offer is accepted with standard packet times.

## Optional runtime audio implementations

The default audio policy remains the seven historical codecs. G726-16/24/32/40, G723 and
CN are optional choices; a new form shows them disabled. Saved choices keep their order and
enabled state. A policy containing only auxiliary CN is not a usable primary audio policy.

Each direction is probed with the actual adapter processing two packets. G.726 transmission
uses the public opaque-state API of an optional system SpanDSP library. No system package is
installed automatically. PyAV's G.726 encoder batches 205 to 512 ms and cannot supply normal
telephone packet times through its supported API, so its presence does not imply TX support.
Without SpanDSP, G726-16/24/32 reception can remain available through PyAV while transmission
is unavailable. G726-40 requires SpanDSP in both directions: the PyAV 17/19 five-bit decoder
failed comparison against the independent ITU Appendix II output. Full duplex UI choices
require both directions. `codec_directions` reports the independent results.

The standard G726 RTP names always mean RFC 3551 LSB-first packing, which corresponds to
FFmpeg `g726le`. FFmpeg `g726` uses the distinct AAL2 packing and is not advertised under a
standard G726 name. The codeword width and octet alignment quantum come from `CODECS`.

G723 is the SDP name for G.723.1. Its decoder accepts 6.3 and 5.3 kbit/s speech plus SID frames
using their tagged frame lengths. The encoder supplies the available 6.3 kbit/s mode and rejects
a receiver restriction to 5.3 kbit/s. Packets contain whole multiples of the 30 ms codec frame.

RFC 3389 CN is auxiliary. Its bounded SID parser accepts optional spectral coefficients and
uses the permitted zeroth-order noise model. The negotiated receiver owns one noise generator;
primary audio ends the SID period. Conference playback uses its existing mixer tick. A SIP
relay schedules a callback only during an active CN period, with no work while CN is off.
Primary audio, media reconfiguration and teardown cancel that callback; stale generations
cannot inject audio into a later period or call.

Codec qualification runs the same production adapters with PyAV 17 and 19. The independent
ITU/FATE files remain outside the repository because redistribution permission is unclear.
`tests/fixtures/audio_codec_sources.json` records their origins and SHA-256 digests;
`VOIP_CODEC_REFERENCE_DIR` enables the conformance tests against a separately supplied corpus.
