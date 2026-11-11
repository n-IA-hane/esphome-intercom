# Ring groups and call queues

Ring groups and queues are VoIP Stack service devices in Home Assistant. A ring
group calls its members together and connects the first answer. A queue keeps
callers in arrival order and offers each call to available agents. Conference
groups retain their separate shared-room behavior described in [Groups](GROUPS.md).

## Create and edit a group

Use **Add group** on the VoIP Stack integration to create a ring group or queue.
Choose a name, an optional numeric extension and the member phones. The group
has its own configuration subentry and service device. Its internal identity
stays the same when its name or extension changes.

A group also appears automatically when a phone first declares its name in
**Ring Group** or **Queues** (**Code** in Italian). Multiple phones declaring the
same name join the same group. These fields accept new names as well as existing
suggestions. Separate multiple names with commas. Queue names are limited to 32
UTF-8 bytes and their declaration list to 240 bytes. Existing HA ring-group
declarations retain their 255-byte text limit. ESP firmware retains its native
group limits of 32 bytes per name and 240 bytes per list.

The central group editor's member checkboxes and the fields on individual phones
edit the same phone declarations. They do not create a second member list. A
group rename updates those declarations while keeping the group's identity and
existing agents' queue state.

The **Keep empty** setting controls lifetime:

- A manually created group keeps an empty service device by default.
- An automatically created group disappears when its last configured membership
  is removed. A temporary phone disconnection does not remove membership.
- Enabling **Keep empty** preserves an automatic group even with no members.

## ESPHome declarations and offline changes

For maintained native firmware:

```yaml
voip_stack:
  ring_groups: "Reception"
  queues: "Support, 1002"
```

With the native HA integration enabled, the firmware exposes **VoIP Ring
Groups** and **VoIP Queues** configuration texts. To omit the queue text while
keeping the other HA controls, set `ha_integration: { queues: false }`. With
`ha_integration: false`, an optional standalone text can be configured using
the existing text platform:

```yaml
text:
  - platform: voip_stack
    type: queues
    name: VoIP Queues
```

HA persists central-editor and card changes before attempting delivery to an
ESP. An offline change remains pending and is delivered through the existing
ESPHome text entity when it becomes available. Neither a membership change nor
its delivery requests a new SIP registration or integration reload.

A reconnect snapshot cannot replace a declaration already owned by HA. Unknown
and unavailable text states preserve it. A matching text report confirms the
pending value; a subsequent live text change on the same connected ESPHome
owner can update it. Because the native text has no revision token, a local
firmware/YAML change made while HA is offline does not override a declaration
HA already knows. Use the HA editor after reconnect to change that declaration.
The first declaration from a previously unknown phone is imported normally.

## Queue agent state

Each phone's participation is independent for each queue:

| State | Meaning |
| --- | --- |
| `available` | Logged in and eligible when the phone is reachable and free. |
| `paused` | Logged in, temporarily excluded from new queue offers. |
| `logged_out` | Excluded until explicitly logged in again. |

A newly created membership starts as `available`. Existing logout and pause
states persist across reconnects and HA restarts. The phone card displays these
states and changes them without modifying the membership declaration. A busy,
offline or wrapping-up phone is not eligible merely because its state is
`available`.

Queue defaults are 10 waiting callers, 60 seconds maximum wait, 20 seconds per
ring attempt, 1 second without any available online agents before fallback,
1 second between retries and no wrap-up delay. Fallback destination and waiting
audio are empty by default. Configure them on the queue device or with
`voip_stack.configure_queue`.

## Actions

| Action | Fields |
| --- | --- |
| `voip_stack.set_phone_queues` | `phone`, `queues` as CSV text or a list of names. |
| `voip_stack.set_phone_memberships` | `phone`, `field` (`queues` or `ring_group`), `value` as CSV text or a list. |
| `voip_stack.set_queue_agent_state` | `queue`, `phone`, `state` (`available`, `paused`, `logged_out`), optional pause `reason`. |
| `voip_stack.get_queue_state` | `queue`, optional `phone`; returns queue and member state. |
| `voip_stack.configure_queue` | `queue` and the policy fields to change. |
| `voip_stack.get_queue_activity` | `queue`, optional `since`, `until`, `limit`, `cursor`; returns Recorder events. |
| `voip_stack.get_queue_metrics` | `queue`, optional `since`, `until`; returns admission-cohort metrics. |

Phone selectors accept an existing phone identity or device. Queue selectors
accept its stable group ID, name or configured extension. The actions enforce
the caller's permissions; queue policy and full queue-state inspection require
administrative access.

