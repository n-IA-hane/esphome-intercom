# Home Assistant Automation Cookbook And Dial Plan

> [!WARNING]
> Initial experimental preview in `2026.8.0`. The normal phonebook route
> remains the default and does not require automations. Event fields, decision
> timing and service semantics may change in a future release while this API is
> validated against more real PBX deployments. Keep critical access-control or
> emergency routing on an explicitly tested stable path.

VoIP Stack has one canonical dial plan: the shared phonebook. Home Assistant
automations may override a call only at the explicit decision points described
below. Disabling automation routing restores the ordinary phonebook path
without changing contacts, extensions or the configured inbound destination.

The Lovelace card never chooses or filters a route. It mirrors the authoritative
backend state and sends user actions to the same router.

The recipes use native Home Assistant Event Entities, state Sensors,
conditions and `voip_stack.*` actions. Replace every example entity, Device ID,
person and destination with selections from your own installation. Prefer the
automation editor's entity and Device selectors over guessed IDs.

## Configure Incoming Trunk Routing

Reconfigure VoIP Stack and choose one **Incoming routing** mode:

| Mode | What the caller experiences | Normal route |
| --- | --- | --- |
| **Route immediately** | No DTMF collection or pre-answer delay. | The call follows the fallback destination unless an automation selects another destination. |
| **Collect extension with DTMF** | HA answers the trunk leg and collects negotiated telephone-event or SIP INFO digits for the configured timeout. | A valid extension wins. With no digits, automation gets a decision point before the fallback. |

**Fallback destination** accepts the same values as the rest of the dial
plan: a phonebook name, HA, an extension, a group, a registered SIP phone, an
Assist extension, a SIP URI or a routable number.

The optional **Allow experimental automation routing overrides** switch is
independent from the incoming mode and is off by default. When enabled:

- Direct mode exposes a 1.5 second `route_requested` decision point before the
  configured default route.
- DTMF mode exposes the same decision point only when the caller enters no
  digits.
- Explicit DTMF digits never pass through automation routing. They always use
  the phonebook, and an unknown explicit extension ends as `route_not_found`
  instead of silently ringing another destination.
- If no matching automation acts during the decision window, the configured
  phonebook route continues unchanged.

Existing configurations migrate transparently. A previous setup with DTMF
enabled and a non-zero timeout becomes DTMF mode. Other setups become Direct
mode. Automation overrides remain disabled until explicitly enabled, while the
existing credentials, target and timeout are preserved.

## Override The Initial Destination

This complete automation routes a trunk call to `Waveshare S3 Audio` before
the default destination. It uses Home Assistant's native Event Entity trigger,
so it needs no Jinja, Call-ID or helper timer:

```yaml
alias: VoIP - Route incoming call to WS3
mode: parallel
max: 10
triggers:
  - trigger: event.received
    target:
      entity_id: event.voip_stack_call
    options:
      event_type:
        - route_requested
actions:
  - action: voip_stack.select_inbound_destination
    data:
      destination: Waveshare S3 Audio
```

Use ordinary Home Assistant conditions between the trigger and action for
presence, time, alarm mode or any other entity. For example, a state condition
can route to an indoor ESP only while someone is home. If the condition is
false, no action runs and the default target takes over when the short decision
window expires.

### Route A Known Caller According To Presence

This example uses only native Home Assistant conditions. Calls from Wildix
extension `426` ring the kitchen ESP while Daniele is home; every other state,
including `not_home` or another HA zone, routes the call to extension `667`
(`Test`). Other callers do not match the automation and continue to the trunk's
configured fallback destination.

