# VoIP Stack for ESPHome and Home Assistant

[![Platform](https://img.shields.io/badge/Platform-ESP32--S3%20%7C%20ESP32--P4-blue.svg)](#supported-hardware)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-native-blue.svg)](https://www.home-assistant.io)
[![ESPHome](https://img.shields.io/badge/ESPHome-2026.6.5%2B-18bcf2.svg)](https://esphome.io)

Turn ESPHome audio devices and Home Assistant into a local SIP phone system.

ESP devices become standards-based audio phones. Home Assistant can be a SIP
video softphone, call router, RTP bridge, local registrar, conference focus,
Assist destination and optional trunk endpoint. Browser phones, wall tablets,
ESP room stations, standard SIP clients and an external PBX can share one
phonebook without requiring a separate Asterisk or FreeSWITCH server for the
normal home use case.

![VoIP Stack dashboard and central phonebook](docs/images/voip-dashboard-phonebook.png)

![Dashboard demo](docs/images/dashboard.gif)

_One dashboard can control a browser phone, an ESP endpoint and the shared
phonebook. Each room phone still has its own identity and call state._

<table>
  <tr>
    <td align="center"><img src="docs/images/call-from-esp-to-homeassistant.gif" width="180"/><br/><b>ESP to HA</b></td>
    <td align="center"><img src="docs/images/ha-sip-video-call.gif" width="180"/><br/><b>SIP video</b></td>
    <td align="center"><img src="docs/images/assistant-animated.gif" width="180"/><br/><b>Assist extension</b></td>
    <td align="center"><img src="docs/images/lvgl-audio-volume.jpg" width="180"/><br/><b>ESP controls</b></td>
  </tr>
</table>

> [!NOTE]
> VoIP Stack is an enthusiast open-source project maintained primarily by one
> person. It is designed for trusted home and laboratory networks, not as an
> emergency telephone service.

## What can you build?

![Video doorbell and room-to-room calls through Home Assistant](docs/images/voip-doorbell-room-to-room.png)

| Goal | What VoIP Stack provides | Start here |
|---|---|---|
| Video doorbell | A SIP video door station can ring a browser phone in a dashboard or Companion app. Standard ESP profiles remain audio-only; qualified ESP32-P4 profiles can also send and receive SIP video. | [SIP video](docs/SIP_VIDEO.md) · [door-station recipe](#door-station-and-unanswered-calls) |
| Room-to-room calls | Create a logical HA phone and dedicated dashboard view for each kiosk or tablet. Calls may be private audio or video. | [Logical phones](#logical-home-assistant-phones) |
| ESP room phones | Flash one maintained VoIP YAML per room. ESPs can call names and extensions from the shared phonebook. | [Deployment guide](docs/DEPLOYMENT_GUIDE.md) |
| Existing SIP equipment | Register Zoiper, Linphone, baresip, an IP phone or an ATA directly to HA. | [Local accounts](docs/SERVICES.md#local-sip-endpoint-account-services) |
| Ring groups | Ring several eligible endpoints; the first answer wins and the losing branches are cancelled. | [Groups](docs/GROUPS.md#ring-group) |
| Audio conferences | Host a local audio conference in HA and optionally ring its members. | [Groups](docs/GROUPS.md#conference-group) |
| Callable Assist | Give a native Assist pipeline an extension and talk to it from ESP, SIP or trunk callers. | [Assist calls](#assist-as-a-phone-extension) |
| External calls | Register an optional provider/PBX trunk for inbound and outbound calls. | [SIP trunk](docs/SIP_TRUNK.md) |
| Contextual routing | Use native HA entities, conditions and services for presence, schedules, no-answer forwarding and in-call DTMF. | [Automation cookbook](docs/AUTOMATION_DIALPLAN.md) |

## Fastest start

1. Install **VoIP Stack** from HACS and restart Home Assistant.
2. Add **VoIP Stack** from **Settings → Devices & services**.
3. Keep SIP `5060` and RTP base `40000` unless they conflict with your network.
4. Choose a maintained YAML under [`yamls/`](yamls/) and adapt only its
   substitutions, pins and secrets.
5. Add the flashed device through the normal ESPHome integration.
6. Add the VoIP Stack card to a dashboard and select the intended phone Device.
7. Call the phonebook name shown by the card or ESP.

For one ESP intercom, that is enough. Local SIP accounts, multiple browser
phones, Assist and a trunk are optional layers.

| Device goal | Maintained starting point |
|---|---|
| ESP VoIP only | [`yamls/voip-only/`](yamls/voip-only/) |
| Voice Assistant + MWW + media + VoIP | [`yamls/full-experience/`](yamls/full-experience/) |
| Native ESPHome mic/speaker paths | [`yamls/voip-only/esphome-native/`](yamls/voip-only/esphome-native/) |
| New or unqualified hardware | [`yamls/experimental/`](yamls/experimental/) |

The [deployment guide](docs/DEPLOYMENT_GUIDE.md) explains how to choose between
single-bus, dual-bus, lightweight AEC and full AFE profiles.

## How it works

![Home Assistant as a local SIP and PBX hub](docs/images/home-assistant-local-sip-pbx.png)

The system has one call model and four main surfaces:

- **ESP endpoint:** A lightweight SIP/SDP/RTP phone with a microphone,
  speaker or both.
- **Home Assistant runtime:** Logical browser phones, routing, media bridges,
  groups, local registration, Assist and the optional trunk.
- **Central phonebook:** Names, extensions, groups, registered clients,
  routable SIP endpoints and external numbers.
- **Lovelace card:** A UI bound to one phone Device; it projects backend state
  and invokes normal HA services instead of running a second call controller.

SIP dialogs, transactions and transports have separate lifecycles. Logical
phones share the listener and RTP pool, so creating another room phone does not
open another SIP server. Each phone owns at most one active call; calls to
different phones remain independent.

The detailed ownership and media model is in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md). Complete peer-to-peer and
HA-bridged sequences are in [`docs/CALL_FLOWS.md`](docs/CALL_FLOWS.md).

## Logical Home Assistant phones

The integration creates one default Home Assistant browser phone. Add more
from:

**Settings → Devices & services → VoIP Stack → Add phone → Home Assistant
browser phone**

Give every real place its own name and optional extension: `Kitchen`,
`Reception`, `Garage`, `201`, and so on. Each logical phone is represented by a
native HA Device with entities for call state, connectivity, DND, extension,
groups, auto answer, camera transmission settings and call events.

Bind the card to that Device:

```yaml
type: custom:voip-stack-card
mode: ha_softphone
device_id: <phone_device_id>
```

`device_id` answers "which local phone owns this card or action?".
`destination` answers "who should this phone call?". The central phonebook
resolves the destination, so a normal call does not require the destination's
Device ID:

```yaml
action: voip_stack.call
data:
  device_id: <kitchen_phone_device_id>
  destination: Garage
```

Omit `device_id` to originate from the default HA phone.

Each room-to-room media endpoint needs a distinct browser or Companion session.
Two cards in one browser tab can display two phones, but one tab still owns one
physical microphone, speaker and camera pipeline.

## ESP endpoints and media roles

ESP media roles are derived from the configured components; they are not a
separate user mode.

| Derived role | Media | Typical use |
|---|---|---|
| `full_duplex` | microphone TX + speaker RX | Room phone, door station, wall panel |
| `mic_only` | microphone TX | Monitor or capture endpoint |
| `speaker_only` | speaker RX | Paging or announcement target |

An endpoint must have at least one real media direction. ESP VoIP intentionally
uses uncompressed PCM for audio. Standard profiles are audio-only; qualified
ESP32-P4 videophone profiles compile exactly one video codec, JPEG or H.264.
HA performs format conversion when a standard SIP peer negotiates another
supported audio codec; ESP firmware is not downgraded to a telephone codec for
that purpose.

The maintained full-experience profiles share one processed post-AEC microphone
surface between VoIP, Micro Wake Word and Voice Assistant. Music, TTS, ringtone
and optional Sendspin playback feed the same speaker-reference path, so those
consumers receive cleaned microphone audio rather than competing raw streams.

![Shared music, TTS, wake word, Voice Assistant and VoIP audio pipeline](docs/images/shared-audio-aec-pipeline.png)

Audio component details live in the companion projects:

- [`esp_audio_stack`](https://github.com/n-IA-hane/esphome-audio-stack)
- [`esp_aec`](https://github.com/n-IA-hane/esphome-audio-stack/tree/main/esphome/components/esp_aec)
- [`esp_afe`](https://github.com/n-IA-hane/esphome-audio-stack/tree/main/esphome/components/esp_afe)
- [`voip_stack`](https://github.com/n-IA-hane/esphome-voip-stack)
- [`runtime_controller`](https://github.com/n-IA-hane/esphome-runtime-controller)

## Home Assistant as a SIP video phone

Video is available to compatible browser phones in current browsers and the
Home Assistant Companion app. Standard SIP video door stations, video phones,
softphones and PBX/trunk legs can negotiate H.264, VP8 or RTP/JPEG. An optional
bounded FFmpeg path can receive selected legacy codecs.

Receiving video does not require camera permission. Sending the browser camera
is a separate persisted phone setting and still requires browser permission.
Audio remains usable when video is unavailable or deliberately disabled.

Audio-only ESP endpoints and audio conferences remain audio-only. Qualified
ESP32-P4 videophone profiles can negotiate RTP/JPEG or H.264 directly, while
the full P4 profile supports bidirectional RTP/JPEG. Video compatibility
depends on the actual offer/answer, packetization and browser decoder, not only
on a codec name printed on a product page.

See the [capability matrix, privacy controls and limits](docs/SIP_VIDEO.md).

## Phonebook and routing

Home Assistant publishes the shared roster through
`sensor.voip_phonebook`. It combines:

- online ESPHome VoIP endpoints;
- logical Home Assistant phones;
- registered local SIP accounts;
- manual contacts;
- Assist destinations;
- dynamic ring/conference groups;
- optional trunk-routed numbers.

A name is the contact identity. An `extension` is an internal alias for that
same destination; a `number` is normally routed through the trunk. The resolver
can also handle canonical SIP URIs.

Use the card, voice intents or `voip_stack.call` with a phonebook name or
extension. Do not copy endpoint addresses into automations unless you
deliberately need a raw SIP route.

- [Resolver rules](docs/DIALPLAN_RESOLVER.md)
- [Phonebook protocol](docs/PHONEBOOK_PROTOCOL.md)
- [Services](docs/SERVICES.md)
- [ESP discovery/entity surface](docs/ESP_ENTITY_SURFACE.md)

![Central phonebook and endpoint resolution](docs/images/phonebook-endpoint.png)

## Groups

![Ring groups and conference groups](docs/images/ring-group-conference-group.png)

A **ring group** forks one call to all eligible members. DND, disabled and busy
members are excluded; the caller is excluded if it belongs to the target
group. The first successful answer wins and every losing leg is cancelled.

An **audio conference group** joins answered members to one HA-hosted mixer.
`conference_ring` optionally rings the declared members when somebody joins.
Auto answer is a per-phone policy: enabling it on several group members means
several endpoints may join or compete immediately, which is the configured
behavior.

Membership declared by a phone becomes visible in HA dynamically. A group
disappears when no current endpoint declares it. See
[`docs/GROUPS.md`](docs/GROUPS.md) for declaration and collision rules.

## Assist as a phone extension

Enable **Include voice assistant** while configuring VoIP Stack, choose the
pipeline and assign an extension. ESPs, registered SIP phones, browser phones
and trunk callers can then call that Assist pipeline like any other contact.

VoIP Stack sends one initial user message such as
`Incoming SIP call from "Daniele".` and then streams the selected pipeline's
STT, conversation and TTS over the same call. It does not inject a second
persistent personality prompt. Put behavioral instructions in the conversation
agent itself.

Optional advanced call context appends caller ID, phonebook match, ingress and
called extension once. Treat those fields as untrusted call metadata, not
authentication.

Voice intents may also resolve commands such as "Call Kitchen", "Answer" and
"Hang up" against the live phonebook and the satellite's selected phone.

<table>
  <tr>
    <td align="center"><img src="docs/images/assistant-neutral.jpg" width="180" alt="Assist idle"/><br/><b>Idle</b></td>
    <td align="center"><img src="docs/images/assistant-speaking.jpg" width="180" alt="Assist speaking"/><br/><b>Speaking</b></td>
    <td align="center"><img src="docs/images/assistant-happy.jpg" width="180" alt="Assist happy"/><br/><b>Happy</b></td>
    <td align="center"><img src="docs/images/assistant-angry.jpg" width="180" alt="Assist angry"/><br/><b>Your prompt did this</b></td>
  </tr>
</table>

## Door station and unanswered calls

![Assist answers an unattended doorbell call](docs/images/assist-unanswered-doorbell.png)

A typical path is:

1. a door station calls `Front door`;
2. a ring group alerts selected browser, SIP and ESP phones;
3. the first endpoint to answer owns the call;
4. if nobody answers, a native HA automation forwards the still-live call to
   another phone or Assist;
5. call events can trigger a mobile notification or another HA action.

Copyable, current recipes:

- [route a trunk caller to a ring group](docs/AUTOMATION_DIALPLAN.md#route-only-providerpbx-trunk-calls-to-a-ring-group)
- [forward an unanswered phone to Assist](docs/AUTOMATION_DIALPLAN.md#forward-an-unanswered-ha-call-to-assist)
- [forward an unanswered phone to a mobile number](docs/AUTOMATION_DIALPLAN.md#forward-an-unanswered-call-to-a-mobile-number)
- [route a known caller according to presence](docs/AUTOMATION_DIALPLAN.md#route-a-known-caller-according-to-presence)
- [send an actionable mobile notification](docs/AUTOMATION_DIALPLAN.md#actionable-doorbell-notification)
- [notify a no-answer timeout](docs/AUTOMATION_DIALPLAN.md#notify-a-no-answer-timeout)

The assistant's personality is entirely up to your prompt. Professional
receptionist and verbally abusive domestic secretary are both technically
valid configurations.

## Automation routing preview

The phonebook is always the normal dial plan. Advanced HA automation routing is
an opt-in preview and is disabled by default.

Automations may act only at explicit points:

- initial `route_requested`, before the configured inbound fallback;
- a logical phone remaining `ringing` for a native HA `for:` duration;
- a connected call producing a negotiated DTMF event.

Explicit extension digits entered during the trunk DTMF window remain
authoritative and bypass the automation override. If an automation does
nothing, the configured fallback continues normally.

Use per-phone state and Event Entities for room-specific behavior. Use
`event.voip_stack_call` for the PBX-wide initial routing decision. The card's
visible text is not an automation source of truth.

Start from a complete recipe:

- [route calls during office hours](docs/AUTOMATION_DIALPLAN.md#route-to-reception-during-office-hours)
- [dial a phonebook extension with initial DTMF](docs/AUTOMATION_DIALPLAN.md#dial-a-phonebook-extension-during-initial-trunk-routing)
- [run an HA action from in-call DTMF](docs/AUTOMATION_DIALPLAN.md#open-a-gate-with-in-call-dtmf)

Read the full [automation cookbook and concurrency rules](docs/AUTOMATION_DIALPLAN.md)
for forwarding, presence routing, missed-call notifications and concurrent
call controls.

> [!WARNING]
> Automation routing semantics may still change as more real installations are
> tested. Do not use preview routing as the only control path for emergency or
> safety-critical access.

## Optional SIP trunk

The trunk is disabled by default. Enable it only when HA must register to a
provider or another PBX.

Inbound mode can route immediately or collect an internal extension through
negotiated RTP `telephone-event`/compatible SIP INFO DTMF. Outbound contacts
with public numbers use the same trunk. Explicit digits, no-digit fallback and
automation overrides have distinct, documented precedence.

See [`docs/SIP_TRUNK.md`](docs/SIP_TRUNK.md) before exposing the listener beyond
a trusted network.

## Installation

### Home Assistant through HACS

1. Search for **VoIP Stack** in HACS.
2. Open the integration and select **Download**.
3. Restart Home Assistant.
4. Open **Settings → Devices & services → Add integration**.
5. Select **VoIP Stack** and complete the config flow.

![Install VoIP Stack from HACS](docs/images/hacs-download-voip-stack.png)

The card is registered automatically. A normal LAN can keep SIP `5060` and RTP
base `40000`. Container, LXC, VPN and multi-subnet installs may need an explicit
reachable **Advertise host** and host networking.

Manual source and release-archive installation, port requirements and network
topologies are documented in the
[deployment guide](docs/DEPLOYMENT_GUIDE.md#home-assistant).

### ESPHome components

Use the maintained YAML whenever possible. A minimal custom external-component
declaration is:

```yaml
external_components:
  - source: github://n-IA-hane/esphome-voip-stack@main
    components: [voip_stack]
  - source: github://n-IA-hane/esphome-audio-stack@main
    components: [esp_audio_stack, esp_aec]
```

Replace `esp_aec` with `esp_afe` only when the profile actually uses the full
AFE pipeline. All maintained YAMLs point to the stable `main` branches.

After a major ESPHome or component upgrade, clear that device's ESPHome build
cache before compiling. The [deployment guide](docs/DEPLOYMENT_GUIDE.md)
contains the complete component and cache instructions.

## Upgrading

This project is maintained by one person and major releases may make deliberate
breaking changes. Maintaining old and new call engines or service semantics in
parallel is not sustainable; new features can require updates to automations,
dashboards, config entries or custom ESPHome YAML.

Before every upgrade:

1. read [`docs/BREAKING_CHANGES.md`](docs/BREAKING_CHANGES.md) and the release
   note;
2. update through HACS and restart HA;
3. run **Reconfigure** on the VoIP Stack integration and review every step;
4. verify phone/routing automations;
5. reset the frontend cache on dashboards or Companion sessions using the card;
6. clear the ESPHome build cache before rebuilding firmware after package
   changes.

Never assume an automation still has the same contract merely because the
integration loaded successfully.

## What's new in `2026.8.1`

`2026.8.1` is an interoperability and consolidation update:

- capability-gated G.722 on HA SIP legs, while ESP endpoints keep their native
  high-quality PCM path;
- automatic Dahua `PCM/16000` interoperability for matching registered
  door-station profiles;
- stronger SIP digest stale-nonce recovery and registered TCP-flow reuse;
- faster vectorized G.711 conversion on HA;
- smaller call-routing orchestrators with the existing single authoritative
  call lifecycle preserved;
- a substantially expanded, schema-checked automation cookbook.

The complete delta is in the
[`2026.8.1` pre-release](https://github.com/n-IA-hane/esphome-intercom/releases/tag/v2026.8.1).
The illustrated
[`2026.8.0` release](https://github.com/n-IA-hane/esphome-intercom/releases/tag/v2026.8.0)
remains the stable feature overview.

## Supported hardware

Ready profiles are examples of complete pin, codec and resource choices; they
are not a claim that every board with the same chip has the same wiring.

| Device/profile | Configuration | Status |
|---|---|---|
| Spotpear Ball v2 | [`spotpear-ball-v2-full-afe.yaml`](yamls/full-experience/single-bus/spotpear-ball-v2-full-afe.yaml) | Field tested |
| Waveshare ESP32-S3 Audio Board | [`waveshare-s3-full-afe.yaml`](yamls/full-experience/single-bus/waveshare-s3-full-afe.yaml) | Field tested |
| Waveshare ESP32-P4 Touch LCD, full JPEG videophone | [`waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml`](yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml) | Field tested |
| Waveshare ESP32-P4 Touch LCD, portrait | [`waveshare-p4-touch-full-afe-portrait.yaml`](yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml) | Experimental layout |
| Generic ESP32-S3, single bus | [`generic-s3-full-aec.yaml`](yamls/full-experience/single-bus/generic-s3-full-aec.yaml) | Reference profile |
| Generic ESP32-S3, dual bus | [`generic-s3-full-aec.yaml`](yamls/full-experience/dual-bus/generic-s3-full-aec.yaml) | Reference profile |
| Native ESPHome mic/speaker | [`generic-s3-full-esphome-native.yaml`](yamls/full-experience/esphome-native/generic-s3-full-esphome-native.yaml) | Reference profile |

The complete hardware, memory and C6 firmware notes are in the
[deployment guide](docs/DEPLOYMENT_GUIDE.md#esp-devices).

## Documentation

| Topic | Document |
|---|---|
| Choose and install a profile | [Deployment guide](docs/DEPLOYMENT_GUIDE.md) |
| Upgrade safely | [Breaking changes](docs/BREAKING_CHANGES.md) |
| Services, selectors and side effects | [Home Assistant services](docs/SERVICES.md) |
| Automations and contextual routing | [Automation cookbook](docs/AUTOMATION_DIALPLAN.md) |
| Ring and conference groups | [Groups](docs/GROUPS.md) |
| SIP video codecs and browser privacy | [SIP video](docs/SIP_VIDEO.md) |
| Provider/PBX registration | [SIP trunk](docs/SIP_TRUNK.md) |
| Names, extensions and route precedence | [Dial-plan resolver](docs/DIALPLAN_RESOLVER.md) |
| ESP/HA phonebook representation | [Phonebook protocol](docs/PHONEBOOK_PROTOCOL.md) |
| Expected signaling and media paths | [Call flows](docs/CALL_FLOWS.md) |
| Runtime ownership and architecture | [Architecture](docs/ARCHITECTURE.md) |
| Every option, trigger and condition | [Reference](docs/reference.md) |
| Logs, captures and qualification | [Testing and debug](docs/TESTING_AND_DEBUG.md) |
| Common failures | [Troubleshooting](docs/troubleshooting.md) |
| All documentation | [Documentation index](docs/README.md) |

## Testing

The repository maintains more than 1,100 automated tests plus real SIP, RTP,
browser, trunk and hardware qualification tools. A green unit suite is not
treated as proof of a complete call: release qualification also checks media
in both directions, remote/local hangup and final resource cleanup.

Developer commands and capture locations are in
[`docs/TESTING_AND_DEBUG.md`](docs/TESTING_AND_DEBUG.md).

## Troubleshooting

Start with [`docs/troubleshooting.md`](docs/troubleshooting.md). It covers:

- an ESP or browser phone that does not ring;
- unknown callers or route failures;
- `488 media_incompatible`;
- missing or one-way audio;
- SIP registration/trunk failures;
- hold, UPDATE and re-INVITE;
- stale card state or frontend cache.

When opening an issue, attach Home Assistant diagnostics and sanitized logs.
Remove passwords, tokens, public numbers, private addresses and SIP credentials.

## Support the project

If this work is useful, consider
[sponsoring it on GitHub](https://github.com/sponsors/n-IA-hane). Donations help
cover development tools, services and test hardware.

Bug reports and hardware feedback are welcome. Include the exact board, ESPHome
and HA versions, relevant YAML substitutions, the peer/PBX model, sanitized
SIP/SDP logs and whether audio/video worked in each direction.

## Contributing

Contributions should preserve standards-based SIP/SDP/RTP behavior and the
single authoritative call lifecycle. Avoid endpoint-specific timing
workarounds when a transaction, dialog, media or ownership invariant can solve
the underlying problem.

Before submitting a change, run the repository test environment documented in
[`docs/TESTING_AND_DEBUG.md`](docs/TESTING_AND_DEBUG.md).

## License

Project-owned code is licensed under the MIT License; see
[`LICENSE`](LICENSE).

External Espressif components and optional system codecs keep their own
licenses. They are consumed as upstream dependencies and are not copied into
the project license.
