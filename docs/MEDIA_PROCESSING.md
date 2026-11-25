# Account media processing

Every VoIP Stack account exposes a native **Media processing** diagnostic sensor
on its existing Home Assistant device. Its identity follows the same native
account migration and entity-registry rules as the account's call-state sensor.
Renaming an entity does not disconnect its state subscription.

The sensor reads the current session and its committed relay. It does not issue
ESPHome API commands, keep a second call registry, poll the network or require a
physical phone. Default firmware packages do not draw diagnostic text or load a
diagnostic font. Standalone ESP telephone operation is unchanged.

## State and attributes

| State | Meaning |
| --- | --- |
| `idle` | This account has no projected live calls. |
| `unknown` | At least one direction has not been measured or its owner does not expose the required processing information. |
| `inactive` | All measured audio/video directions are inactive. |
| `forwarded` | All active measured directions forward the media representation. |
| `processed` | All active measured directions perform a representation, codec or packetization operation. |
| `mixed` | Some active directions forward while others process media. |

The `calls` attribute keeps each call occurrence separately: `call_id`,
`generation`, `leg_id`, `sip_call_id`, `audio_tx`, `audio_rx`, `video_tx` and
`video_rx`. TX means media sent **by this account toward its peer**; RX is the
reverse. The peer's sensor therefore reverses these directions. `call_count`
counts projected occurrences, including a pending call whose processing is not
known yet. The aggregate never silently chooses one of several calls.

Audio operations are `forwarded`, `repacketized`, `resampled`,
`codec_converted`, `resampled_codec_converted`, `inactive` or `unknown`.
`resampled` denotes conversion of the PCM representation or geometry, which is
not necessarily a sample-rate change. Video operations are `forwarded`,
`converted`, `inactive` or `unknown`; conversion can include resizing while
retaining the same codec.

`forwarded` does not mean peer-to-peer networking or zero CPU work. HA may relay
RTP, rewrite its headers or decode a separate observation/recording copy. Hold
is projected from actual media directions, so a one-way stream such as music on
hold does not become falsely inactive. A browser/local PCM adapter currently
reports `unknown` when its complete processing boundary is not exposed. The
older internal `direct`/`ha_transcoding` route label is not used as a substitute.

The first admitted packet, a negotiated format change or a video-processing
transition can update the sensor. Identical packets do not generate repeated
state changes. Finishing a call removes its occurrence; callbacks retained by an
old relay owner cannot update its successor's diagnostic state.

## Optional ESPHome presentation

Use the standard Home Assistant text sensor if you want to draw the aggregate
on a particular screen. Set `account_media_processing_entity` to the actual entity ID from your
account's device; the example below does not invent an account identifier:

```yaml
substitutions:
  account_media_processing_entity: sensor.your_actual_entity_id

text_sensor:
  - platform: homeassistant
    id: account_media_processing
    entity_id: ${account_media_processing_entity}
    internal: true
    on_value:
      - lambda: |-
          ESP_LOGD("account_media", "Media processing: %s", x.c_str());
```

Replace the log action with your own LVGL label update if desired. Presentation
is a user choice; the account, media relay and ESP video renderer do not own it.
The former private `set_pbx_media_diagnostics` API and ESP `media_diagnostics`
text-sensor type have been retired. Remove those declarations from custom YAML
and import the native HA sensor instead.
