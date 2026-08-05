# Breaking changes

Read every section newer than the stable version currently installed before
upgrading. VoIP Stack is maintained by one person and may deliberately replace
an earlier development contract instead of carrying two parallel APIs. The
config-entry migration preserves supported persisted settings, but copied card
YAML and automations cannot be migrated by Home Assistant automatically.

## 2026.8.1: P4 full profile renamed for SIP video

The P4 full landscape profile now includes bidirectional SIP video over JPEG,
in addition to AFE, micro wake word, Voice Assistant, media playback and its
native Home Assistant camera entity. The filename now identifies it as a
videophone.

Replace:

```text
yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-landscape.yaml
```

with:

```text
yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml
```

The ESPHome node name and entity identities are unchanged. Only the example
YAML filename and references to it must be updated.

## 2026.8.1: phone actions and cards use device identifiers only

VoIP Stack no longer accepts the synthetic
`__voip_stack_ha_softphone__` selector or a card `endpoint_id`. Open each
`ha_softphone` card in the editor and select its Home Assistant phone again.
The saved card configuration must contain only the selected `device_id`.
Omitting the selection uses the preferred Home Assistant phone configured by
the integration.

Update copied automations so `device_id` contains the real Home Assistant
Device Registry ID of the local phone. `endpoint_id` remains visible in call
state and WebSocket snapshots for internal session and media correlation, but
it is not accepted as user configuration.

The `voip_stack.call` action response now exposes only schema version 2. Read
the selected phone from `phone.device_id`, `phone.kind` and `phone.name`; read
call state from `call.call_id`, `call.state` and `call.destination`. The former
duplicate flat fields such as top-level `device_id`, `endpoint_id`, `call_id`
and `destination` were removed.

ESPHome phones controlled from Home Assistant must expose the explicit
`start_call`, `answer_call`, `decline_call` and `hangup_call` native API
actions provided by the current `packages/voip/ha_phone.yaml`. VoIP Stack no
longer treats call/decline buttons as remote-control actions and no longer
uses `decline_call` as a substitute for hangup. Rebuild and flash custom
firmware that included only the older entity package.

Custom frontends must use the current scoped
`voip_stack/subscribe_ha_softphone` WebSocket request. The card no longer
retries the older unscoped subscription when the server rejects that request.

Video WebSocket negotiation now exposes codec parameters only inside the
directional `send` and `receive` objects. Custom clients must stop reading the
former flat `codec`, `encoding`, `clock_rate`, `payload_type` and `fmtp`
aliases from the negotiation root.

Softphone state events must include `endpoint_id` or `device_id`. The frontend
no longer guesses a phone for an endpoint-less event from the subscription
that delivered it.

The original singleton `sensor.voip_stack_call_state` has been removed. The
historical default Home Assistant phone now uses the same translated,
Device-owned call-state entity as every other browser phone. Update
automations to select that phone's `Call state` entity from its Home Assistant
Device instead of relying on the former global entity ID.

## 2026.8.0: upgrade checklist

