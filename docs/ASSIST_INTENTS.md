# Calling by voice with Assist

Enable **Assist intents** in the VoIP Stack integration to use voice commands
for calling, answering, declining and hanging up. The command acts on the VoIP
device that heard it. A contact name, extension or Home Assistant area can be
used as the destination.

Sentence examples are available for [English](../examples/home-assistant/custom_sentences/en/voip_stack.yaml),
[Italian](../examples/home-assistant/custom_sentences/it/voip_stack.yaml) and
[German](../examples/home-assistant/custom_sentences/de/voip_stack.yaml).
The German sentences and the proposal for customizable responses were
contributed by [Gafielt](https://github.com/Gafielt) in
[PR #137](https://github.com/n-IA-hane/esphome-intercom/pull/137).

## Install or update your sentences

Place the example for your Assist language under
`custom_sentences/<language>/voip_stack.yaml` in Home Assistant's configuration
directory. For German, that is `custom_sentences/de/voip_stack.yaml`. Restart
Home Assistant after saving the file to reload its sentences and responses.
These files are not installed or overwritten by a HACS update.

If you already have customized sentences, merge the response settings into
your file instead of replacing your phrases. Existing files without the new
response setting keep their existing English spoken confirmations and errors.
The call commands and target resolution do not change.

Each sentence group in the new examples contains:

```yaml
intents:
  VoipCall:
    data:
      - sentences:
          - "call {target}"
        slots:
          voip_response: template
        response: default
```

`voip_response: template` tells the handler to leave speech to Home Assistant's
normal response template. `response: default` selects the template below.
Keep both settings and the matching response definition together. Add them to
each sentence group whose response you want to customize.

```yaml
responses:
  intents:
    VoipCall:
      default: >-
        {% if slots.error %}
          I could not complete that call to {{ slots.target }}.
        {% else %}
          Calling {{ slots.target }}.
        {% endif %}
```

This is Home Assistant's existing custom-sentence and Jinja response mechanism,
not a separate template engine. The complete examples include specific error
messages and share their template across the four intents with a YAML anchor.
You can instead give each intent its own `default` template.

## Response variables

| Variable | Meaning |
| --- | --- |
| `slots.target` | Resolved contact name on success, or the requested contact/area when it could not be resolved. |
| `slots.spoken_target` | Destination as recognized from the user's words, before contact/area resolution. |
| `slots.action` | `VoipCall`, `VoipAnswer`, `VoipHangup` or `VoipDecline`. |
| `slots.error` | Empty on success; otherwise one of the error keys below. |

For example, if "call kitchen" resolves to the contact **Kitchen phone**,
`slots.target` is **Kitchen phone**, while `slots.spoken_target` is **kitchen**.
An unsuccessful call remains an error in Home Assistant's conversation result,
even when you customize its wording.

The supplied templates handle these error keys:

- `unknown_origin`: the device that heard the command could not be identified.
- `missing_target`: the call has no destination.
- `not_found`: no matching contact or area was found.
- `ambiguous`: more than one contact matches.
- `ambiguous_area`: more than one area matches.
- `area_empty`: the area contains no VoIP device.
- `ambiguous_area_device`: the area contains several VoIP devices.
- `call_failed`, `answer_failed`, `hangup_failed`, `decline_failed`: the requested
  call operation failed. Technical details remain in the Home Assistant log.

The integration supplies these values on the intent response. They are not
available through the generic `responses.errors` exception templates, which is
why the examples render errors inside `responses.intents` as well.

## Choosing silence explicitly

To suppress a spoken confirmation for one intent, keep its template setting and
replace its response with a template that renders an empty string:

```yaml
responses:
  intents:
    VoipHangup:
      default: "{{ '' }}"
```

The hangup action still runs. This controls speech only; it does not create or
guarantee a notification sound. Any device acknowledgement sound is governed by
that device's configuration. The example above also silences that intent's
spoken errors, so retain an error branch if you want failures announced.

See Home Assistant's [custom sentence documentation](https://www.home-assistant.io/voice_control/custom_sentences_yaml)
for the standard file layout and response syntax.
