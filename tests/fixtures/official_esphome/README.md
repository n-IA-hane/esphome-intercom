# Immutable official ESPHome test sources

These snapshots are test inputs, not production external components. Maintained
device packages fetch the official Git commits below; host/schema tests use
the same bytes offline. `sources.json` records the original path, full commit
and SHA256 of every copied file. `scripts/official_component_fixtures.py` checks
those identities against `esphome/upstream_sources.json` before a test uses them.

| Fixture | Official revision | Purpose |
| --- | --- | --- |
| `components/audio_http` | `ce3f2f4c2cb98bed6e23176832afdce40be6661b` | Merged PR22787, completed decoder retirement before IDLE callbacks |
| `components/micro_wake_word` | `26c2b61c405ad004e08c6472de08ed264e97ea3e` | Merged PR22781, model reload starts with fresh input and score state |
| `mww_before_reload_fix` | `373224c141dceadbb67ace263040cbfc802c5968` | Historical negative control; intentionally fails eight reload cases |

The negative control does not depend on the installed ESPHome version. Positive
MWW cases assert observable inference input and invocation timing in feature
steps. They allow bookkeeping to reset at unload or successful construction;
neither is a physical inference-latency measurement. TFLite and device services
are bounded test substitutes around the actual `streaming_model.cpp` owner.

To update a pin, obtain the component files from that exact official commit,
update their hashes and package selection together, and run the provider
composition and lifecycle contracts. Unit tests perform no Git/network fetch.
ESPHome's split license applies: C/C++ runtime files retain GPLv3; Python and
other non-runtime files retain MIT. See `licenses/ESPHOME-LICENSE.txt`.
