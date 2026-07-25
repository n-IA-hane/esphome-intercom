# Reference

## ESP `voip_stack`

```yaml
voip_stack:
  id: phone
  extension: "300"
  ring_groups: "Home,Workshop"
  conference_groups: "Home"
  conference_ring: false
  transport: udp  # SIP signaling transport only; audio is always RTP/UDP.
  sip_port: 5060
  rtp_port: 40000
  static_contacts:
    - name: Kitchen
      ip: 192.168.1.42
      transport: udp
      port: 5060
      rtp_port: 40000
```

### ESP Component Options

| Option | Meaning |
| --- | --- |
| `id` | Component ID. Required when an action or entity cannot infer the only `voip_stack` instance. |
| `transport` | SIP signaling transport: `udp` or `tcp`. SIP is implicit; this is not a protocol-family selector and does not move audio to TCP. RTP audio remains UDP. |
| `sip_port` | Local SIP listener port. |
| `rtp_port` | Local RTP media port. |
| `udp_max_payload` | RTP payload budget, default `1200` bytes. The accepted implementation range is `576..1488`; raise it only for a LAN whose MTU was verified. |
| `microphone` / `microphone_source` | Optional TX audio source. Use only one. `microphone_source` adds channel/sample-width selection for a wider native microphone. |
| `speaker` | Optional RX audio sink. Omitting the speaker produces a `mic_only` endpoint; omitting the microphone produces a `speaker_only` endpoint. |
| `audio.tx` / `audio.rx` | Primary per-direction PCM contract. Fields are `sample_rate`, `pcm_format`, `channels`, and `frame_ms`; `auto` derives the wired audio surface. |
| `audio.tx_formats` / `audio.rx_formats` | Up to seven extra explicit formats per direction. TX extras may only change `frame_ms` from `audio.tx`; RX extras may describe other formats the speaker path can accept. |
| `extension` | Optional internal dial-plan alias published to HA when the endpoint entity surface is exposed. |
| `ring_groups` | Optional comma-separated PBX ring group memberships. |
| `conference_groups` | Optional comma-separated conference group memberships. |
| `conference_ring` | Ring this ESP when another participant starts one of its conference groups. Requires `conference_groups`. |
| `static_contacts` | Optional local contacts loaded at boot. Structured entries accept `name`, `ip`, `port`, `rtp_port`, and `transport`. HA-managed `sensor.voip_phonebook` is recommended for normal installs. |
| `use_ha_as_first_contact` | Select the HA peer after the first roster population. |
| `ha_phonebook_text_sensor_id` | HA-published central roster source. |
| `delete_contact_missing_from` | Optional stale-contact pruning policy with `updates_number: 1..10`. |
| `ringing_timeout` / `calling_timeout` | Optional guard timers. |
| `dc_offset_removal` | Remove DC bias from TX microphone samples. |
| `buffers_in_psram` | Place VoIP-owned staging buffers in PSRAM. |
| `task_stacks_in_psram` | Place supported VoIP task stacks in PSRAM. Requires PSRAM and is rejected on the original ESP32. |
| `network_socket_headroom` | Validation-only reservation for additional lwIP sockets in composite firmware. |
| `audio_debug` | Verbose PCM-level diagnostics; keep off outside targeted tests. |

### ESP Triggers

| Trigger | Meaning |
| --- | --- |
| `on_calling` | Automation hook for outbound INVITE state. |
| `on_ringing` | Automation hook for inbound INVITE ringing state. |
| `on_dest_ringing` | Automation hook for remote `180 Ringing`. |
| `on_incoming_call` | SIP-aware hook with `call_id`, `caller`, `callee`, `uri`. |
| `on_outgoing_call` | SIP-aware hook with `call_id`, `caller`, `callee`, `uri`. |
| `on_bridge_request` | SIP-aware hook when the selected route targets HA/bridge. |
| `on_in_call` | Automation hook for established SIP call. |
| `on_idle` | Automation hook when the call FSM returns to idle. |
| `on_hangup` | Terminal/hangup hook. |
| `on_call_failed` | Terminal failure hook. |
| `on_destination_changed` | Selected phonebook destination changed. |
| `on_phonebook_update` | Local phonebook content changed. |

### ESP Actions

