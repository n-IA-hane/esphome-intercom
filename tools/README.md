# Development tools

These tools are development and qualification helpers. They are not installed
with the Home Assistant integration.

## Standard local gate

`voip_dev_check.py` runs the fast source, schema and JavaScript checks used
before the full pytest suite.

The deterministic simulator is under `simulator/`:

- `contract_simulator.py` owns the simulated call state;
- `scenario_runner.py` executes checked-in scenarios;
- `simctl.py` is the small command-line client.

Use `scripts/run_virtual_device_tests.sh` instead of starting those processes
individually.

## Live Home Assistant qualification

Each live runner owns a distinct topology:

- `ha_softphone_matrix.py` covers one browser phone against SIP peers;
- `local_softphone_live_matrix.py` covers browser-to-browser logical phones;
- `ring_group_live_matrix.py` covers first-answer, cancellation and cleanup;
- `inbound_routing_qualification.py` covers registered and trunk ingress;
- `live_voip_qualification.py` covers the general ESP and Home Assistant call
  matrix;
- `ha_softphone_card_trace.py` records frontend state transitions.

`live_voip_qualification.py --esp-host ADDRESS` overrides a stale DHCP address
without changing the checked-in device matrix.

When the physical ESP member is unavailable,
`ring_group_live_matrix.py --skip-esp-winner` still qualifies every browser
winner, decline, cancellation and cleanup path. This is not a substitute for
the separate ESP winner qualification.

The isolated Home Assistant environment and browser-token refresh workflow are
documented in [`ha_voip_lab/README.md`](ha_voip_lab/README.md).

## Video qualification

`sip_video_peer.py` generates controlled SIP video offers and media.
`sip_video_browser_probe.py` validates card ownership, negotiated media and
rendered browser output. Use separate peer runs for codecs whose encoders
cannot be switched reliably inside one process.

## Device diagnostics

`jtag_snapshots.py` captures bounded intrusive task snapshots.
`analyze_jtag_snapshots.py` summarizes the resulting GDB logs. Neither tool is
an audio-quality test because halting the target disturbs real-time media.

### Micro wake word analysis

Install the optional host dependencies from `tools/requirements-mww-analyze.txt`.
`mww_analyze.py` inspects PCM levels, clipping and capture gaps, and replays
16 kHz mono audio through a stateful frontend and model. It follows ESPHome's
integer feature quantization and decision window. Host results require a
separate comparison with the identified target firmware.

```bash
python tools/mww_analyze.py analyze captures/ --output levels.json
python tools/mww_analyze.py replay phrase.wav --output replay.json
python tools/mww_analyze.py compare before.wav after.wav
```

The official-corpus replay tests are opt-in through `MWW_TEST_CORPUS`. That
directory must contain an identified `manifest.json` and the corresponding WAV
files.

## Deployment helper

`deploy_ha_voip_stack.sh` is only for an explicitly authorized development
deployment. It creates a remote backup, installs the exact local component,
restarts Home Assistant and waits for a terminal service state.
