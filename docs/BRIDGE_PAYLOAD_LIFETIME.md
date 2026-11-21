# Bridge planning and dialog payload lifetime

`core/media/bridge.py` plans a caller contract and format adaptation over two
existing leg contracts. It calls the current SDP negotiator, preserving codec
policy and actual local capabilities. It owns no SIP transaction, media socket
or task. `sip_bridge.bridge_source_audio` adapts SIP values and leaves commit
with the existing bridge and dialog owners.

A caller description is frozen only after transmission (B3). While still
free, the callee contract may rank feasible caller alternatives (B2). The
preserved PCM witness keeps 48 kHz reception and 16 kHz transmission at the
caller rather than moving conversions to lower its negotiated sample rate.
The existing compressed-codec preference and packet-time capability witnesses
remain unchanged.

Each direction records the required format work independently: unchanged
encoded mapping and packet time, repacketization, decoded PCM conversion, or
decode/encode. More than one operation may be needed; 16 kHz/20 ms to 48 kHz/10 ms
requires both resampling and repacketization. Runtime packet validation,
recording taps and codec resource admission remain with the RTP relay.

The extraction was compared on the same native SIP/RTP witnesses before and
after the move. The planner does not manufacture a new codec candidate or
perform local rollback of a remotely committed offer.

`SipDialog.audio_payloads` and `video_payloads` now own the active send and
receive number sets and the two immutable histories for each RTP stream. The
old `recv_payload_history` attribute is a read-only tuple projection. Its
legacy validation function remains an adapter for existing callers, with no
separate stored history. Delayed-offer seeds contain decoded PCM and add no
RTP meaning before a real description is negotiated.

The map identity is encoding, RTP clock and channels. Packet time, receiver
bounds, H.264 profile/packetization parameters and frame-rate recommendations
remain decisions of the existing SDP negotiator. The static video identity
facts come from the video parser's existing registry, not another table.
Each prepared SIP candidate carries the previous immutable histories. A
retired peer receive PT cannot acquire another codec after an offerer change;
the local receive namespace is checked independently. Video removal clears
the active sets and retains the histories. A renumbered answer uses the peer
PT for transmit and the original offer PT for receive, without an alias.

A conflicting optional video offer is answered with port 0 while audio stays
usable. A conflicting answer to an already sent offer is rejected as a
candidate and remains with the existing SIP transaction/compensation owner.
The map helper emits no SIP response and never undoes far-end state locally.

The preserved negative witness used PCM on peer receive PT120, retired it for
PCMU, and then accepted L16 on PT120 in the old client because only the opposite
receive history was checked. Production candidate tests now refuse that
change for either offerer, allow the original meaning to return, and preserve
the active dialog on failure. H.264/JPEG/VP8 namespace tests, video removal,
frame-rate changes and the existing atomic bridge recovery fixtures cover
the corresponding video and lifecycle contracts.

References: [RFC 3264 section 8.3.2](https://www.rfc-editor.org/rfc/rfc3264.html#section-8.3.2)
and [RFC 6141 section 3.2](https://www.rfc-editor.org/rfc/rfc6141.html#section-3.2).

The DTMF relay and browser media adapter capture one digit's payload, event
set, clock, SSRC and timestamp under their existing send locks. Attribute
updates do not split its initial report or three final reports; the next
digit reads the newer negotiated mapping. No scheduler or session owner was
added. Peer replacement, relay stop, browser close and media-generation change
still interrupt a retired source before it emits another packet.

Independent UDP witnesses first observed mixed PT101/PT110 in the relay and
PT100/PT110 in the browser during one digit. The corrected paths retain the
old mapping through the terminal repeats, then send the next digit with the
new payload and clock. The 40 ms terminal duration is 320 ticks at 8 kHz and 1920
ticks at 48 kHz, independently decoded from actual packet bytes.