- Call control: `voip_stack.start`, `voip_stack.stop`,
  `voip_stack.call_toggle`, `voip_stack.answer_call`,
  `voip_stack.decline_call`, and `voip_stack.call` (`target`).
- Contact navigation: `voip_stack.next_contact`, `voip_stack.prev_contact`,
  and `voip_stack.set_contact` (`contact`).
- Local phonebook: `voip_stack.add_contact` (`entry` or structured
  `name`/`ip`/ports/`transport`), `voip_stack.remove_contact` (`entry`),
  `voip_stack.set_contacts` (`contacts_csv`), `voip_stack.set_roster_json`
  (`roster_json`), `voip_stack.flush_contacts`, and
  `voip_stack.update_contacts`.
- Routing/identity: `voip_stack.set_remote_endpoint` (`ip`, optional `port` and
  `rtp_port`) and `voip_stack.set_ha_peer_name` (`name`).
- Audio/diagnostics: `voip_stack.set_volume` (`volume`),
  `voip_stack.set_mic_gain_db` (`gain_db`), and
  `voip_stack.publish_entity_states`.

`voip_stack:` by itself is only the SIP/RTP engine. ESPs that should be
discovered by HA, mirrored by Lovelace, receive central phonebook sync or expose
dynamic groups must also expose the maintained entity surface:

```yaml
packages:
  voip_ha_integration: !include packages/voip/ha_integration.yaml
```

That package declares the `voip_endpoint` text sensor, call-state mirror text
sensors, editable `voip_ring_groups` / `voip_conference_groups` text entities
and the `voip_conference_ring` switch. See
[ESP_ENTITY_SURFACE.md](ESP_ENTITY_SURFACE.md) for the manual YAML.

ESPHome native API actions generated by the standard packages include
`esphome.<slug>_add_contact`, `esphome.<slug>_remove_contact`,
`esphome.<slug>_set_contacts`, `esphome.<slug>_flush_contacts`,
`esphome.<slug>_update_contacts`, `esphome.<slug>_set_roster_json`,
`esphome.<slug>_start_call` and `esphome.<slug>_decline_call`.
The contact actions mutate only that ESP's local mirror. Use HA
`voip_stack.add_contact` / `remove_contact` / `set_contacts` for the central
phonebook.

ESP static contacts and the structured `add_contact` action intentionally use a
small local contract: `name`, optional `ip`, `port`, `rtp_port`, and
`transport: udp|tcp`. The richer central HA roster additionally supports
`address`, `sip_uri`, `extension`, `number`, groups, and media metadata. HA
shapes that central data into the compact roster pushed to each ESP.

### ESP Conditions

- `voip_stack.is_idle`
- `voip_stack.is_calling`
- `voip_stack.is_remote_ringing`
- `voip_stack.is_ringing`
- `voip_stack.is_in_call`
- `voip_stack.is_incoming`
- `voip_stack.destination_is` (`destination`)
- `voip_stack.is_ha_destination`

## HA Logical Phones

The integration entry always owns a backward-compatible default Home Assistant
phone. Add more under **Settings > Devices & services > VoIP Stack > Add
phone**. Each phone is stored as a native Home Assistant config subentry and is
addressable by stable `endpoint_id`, Device ID, one of its Entity IDs, unique
name, extension or SIP username.

| Phone kind | HA representation | Transport behavior |
| --- | --- | --- |
| Home Assistant browser phone | Integration-owned `DeviceEntryType.SERVICE` Device with call-state sensor, connectivity binary sensor, DND switch and call event entity. | One or more cards attach through authenticated WebSockets. Browser-to-browser calls use HA's in-memory local bridge. |
| SIP account | Integration-owned `DeviceEntryType.SERVICE` Device with the same logical state surface. | A normal SIP UA registers to HA. The Device persists while its Contact is offline. |
| ESPHome phone | The existing ESPHome physical Device and its standard VoIP entity surface. | VoIP Stack discovers and routes it but does not merge, adopt or duplicate the ESPHome Device. |

There is no custom `phone` entity platform. Home Assistant's Device Registry is
the phone/container and standard sensor, binary sensor, switch and event
entities expose its state and controls. This preserves native areas, Device
automations, entity ownership and config-subentry removal semantics without a
private HA Core patch.

