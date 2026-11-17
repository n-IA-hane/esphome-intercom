# SIP audio negotiation

VoIP Stack HA uses one SDP selection engine for incoming calls, outgoing
answers and re-INVITEs. A registered ESP, a regular SIP phone and an unknown
caller use the same local backend capabilities. Identifying a device does not
run a second audio negotiation.

## Capabilities and preferences

`core/audio_capabilities.py` describes the local codec, packetization and wire
budgets. The PCM lists in `core/audio_format.py` order preferences; they are not
an exhaustive table of accepted rate/packet-time combinations. The normal HA
preferences remain 48 kHz mono TX and 16 kHz mono RX at 10 ms.

Discovery and OPTIONS can rank an outgoing proposal. They cannot add an
unsupported local codec or invalidate a compatible authoritative INVITE or
answer. The same shared profile builder keeps local PCM preferences separate
from discovery ordering of RTP candidates.

An SDP answer may select only mappings present in its offer. Receive limits
come from the local receiver and the actual local offer, not the peer's receive
limits. `ptime` describes a receive preference; the two directions need not use
the same packet duration. `maxptime` bounds packet duration for that receiver.

The optional per-payload directional attributes use the same codec checks.
Older directional peers keep their shared-quantum compatibility rule. Ordinary
standard SIP does not require those attributes or ESP discovery.

## Implemented media envelope

PCM format values support positive finite durations up to 1000 ms, integer
sample rates from 1 through 192000 Hz, one or two channels and whole PCM sample
counts. This is a bounded local implementation envelope, not an RFC requirement.
RTP negotiation further applies the codec's actual rules and transport budgets:

- L16 and L24 use their declared RTP sample clock and network byte order.
- PCMA and PCMU use the G.711 8 kHz clock.
- G.722 uses 16 kHz decoded PCM with an 8 kHz RTP clock. Its HA encoder is not
  restricted to the 20 ms default.
- Opus uses `opus/48000/2` in SDP and the existing HA mono PCM adapter. TX uses
  the libopus durations 2.5, 5, 10, 20, 40 and 60 ms; RX supports valid Opus
  packets through 120 ms. Opus receiver `maxaveragebitrate` and
  `maxplaybackrate` reach the actual encoder. Invalid optional values are
  ignored according to RFC 7587.
- The existing nonstandard Dahua `PCM/16000` little-endian compatibility
  profile remains explicitly scoped; it does not change standard L16.

HA's conservative RTP TX payload budget remains 1200 bytes. RX remains bounded
by the existing 12 KiB audio payload limit. A preferred duration may be reduced
within the peer's bounds when needed to fit the sender budget. An incompatible
hard bound is rejected rather than silently changing to an unoffered format.

The browser validates the negotiated PCM format numerically and resamples to
or from its AudioContext. Its WebSocket ingress limit is derived from that
session's actual PCM frame plus its framing byte, retaining the 4096-byte base
for ordinary control/conference sessions. Exact frame-size validation remains
in force. PCM conversion preserves rational resampler phase across packets.

A bridge keeps an already selected source receive geometry when a new choice
would only move a PCM conversion to the other direction. A strict reduction in
conversions remains usable; packet-time repacketization stays independent.

## Verification

`test_sip_negotiation_capabilities_runtime.py` covers generic formats, decimal
packetization, actual codec adapters, receiver bounds and complete UDP dialogs.
`test_ha_generic_negotiation_ha.py` exercises the public HA call service against
an independent registered phone offering only L16/8000 at 32 ms, with known
OPTIONS capabilities or an ordinary 405 response. Both retain the same final
media contract without a manufacturer-specific path.

Existing community regressions retain their actual media bytes, payload types,
RTP timestamps, call cleanup and lifecycle checks. An old preset exclusion is
not evidence that the backend cannot handle a valid format.

References: [RFC 3264](https://www.rfc-editor.org/rfc/rfc3264.html),
[RFC 3551](https://www.rfc-editor.org/rfc/rfc3551.html),
[RFC 7587](https://www.rfc-editor.org/rfc/rfc7587.html).
