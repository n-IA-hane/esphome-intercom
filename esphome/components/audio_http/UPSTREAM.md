# ESPHome upstream alignment

This component is based on ESPHome `dev` commit
`5f6a910e2d6e41d3716668a66e5dff8cca25f2ea`, path
`esphome/components/audio_http`.

The local fork adds one configuration option, `persistent_ring_buffer`, and
passes it to micro-decoder 0.4.0. When enabled, the encoded-audio ring is
allocated during setup and retained across playback cycles. The default is
`false`, matching upstream behavior for profiles that do not need the reserved
capacity.

The local IDF MIME adapter adds RFC 2361 `audio/vnd.wave` recognition to
micro-decoder 0.4.0 when WAV support is compiled. It substitutes a generated
copy of the existing detector translation unit, leaving downloaded sources
untouched. It adds no decoder, queue or task, and is inactive when upstream
already recognizes this MIME type. MP3/WAV decoder selection remains explicit
in the profile's `audio.codecs` configuration.
