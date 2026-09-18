# ESP phones and Home Assistant

Starting with 2026.10.0, the VoIP component supplies its Home Assistant
interface directly. No VoIP HA package is required. Compile with **ESPHome
2026.9.0 or newer**.

Keep your board's network, microphone and/or speaker configuration. Add the
native API option below alongside any existing API encryption settings:

```yaml
api:
  custom_services: true

voip_stack:
  id: phone
  microphone_source:
    microphone: mic_main
    channels: [0]
  speaker: hw_speaker
```

`mic_main` and `hw_speaker` refer to your existing audio components. Mic-only
and speaker-only phones are supported: omit the direction you do not have.
This works with native ESPHome audio or Audio Stack. Voice Assistant, a display
and the runtime controller are not prerequisites.

## What appears automatically

When the native API is present, `voip_stack` creates the existing **VoIP
Endpoint**, **VoIP State**, **VoIP Caller**, **VoIP Destination**, **VoIP Last
Reason**, **VoIP Media Route** and **VoIP Contacts** entities. It also exposes
extension, ring/conference groups and the conference-ring switch.

The endpoint entity advertises the phone's SIP address and media capabilities.
The HA integration discovers it through the ESPHome device and pushes the
central phonebook using the native `set_roster_json` action. Call, answer,
decline, hangup and manual contact actions use the same native API connection.
Entity names and action names remain compatible with the earlier packages.

Each responsibility has one owner:

- The ESP VoIP component owns calls and its local phonebook.
- Home Assistant owns the central phonebook and delivers its updates.
- The native API adapter exposes the component's existing methods and states.
- Optional packages compose the display, ringtone and Full-profile activities.

No second SIP connection, audio pipeline or polling task is added for this
interface. ESPHome supplies retained entity states when an API client connects;
IP changes continue to update the endpoint through the component's network events.

## Customize an entity

The generated entities accept their usual ESPHome options under
`voip_stack.ha_integration`. For example:

```yaml
voip_stack:
  ha_integration:
    media_route:
      on_value:
        - logger.log:
            format: "Call media route: %s"
            args: ['x.c_str()']
```

Use this location instead of declaring another `text_sensor` for a managed
role or extending an entity inside a retired package. Leave the default names
unless your HA discovery setup has been checked with custom names. Optional
buttons, volume controls and diagnostic sensors remain explicit declarations.

## Standalone SIP

Without `api:`, the HA adapter is omitted. With API enabled only for other
purposes, explicitly disable the phone's HA interface:

```yaml
voip_stack:
  ha_integration: false
```

Static contacts and direct SIP calling remain available. Disabling this
interface does not disable SIP or RTP. Conversely, enabling it does not force
calls through HA: `use_ha_as_first_contact` remains a separate routing choice.

## Migrating an existing phone

Remove includes of `voip/ha_phone.yaml`, `voip/ha_integration.yaml`,
`voip/ha_actions.yaml`, `voip/ha_api.yaml` and
`voip/phonebook_subscribe.yaml`. These paths now fail validation with migration
instructions. Remove copied definitions of the native phone actions and managed
entities as well; duplicates are rejected before compilation.

Full profiles replace `voip/ha_api_runtime.yaml` with
`runtime/ha_connectivity.yaml` for their runtime connectivity events and
runtime diagnostic actions. This replacement is not needed by a basic phone.
Keep unrelated hardware, display, ringtone and audio packages.

Add `custom_services: true` to the existing `api:` block. If you previously
used `phonebook_subscribe.yaml` or `ha_phone.yaml` and want to preserve their
routing preference, keep `use_ha_as_first_contact: true` in `voip_stack`.
Maintained profiles already include these migration changes.

After upload, check the available **VoIP Endpoint**, delivery of contacts,
calling and hangup. Updating the HACS integration alone does not recompile the
firmware or migrate a custom device YAML.

## Dashboard controls

The ESP mirror card uses the same call state and native controls.

![ESP mirror card keypad and options](images/esp-mirror-card-keypad-options.png)
