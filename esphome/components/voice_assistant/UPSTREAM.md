# Upstream alignment

Source: ESPHome development commit
`373224c141dceadbb67ace263040cbfc802c5968`,
`esphome/components/voice_assistant`. The machine-readable reference, original
upstream hashes and exact local file hashes are in `esphome/upstream_sources.json`.

## Remaining adaptations

- Emit on_idle once on a real noncontinuous transition to IDLE.
- Publish desired state before callbacks so a newly started run retains its owner.

A synchronous on_idle automation can call voice_assistant.start. Its new desired
state must not be overwritten by the transition that emitted the callback.
The Runtime Controller also observes on_idle separately from on_end: idle means
the local provider has stopped, while pipeline end can precede output completion.

The complete provider retains upstream's deferred on_end completion, including
its client guard and two-second fallback while awaiting RUN_END (#22394).
The local idle notification neither replaces nor emits that pipeline-end event.
The public schema, configured buffers, codecs and upstream wake-word audio
support are retained. There is no additional task, timer or state owner.

These corrections are proposed in
[ESPHome PR22782](https://github.com/esphome/esphome/pull/22782).
The proposal is open, not integrated. Remove the external provider and its
package selection together after a qualified official revision includes them.

The focused host regressions execute the actual transition methods, native end
helper and IDLE branch in a small host class, including restart from an idle
callback and coexistence with deferred on_end. They are not complete Voice
Assistant or Home Assistant integration tests, and do not qualify physical TTS,
acoustic behavior or resource headroom on every board.

Use `scripts/export_fork_diff.py` to export the focused source delta against the
recorded upstream commit. These files retain the ESPHome split license: C++
runtime files are GPLv3, Python and other files are MIT. See
`licenses/ESPHOME-LICENSE.txt` and `THIRD_PARTY_NOTICES.md`.
