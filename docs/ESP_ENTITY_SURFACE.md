# ESP VoIP entity surface

`voip_stack:` is the SIP/RTP engine. It can run headless: an ESP can call
static contacts or direct SIP peers without exposing any Home Assistant entity.

Home Assistant features need entities and native API actions. Custom physical
phones should normally include the complete package:

```yaml
packages:
  voip_ha_phone: !include packages/voip/ha_phone.yaml
```

This combines the `ha_integration.yaml` entity surface,
`ha_api.yaml` connectivity wrapper and `phonebook_subscribe.yaml`.
`ha_api.yaml` imports the single shared `ha_actions.yaml` definition for
`start_call`, `answer_call`, `decline_call` and `hangup_call`. Selecting the ESP
as `device_id` in a public VoIP Stack action invokes the matching native action
on that physical phone.

Maintained YAMLs already include the appropriate pieces. Full
runtime-controller profiles use `ha_integration.yaml` with
`ha_api_runtime.yaml` instead. When building a custom YAML from the bare
`voip_stack` component, include `ha_phone.yaml` or declare the equivalent
entities and API actions manually. Otherwise the ESP may still be a working SIP
phone, but Home Assistant cannot fully discover and control it.

## Entities

| Entity | Required for | Purpose |
| --- | --- | --- |
| `text_sensor: type: endpoint` | HA phonebook/dialplan | Publishes ESP SIP identity, ports, transport, extension and audio formats. |
| `text_sensor: type: state` | ESP mirror card | Current ESP call state, for example `idle`, `ringing`, `calling`, `in_call`. |
| `text_sensor: type: caller` | ESP mirror card | Current incoming caller. |
| `text_sensor: type: destination` | ESP mirror card | Selected/outgoing destination. |
| `text_sensor: type: last_reason` | ESP mirror card | Why the last call ended. |
| `text_sensor: type: contacts` | Local UI/card context | Compact contact count/status. |
| `text: type: ring_groups` | PBX groups | Comma-separated ring group memberships, editable from HA. |
| `text: type: conference_groups` | PBX groups | Comma-separated conference group memberships, editable from HA. |
| `switch: conference_ring` | PBX conference ringing | Whether this endpoint rings when another member starts one of its conference groups. |
| `text_sensor: type: transport` | Debug | Active SIP signaling transport. |
| `text_sensor: type: sip_snapshot` | Debug | Compact SIP/media diagnostic snapshot. |

If the card does not mirror an ESP's state, check that the device exposes at
least `state`, `caller`, `destination` and `last_reason`.

![ESP mirror card and keypad](images/esp-mirror-card-keypad-options.png)

If the ESP does not appear in the HA phonebook, check that it exposes
`endpoint` and that the endpoint state is not `unknown` or `unavailable`.

## Entity-only package and manual YAML

`ha_integration.yaml` is intentionally the low-level entity-only package. It
does not expose call control. `ha_actions.yaml` is the shared low-level action
surface used by both connectivity wrappers. Equivalent manual entity YAML:

```yaml
text_sensor:
  - platform: voip_stack
    type: endpoint
    name: VoIP Endpoint
  - platform: voip_stack
    type: state
    name: VoIP State
  - platform: voip_stack
    type: caller
    name: VoIP Caller
  - platform: voip_stack
    type: destination
    name: VoIP Destination
  - platform: voip_stack
    type: last_reason
    name: VoIP Last Reason
  - platform: voip_stack
    type: contacts
    name: VoIP Contacts

text:
  - platform: voip_stack
    type: ring_groups
    name: VoIP Ring Groups
  - platform: voip_stack
    type: conference_groups
    name: VoIP Conference Groups

switch:
  - platform: voip_stack
    conference_ring:
      name: VoIP Ring On Conference
```

## Debug package

Optional diagnostics:

```yaml
packages:
  voip_debug: !include packages/voip/debug.yaml
```

This adds `transport` and `sip_snapshot`.

LVGL profiles may project the same public call state and terminal reason:

![LVGL call terminal reason](images/lvgl-hangup-reason.jpg)
