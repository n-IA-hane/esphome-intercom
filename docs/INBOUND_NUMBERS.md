# Incoming numbers, DID and number blocks

A trunk can deliver many public numbers to the same registered SIP account.
VoIP Stack keeps the original called number separately from the phone currently
ringing. Home Assistant automations can use that number to choose a phone,
ring group, queue, Assist endpoint or Automation contact.

The same mechanism applies to an individual DID or a GNR number block. A block
does not require one trunk, SIP account or automation per public number.

## Choose the field actually supplied by the trunk

Edit the named SIP trunk and select its **Called-number source**. Inspect an
incoming SIP request or obtain the provider's documented format first. The
registration username is not necessarily the public number.

| Source | Value used |
| --- | --- |
| `request_uri` | User part of the INVITE Request-URI. This remains the default for existing configurations. |
| `to` | User part of the single `To` address. SIP/SIPS and `tel:` addresses are accepted. |
| `p_called_party_id` | User part of the single `P-Called-Party-ID` address. |
| `custom_header` | The explicitly configured header, containing one SIP/SIPS/tel address or a plain number. |
| `fixed` | The fixed string entered for this trunk. Useful when a single-number trunk carries only its account identity. |

For example, this request contains two different identities:

```text
INVITE sip:registered-account@pbx.example SIP/2.0
To: <sip:0009900042@provider.example>
```

Choose `to` to expose `0009900042`. Choosing `request_uri` exposes
`registered-account`. VoIP Stack does not guess which one the provider intends.
An unrecognized or ambiguous trunk source cannot gain trust by supplying one
of these headers.

**Fallback sources** are optional and ordered. They are tried only if the
selected source is missing, malformed or ambiguous. For example, select
`p_called_party_id`, then explicitly add `to` and `request_uri` as fallbacks.
Leaving the list empty avoids silently substituting the account username.
Duplicate called-party headers are ambiguous rather than an instruction to
take an arbitrary value.

**Prefix rules** are an ordered list of literal replacements. The first match
wins; there are no implicit country or provider rules:

```yaml
- prefix: "+00"
  replacement: "00"
```

This is only an illustration of the rule format. Configure the transformation
required by your actual trunk. Every number and replacement is text, so leading
zeroes survive. Keep quoted number strings in automations as well.

`History-Info` describes retargeting history. This implementation does not infer
one public DID from its potentially multiple entries. The distinction between
the current Request-URI and logical `To` recipient comes from
[RFC 3261](https://www.rfc-editor.org/rfc/rfc3261.html#section-20.39);
the pre-retarget use of `P-Called-Party-ID` is described in
[RFC 7315](https://www.rfc-editor.org/rfc/rfc7315.html#section-4.2), and request
history in [RFC 7044](https://www.rfc-editor.org/rfc/rfc7044.html).

## Route a block with one ordinary HA automation

Create a native **VoIP route requested** trigger. Select the incoming trunk to
limit the rule to that line. Its filter uses the trunk's stable identity, so
renaming the line does not change which trunk the automation selects. The
example below uses the stable identity as `options.trunk`; choose the real
line in the editor or replace the example identity in YAML.

Use **Select inbound destination** to choose the initial destination. The
native execution supplies its own call context, including during concurrent
calls, so the action does not need a copied Call-ID.

```yaml
alias: Public numbers to internal destinations
mode: parallel
triggers:
  - trigger: voip_stack.route_requested
    options:
      trunk: example-incoming-line-id
variables:
  did_routes:
    "0009900000": Reception
    "0009900001": Sales
    "0009900002": Support
    "0009900042": Assist
actions:
  - choose:
      - conditions: "{{ trigger.call.caller_number == '009911' }}"
        sequence:
          - action: voip_stack.select_inbound_destination
            data:
              destination: Security
    default:
      - action: voip_stack.select_inbound_destination
        data:
          destination: "{{ did_routes.get(trigger.call.called_number, 'Reception') }}"
```

The names in this example must exist in your phonebook. Add the rest of the
block as dictionary entries, keeping the public numbers as quoted strings.
Several numbers can select the same destination. Use ordinary HA conditions
for opening hours, caller exceptions or presence. A queue or ring group is
selected by its phonebook name or extension in the same destination field.

The route window is bounded, currently 1.5 seconds. Keep this initial decision
short. A delay, TTS request or external network request can miss the window;
the configured normal route remains the fallback. An attached native trigger
can open its matching route window without enabling the broad experimental
automation-routing switch on every call.

## Fields available in `trigger.call`

| Field | Meaning |
| --- | --- |
| `called_number` | Original incoming called number after the configured prefix rule. |
| `called_number_raw` | Extracted original user/number before prefix replacement. This is not a complete raw SIP header. |
| `called_number_source` | `request_uri`, `to`, `p_called_party_id`, `fixed`, or `header:Header-Name`; empty if extraction failed. |
| `caller_number` | Original SIP From user. |
| `caller_name` | Original From display name, or empty when absent. |
| `inbound_trunk_id` | Stable identity of the recognized incoming trunk. |
| `outbound_trunk_id` | Selected outgoing line when the call also has an outbound trunk leg. |

Forwarding and media renegotiation preserve the original incoming number and
caller fields. `callee`, routing history and connected-party fields can follow
the current destination. Legacy event fields remain available. A missing
incoming number stays empty rather than acquiring the forwarded phone's number.

## Verification and practical limits

Check an actual call on the reported line. Confirm the incoming Request-URI,
the selected source and `trigger.call.called_number` in the automation trace,
then verify which phone received the outgoing INVITE and that hangup closes
both legs. A successful dictionary lookup alone does not prove the destination
rang or media flowed.

The automated full-route test sends 100 distinct fictional DID strings through
the HA listener, native trigger, HA script and existing selection action to
two independently registered SIP peers. It checks actual destination INVITEs,
caller and number exceptions, an unmatched fallback and two calls with exact
bidirectional RTP samples and BYE cleanup. This is local software and wire
evidence, not a qualification of a provider's number format or a physical phone.

See [SIP trunks](SIP_TRUNK.md), [automation routing](AUTOMATION_DIALPLAN.md) and
[groups and queues](GROUPS_AND_QUEUES.md) for the corresponding setup.
