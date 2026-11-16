# Upstream alignment

Source: ESPHome development commit
`373224c141dceadbb67ace263040cbfc802c5968`,
`esphome/components/audio_http`. The machine-readable reference, original
upstream hashes and exact local file hashes are in `esphome/upstream_sources.json`.

## Remaining adaptations

- Join completed decoder and reader workers before publishing natural IDLE.
- Disable the old loop before callbacks may start another playback.

The callback runs from the component's main-loop decoder observer. A listener
may start the next URI from that IDLE notification; the previous worker owners
must already be retired, and the old loop must not disable the new playback.
The complete provider retains the upstream TLS 1.3 reader-stack allowance and
public schema, configured buffers and codecs. There is no additional task,
timer, state owner or runtime version guard.

The correction is proposed in
[ESPHome PR22787](https://github.com/esphome/esphome/pull/22787).
The proposal is open, not integrated. Remove this provider and its external
selection together after a qualified official revision covers both contracts.

The two independent host regressions drive play_uri, real AudioReader and
DecoderSource workers and FLAC decoding through natural end and callback-started
reuse. Only the HTTP transport and SDK boundary are doubles. The Linux linker
oracle counts actual pthread joins, and every decoded PCM byte is checked before
explicit cleanup. Speaker Source is unmodified in this host candidate. The
canonical combined component SDK fixture also compiles the exact production
translation units on ESP-IDF 5.5.0 and 5.5.6. These are software and compiler
results, not physical HTTP/TTS, acoustic or resource-headroom qualification.

Use `scripts/export_fork_diff.py` to export the focused source delta against the
recorded upstream commit. These files retain the ESPHome split license: C++
runtime files are GPLv3, Python and other files are MIT. See
`licenses/ESPHOME-LICENSE.txt` and `THIRD_PARTY_NOTICES.md`.
