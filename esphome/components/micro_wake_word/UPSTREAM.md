# Upstream alignment

Source: ESPHome 2026.10.0, commit `704551a6c93c29a311802f7ee97d67a9e9bce718`,
`esphome/components/micro_wake_word`. The machine-readable reference, original
upstream hashes and exact local file hashes are in `esphome/upstream_sources.json`.

## Remaining adaptation

After a newly created interpreter passes tensor validation, reset its input
stride phase and pending probability. A restarted model must receive a complete
stride of new feature slices before its first inference, rather than consume unwritten rows
because the previous interpreter stopped partway through a stride.

Normal detection probability resets retain the current interpreter and stream
phase. The schema, feature generation, model, cutoff and window remain unchanged.
There is no additional task, buffer, state field or runtime version hook.

This generic provider keeps the project's ESPHome 2026.10.0 schema. It is
selected through `ext_components_source` by the package that instantiates MWW;
VoIP-only compositions do not add it. `core_compat.h` also accepts the current
2026.11 development core's borrowed `AudioCapture` callback, one-argument audio
fill and unnamed momentary error API. Overload resolution selects the owner API
at compile time. The adapters preserve the original byte pointer and allocate
no memory. No frontend, interpreter or probability math is adapted.

The current S3 and P4 configurations use the common schema and defaults: a
120ms capture ring, an unpinned inference task and default memory placement.
This provider does not import the newer core's `task_core`,
`ring_buffer_duration` or `prefer_internal_ram` options. They remain unsupported
by this schema; extending it requires separate validation. The private observer
and BSS selection investigation are not part of this provider.

The focused host regression compiles the actual streaming model implementation
with external dependencies doubled and checks complete input after reload,
ordinary reset continuity, failure cleanup and disabled models. A second host
contract exercises both API shapes and verifies zero adapter allocations. Those tests
qualify the state contract, not acoustic recognition or resource headroom on
every board. This change alone is not a demonstrated barge-in correction.

Use `scripts/export_fork_diff.py` to export the small source delta against the
recorded upstream commit. These files retain the ESPHome split license: C++
runtime files are GPLv3, Python and other files are MIT. See
`licenses/ESPHOME-LICENSE.txt` and `THIRD_PARTY_NOTICES.md`.
