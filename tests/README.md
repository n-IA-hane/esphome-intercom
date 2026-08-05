# Test suite

The suite separates executable behavior checks from static architecture and
packaging checks. A source string assertion can protect a public symbol or a
packaging rule, but it is not evidence that a SIP, Home Assistant or browser
workflow works.

## Local commands

Create the regular test environment from `requirements-test.txt`. Home
Assistant API tests use the matching environment in
`../ha-voip-lab/.venv`, or the interpreter selected by `HA_PYTHON`.

```bash
./scripts/test_suite.sh fast
./scripts/test_suite.sh full
./scripts/test_suite.sh coverage
./scripts/test_suite.sh ha
./scripts/test_suite.sh browser
```

Tests stop at the first failure by default. Use `--keep-going` only when an
independent failure inventory is useful. Use `--seed N` to reproduce generated
state-machine cases.

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