```yaml
alias: VoIP - Route 426 according to Daniele presence
description: Route one known trunk caller to WS3 at home, otherwise Test.
mode: parallel
max: 10
triggers:
  - trigger: event.received
    target:
      entity_id: event.voip_stack_call
    options:
      event_type:
        - route_requested
conditions:
  - condition: state
    entity_id: event.voip_stack_call
    attribute: ingress
    state: trunk
  - condition: state
    entity_id: event.voip_stack_call
    attribute: caller
    state: "426"
actions:
  - if:
      - condition: state
        entity_id: person.daniele
        state: home
    then:
      - action: voip_stack.select_inbound_destination
        data:
          destination: Waveshare S3 Audio
    else:
      - action: voip_stack.select_inbound_destination
        data:
          destination: "667"
```

The `caller` value is the resolved name or number shown by
`event.voip_stack_call`; replace `426` with the exact value exposed by your
caller. The destinations may likewise be phonebook names, extensions, groups,
registered SIP phones or Assist.

### Route Only Provider/PBX Trunk Calls To A Ring Group

`route_requested` may also describe an HA-owned extension call. Filter on the
stable `ingress` attribute when an automation must affect only calls entering
from the configured provider/PBX trunk:

```yaml
alias: VoIP - Inbound trunk to RG Casa
mode: parallel
max: 10
triggers:
  - trigger: event.received
    target:
      entity_id: event.voip_stack_call
    options:
      event_type:
        - route_requested
conditions:
  - condition: state
    entity_id: event.voip_stack_call
    attribute: ingress
    state: trunk
actions:
  - action: voip_stack.select_inbound_destination
    data:
      destination: RG Casa
```

Use `state: extension` for calls originating from a local ESP, browser phone or
registered SIP endpoint. Do not filter on `scope`: scope identifies the
internal state owner, while `ingress`/`origin` describe where the call entered
the PBX. A ring group uses normal PBX semantics: eligible members ring, the
first answer wins, losing legs are cancelled, and the caller is excluded when
it is itself a member of the destination group.

This action is only for the initial `route_requested` decision. When exactly
one route is waiting, no Call-ID template is required. With concurrent pending
routes, pass the `call_id` from the event explicitly. The configured fallback
remains authoritative when no automation acts. Use `voip_stack.forward` only
after a call has already been delivered to a ringing or connected endpoint.

## Forward An Unanswered HA Call To Assist

Initial routing and no-answer forwarding are separate operations. Once the HA
softphone is ringing, its durable state sensor supports Home Assistant's native
`for:` timing:

```yaml
alias: VoIP - HA unanswered to Assist
mode: parallel
max: 10
triggers:
  - trigger: state
    entity_id: sensor.voip_stack_call_state
    to: ringing
    for: "00:00:30"
conditions:
  - condition: state
    entity_id: sensor.voip_stack_call_state
    attribute: ingress
    state: trunk
actions:
  - action: voip_stack.forward
    data:
      destination: "1666"
      on_failure: resume
```

This lifecycle is covered by the release tests, including unanswered calls,
forwarding to Assist and final remote hangup.

The `ringing` state already means that this phone is the incoming call target;
an additional `direction: incoming` condition would be redundant. Keep the
`ingress: trunk` condition when only provider/PBX calls should fall through to
Assist. Remove the entire `conditions:` block when unanswered local-extension
calls should follow the same rule.

Replace `1666` with any destination understood by the phonebook. When exactly
one HA-owned call is forwardable, the backend resolves its Call-ID and current
revision itself. The source call remains open while VoIP Stack releases the HA
softphone, cancels any replaced ringing leg with SIP CANCEL, and attaches the
new destination.

If multiple calls are simultaneously forwardable, an advanced automation must
identify the intended `call_id`. Ambiguous requests fail explicitly instead of
guessing.

With multiple logical phones, select the call-state entity attached to the
phone that owns the ringing leg. The migrated default phone deliberately keeps
the compatibility entity ID `sensor.voip_stack_call_state`; additional phones
receive normal generated IDs such as `sensor.test_call_state` (localized HA
installations may use a translated form). Always select the entity from the
phone Device in the automation editor instead of guessing its ID.