1. Read the `2026.8.0` sections below and the
   [`2026.8.0` release notes](https://github.com/n-IA-hane/esphome-intercom/releases/tag/v2026.8.0).
2. Update VoIP Stack, restart Home Assistant and open **Reconfigure** once.
   Confirm the incoming-routing, Assist and SIP-video choices.
3. Open every additional phone under **VoIP Stack > Add phone** and verify its
   extension, groups, DND and video options.
4. Update copied automations to the service and routing contracts below.
5. Hard-refresh every browser or Companion WebView that loads the Lovelace
   card. An old JavaScript module cannot safely drive the new backend.

Do not delete and recreate the integration merely to perform this upgrade.
Config entry version 3 migrated the historical Home Assistant phone and its
persisted DND, extension and group settings into a phone subentry. Version 5
removes its special default behavior. A new installation creates one ordinary
browser phone named from the Home Assistant location, and that phone can be
renamed or removed like any other. Existing local SIP registrar accounts are
also converted into phone subentries.

## 2026.8.0: every logical phone is a separate Home Assistant device

The former single HA softphone model is now a collection of native config
subentries and Devices. Browser phones and standard SIP accounts are created with
**Settings > Devices & services > VoIP Stack > Add phone**.

Each phone owns its call state, DND, extension, groups and video settings.
Standard SIP accounts additionally own their unregistered-endpoint policy.
Consequently:

- bind each `ha_softphone` card to the intended phone Device;
- trigger room-specific automations from that phone's own call-state Sensor or
  call Event Entity;
- do not assume that the aggregate `event.voip_stack_call` identifies one
  particular room phone;
- do not use browser-card presence as the definition of whether a logical
  phone exists. An offline browser phone may still ring logically so that
  timeout and forwarding automations can run.

Every phone receives its Device-owned entity IDs from Home Assistant. Select
entities from the Device instead of constructing an ID from the phone name.

Card YAML contains only `device_id`. The backend supplies the internal
`endpoint_id` in runtime snapshots when the frontend needs to correlate media
with a call.

## 2026.8.0: one Home Assistant call-action vocabulary

Home Assistant actions now use `destination` as the only call destination
field. Replace `target:` or `call:` inside `voip_stack.call`,
`voip_stack.forward` and `voip_stack.route` action data with `destination:`.
Phone actions now use `device_id` as their only optional phone selector;
remove `endpoint_id`, `entity_id`, `source`, `source_device_id`, `source_name`,
`name` and `friendly_name` from action data. Internal endpoint/entity IDs
remain available in state and events for correlation.

Before:

```yaml
- action: voip_stack.call
  data:
    target: Kitchen
```

After:

```yaml
- action: voip_stack.call
  data:
    destination: Kitchen
```

`destination` is resolved by the central phonebook; no Device ID is needed to
identify Kitchen. The optional `device_id` selects the **local calling phone**,
not the remote destination. For example, add the Device ID of a Reception
browser phone when Reception must place the call:

```yaml
- action: voip_stack.call
  data:
    destination: Kitchen
    device_id: <reception_phone_device_id>
```

Omit `device_id` only when a preferred phone is configured or exactly one
compatible phone exists. An ambiguous request fails explicitly. This same
local-phone selector rule applies to `answer`, `decline`, `hangup`, `forward`,
`set_dnd` and `set_ha_softphone_settings`. `call_id` remains an optional flat
action field when one of several concurrent calls must be selected;
**Advanced options** is only how the automation editor groups that field
visually.

The duplicate `voip_stack.export_accounts` action was removed; use the
identical `voip_stack.list_accounts` response action. These development-only
aliases were removed before the stable release so the automation editor and
API expose one predictable path. The development-only
`voip_stack/ha_softphone_start` WebSocket command was also removed: cards and
clients must use the standard Home Assistant `call_service` command with the
`voip_stack.call` action.

## 2026.8.0: incoming trunk routing is explicit

Reconfigure the integration and choose one incoming-routing mode:

- **Route immediately** sends the call to the configured fallback destination
  without pre-answering it for digit collection.
- **Collect extension with DTMF** pre-answers the trunk leg and collects
  negotiated RFC 4733 `telephone-event` or compatible SIP INFO digits. An
  explicit valid extension is authoritative. An unknown explicit extension
  fails instead of silently ringing the fallback.

Existing entries migrate without changing their effective mode: an enabled,
non-zero legacy DTMF timeout becomes DTMF mode; every other configuration
becomes Direct mode. Experimental automation overrides are always disabled by
the migration and must be enabled deliberately.

When automation routing is enabled, `route_requested` is a bounded initial
decision point. Use:

```yaml
- action: voip_stack.select_inbound_destination
  data:
    destination: RG Casa
```

Do not use `voip_stack.forward` for that initial decision. `forward` moves an
already delivered ringing or connected HA-owned call. `voip_stack.route` is
the advanced low-level decision action and is not the ordinary phonebook
route.

In Direct mode the decision occurs before the fallback. In DTMF mode it occurs
only when no digits were entered. If no automation acts within the window, the
configured fallback remains authoritative.

Routing a group now re-enters the canonical PBX dispatcher: eligible members
ring in parallel, the first answer wins, losing legs are cancelled and the
originating endpoint is excluded when it belongs to the destination group.
Automations that previously expanded a group themselves should select the
group name instead.

## 2026.8.0: use phone-scoped state for phone-scoped automations

`event.voip_stack_call` is the aggregate PBX-wide Event Entity and is the
correct trigger for `route_requested`. Each integration-owned phone also has a
scoped call Event Entity and durable call-state Sensor. Use the scoped Sensor
for rules such as "Casa has been ringing for 30 seconds": the selected entity,
not the word `ringing`, determines which phone owns the automation.

Call state and event attributes now expose:

- `direction`: `incoming` or `outgoing` from the selected phone's perspective;
- `ingress` and `origin`: `trunk` for a provider/PBX call or `extension` for a
  call originating from a local ESP, browser phone or registered SIP endpoint;
- `scope`: internal state ownership, not the transport source.

A phone in `ringing` is already receiving a call, so an additional
`direction: incoming` condition is redundant. Use an `ingress: trunk`
condition when a no-answer rule must apply only to external PBX/provider
calls. Replace route-source filters based on `scope` or internal owner IDs with
`ingress`.

## 2026.8.0: SIP account secrets are returned, not broadcast

Local-account and phonebook export actions are administrator-only when invoked
by an authenticated user. Internal Home Assistant automations remain allowed
where documented.

`voip_stack.create_account` no longer publishes a generated password in a
persistent notification or call event. When Home Assistant generates the
password, it is returned once in that administrator action response. A
user-supplied password is preserved but never echoed. Likewise,
`voip_stack.rotate_account_password` returns the replacement once, and
`voip_stack.list_accounts` returns account metadata without passwords.

If an old script waited for `sip_account_created`,
`sip_account_password_rotated`, `list_accounts` or `export_accounts` call
events, replace that flow with an action response. Save a generated or rotated
password immediately; it cannot be recovered later and must instead be
rotated again.

## 2026.8.0: optional ESPHome entities are explicit platforms

The ESPHome `voip_stack` core no longer accepts `auto_entities`. ESPHome's
upstream contribution rules prohibit using `AUTO_LOAD` for primary entity
platforms such as `switch`, `number`, `button`, `text` and `text_sensor`.
Declare the required `platform: voip_stack` entities explicitly, or include
`packages/voip/auto_entities.yaml` in maintained full-duplex configurations.
Configurations that do not need those entities now compile without declaring
empty platform sections.

## 2026.7.1: contract updates

The stable `2026.7.0` migration below remains the main breaking change.
`2026.7.1` additionally makes several previously implicit
contracts explicit. Review them before upgrading an older custom YAML,
automation, SIP client or card fork.

- **ESP contact schema.** Structured ESP `static_contacts` and
  `voip_stack.add_contact` entries use `name`, optional `ip`, `port`,
  `rtp_port` and `transport`. `address`, `sip_uri`, `extension`, `number`,
  groups and media metadata are HA central-phonebook fields.
- **No inbound phonebook allowlist.** An unknown or unregistered SIP peer with
  network reachability can call HA or an ESP. Normal destination, busy/DND and
  SDP checks still apply. Enforce caller admission at a firewall, VLAN, VPN or
  SBC boundary when required.
- **Constrained in-dialog renegotiation.** ESP endpoints still return `488 Not
  Acceptable Here` for media-changing re-INVITEs. Direct HA browser dialogs
  accept compatible peer-initiated UPDATE/re-INVITE changes, including
  hold/resume, RTP endpoint changes and video add/remove. An audio-only
  SIP-to-SIP bridge may add video through a paired destination re-INVITE and a
  direct or transcoded relay. HA does not initiate an unsolicited media change.
  A rejected offer leaves the previous media/dialog active.
- **DTMF route input.** The trunk digit router accepts RTP `telephone-event`
  and compatible legacy SIP INFO DTMF. It does not decode acoustic in-band tones from
  the call audio.
- **Processor bypass semantics.** When the configured audio processor is
  enabled, unavailable processed output fails closed to silence. Disabling the
  parent AEC switch explicitly publishes converted raw microphone audio on the
  same public microphone surface.
- **Software-AEC reference lifecycle.** Maintained generic single- and dual-bus
  profiles now default to `aec_reference: previous_frame`. The optional
  `ring_buffer` mode remains supported, but its reference is session-scoped: it
  is cleared when the first microphone consumer starts and when the last one
  stops, and it is not filled while no microphone consumer exists. Custom code
  must not treat old speaker samples as a reference for a later call.
- **Bounded reentrant runtime events.** Runtime-controller events/actions
  created during a drain stay queued for the next main-loop turn. Custom
  automations must not depend on an unbounded synchronous self-trigger chain.
- **Plaintext local media boundary.** ESP SIP is UDP/TCP without TLS and RTP is
  UDP without SRTP. Do not expose an ESP listener directly to an untrusted
  network.

The full change and validation summary is in the
[`2026.7.1` release](https://github.com/n-IA-hane/esphome-intercom/releases/tag/v2026.7.1).

## 2026.7.0: ESPHome devices are SIP phones now

This release is the SIP/VoIP migration. It is not a small protocol tweak and it
is intentionally not backward compatible with the retired project-specific
call-control path.

The practical change:

- ESP `voip_stack` devices are now SIP phones.
- Home Assistant `voip_stack` is now a SIP softphone, dial-plan authority,
  SIP router/B2BUA, RTP bridge/resampler and optional SIP trunk client.
- The Home Assistant integration domain and services are `voip_stack.*`.
  Existing automations that used older development names must be updated.
- Older development installs used the Home Assistant domains `intercom_native`
  or `homeassistant_voip_stack`. The HACS repository now exposes only
  `custom_components/voip_stack`; remove old test config entries and stale
  old-domain folders before adding the current VoIP Stack integration.
- Home Assistant no longer exposes local SIP transport toggles. Configure ports
  and optional features, not listener modes.
- VoIP Stack supports Home Assistant's native Reconfigure action. You no longer
  need to remove and re-add the integration to change ports, debug mode,
  local SIP registrar support, Assist intents or optional trunk settings.
- Standard softphones can register to Home Assistant as local SIP accounts and
  become phonebook contacts.
- Local SIP account services now describe generic SIP endpoint accounts, not
  only "softphones". Real VoIP phones, ATAs, baresip, pjsua, Zoiper and Linphone
  all use the same account model.
- Home Assistant can register one provider/PBX trunk, so ESPs and the HA
  softphone can make and receive external calls without requiring Asterisk next
  to the integration.
- The shared phonebook is now a SIP dial plan: explicit `sip:name@host` /
  `name@host` routes go direct, known local endpoints can be bridged by HA, and
  external numbers can use the trunk when configured.
- Phonebook `extension` values are the internal dial-plan aliases. Static DTMF
  route maps are not a second routing table; inbound DTMF and numeric local
  calls resolve against roster extensions.
- SIP call reasons are surfaced to ESP displays, HA state and cards: busy, DND,
  declined, cancelled, timeout, media-incompatible, transport-unreachable and
  route errors are no longer hidden behind the old intercom FSM.
- The reusable ESP audio backend has moved out of this repository. If you are
  looking for `esp_audio_stack`, `esp_aec` or `esp_afe`, use the dedicated
  [`esphome-audio-stack`](https://github.com/n-IA-hane/esphome-audio-stack)
  repository. This repository now consumes it as the audio engine for full
  voice/VoIP products.

Migration impact:

- Rebuild ESP firmware from the maintained 2026.7.0 YAMLs or update custom
  YAMLs to the SIP `voip_stack` contract.
- Update custom `external_components` entries that previously pointed at this
  repository for `esp_audio_stack`, `esp_aec` or `esp_afe`; those components now
  resolve from `github://n-IA-hane/esphome-audio-stack@main`.
- `transport: udp|tcp` still exists in ESP YAML, but it means SIP signaling
  transport only. RTP media is UDP.
- ESP devices do not REGISTER to a provider/PBX and do not require SIP auth.
  Provider/PBX registration belongs to Home Assistant's optional trunk client.
- The old ESP-only network scanning/discovery path is gone. Use explicit SIP
  URIs, ESP `static_contacts` entries or the HA-managed roster.
- The old project-specific VoIP call-control protocol is not a fallback.
- ESP group membership is not packed into the endpoint sensor anymore. The
  endpoint sensor stays short and stable; group membership is read from optional
  sibling ESPHome entities (`voip_ring_groups`, `voip_conference_groups` and
  `voip_conference_ring`) when those entities are exposed by the HA integration
  package.
- `voip_stack:` alone is only the ESP SIP/RTP engine. HA discovery, mirror-card
  state, group membership and phonebook synchronization require the maintained
  `packages/voip/ha_integration.yaml` entity surface or equivalent manual
  `platform: voip_stack` entities.

## SIP consolidation audit

The active branch is intentionally SIP-first and breaking:

- ESP `voip_stack` is a SIP phone. `transport: udp|tcp` means SIP signaling
  transport only.
- HA `voip_stack` is a SIP softphone, dial-plan authority, SIP/RTP
  bridge and optional SIP trunk endpoint. Only trunk/provider transport remains
  configurable.
- The retired proprietary intercom protocol is not a compatibility layer.
- ESP discovery/scanning between standalone peers is not a functional
  primitive. Use explicit SIP URIs, ESP `static_contacts` entries or the HA
  roster.
- Cards mirror their owner. HA cards mirror HA softphone state. ESP cards mirror
  ESPHome entities and controls.
- Optional HA trunk support is disabled by default. If disabled, no provider
  registration, external route or DTMF routing path starts.
- Inbound trunk routing uses standard SIP plus RFC2833/telephone-event DTMF to
  select local phonebook targets.

## 2026.7.0: source-based full media path and native 48 kHz VoIP presets

`2026.7.0` introduces the new full-experience audio/media path and
Sendspin/Music Assistant integration.

Maintained full-experience YAMLs now use ESPHome's `speaker_source` media
player path. Media, announcements, timer sounds, local audio files and optional
Sendspin streams enter one media player and then flow through the existing
mixer. VoIP call audio still has its own mixer source and keeps priority through the
existing call-state logic. Custom YAMLs copied from older full-experience files
should migrate away from `platform: speaker` media-player blocks and local
`files:` entries toward `media_source` plus `media_player.play_media` with
`audio-file://...` URLs.

Maintained non-native full-experience YAMLs now use the generic `runtime_controller`
component for runtime state arbitration. Custom full-experience YAMLs copied
from older releases should migrate away from `update_status`,
`timer_ringing`, local VA pending flags and callback-local LED/display/ducking
decisions. The new pattern is: callbacks send `runtime_controller.event`, the reducer
sets activities, and policies drive LED/display/audio/timer outputs from one
committed snapshot.

Voice Assistant response state is now tied to TTS/media-player announcement
lifecycle callbacks through `runtime_controller`. Slow local TTS backends can
exceed ESPHome's historical 2-second playback-start watchdog, especially XTTS
running locally. This release temporarily ships a project-local
`voice_assistant` fork that exposes `tts_playback_start_timeout`; maintained
full profiles set it to `10s` through the full voice/runtime packages.

Sendspin is included in maintained full-experience profiles as a Music Assistant
media source. It is not required for normal HA media, TTS, timer sounds,
ringtones, Voice Assistant or VoIP calls. WS3, Spotpear and P4 grouped
playback were validated with the shared `speaker_source` path and the
hardware-clocked ESP audio stack timing model.

Native ESPHome voip-only presets now use 48 kHz PCM where the actual
native I2S microphone or speaker path supports it. SIP/TCP profiles may use
larger packet times; SIP/UDP profiles use short packet times such as 10 ms so
48 kHz/s16/mono remains below the default 1200-byte UDP datagram limit.
AFE/AEC-backed profiles keep 16 kHz TX because that is the Espressif AFE/AEC
output format; this is a local branch constraint, not a global SIP media limit.

UDP custom formats are validated against `udp_max_payload` at build time and by
Home Assistant when publishing the phonebook. The default is intentionally
conservative at 1200 bytes per audio frame. If you deliberately run larger LAN
datagrams, set the same larger `udp_max_payload` in the ESPHome YAML and the
VoIP Stack integration options; otherwise use TCP for high-rate, stereo or
32-bit PCM.

ESP endpoint publication now waits for a valid IPv4 address from ESPHome's
network API and republishes on Wi-Fi or Ethernet IP events. Endpoint sensors
should no longer publish incomplete rows before the board has a usable address.

Inbound SIP calls are now treated consistently across SIP/TCP and SIP/UDP: the
callee does not require the caller to be present in its local phonebook. The
phonebook is the outbound dial plan; inbound INVITE already carries caller and
destination identity. This matters for HA SIP bridges, routed subnets, VPN
callers and direct SIP clients. If a custom automation previously assumed that
an unknown inbound caller would be rejected by missing phonebook state, replace
that policy with DND, routing rules or an explicit bridge/service check.

Call-ended UI now preserves the real incoming caller through the terminal
callback before clearing the caller sensor. Device displays and the HA card
should show the peer from the active call, not whichever phonebook contact is
currently selected for the next outbound call.

The HA softphone card exposes Auto Answer, DND, Send Camera and browser ringtone
behind an idle-only Options panel. Auto Answer, DND and Send Camera are
persistent logical-phone settings exposed through HA entities and services.
Browser ringtone remains a per-browser localStorage preference.

ESP mirror and HA-softphone Lovelace modes have intentionally separate
semantics. `esp_mirror` mirrors one ESP endpoint and exposes controls from that
ESP's perspective. `ha_softphone` represents Home Assistant itself. If you
previously depended on one card mixing both models, split it into one ESP mirror
card and one optional HA softphone card.

ESP mirror cards now have a keypad/manual target view. This still calls the
mirrored ESP's own `start_call` action; it is not a separate HA-side contact or
number path. The ESP uses its local synced phonebook first and sends unresolved
targets to HA for central dial-plan/trunk/group routing.

Browser audio setup now waits for the HA SIP softphone control response and
uses the negotiated TX/RX formats from that response before creating the AudioWorklets.
Custom frontend code that starts microphone/playback worklets before the
control reply should be updated; otherwise ESP -> HA answered calls can use the
wrong frame size when one direction negotiates 48 kHz.

ESP caller playback now applies the negotiated RX speaker format before the
call is activated and re-applies it when the SIP answer confirms the effective
direction formats. Custom ESP integrations that bypass the maintained
`voip_stack` speaker setup must do the same before feeding high-rate PCM to
the local speaker path.

The Lovelace frontend derives ringtone/worklet cache keys from the loaded card
module version instead of a manually bumped audio asset constant. During custom
frontend testing, clear the browser cache or change the card resource URL when
serving files outside the packaged release flow.

Older upgrade notes are kept in their original GitHub release pages. The
sections above track the supported upgrade contracts through `2026.8.0`.
