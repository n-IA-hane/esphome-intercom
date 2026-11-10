# Upstream alignment

Source: ESPHome 2026.10.0, commit `704551a6c93c29a311802f7ee97d67a9e9bce718`,
`esphome/components/voice_assistant`. The machine-readable reference and exact local
file hashes are in `esphome/upstream_sources.json`.

## Remaining adaptations

- Emit on_idle once on a real noncontinuous transition to IDLE.
- Publish desired state before callbacks so a newly started run retains its owner.

The public ESPHome interfaces, configured buffers and codecs are retained.
There is no additional task, timer, state owner or runtime version guard.
The focused host regressions exercise the changed methods. They do not qualify
physical TTS success, acoustic behavior or resource headroom on every board.

Use `scripts/export_fork_diff.py` to export this delta against the recorded
upstream commit. These files retain the ESPHome split license: C++ runtime
files are GPLv3, Python and other files are MIT. See
`licenses/ESPHOME-LICENSE.txt` and `THIRD_PARTY_NOTICES.md`.