Logical ringing is independent from browser connectivity. A browser softphone
that belongs to a ring group is allowed to enter `ringing` while its
connectivity entity says `Disconnected`; no physical card rings, but state
timers and missed-call automations still run. Opening the matching card during
that window makes the call answerable. DND and administratively disabled phones
are not ring candidates.

For example, the Casa phone can fall through to the Cucina tablet without
matching caller names or inspecting the global event stream:

```yaml
alias: VoIP - Casa unanswered to Cucina
mode: parallel
max: 10
triggers:
  - trigger: state
    entity_id: sensor.voip_stack_call_state  # Call state entity on Device Casa
    to: ringing
    for: "00:00:30"
conditions:
  - condition: state
    entity_id: sensor.voip_stack_call_state
    attribute: ingress
    state: trunk
actions:
  - action: voip_stack.forward
    data:
      destination: Cucina
      on_failure: resume
```

## Common Automation Recipes

Keep the initial destination decision and later call handling separate. These
are the most common patterns:

| Goal | Trigger/condition | Action |
| --- | --- | --- |
| Ring the whole house for an external call | Aggregate `route_requested` with `ingress: trunk` | `select_inbound_destination` to a ring group |
| Route differently when nobody is home | Same initial event plus a normal person/presence state condition | Select an ESP, HA phone, Assist or another group; otherwise let the configured fallback run |
| Use an office-hours destination | Same initial event plus a time condition | Select Reception during opening hours; allow the fallback or select Assist outside them |
| Send an unanswered room phone elsewhere | That phone's call-state sensor remains `ringing` for a duration | `forward` to another room, group or Assist |
| Notify on a missed call | That phone's Event Entity receives `missed` | Send a normal HA notification; no routing action is required |
| React to keypad input during a connected call | The phone or aggregate Event Entity receives `dtmf` | Run a gate, light or other HA action |

An explicit DTMF extension entered during initial trunk collection remains
authoritative and bypasses the automation override. This prevents a broad
automation from replacing a destination deliberately dialled by the caller.
Likewise, a false condition should normally perform no action: after the short
decision window, VoIP Stack follows the configured fallback transparently.

## Actionable Doorbell Notification

Use the call Event Entity attached to the **receiving Home Assistant phone**.
The `ringing` occurrence contains caller information, while the selected phone
Device lets `voip_stack.decline` resolve the current call without copying a SIP
Call-ID into the automation.

The **Answer** button must open the dashboard containing that phone's card.
Only a browser or Companion view can request microphone/camera permission and
attach media. The card consumes `?voip_answer=1` and answers the ringing call.

```yaml
alias: VoIP - Actionable doorbell notification
mode: restart
triggers:
  - trigger: event.received
    target:
      entity_id: event.casa_call
    options:
      event_type:
        - ringing
conditions:
  - condition: state
    entity_id: event.casa_call
    attribute: caller
    state: Front Door
actions:
  - action: notify.mobile_app_your_phone
    data:
      title: "🔔 Front door"
      message: "Front Door is calling Casa"
      data:
        tag: voip_front_door
        channel: doorbell
        importance: high
        ttl: 0
        priority: high
        actions:
          - action: URI
            title: Answer
            uri: /lovelace/phones?voip_answer=1
          - action: VOIP_DECLINE_CASA
            title: Decline
  - wait_for_trigger:
      - trigger: event
        event_type: mobile_app_notification_action
        event_data:
          action: VOIP_DECLINE_CASA
    timeout: "00:00:30"
  - if:
      - condition: template
        value_template: "{{ wait.trigger is not none }}"
    then:
      - action: voip_stack.decline
        data:
          device_id: <casa_phone_device_id>
  - action: notify.mobile_app_your_phone
    data:
      message: clear_notification
      data:
        tag: voip_front_door
```

Replace `/lovelace/phones` with the real view containing the card bound to the
same Casa phone Device. Receiving the notification does not itself answer or
claim media. If the call ends before the action is pressed, the backend rejects
the stale decline instead of affecting a later call.

