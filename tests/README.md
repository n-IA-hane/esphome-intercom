# Test suite

The suite separates executable behavior checks from static architecture and
packaging checks. A source string assertion can protect a public symbol or a
packaging rule, but it is not evidence that a SIP, Home Assistant or browser
workflow works.

## Local commands

Create the regular test environment from `requirements-test.txt` and
`requirements-firmware-test.txt` for the ESPHome package/schema checks. Home
Assistant API tests use the matching environment in
`../ha-voip-lab/.venv`, or the interpreter selected by `HA_PYTHON`.

```bash
./scripts/test_suite.sh fast
./scripts/test_suite.sh software-full
./scripts/test_suite.sh peer
./scripts/test_suite.sh coverage
./scripts/test_suite.sh ha
./scripts/test_suite.sh js-runtime
./scripts/test_suite.sh browser
```

Tests stop at the first failure by default. Use `--keep-going` only when an
independent failure inventory is useful. Use `--seed N` to reproduce generated
state-machine cases.

`software-full` runs serially by default. CI splits it into the shards planned
by `scripts/software_shards.py`: each test file belongs to exactly one shard,
balanced with the measured durations in `scripts/software_shard_durations.json`,
and the `software` gate proves that the shards cover the unsharded selection
exactly once. A file without a measured duration is still assigned. Refresh the
table from the CI JUnit reports with `scripts/software_shards.py
update-durations`. `--shard I/N` runs one shard. `--parallel-shards N` runs all
of them at once on Linux, each in its own network namespace and temporary
directory because the suite binds fixed SIP and RTP ports. Reports go to
`--shard-results` (default `test-results`) and scratch data to `--shard-work`.

`software-full` includes the Node runtime tests and excludes the real-browser
cases. `browser` runs all Chromium regressions, including microphone/camera
selection and the first audio samples after a slow worklet download. Install
`requirements-browser-test.txt`, Chromium and FFmpeg for that suite. Missing
browser dependencies fail its preflight instead of silently skipping tests.
`js-runtime` selects the Node tests without launching Chromium.
The real JPEG normalization and codec-to-codec RTP regressions run in the HA
environment, where their runtime dependency and FFmpeg are installed. Their
controlled media-owner adapter is restored after each test, so collection order
does not supply missing HA imports or replace another test's runtime lookup.

CI keeps separate JUnit artifacts for software, Home Assistant and browsers.
`scripts/check_test_results.py` rejects empty reports, failures, errors and
skipped cases. A test deliberately assigned to a different suite is deselected,
not counted as a pass. An optional local dependency can still cause a direct
pytest invocation to skip a test; that result is not a completed CI gate.

`peer` runs sanitized Zoiper, Wildix, FRITZ!Box, registrar, DTMF and Assist
regressions in separate Python processes. This prevents Home Assistant test
stubs from making protocol results depend on collection order.

The generated qualification matrix is only a scenario catalog. Its tests prove
that intended axes are listed, not that calls were executed. Release evidence
must come from executable peer and live-device runners.

Mutation testing is intentionally refused in the primary checkout. Create a
disposable linked worktree at a committed milestone, install
`requirements-mutation-test.txt`, then run `./scripts/test_suite.sh mutation`
there. This keeps generated mutants and deliberate failures away from local
device YAML edits. The score gate is 63.5 percent and covers selected lifecycle,
endpoint, SIP transaction and video relay modules. Raise the floor only after a
complete run from a clean committed worktree.

If a selected production module imports Home Assistant, keep the regular test
environment as `PYTHON` and provide the HA laboratory interpreter separately:

```bash
HA_PYTHON=/path/to/ha-venv/bin/python ./scripts/test_suite.sh mutation
```

The runner adds only the HA packages to the mutation process and disables
unrelated pytest plugin auto-loading. This preserves Hypothesis and mutmut from
the regular environment without loading HA's complete Bluetooth/DBus test
stack.

## Layers

- `unit`: protocol parsing, codecs, value objects and lifecycle primitives.
- `integration`: multiple production modules running together without a live
  external peer.
- `ha`: the real custom integration loaded by Home Assistant and invoked
  through supported service APIs.
- `browser`: frontend modules executed by Node or a browser runtime.
- `architecture`: source layout, YAML, documentation and packaging policy.
- `fault`: focused cancellation, stale-callback and partial-failure checks.
- `mutation`: deliberate faults and mutation qualification.
- `live`: opt-in HA, SIP peer or physical-device qualification.

Coverage is measured from executable behavior tests and excludes architecture
contracts. The statement and branch floor is a regression guard, not a quality
score. Critical lifecycle code still needs fault injection, generated state
sequences and complete peer-observed call tests.

## Adding a regression test

Reproduce the failure first, then assert the externally observable result. For
call lifecycle changes, cover setup, both termination legs and cleanup of
session owners, sockets, RTP ports and media tasks. For async tests, prefer
events and bounded waits over fixed sleeps.

Static source checks are appropriate only for rules that are static by nature,
such as forbidden dependencies, public file layout or compile-time gating.
Do not use them as substitutes for calling production code.