For example, an automation can pause one phone without changing its other
queues:

```yaml
action: voip_stack.set_queue_agent_state
data:
  queue: Support
  phone: Reception
  state: paused
```

## SIP keys and BLF

An authenticated SIP phone can dial or subscribe to these user parts at the HA
SIP domain, replacing `<groupid>` with the queue's stable group ID:

- `queue-login+<groupid>` toggles its own state between logged out and available.
- `queue-pause+<groupid>` switches a paused phone to available; otherwise it sets
  the phone to paused. Using it while logged out therefore logs in as paused.

These controls resolve the authenticated calling/subscribing phone. They do
not accept another agent's identity and require that phone to belong to the
queue. The existing dialog-info BLF projection reports the login key active
while logged in, including paused, and the pause key active while paused.
Physical key colors and lamp behavior depend on the SIP phone's BLF settings.

The local automated tests cover HA configuration, synchronization, service and
dialog-info contracts. They do not establish physical lamp behavior or acoustic
quality on a particular phone.

## Distribution and exits

`strategy` defaults to `ring_all`. The alternatives reuse the same global phone
reservation owner:

- `round_robin`: remember the last operator actually offered a call, including
  across restarts. An unavailable phone or a failed preparation does not advance
  the cursor.
- `longest_idle`: offer to the eligible phone whose last conversation ended
  longest ago, including ordinary calls outside a queue.
- `ordered`: offer in configured order and escalate after an unsuccessful
  attempt. `agent_order` is an ordered list of stable phone identities. In the
  editor, select the preferred phones in that order; remove and select again to
  change it. Other members follow in their existing order. It does not change
  membership.

Sequential strategies reserve only the current candidate. Other queues may
still use another eligible phone. Callers remain FIFO within their queue; there
is no priority or aging policy between queues in this release.

`auto_pause_after` is disabled at `0`. Set it to `3` to pause an operator in
this queue after three consecutive offers that actually rang and timed out.
Busy, DND, offline, cancelled offers and losing legs do not count as missed
answers. A successful answer or setting the operator available clears the
counter. The pause reason is `no_answer`; other queues retain their state.

The common `fallback` is inherited by empty specific exit settings:
`fallback_full`, `fallback_timeout`, `fallback_no_agents`, `fallback_disabled`
and `fallback_removed`. Their reasons remain distinct in activity. Without a
fallback, a full queue rejects admission with SIP 486; a caller already answered
is terminated through its existing call owner. A transfer between two distinct
queues preserves the caller and replaces the admission atomically. The same
queue cannot immediately re-enter itself, and genuine routing loops remain
bounded.

`get_queue_state` includes unavailable reasons, reservation queue and attempt,
remaining wrap-up, last answer/end, consecutive misses and the oldest waiting
call. The phone card shows the primary exclusion reason beside the queue name, with
additional detail in its label tooltip.
Wrap-up starts when the connected conversation ends, not when it is answered.

## Activity and metrics

Each queue device has an activity event entity. The raw versioned
`voip_stack.queue_event` occurrences are recorded by Home Assistant Recorder
and described in Logbook. Admission, offer, answer, exit and operator changes
have separate identities; multiple attempts are not multiple incoming calls.
No audio, credentials or SIP header dumps are retained in these events.

The two history actions require administrative permission. They default to the
last 24 hours, accept timezone-qualified ISO timestamps and limit each window to
31 days. Activity is paginated (`limit` 100 by default, maximum 1000). Reuse the
returned `next_cursor` with the same queue and window. Retention and event
exclusions are controlled by Recorder. Without Recorder, calls still work and
history reports that it is unavailable.

Metrics select admissions in `[since, until)` and evaluate their state as of
`until`. They distinguish pending, answered, abandoned, timeout, transfer and
rejected calls. Wait statistics include completed waits only; pending calls are
not counted as successful zero-wait answers. Operator workloads and talk time
come from answered/completed conversations. An admission before the requested
window is not counted as a new call just because its exit occurs inside it.
These are operational counters, not a contractual service-level guarantee.

The distribution semantics follow the established ideas documented in
[Asterisk queue configuration](https://github.com/asterisk/asterisk/blob/master/configs/samples/queues.conf.sample)
and [FreeSWITCH call queues](https://developer.signalwire.com/freeswitch/applications/call-queues/).
Their settings are not copied into an extra PBX engine: HA endpoints, sessions,
Recorder and the existing fork/reservation owners remain authoritative.