For several phones or simultaneous door stations, use one distinct notification
tag/action name per receiving phone. Expert flows may also copy `call_id` from
the event and pass it through action data, but it is unnecessary when exactly
one call is ringing on the selected Device.

The same automation is available as a standalone file:
[`examples/doorbell-automation.yaml`](../examples/doorbell-automation.yaml).

![Answer a VoIP call from a Companion notification](images/mobile-notification-answer.gif)

## Native Automation Entities

### Event Entity

Every integration-owned phone Device exposes its own call Event Entity, for
example `event.casa_call` or `event.test_call` (the visible/entity names are
localized). It publishes only occurrences involving that phone. Use it for a
doorbell notification, a missed-call log, or behavior specific to one room.

`event.voip_stack_call` remains the aggregate PBX-wide surface and publishes
stateless occurrences for every HA-owned call:

- `route_requested`, `incoming_call`, `outgoing_call`, `calling`
- `ringing`, `remote_ringing`, `forwarding`
- `answered`, `connected`
- `calling_timeout_requested`, `ringing_timeout_requested`
- `dtmf`
- `ended`, `missed`, `failed`, `state_changed`

Select these through the `event.received` trigger in the automation editor.
Each occurrence includes call metadata such as caller, callee, direction,
route kind, owner and controllability. The aggregate entity is useful for
initial `route_requested` decisions and advanced inspection; prefer the phone's
own Event Entity for room-specific logic.

### Durable State Sensor

Each logical browser/SIP-account phone exposes an enum call-state Sensor Entity.
The default phone keeps `sensor.voip_stack_call_state` for backward
compatibility. Each sensor follows only its phone through ringing, bridging and
Assist. Its stable states are:

- `offline`
- `idle`
- `ringing`
- `calling`
- `remote_ringing`
- `connecting`
- `in_call`
- `held`
- `terminating`

Attributes include stable endpoint identity plus active-call `call_id`,
`direction`, `ingress`, `peer_name` and `terminal_reason`. `ingress` is
`trunk` for provider/PBX calls and `extension` for locally originated SIP
calls. Ordinary single-call automations do not need to read these fields.

The phone Device itself is the Home Assistant registry container; entities are
its triggerable state/event surfaces. The per-phone Event Entity, durable
sensor, WebSocket stream and card are all derived from the same backend call
session.

## DTMF During A Connected Call

Initial trunk extension selection and established-call DTMF are deliberately
separate:

- Digits used before routing select a phonebook extension and do not become
  in-call automation events.
- During an HA-bridged established call, each negotiated key emits one `dtmf`
  occurrence while audio continues.
- DTMF processing remains HA-side and adds no work to ESP firmware.

This supports actions such as opening a gate when a participant presses a key,
without turning the keypad into a second routing state machine.

## Advanced Concurrency Controls

Every HA-owned logical call has one owner and a monotonic `revision`. Control
changes such as route selection, destination replacement and ownership handoff
advance the revision even if the visible state string stays the same. Delayed
callbacks cannot restore an older state.

For expert scripts that manage several concurrent calls, `call_id`,
`expected_state` and `expected_sequence` remain accepted. Explicit deadlines
also remain available for multi-stage policies, but they are unnecessary for a
normal no-answer forward.

## Boundaries

- Direct ESP-to-ESP calls remain peer-to-peer and observable only. HA cannot
  redirect media it does not own.
- The current operation is an HA B2BUA redirect, not a SIP phone transfer.
- Supported signaling includes INVITE, ACK, BYE, CANCEL, REGISTER, OPTIONS,
  SIP INFO DTMF, RTP telephone-event and peer-initiated UPDATE on HA-owned
  dialogs.
- REFER/NOTIFY transfer, locally originated renegotiation, offerless re-INVITE
  delayed offer/answer, PRACK/100rel and session timers are not implemented.
- Raw internal bus events are implementation plumbing for the Event Entities,
  not a second public automation API. Build new automations from the native
  entities and services above.
