# ESPHome upstream alignment

This component is based on ESPHome 2026.8.0 `audio_http`.

The local fork adds one configuration option, `persistent_ring_buffer`, and
passes it to micro-decoder 0.4.0. When enabled, the encoded-audio ring is
allocated during setup and retained across playback cycles. The default is
`false`, matching upstream behavior for profiles that do not need the reserved
capacity.