Common phone options are name, extension, enabled, DND, ring/conference group,
conference ringing and video capability. A configured browser phone remains a
routable logical handset even with no card connected: it enters `ringing`,
automations can act on that state, and a card connected during the ring window
can answer. Browser presence controls media availability, never dial-plan
membership. Unregistered SIP accounts may instead reject or forward because
they have no reachable Contact. Each phone owns at most one call; concurrent
calls receive `486 Busy Here`.

Browser-phone Devices expose extension and group membership as native text
entities plus DND and conference-ringing switches. These entities, the card
and the settings action share the same config-subentry-backed values.

One HA phone may be displayed by several cards. The first browser that answers
atomically owns its audio/video media; later answers cannot steal it. Create a
separate phone when a tablet must behave as a separately callable handset. A
dashboard reload may reclaim its still-active call only after every old media
socket has closed; a second live tab cannot preempt the owner. Audio-only
destinations remain audio-only even if the caller requests video. For
video-capable browser calls, offer/answer direction and each browser's camera
permission are independent.

## HA Services

- `voip_stack.call`
- `voip_stack.answer`
- `voip_stack.decline`
- `voip_stack.hangup`
- `voip_stack.forward`
- `voip_stack.route`
- `voip_stack.select_inbound_destination`
- `voip_stack.set_deadline`
- `voip_stack.cancel_deadline`
- `voip_stack.set_dnd`
- `voip_stack.set_auto_answer`
- `voip_stack.set_send_video`
- `voip_stack.set_ha_softphone_settings`
- `voip_stack.add_contact`
- `voip_stack.remove_contact`
- `voip_stack.set_contacts`
- `voip_stack.clear_contacts`
- `voip_stack.export_phonebook`
- `voip_stack.push_phonebook`
- `voip_stack.purge_devices`
- `voip_stack.create_account`
- `voip_stack.remove_account`
- `voip_stack.rotate_account_password`
- `voip_stack.enable_account`
- `voip_stack.disable_account`
- `voip_stack.list_accounts`

`call` accepts one required `destination`. The destination can be a
phonebook name, extension, ring group, conference group, SIP URI, direct
`user@host` target or external number. Set `ha_bridge: true` to force the HA
bridge path. Set `send_video: true` to offer the selected browser phone's
camera when SIP video is enabled. `answer` accepts the same
`send_video` choice; receiving video never requires it.
`set_auto_answer` and `set_send_video` persist the selected logical phone's
defaults and update its native switches. Browser microphone/camera permission
remains local and is never stored by Home Assistant.

`call`, `answer`, `decline`, `hangup`, `forward`, `set_dnd` and
`set_ha_softphone_settings` expose one optional `device_id` phone selector. If
omitted, the default HA phone is used. This is the local phone performing the
action, not the remote destination: `destination` is resolved independently by
the central phonebook. Internal endpoint and entity IDs are reported in
state/events for correlation, but are not alternative action inputs. Use
`call_id` when a concurrent-call automation must select one call.

`route` applies an automation decision to a pending inbound SIP route. Use
`action: answer_ha`, `decline`, `busy`, `cancel`, `forward`, `bridge`, or
`default`.

`select_inbound_destination` is the ordinary initial-routing action for a
pending `route_requested` occurrence. `set_deadline` and `cancel_deadline` are
advanced call-global controls: deadline expiry publishes a timeout-requested
occurrence but never changes routing by itself.

`forward` moves an existing HA-owned call to another dial-plan destination.
Pass `call_id` when more than one call could be eligible; when exactly one call
is forwardable for the selected logical phone, HA infers it. Use `call` to
originate a new call.

`add_contact` requires only `name`. Optional fields are `id`,
`address`, `sip_uri`, `extension`, `number`, `ha_bridge`, `transport`, `port`,
`rtp_port`, `tx_rate`, `rx_rate`, `tx_formats`, `rx_formats`, and
`max_payload_bytes`, `ring_group`, `conference_group` and `conference_ring`.
HA updates `sensor.voip_phonebook` and pushes the roster to online ESP devices.

`remove_contact` exposes one `name` input whose value may match a manual
contact's name, stable ID, extension or number.
`set_contacts` replaces manual central contacts from JSON.
`clear_contacts` removes manual central contacts. `push_phonebook` republishes
the current merged roster without changing it.

