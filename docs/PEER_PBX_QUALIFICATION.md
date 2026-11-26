# Peer PBX qualification

Date: 2026-11-26.

This laboratory exercises two independent `HomeAssistant` core objects with separate native configuration entries, account registries, SIP listeners, registrar owners, trunk clients, call owners and RTP relays. It does not install Home Assistant on a device. Discovery is replaced with an empty ESP roster; SIP admission, registration, routing, codec negotiation, media and termination use production code.

## Reproduction

Run the ordinary HA test environment:

```sh
python -m pytest tests/test_two_ha_peer_calls_ha.py
```

Six direct-link scenarios run without external software. Four independent middle-PBX scenarios require an extracted Asterisk runtime:

```sh
VOIP_TEST_ASTERISK_ROOT=/path/to/extracted/runtime \
  python -m pytest tests/test_two_ha_peer_calls_ha.py \
  --basetemp=/path/to/qualification-evidence
```

The optional runtime must contain `usr/sbin/asterisk`, `usr/lib/asterisk/modules`, its shared libraries, and the standard configuration samples under `usr/share/asterisk/conf/samples`. The fixture creates isolated configuration, log, database and spool directories. It does not install packages, use a system Asterisk service, or contact a public SIP provider. It runs serially with an isolated loopback RTP range.

Without `VOIP_TEST_ASTERISK_ROOT`, the four Asterisk cases are explicitly skipped. A successful direct-link run alone is not evidence of external-PBX qualification.

## Executed matrix

All ten scenarios passed in the recorded environment.

| Topology | Link mode | Transport | Calls and identity |
| --- | --- | --- | --- |
| HA A to HA B | Static trunks, bound local accounts, Digest on INVITE | UDP | 101 to 202 and 202 to 101, original caller preserved |
| HA A to HA B | Static trunks, bound local accounts, Digest on INVITE | TCP | Same |
| HA A to HA B | Crossed production REGISTER, bound local accounts, Digest on INVITE | UDP | Same, using the registered signaling owners |
| HA A to HA B | Crossed production REGISTER, bound local accounts, Digest on INVITE | TCP | Same |
| HA A to HA B | Crossed production REGISTER | UDP | 101 to 202 and 102 to 203 simultaneously through one peer account |
| HA A to HA B | Crossed production REGISTER | TCP | Same |
| HA A to Asterisk to HA B | Static trunks, Digest on both PBX hops | UDP | Both directions, Asterisk preserves From |
| HA A to Asterisk to HA B | Static trunks, Digest on both PBX hops | UDP | Both directions, Asterisk explicitly sets `from_user=central-pbx` |
| HA A to Asterisk to HA B | Static trunks, Digest on both PBX hops | TCP | Both directions, Asterisk preserves From |
| HA A to Asterisk to HA B | Static trunks, Digest on both PBX hops | TCP | Both directions, Asterisk explicitly sets `from_user=central-pbx` |

The phone models register by constructing Digest independently with Python's MD5 implementation. The calling phone uses `SipCallClient`; the receiving phone is an independent wire responder. Asterisk is a real independent process using `chan_pjsip`, not a Python PBX simulation.

Every topology uses native HA route triggers with numeric ranges 200 through 299 on A and 100 through 199 on B. Each action explicitly selects the existing named trunk. Neither HA has a default trunk. The intermediary uses the ordinary Asterisk `_1XX` and `_2XX` dialplan patterns.

Assertions cover:

- The called username and presented From survive both PBXs unless the intermediary explicitly rewrites From.
- Digest principal, From and Contact are distinct. No private SIP headers are required.
- A bound account proves its identity even when it has already registered.
- Both calls can coexist through one authenticated peer account without merging their session or RTP ownership. The concurrent calls carry distinct PCM signals to detect swapped media paths.
- UDP and TCP actually reach the receiving HA listener as the requested transport.
- G.711 A-law is negotiated on the inter-PBX hop. Independent PCM signals travel in both directions through the real RTP relays and resamplers.
- Max-Forwards decreases across PBX legs.
- BYE releases both HA call owners, their RTP reservations and registered-flow dialog usages. Asterisk ends with zero active channels.

Other admission tests in `tests/test_peer_trunk_ingress_ha.py` cover incorrect Digest using independent raw UDP/TCP requests, ordinary phone From restrictions, disabled or duplicate account bindings, the owned registered-flow admission gate, unknown-target 404, a live native route decision, decision-window 504, and the unchanged legacy fallback.

## REGISTER defect discovered by this test

Before the Contact-owner correction, both crossed registrations succeeded and the destination phone answered, but the originating call failed with SIP 480. A receiving HA main listener published the public GRUU obtained by that HA's separate outgoing registration. PRACK then followed that GRUU to the registered trunk socket, which did not own the main listener's early dialog and returned 481.

The correction keeps the main listener's Contact for an explicitly authenticated peer admitted there. A request actually owned by the registered trunk flow still uses that flow's public GRUU. Existing provider behavior is preserved. The end-to-end registered UDP/TCP scenarios are the regression witness.

## Recorded environment

- Python 3.14.10.
- Home Assistant 2026.11.1.
- Linux x86-64, glibc 2.41.
- Independent PBX: Debian Asterisk `16.28.0~dfsg-0+deb11u4`, extracted locally with its modules and OpenSSL compatibility library. This version is a test fixture chosen for the available runtime, not a deployment recommendation.
- Asterisk CLI control uses its ordinary foreground console. The optional Unix control socket and network-interface enumeration are unavailable in this execution environment. SIP and RTP use real loopback sockets; ICE is explicitly disabled for this local test.

Downloaded package SHA-256 values:

```text
d74821cc65e7130613e8857dbed1ac1acf6df60aed35dbb86aeb05e636521207  asterisk_16.28.0~dfsg-0+deb11u4_amd64.deb
e6940d74ec5c5f2a08e6e5831495bea95299fd69c6b36bf3c19fbec4afa677c4  asterisk-modules_16.28.0~dfsg-0+deb11u4_amd64.deb
aadf8b4b197335645b230c2839b4517aa444fd2e8f434e5438c48a18857988f7  libssl1.1_1.1.1w-0+deb11u1_amd64.deb
```

## Limits

This proves the recorded SIP/RTP paths and ownership behavior in a local process laboratory. It does not qualify office deployment, NAT/firewall traversal, WAN packet loss, appliance firmware, video, SRTP, every external PBX, or every provider's caller-ID authorization policy. The intermediary cases preserve caller identity through From and do not assert support for P-Asserted-Identity or Remote-Party-ID.
