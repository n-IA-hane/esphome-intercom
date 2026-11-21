# Media negotiation corpus

Real and reconstructed SIP session descriptions, each with the contract that
[`docs/MEDIA_NEGOTIATION_CONTRACT.md`](../../docs/MEDIA_NEGOTIATION_CONTRACT.md) requires for
it. The Home Assistant integration and the ESP firmware run the same cases. A reported failure
becomes a case here before any code changes.

## Layout

```
cases/<id>/case.json     description, steps, expected contract
cases/<id>/NN_<name>.sdp one description per step, byte exact (CRLF line endings)
golden/legacy.json       what the engine at the recorded commit answers for every case
ledger.json              every field where that engine differs from the contract, with the declared change that excuses it
```

Descriptions are stored byte exact because line endings, letter case and attribute order are part
of what a peer sends. Do not reformat them.

## case.json

| Field | Meaning |
|---|---|
| `id` | Directory name. |
| `title` | One line. |
| `source.kind` | `capture` (taken from the wire), `reconstructed` (rebuilt from a log or a report), `synthetic`. |
| `source.origin` | Device, software version and date, or the issue number. |
| `source.redaction` | What was replaced. Addresses use the RFC 5737 documentation ranges. |
| `peer.kind` | `phone`, `softphone`, `door_station`, `trunk`, `esp`, `browser`. |
| `peer.user_agent` | As sent by the peer, when known. |
| `peer.profile` | Compatibility profile the peer triggers, or empty. |
| `steps` | Ordered operations: `answer_offer` (the peer offers), `apply_answer` (the peer answers the description in `local_offer`). Each names its `sdp` file and the local `policy` preset. |
| `expect` | Partial contract per step: `outcome` (`accept` or `reject`), and per direction `codec`, `ptime`, `payload`; `dtmf`. Only stated fields are checked. |
| `rules` | Contract rules the case defends. |

## Adding a case from a report

1. Save the description exactly as received.
2. Replace addresses with documentation addresses and remove names. Record it in `redaction`.
3. Write the expected contract from the rules, not from what the code answers today.
4. Run `tests/test_media_corpus_ha.py`. If the legacy engine disagrees, name the contract change.
