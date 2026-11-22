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
4. Run `tests/test_media_active_entrypoints_ha.py` against production. State expectations from
   the written contract, independently of the engine's current output.

The historical algorithm is retired. `golden/legacy.json` and the captured SDP files are frozen
evidence, checked by fixed hashes in `test_media_single_engine_ha.py`. The ledger and its negative
controls compare that reference with the normative cases; they do not execute a second engine.

## What the corpus covers today

Inbound initial offers and answers to local offers, in Home Assistant. Not yet: re-offers inside
a dialog, forked dialogs, the bridge, RTP, the ESP engine. Each of those gets its cases before its
first functional change. The importer pairs the first offer of a Call-ID with its first answer;
it does not yet keep dialog tags or tell a provisional answer from a final one.