`create_account` creates or replaces a local account for Zoiper, Linphone,
baresip, pjsua, a VoIP desk phone or another standard SIP endpoint registering
directly to HA. The `username` is the SIP username and central roster ID;
`display_name`, `password`, `enabled`, `replace`, `extension`, `ring_group`,
`conference_group` and `conference_ring` are optional. If `password` is omitted,
HA generates one and returns it once in the action response. Capture that
response in an automation with `response_variable`, or copy it from Developer
Tools immediately. A caller-supplied password is preserved but deliberately
not echoed. Registered clients appear in the central phonebook and are pushed
to ESPs. `list_accounts` returns configured accounts without passwords.

Ring groups and conference groups are dynamic phonebook entries. A ring group
forks a call to all callable members except the caller and bridges the first
answered member. A conference group creates an HA-hosted SIP conference room;
calling the group joins immediately, while members with `conference_ring`
enabled are invited when the room starts.

## HA Setup Options

The setup flow has two layers:

| Option | Meaning |
| --- | --- |
| `sip_port` | HA SIP listener port. HA accepts SIP signaling over both UDP and TCP on this port. |
| `rtp_port` | Base HA RTP UDP port used by HA softphone media and relays. |
| `advertise_host` | Optional Contact/SDP host override for routed, VPN, LXC, Docker or multihomed installs. |
| `assist_intents` | Optional Assist intents for call, answer, decline and hangup. |
| `assist_endpoint_enabled` | Publish a native HA Assist pipeline as a callable phonebook destination. Disabled by default. |
| `assist_extension` | Explicit 1-8 digit extension for the Assist destination. No extension is assumed or reserved. |
| `assist_pipeline` | HA pipeline ID, or `preferred` to resolve HA's preferred pipeline. The pipeline's existing STT, conversation agent, TTS, language and voice settings are used. |
| `assist_advanced_call_context` | Disabled by default. Appends caller ID, phonebook match, source and called extension once to the initial `Incoming SIP call from ...` user message. These values are untrusted metadata; a phonebook match is not authentication. |
| `debug_mode` | Opt-in detailed diagnostics plus private WAV/JSON call captures under `~/.cache/voip_stack_debug`: up to 15 s per HA-softphone direction and 8 s per relay leg, retained at most 24 files / 64 MiB with directory mode `0700`. Leave disabled for normal operation and remove artifacts according to your privacy policy. |
| `experimental_sip_video` | Enables the supported SIP video profile for HA browser phones. Direct H.264, VP8 and JPEG require a secure context and compatible browser. ESP endpoints remain audio-only. The persisted option key retains its original name so existing configured entries do not need migration. |
| `video_transcoding_enabled` | Shown only after SIP video is enabled. Uses Home Assistant's available FFmpeg binary for bounded receive-only H.263, H.263-1998 or H.265 to VP8 conversion. Direct codecs never use it. |
| `video_camera_send_enabled` | Shown only after SIP video is enabled. Exposes the logical HA phone's persistent **Send Camera** switch for negotiated H.264 or VP8 transmit media. Browser camera permission remains local; receiving video never needs it. |
| `sip_registrar_enabled` | Allow standard SIP endpoints to register to HA with accounts created through the account services. This does not gate inbound calls by phonebook membership. |
| `trunk_enabled` | Enables the second setup step for provider/PBX registration. When false, no trunk client, registration, external route or DTMF collector starts. |

`sip_port` and `rtp_port` belong to the integration runtime, not to individual
phones. Every logical phone shares the same SIP listeners and dynamic RTP pool;
adding ten kiosk phones does not require ten signaling ports or ten fixed RTP
ranges.

When `assist_endpoint_enabled` is true, the setup flow asks for
`assist_extension` and `assist_pipeline`. The resulting contact is part of the
central phonebook/dial plan. Calls run directly against the selected HA Assist
pipeline; no separate SIP port or Assist satellite is created.

When `trunk_enabled` is true, the second step adds:

