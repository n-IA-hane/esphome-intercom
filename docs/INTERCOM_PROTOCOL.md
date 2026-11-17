# Retired proprietary intercom protocol

This document is retained only as a tombstone for old links.

The proprietary intercom wire protocol is no longer the project contract. It is
not a compatibility layer and should not be used for new code, tests, YAML
packages, cards, or tools.

The current contract is:

- SIP signaling: `INVITE`, `ACK`, `CANCEL`, `BYE`, `OPTIONS`, and standard SIP
  status codes.
- Dialog identity: Call-ID, From tag, To tag, CSeq, Via branch/rport, Contact,
  and route target.
- SDP offer/answer for media negotiation.
- RTP/UDP media using the configured audio codec and optional negotiated video.
- Public state via `SipPhoneState`.

Use the [current profile](voip_profile.md), [SIP video guide](SIP_VIDEO.md) and
[reference](reference.md) for the supported contracts. This tombstone does not
freeze every firmware to the historical PCM-only feature set.
