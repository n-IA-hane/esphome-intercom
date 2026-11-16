# Upstream alignment

Source: ESPHome development commit
`373224c141dceadbb67ace263040cbfc802c5968`,
`esphome/components/micro_wake_word`. The machine-readable reference, original
upstream hashes and exact local file hashes are in `esphome/upstream_sources.json`.

## Remaining adaptation

After a newly created interpreter passes tensor validation, reset its input
stride phase and pending probability. A restarted model must receive a complete
stride of new feature slices before its first inference, rather than consume
unwritten rows because the previous interpreter stopped partway through a stride.
Normal detection probability resets retain the current interpreter and stream
phase. Model data, feature generation, cutoffs and sliding windows remain upstream.
There is no additional task, buffer, state field or runtime version hook.

The focused correction is proposed in
[ESPHome PR22781](https://github.com/esphome/esphome/pull/22781).
It is open, not integrated. Remove this external provider only after a qualified
official revision includes both resets; remove the package selection together.

The complete Python schema and native AudioCapture callback match the pinned
upstream provider. Its task_core, ring_buffer_duration and prefer_internal_ram
options are retained with their official defaults. The former core_compat.h
adapter for the 2026.10 core has been retired; this provider requires the current
project core baseline. VoIP-only compositions do not add it.

The focused host regression compiles the actual streaming model implementation
with external dependencies doubled and checks complete input after reload,
ordinary reset continuity, failure cleanup and disabled models. The native
capture callback regression exercises borrowed input, stopped and expired
consumers, producer-side overflow notification and zero callback allocations.
These are software state contracts, not acoustic recognition or resource-headroom
qualification for every board. This change alone is not a demonstrated barge-in
correction.

Use `scripts/export_fork_diff.py` to export the focused source delta against the
recorded upstream commit. These files retain the ESPHome split license: C++
runtime files are GPLv3, Python and other files are MIT. See
`licenses/ESPHOME-LICENSE.txt` and `THIRD_PARTY_NOTICES.md`.
