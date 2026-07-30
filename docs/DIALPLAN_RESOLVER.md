# Dial plan and resolver

This document is the practical routing contract for VoIP Stack. The central
idea is simple: Home Assistant owns the dial plan through
`sensor.voip_phonebook`, and every caller uses names, extensions, SIP URIs,
groups or public numbers against that same roster.

The Lovelace card does not route calls. ESP devices do not own a separate
central dial plan. The card calls the HA softphone service with the selected
target; ESP devices call targets from their synced phonebook; registered SIP
endpoints send normal SIP INVITEs to HA. Home Assistant resolves the target.

## Roster sources

The central roster is rebuilt from these sources:

- Online ESPHome `voip_stack` endpoint sensors, plus optional sibling group
  entities on the same ESPHome device.
- The Home Assistant softphone virtual endpoint.
- Manual contacts created with `voip_stack.add_contact` or
  `voip_stack.set_contacts`.
- Local SIP endpoint accounts that are currently registered to HA.
- Dynamic ring group and conference group entries derived from endpoint
  declarations.

ESP endpoint state is intentionally short: it carries identity, SIP/RTP ports,
audio formats, transport and extension. ESP group membership is read from
dedicated sibling entities, typically `text.*_voip_ring_groups`,
`text.*_voip_conference_groups` and `switch.*_voip_conference_ring`.

If a source disappears, its dynamic roster entry disappears too. A registered
SIP endpoint is callable only while it has an active registration. A dynamic
group exists only while at least one endpoint/manual contact declares it.

## Target classes

Targets are classified before routing:

- `sip:user@host[:port]`: direct SIP URI.
- `user@host[:port]`: direct SIP URI after adding the `sip:` prefix.
- Numeric target such as `101`, `666`, `418`, `+390...`: extension or number.
- Any other text: roster name or ID.

Matching is case-insensitive for names and IDs. Numeric matching first checks
roster `extension`, then falls back to `number` or trunk rules where allowed.

## Route actions

The resolver returns one of these route actions:

- `direct`: the caller can call the target URI directly.
- `forward`: HA originates a SIP call to the resolved target and manages state.
- `bridge`: HA anchors signaling/media between source and destination.
- `answer_ha`: the target is the HA softphone.
- `group`: the target is a ring group or conference group.
- `trunk`: the target is routed through the configured SIP trunk.
- `reject`: no valid route exists or the target is disabled.

## ESP origin

When an ESP calls:

- A raw SIP URI or `user@host` is direct.
- Another ESP/contact with a direct address or SIP URI is direct unless
  `ha_bridge` is set.
- A name-only contact is bridged by HA.
- A numeric target is sent to HA. HA resolves it as an extension first, then as
  an external number/trunk target.
- A group name is sent to HA because groups are HA-owned.

Example: `WS3` calls `Garage`.

- If `Garage` is an online ESP with an address, WS3 calls it directly.
- If `Garage` is a registered SIP endpoint, HA resolves the current Contact and
  bridges/forwards to it.
- If `Garage` is a name-only manual contact, HA receives the call and applies
  the central route.
- If `Garage` does not exist, HA rejects with route not found.

## HA softphone origin

When Home Assistant or the card calls:

- A roster name resolves to the current roster entry.
- An extension resolves to the entry that owns that extension.
- A registered SIP endpoint resolves to its current registration Contact.
- A group name runs the group controller.
- A public number uses the trunk when the trunk is enabled and registered.
- A missing non-numeric name is rejected.

`voip_stack.forward` uses the same central dial plan. If the destination is a
registered SIP endpoint, HA keeps the endpoint's registration Contact as the
bridge destination. It must not call itself via `ha_uri_for()`, otherwise the
call re-enters the inbound router and becomes a `route_requested` loop.

## Registered SIP endpoint origin

Local SIP endpoint accounts are standard SIP users registered to HA. They are
not special card endpoints and they are not limited to softphones: Zoiper,
Linphone, baresip, ATAs and desk phones can all be accounts.

When a registered endpoint calls HA:

- If the target is in the central roster, HA routes it immediately.
- If the target is the HA softphone name or extension, HA rings.
- If the target is another registered endpoint, HA forwards/bridges to that
  endpoint's current Contact.
- If the target is a ring group or conference group, HA runs the group logic.
- If the target is a public number and the trunk is ready, HA sends it to the
  trunk.
- It must not enter the automation `route_requested` path for ordinary roster
  targets. Registered SIP endpoints use the same central dial plan as ESP and
  HA-originated calls.

## Trunk fallback

Trunk routing is only for targets that are not internal roster extensions and
look like public numbers, or for contacts that explicitly carry `number`.

Rules:

- Internal `extension` wins over trunk.
- Contact `number` can route through the trunk.
- Raw public numbers can route through the trunk when the trunk is registered.
- Missing trunk registration means `trunk_unavailable`.
- Unresolved text names are not sent to the trunk.

## Groups

Ring groups and conference groups are roster entries. They are declared by
endpoints or manual contacts; HA materializes them dynamically.

Ring group:

- Caller is excluded from the fan-out.
- All callable members ring in parallel.
- First answer wins.
- Losing early dialogs receive CANCEL.
- Caller hangup before answer cancels every fork.

Conference group:

- Caller joins the room immediately.
- Other endpoints can join later by calling the same group name.
- Members with conference ringing enabled are invited when the room starts.
- HA can be a member through its softphone settings.
- The room stops when the last participant leaves.

## Canonical examples

`Garage` is an ESP:

- `Garage` by name from HA: `forward` to `sip:Garage@<esp-ip>`.
- `Garage` by name from ESP: usually `direct`.
- `Garage` by extension from any caller: HA resolves extension to `Garage`.

`MobileOffice` is a registered SIP endpoint with extension `210`:

- `MobileOffice` by name: HA forwards to its active Contact.
- `210`: HA resolves to `MobileOffice`.
- If it unregisters: it disappears from the dynamic roster and is no longer
  callable.

`Daniele` is a manual contact with `number: 418`:

- `Daniele`: trunk route to `418` when the trunk is ready.
- `418`: trunk route unless another roster entry owns extension `418`.

`CG Casa` is a conference group:

- Calling it joins the conference.
- Members with conference ring enabled are invited.
- Members with conference ring disabled can still join manually.

`RG Casa` is a ring group:

- Calling it rings members.
- First answer wins and all other forks are cancelled.
