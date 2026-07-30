# Ring groups and conference groups

VoIP Stack implements two PBX-style group primitives on the HA side. ESP
devices and SIP endpoints only declare membership and place normal SIP calls.

## Ring group

A ring group is the PBX "ring many, first answer wins" primitive.

Example: `RG Casa`.

Behavior:

- Caller dials `RG Casa`.
- HA excludes the caller from the member list.
- HA sends parallel INVITEs to all callable members.
- The first endpoint that answers wins.
- All other early dialogs receive CANCEL.
- Caller is bridged to the winning endpoint.
- Busy/declined/unreachable members do not fail the whole group while another
  member can still answer.
- If nobody answers, caller receives a terminal SIP failure.

Use ring groups for "call anyone in this area".

## Conference group

A conference group is a SIP conference focus hosted by HA.

Example: `CG Casa`.

Behavior:

- Caller dials `CG Casa`.
- Caller joins the conference immediately.
- Other members can join by calling the same group.
- Members with conference ringing enabled are invited when the room starts.
- There is no winner: every answered member becomes a participant.
- Participants hear an N-1 mix: everyone except themselves.
- The room stops when the last participant leaves.

Use conference groups for "join a shared room".

## Ring on conference

`conference_ring` is not a separate group type. It is a per-member preference
inside a conference group.

When a conference room starts:

- members with `conference_ring: true` receive an invitation;
- members with `conference_ring: false` do not ring, but can still join
  manually by calling the group.

This is useful when HA should ring as a central listener but ESP endpoints
should only join on demand, or when mic-only endpoints should auto-answer into
a monitoring conference.

## Declaring groups

ESP YAML:

```yaml
voip_stack:
  conference_groups: "CG Casa"
  ring_groups: "RG Casa"
  conference_ring: false

packages:
  voip_ha_integration: !include packages/voip/ha_integration.yaml
```

The package exposes `text.voip_ring_groups`, `text.voip_conference_groups` and
`switch.voip_conference_ring`. Editing those entities from HA updates the ESP
membership without reflashing. `voip_stack:` by itself can still run standalone,
but HA cannot discover group membership unless these entities are exposed.

HA softphone service:

```yaml
service: voip_stack.set_ha_softphone_settings
data:
  conference_group: "CG Casa"
  conference_ring: true
  ring_group: "RG Casa"
```

Registered SIP endpoint account:

```yaml
service: voip_stack.create_account
data:
  username: Zoiper
  password: "..."
  extension: "210"
  conference_group: "CG Casa"
  conference_ring: true
  ring_group: "RG Casa"
```

Manual contact:

```yaml
service: voip_stack.add_contact
data:
  name: Garage Phone
  sip_uri: sip:garage@192.168.1.80:5060;transport=udp
  conference_group: "CG Casa"
  ring_group: "RG Casa"
```

Multiple groups use comma-separated values:

```yaml
ring_group: "RG Casa, RG Garage"
conference_group: "CG Casa, CG Monitor"
```

For ESP YAML root defaults the keys are plural:

```yaml
ring_groups: "RG Casa, RG Garage"
conference_groups: "CG Casa, CG Monitor"
```

## Dynamic group lifecycle

Groups are generated from declarations:

- if at least one endpoint/contact declares `RG Casa`, the roster contains
  `RG Casa`;
- if the last declaration disappears, `RG Casa` disappears;
- adding a new ESP with a new group name creates that group automatically;
- changing HA softphone group settings or ESP group text entities rebuilds and
  pushes the phonebook.

Group entries are ordinary roster entries from the dial-plan point of view.
The card and ESP devices see the same central phonebook.

## Collision rules

- A group name must not collide with a real endpoint/contact name.
- If the same group name is declared as both ring and conference, conference
  wins and the ring declaration is ignored.
- The caller is never called back by its own ring group fan-out.
