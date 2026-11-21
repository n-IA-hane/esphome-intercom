# Bridge planning and dialog payload lifetime

`core/media/bridge.py` plans a caller contract and format adaptation over two
existing leg contracts. It calls the current SDP negotiator, preserving codec
policy and actual local capabilities. It owns no SIP transaction, media socket
or task. `sip_bridge.bridge_source_audio` adapts SIP values and leaves commit
with the existing bridge and dialog owners.

A caller description is frozen only after transmission (B3). While still
free, the callee contract may rank feasible caller alternatives (B2). The
preserved PCM witness keeps48 kHz reception and16 kHz transmission at the
caller rather than moving conversions to lower its negotiated sample rate.
The existing compressed-codec preference and packet-time capability witnesses
remain unchanged.

Each direction records the required format work independently: unchanged
encoded mapping and packet time, repacketization, decoded PCM conversion, or
decode/encode. More than one operation may be needed;16 kHz/20 ms to48 kHz/10 ms
requires both resampling and repacketization. Runtime packet validation,
recording taps and codec resource admission remain with the RTP relay.

The extraction was compared on the same native SIP/RTP witnesses before and
after the move. The planner does not manufacture a new codec candidate or
perform local rollback of a remotely committed offer.
