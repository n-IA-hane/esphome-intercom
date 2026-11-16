# Upstream alignment

Source: ESPHome development commit
`373224c141dceadbb67ace263040cbfc802c5968`,
`esphome/components/speaker_source`. The machine-readable reference, original
upstream hashes and exact local file hashes are in `esphome/upstream_sources.json`.

## Remaining adaptations

- Retire a terminal source error through the existing STOP and IDLE cleanup.
- Preserve queued media and replacement ownership without retrying failed items.

ERROR belongs only to the active or pending source. STOP joins that source's
workers, and the existing IDLE handler releases its speaker and pointers without
advancing the failed item twice. A new PLAY_URI fences the previous source before
replacing its playlist, so its delayed ERROR cannot remove the replacement.
An owned asynchronous ERROR is treated as terminal even under repeat; remaining
playlist order and the other pipeline are retained. Immediate play_uri refusal
keeps the upstream policy.

The complete provider retains upstream's schema, public APIs, pending-frame
accounting and volume/mute behavior. There is no additional task, timer or state
owner. This broader delta was contributed as a
[follow-up to PR22118](https://github.com/esphome/esphome/pull/22118#issuecomment-6914576261),
with a [public tested snapshot](https://github.com/n-IA-hane/esphome/blob/0772fe5368500b8144422f42bb68cbf449756f7e/tests/components/speaker_source/speaker_source_stop.cpp).
The existing PR addresses a narrower startup-error stall. Treating an ERROR as
terminal under repeat is an explicit proposal to its author, not accepted
upstream behaviour. PR22118 alone does not establish equivalence with all the
local terminal-error and replacement contracts.

Host cases enter through MediaPlayerCall, MediaSourceListener reports and the
real component loop. Natural end, startup/runtime errors, delayed IDLE,
replacement, repeat modes and independent announcement/music state are covered.
The independent player host run passes 14 cases with MediaSource and speaker
doubles at their boundaries and stock HTTP code. The real HTTP-worker/PCM
regressions are separate. A combined canonical SDK fixture compiles both exact
production sources on ESP-IDF 5.5.0 and 5.5.6. These tests do not qualify physical
TTS, acoustic behaviour or resource headroom on every board.

Use `scripts/export_fork_diff.py` to export the focused source delta against the
recorded upstream commit. These files retain the ESPHome split license: C++
runtime files are GPLv3, Python and other files are MIT. See
`licenses/ESPHOME-LICENSE.txt` and `THIRD_PARTY_NOTICES.md`.