| Option | Meaning |
| --- | --- |
| `trunk_transport` | SIP transport used toward the provider: `udp` or `tcp`. |
| `trunk_server` | Provider registrar/proxy host. |
| `trunk_port` | Provider SIP port, normally `5060`. |
| `trunk_domain` | Optional SIP realm/domain; defaults to `trunk_server`. |
| `trunk_username` | SIP account user and default incoming Request-URI user. |
| `trunk_auth_username` | Optional digest auth username when different from `trunk_username`. |
| `trunk_password` | Digest auth password. |
| `trunk_register_expires` | REGISTER expiration in seconds. |
| `trunk_outbound_proxy` | Optional proxy host or `sip:host:port` used as signaling next hop. |
| `trunk_inbound_default_target` | Canonical phonebook target used by Direct mode or by the DTMF no-digits fallback. Default `HA`. |
| `trunk_inbound_mode` | `direct` resolves the default target immediately; `dtmf` pre-answers and collects an explicit phonebook extension. |
| `automation_routing_enabled` | Experimental, disabled by default. Exposes a bounded automation decision before Direct routing or the DTMF no-digits fallback. Explicit digits are never overridden. |
| `trunk_dtmf_timeout_ms` | DTMF-mode digit window. The setup UI shows seconds; internally this is stored in milliseconds. Default 3 s, maximum 10 s. |
| `trunk_dtmf_terminator` | Optional terminator digit such as `#`. Empty means timeout or exact phonebook extension match decides. |

Ambiguous DTMF digit prefixes are resolved at runtime against the live
phonebook `extension` fields. HA collects within the timeout and tries the
final buffer. If no digits arrive, HA uses `trunk_inbound_default_target`. If
explicit digits arrive and do not resolve, HA logs the digits and terminates
the answered leg as `route_not_found`.

Version 1 config entries migrate without changing their effective behavior:
an enabled non-zero DTMF configuration becomes `dtmf`, other configurations
become `direct`, and automation routing remains disabled until selected.

## Home Assistant Automation Events

`event.voip_stack_call` is the preferred native automation surface. It exposes
the call lifecycle, routing deadlines and DTMF through a browsable HA event
entity. Each integration-owned phone Device also has a scoped call Event Entity
for room-specific automations.

Public occurrence types are:

- `route_requested`, `outgoing_call`, `calling`
- `ringing`, `remote_ringing`, `forwarding`
- `answered`, `connected`
- `calling_timeout_requested`, `ringing_timeout_requested`
- `dtmf`
- `ended`, `missed`, `failed`, `state_changed`

The payload includes the canonical SIP fields when available: `state`,
`sip_state`, `type`, `call_id`, `sequence`, `previous_state`, `route_history`,
`automation_control`, `caller`, `callee`, `peer_name`, `direction`,
`local_name`, `target`, `sip_uri`, `route_kind`, `sip_transport`,
`sip_status_code`, `terminal_reason`, `endpoint_id`, source/destination endpoint
and Device IDs, stable `ingress` / `origin` (`trunk` or `extension`), selected
media formats, and RTP counters.

Use Home Assistant's native `event.received` trigger. Raw event-bus messages
are internal plumbing between the integration, frontend and Event Entities and
are not a second public automation API. See the
[automation cookbook](AUTOMATION_DIALPLAN.md) for complete recipes.
Completed calls are described automatically in the Home Assistant Logbook as
compact entries such as `Cucina called Portone · 45 s`. Missed and failed calls
use equally explicit summaries. `duration_seconds` is included in terminal
events when the call reached `in_call`; unanswered calls deliberately have no
talk duration.
With SIP/RTP debug enabled, the HA softphone snapshot also exposes `sip_trunk`
when a trunk client exists,
including registration status, last SIP status and last trunk SIP event.

The in-call DTMF payload uses the same call envelope and contains `call_id`,
`dest_call_id`, `caller`,
`callee`, `source`, `source_leg`, `side`, `digit` and `transport`. `digit` is
one string value from `0-9`, `*`, `#` or `A-D`; a multi-key sequence produces
one event per key. `transport` is `rtp_event` for negotiated RFC 4733 named
events or `sip_info` for the widely deployed legacy SIP INFO representation.
HA can observe this only when VoIP Stack is a signaling/media participant in
the call; direct ESP-to-ESP or third-party peer-to-peer calls bypass HA.

## SIP State Values

Public SIP call states: `idle`, `calling`, `remote_ringing`, `ringing`,
`connecting`, `in_call`, `terminating`, `busy`, `declined`, `cancelled`,
`media_incompatible`, `transport_unreachable`, and
`auth_required_unsupported`. A durable logical-phone sensor may additionally
show `offline` when its endpoint is unavailable and `held` while an established
call is on hold; these are phone/entity availability phases, not terminal SIP
outcomes.
