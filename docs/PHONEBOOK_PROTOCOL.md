# SIP phonebook contract

The phonebook is the SIP dial plan shared by ESP devices, Home Assistant,
registered local SIP endpoints and the optional trunk. SIP is implicit everywhere:
`transport` only chooses SIP/TCP or SIP/UDP signaling, never a second
call-control protocol.

For behavior-level routing examples, see
[DIALPLAN_RESOLVER.md](DIALPLAN_RESOLVER.md). For signaling/media flows, see
[CALL_FLOWS.md](CALL_FLOWS.md). For service calls that mutate the roster, see
[SERVICES.md](SERVICES.md).

## ESP static contacts

Declare static local entries directly in `voip_stack` only when an ESP must
have contacts before HA sync, work offline, or keep a tiny fixed local roster:

```yaml
voip_stack:
  id: phone
  transport: udp  # SIP signaling transport only; audio is always RTP/UDP.
  static_contacts:
    - name: Kitchen
      ip: 192.168.1.42
      transport: udp
      port: 5060
      rtp_port: 40000
    - name: Gate
```

Runtime actions use the same model:

```yaml
on_press:
  - voip_stack.add_contact:
      name: Kitchen
      ip: 192.168.1.42
      port: 5060
      transport: udp
```

Rules:

- `name` is required.
- `ip`, `port`, `rtp_port`, and `transport` are optional on structured ESP
  static contacts and the ESP `add_contact` action.
- If `transport` is omitted for a direct address, SIP uses its default
  transport behavior for that context.
- Name-only entries are logical targets and can be resolved or bridged by HA.
- A numeric target from an ESP is routed to HA. HA resolves `extension` as an
  internal target and `number` as an external trunk target.
- HA-managed sync through `sensor.voip_phonebook` is the recommended path.
  Static contacts are local additions for offline/custom installs, not a second
  central roster.

The ESP-local schema is intentionally smaller than the HA central-contact
schema. Fields such as `address`, `sip_uri`, `extension`, `number`, group
membership, and media capabilities are authored on HA or discovered from
endpoint entities, then normalized before HA pushes the roster to ESPs.

The contact `name` is the only user-facing identity field required by an ESP
static contact. Users do not need to provide a second underscore-safe version
of the same name. SIP keeps routing and presentation separate on the wire:

- the URI user is encoded as a valid, stable SIP route;
- the quoted display name preserves spaces and is shown to the peer;
- an incoming caller display is read from the peer's `From` header, not
  replaced from the roster.

For the ESP itself, ESPHome's node name is the stable URI user and
`friendly_name` is the SIP display name. For example, node
`waveshare-p4-touch` with friendly name `Waveshare P4 Touch` sends both values
in their standard SIP roles without asking the YAML author to duplicate them.

## HA roster

HA owns the central `sensor.voip_phonebook` roster. It contains ESP peers,
HA itself, local SIP endpoints registered to HA, manual phone endpoints,
trunk-routed external targets when configured, and groups.

ESP discovery starts from `text_sensor platform: voip_stack type: endpoint`.
That endpoint state is deliberately short and does not contain group lists. If
an ESP participates in PBX groups, HA reads `text platform: voip_stack
type: ring_groups`, `text platform: voip_stack type: conference_groups` and
`switch platform: voip_stack conference_ring` from the same ESPHome device.

The maintained `packages/voip/ha_integration.yaml` package exposes the normal
entity surface. Custom YAMLs can omit it for standalone ESP-only SIP devices,
but then HA will not discover that ESP, mirror its call state or read dynamic
group membership.

Roster entries use JSON fields:

- `id`
- `name`
- `address`
- `sip_uri`
- `extension`
- `number`
- `port`
- `ha_bridge`
- `metadata`, including `transport`, `sip_transport`, `sip_port`, `rtp_port`, and audio
  format metadata

Routing is data-driven:

- `address` or `sip_uri` describes a direct SIP endpoint;
- `extension` is a local/internal alias used by HA routing and inbound DTMF;
- `number` is an external/public number used through the optional trunk;
- registered SIP accounts become callable contacts while registered;
- HA and discovered ESP entries are generated automatically.
- group entries are generated automatically from endpoint or manual-contact
  membership declarations.

Manual contacts and service calls use the same minimum contract:

```yaml
service: voip_stack.add_contact
data:
  name: MobileOffice
  extension: "210"
```

`name` is the only required field. `extension` is an optional internal alias.
`number` is an optional external/public number.
`address`, `sip_uri`, `transport`, `port`, and `rtp_port` are optional
and are filled by ESP endpoint publication, manual entries or SIP account
registration when available.

Group membership uses the same roster metadata for every endpoint type:

- ESP peers publish a short endpoint identity plus optional sibling entities:
  `text.voip_conference_groups`, `text.voip_ring_groups` and
  `switch.voip_conference_ring`. HA re-reads the device whenever those entities
  change and rebuilds the central roster.
