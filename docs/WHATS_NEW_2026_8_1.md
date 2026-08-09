# What is new in 2026.8.1-dev

This page explains the new capabilities without duplicating the existing setup
and automation manuals.

## One phone action model

Every public phone action uses the same selector:

```text
device_id   = phone performing the action
destination = remote party
```

The selected Device may represent a Home Assistant browser phone or a
compatible ESPHome phone. Internal adapter details no longer change the public
action or produce browser-specific errors for an ESP phone.

`endpoint_id` remains an internal runtime identity for sessions, media and
snapshots. New card configurations persist only `device_id`.

## Ordinary and preferred Home Assistant phones

The phone created during first setup is now an ordinary logical phone. Its
default name still comes from the Home Assistant location name, such as
`Casa`; the name is not hardcoded.

The preferred phone is only the deterministic source used when an action omits
`device_id`. It does not remove Casa, change incoming routing or prevent more
browser phones from being created.

## Existing routing, clearer boundaries

Presence-based initial routing and HA-owned forwarding already existed in
`2026.8.0`. They remain the normal automation tools and keep their public
actions. This release clarifies their lifecycle boundaries and routes their
termination through the consolidated call owner.

The genuinely new public operation is established-call SIP transfer.

| Operation | When to use it | Public action |
| --- | --- | --- |
| Initial redirect, existing | Before a destination starts ringing | `voip_stack.select_inbound_destination` |
| HA B2BUA forward, existing | While an HA-owned phone is ringing or bridged | `voip_stack.forward` |
| SIP transfer, new | During an established REFER-capable dialog | `voip_stack.transfer` |

Blind transfer accepts `destination`. Attended transfer accepts
`replaces_call_id`, allowing the backend to build the Replaces identity from
the two real dialogs instead of requiring SIP tags in an automation.

See the [automation cookbook](AUTOMATION_DIALPLAN.md) for presence, time,
fallback, notification and DTMF examples.

## Stronger SIP interoperability

The HA signaling stack now includes:

- Digest MD5, SHA-256 and SHA-512-256 with `auth` and `auth-int`;
- reliable provisional responses through `100rel` and PRACK;
- RFC 4028 session refresh and deterministic expiry;
- initial and in-dialog delayed offer/answer;
- correct settlement of remote proxy forks and late successful branches;
- blind and attended REFER transfer with NOTIFY outcome tracking;
- RFC 3263 NAPTR/SRV discovery;
- IPv4 and IPv6 SIP addressing;
- verified TLS for supported HA outbound and trunk client legs;
- preserved `sip:` and `sips:` identities through routing and transfer.

These are protocol behaviors, not extra automation branches. Existing local
names, extensions, groups and trunk service codes continue through the central
dial plan.

## Transactional media changes

An established audio call can add or remove compatible video through
re-INVITE. The current media contract remains active until every affected
dialog accepts the replacement. A rejection or stale callback leaves the last
working audio/video path intact.

Qualified P4 profiles support JPEG or H.264 according to the selected firmware
profile. Browser phones may send video by setting `send_video: true` on Call or
Answer.

## One termination authority

Browser, ESP, trunk, forwarded, grouped and conference calls now converge on
one authoritative termination and cleanup barrier. The first terminal intent
wins; later callbacks cannot recreate an older call generation.

The barrier owns dialog signaling, media relays, tasks, browser owners and RTP
reservations. Public idle state and post-call resource cleanup therefore derive
from the same call lifetime.

## Better operational visibility

Home Assistant Repairs reports actionable firmware or configuration problems,
including missing ESPHome call-control actions. System Health exposes aggregate
listener, endpoint, trunk, call, RTP and media-owner state without leaking
phone numbers, Device IDs or complete Call-IDs.

## Qualification instead of test counts

The development qualification system records the exact source candidate and
selects software, real Home Assistant, browser, SIP peer, firmware-build and
hardware tests according to the changed risk area. Required skipped jobs,
foreign artifacts and stale evidence fail closed.

This does not make every environment interchangeable. Real FRITZBox, Wildix,
Dahua, browser and ESPHome evidence remains identified separately from model or
unit tests.
