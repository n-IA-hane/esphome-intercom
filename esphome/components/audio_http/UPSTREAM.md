# ESPHome upstream alignment

This component is identical to ESPHome `dev` commit
`cd28a8a03e1fd00cde1e94e65ad089f3823ef274`, path
`esphome/components/audio_http`. The `persistent_ring_buffer` option entered
upstream in commit `5ed20805ce` through pull request `#18708`.

The local copy is temporary compatibility for ESPHome 2026.8.1. It can be
removed when the required stable ESPHome release includes that upstream
commit. The option keeps micro-decoder 0.4.0's encoded ring allocated between
playback cycles and preserves the upstream default of `false`.