- Manual contacts and registered SIP endpoints can declare
  `conference_group`, `conference_ring` and `ring_group` through
  `voip_stack.add_contact` or account services.
- The HA softphone publishes a virtual endpoint using the same identity
  contract. Its entity state stays short (`online` / `unavailable`) and the
  full endpoint string is stored in the sensor `endpoint` attribute.
- HA creates one name-only `ha_bridge` roster entry per group with
  `metadata.group_type` set to `conference` or `ring` and
  `metadata.members` containing the member names. Conference entries may also
  carry `metadata.ring_members`.
- A conference group is an HA-mixed SIP conference focus. Calling the group
  joins immediately. Other members can join manually by calling the same group.
  Members listed in `ring_members` are invited when the room starts; answered
  invitations join the room rather than winning a bridge.
- A ring group forks the call to all callable members except the caller. The
  first answer wins, the caller is bridged to that member, and the other early
  dialogs are cancelled.
- Group names must not collide with device or contact names. If the same name
  is declared as both ring and conference, conference wins.
- The Lovelace card consumes the same roster-derived softphone targets as ESP
  devices. It must not implement independent routing filters or group policy.
  The active browser owns HA softphone media; other cards mirror state only.

Central roster services:

- `voip_stack.add_contact`: add or replace one manual central
  contact. `name` is the only required field.
- `voip_stack.remove_contact`: remove one manual central contact
  through its required `name` input, which may match the contact name, stable
  ID, extension or number.
- `voip_stack.set_contacts`: replace manual contacts from a JSON
  roster document.
- `voip_stack.clear_contacts`: clear manual central contacts.
- `voip_stack.push_phonebook`: push the current roster immediately to
  online ESP devices.
- `voip_stack.export_phonebook`: return the current roster privately in the
  administrator-only service response for diagnostics/backup.

ESPHome also exposes native API actions such as
`esphome.<slug>_add_contact`, `esphome.<slug>_remove_contact`,
`esphome.<slug>_set_contacts`, `esphome.<slug>_flush_contacts` and
`esphome.<slug>_update_contacts`. These are local ESP actions: they change only
that device's mirror/manual entries. In HA-managed installs, prefer the central
`voip_stack.*` services above and let HA push `sensor.voip_phonebook` to the
devices.

HA pushes the canonical roster whenever the phonebook sensor changes. It also
refreshes after ESPHome registers an `esphome.<slug>_set_roster_json` service,
which covers the common reboot timing where a device's endpoint sensor appears
before its action service is callable. `voip_stack.push_phonebook` remains a
diagnostic/manual recovery tool, not the normal synchronization path.

Local SIP accounts are created with `voip_stack.create_account`.
The `username` becomes the SIP username and central roster ID. If `password` is
omitted, HA generates one and returns it once in the administrator-only action
response. Registered clients publish a dynamic Contact into the roster so ESP
devices can call them by name.

After a routed call is answered, HA and compatible ESP endpoints can publish
the selected callee through an RFC 4916 connected-identity UPDATE. This lets a
caller that dialed an extension show the canonical destination name. Peers that
do not support the in-dialog UPDATE keep the original dialed identity without
affecting the call.

## Routing

- `sip:name@host[:port]` and `name@host[:port]` route direct.
- `name` resolves through the phonebook.
- Phone/number targets require HA routing.
- `ha_bridge: true` forces HA to act as a SIP bridge.
- If HA has a registered trunk, external numbers and unresolved number-like
  targets can route through the trunk.
- Inbound trunk DTMF digits resolve against phonebook `extension` values. No
  DTMF route hint means "use the configured inbound default target". A
  received explicit route hint that cannot be resolved terminates as
  `route_not_found`; it does not silently fall back to HA or another target.
- When the experimental automation override is enabled, HA automations select
  the initial target from the `route_requested` occurrence on
  `event.voip_stack_call` by calling
  `voip_stack.select_inbound_destination`. `voip_stack.route` is the advanced
  action for explicitly pending route decisions, not the ordinary initial
  destination selector.
- Missing or incompatible media routes must fail explicitly with SIP terminal
  reasons such as `media_incompatible` or `transport_unreachable`.

## Default routing rules

ESP-origin calls:

- explicit `sip:user@host` or `user@host` route direct;
- known ESP contact with direct URI or address routes direct unless
  `ha_bridge` is set;
- known contact without direct route data routes through HA;
- unknown names route through HA;
- numeric targets route through HA.

HA-router calls:

- local HA target rings the HA softphone;
- ESP targets forward to their SIP URI/host;
- local SIP endpoints forward to their registered Contact while registered;
- phone/external numbers use the trunk only when the trunk is registered;
- disabled entries reject with a SIP terminal reason.

Inbound trunk calls:

- if no route hint arrives, the configured inbound default target is resolved;
- if a DTMF/SIP route hint resolves, HA bridges to that local target;
- if a DTMF/SIP route hint is explicit but does not resolve, HA terminates the
  answered trunk leg with `route_not_found`.
