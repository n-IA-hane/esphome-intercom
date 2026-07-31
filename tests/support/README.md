# VoIP test support

`voip_matrix.py` is the fast local SIP/PBX model used to validate broad call
semantics before deploying to Home Assistant or ESP devices.

Run the full matrix:

```bash
python tests/support/voip_matrix.py --scenario all --validate
```

Run one diagnostic scenario with JSON output:

```bash
python tests/support/voip_matrix.py --scenario audio --validate --json
```

The matrix models the project-specific infrastructure: HA as a virtual endpoint
with an endpoint sensor, central phonebook rebuild/push behavior, direct calls,
ring groups, conference groups, contact scrolling, DND, auto-answer, abrupt
disconnects, service-created contacts/accounts, trunk calls, and directional
audio negotiation. The pytest suite imports the same runner, so CLI and CI
exercise the same contracts.
