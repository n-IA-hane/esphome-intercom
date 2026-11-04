# VoIP Stack for Home Assistant and ESPHome

**An independent, open-source SIP PBX running inside Home Assistant, installable through HACS.**

![VoIP Dashboard with incoming, active and held calls](docs/images/voip-dashboard-desktop.png)

_Dashboard demo showing incoming, active and held calls, presence, voicemail
and Do Not Disturb._

Connect SIP desk phones and softphones, receive audio and video from your SIP
doorbell, and answer in Home Assistant phone cards or the VoIP Dashboard.
Give each room, tablet or Companion app session its own phone and extension
for room-to-room and app-to-app calls.

Home Assistant runs the telephone system: SIP registration, shared contacts,
call routing and media. Use ring groups, conferences, transfers, parking,
pickup and voicemail, then add native HA automations for presence, schedules,
spoken menus, DTMF and unanswered calls. Provider and external PBX trunks are
optional.

- **Your phones:** standard SIP equipment and browser or Companion phones,
  with individual identities, settings and call state.
- **Your directory:** shared contacts, extensions, LDAP, busy lamps and
  voicemail message indicators on compatible phones.
- **Your call controls:** compact phone cards, an operator Dashboard and
  optional call popups over other Home Assistant views.
- **Your media:** audio and video negotiated for each endpoint, direct encoded
  video where compatible and optional conversion when the route needs it.

**[Install](#installation) · [Make your first call](#fastest-start) · [Choose a card](#cards-dashboard-and-global-call-popups) · [Automate calls](#automations-as-dialplan) · [ESP phones and videophones](#esp-endpoints-and-media-roles)**

*Developed and maintained by n-IA-hane, with community contributions and support. [❤️ Sponsor the project](https://github.com/sponsors/n-IA-hane)*

![Real Mobotix video call in the Home Assistant phone card](docs/images/pbx-mobotix-video-green.png)

_A real video call with a MOBOTIX door station in the VoIP Stack Home Assistant
card, with the local camera off. [Learn the phone controls](docs/USER_GUIDE.md#read-the-phone-card)
and [how to park and retrieve audio calls](docs/CALL_PARKING.md)._

![Doorbell video call demo in the VoIP Dashboard](docs/images/voip-dashboard-gate-demo.png)

_Demo of receiving a doorbell video call in the VoIP Dashboard, using an
illustrative gate-camera feed._

## What can you build?

![Video doorbell and room-to-room calls through Home Assistant](docs/images/voip-doorbell-room-to-room.png)

| Goal | What VoIP Stack provides | Start here |
|---|---|---|
| Video doorbell | Ring a browser or Companion phone, or a configured ESP display endpoint. Video direction and codecs follow the source, sink and board profile. | [SIP video](docs/SIP_VIDEO.md) · [door-station recipe](#door-station-and-unanswered-calls) |
| Room-to-room calls | Create a logical HA phone and dedicated dashboard view for each kiosk or tablet. Calls may be private audio or video. | [Logical phones](#logical-home-assistant-phones) |
| Existing SIP equipment | Register Zoiper, Linphone, baresip, an IP phone or an ATA directly to HA. | [Local accounts](docs/SERVICES.md#local-sip-endpoint-account-services) |
| Ring groups | Ring several eligible endpoints; the first answer wins and the losing branches are cancelled. | [Groups](docs/GROUPS.md#ring-group) |
| Audio conferences | Host a local audio conference in HA and optionally ring its members. | [Groups](docs/GROUPS.md#conference-group) |
| Callable Assist | Give a native Assist pipeline an extension and talk to it from ESP, SIP or trunk callers. | [Assist calls](#assist-as-a-phone-extension) |
| External calls | Connect an optional provider/PBX trunk using registration or source-IP authorization. | [SIP trunk](docs/SIP_TRUNK.md) |
| Contextual routing | Use native HA entities, conditions and services for presence, schedules, no-answer forwarding and in-call DTMF. | [Automation cookbook](docs/AUTOMATION_DIALPLAN.md) |

## Installation

Requires **Home Assistant 2026.7.0 or newer**. Choose the release channel that
matches the features and firmware you intend to use:

| Channel | Version | Choose it for |
| --- | --- | --- |
| [Stable](https://github.com/n-IA-hane/esphome-intercom/releases/tag/v2026.11.0) | **2026.11.0** | The stable PBX, phone cards and coordinated firmware from `main`. |
| [Development preview](https://github.com/n-IA-hane/esphome-intercom/releases/tag/v2026.11.1-dev.1) | **2026.11.1-dev.1** | The current product described below, including the Dashboard, global call popups and updated ESP media profiles. |

The [changelog](CHANGELOG.md) records release differences. Read the
[breaking changes](docs/BREAKING_CHANGES.md) when updating an existing setup.

### Home Assistant through HACS

1. Search for **VoIP Stack** in HACS and open the integration.
2. Choose **Download** and select your release. For an existing installation,
   use **Redownload** and choose the intended stable or preview version.
3. Restart Home Assistant.
4. Open **Settings > Devices & services > Add integration**.
5. Select **VoIP Stack** and complete the setup.

![Install VoIP Stack from HACS](docs/images/hacs-download-voip-stack.png)

The phone cards are registered automatically. An ordinary LAN can keep SIP
`5060` and RTP base `40000`. Container, LXC, VPN and multi-subnet installations
may need an explicit reachable **Advertise host** and host networking.
Browser audio requires HTTPS or another secure browser context and microphone
permission. Receiving video does not require camera permission.

HACS installs the HA integration and frontend. ESPHome and ESP hardware are
optional; adding or updating ESP firmware is a separate step. Manual source
installation and network requirements are in the
[deployment guide](docs/DEPLOYMENT_GUIDE.md#home-assistant).

## Fastest start

1. Install **VoIP Stack** from HACS and restart Home Assistant.
2. Add **VoIP Stack** from **Settings → Devices & services**.
3. Keep SIP `5060` and RTP base `40000` unless they conflict with your network.
4. Use the Home Assistant phone created during setup and add a second browser
   phone from the integration's phone configuration.
5. Add a VoIP Stack card for each phone. Select its Phone in the visual editor;
   you do not need to copy an internal device ID into YAML.
6. Open the phones in separate browser tabs or devices, allow microphone access,
   and call the other phone from the shared phonebook. Use HTTPS for browser audio.

That first call requires Home Assistant and the cards. To connect a desk phone,
softphone or ATA, [create a local SIP account](docs/SERVICES.md#local-sip-endpoint-account-services).
Groups, Assist, automations and a provider trunk can be added independently.

Build outward from that first call: add a SIP handset or door station, create
a ring group for several rooms, choose an unanswered-call rule, and connect a
provider only when you need external calls. The
[user guide](docs/USER_GUIDE.md) walks through each step.

## PBX calling features

Phones, routing rules and mailboxes belong to Home Assistant. A card controls
one phone; closing that card leaves its identity and configured routing in
place. Browser media still needs a running receiving frontend.

| Feature | What you can do | Guide |
| --- | --- | --- |
| SIP registration | Register a desk phone, softphone or ATA directly to HA with its own account. | [Local SIP accounts](docs/SERVICES.md#local-sip-endpoint-account-services) |
| Forwarding and no-answer rules | Set a ringing timeout, forward all calls or choose busy and unavailable destinations. Native automations can select a different outcome for their call. | [Phone routing](docs/PHONE_ROUTING.md) |
| Ring groups | Alert several eligible phones; the first answer takes the call. | [Groups](#groups) |
| Audio conferences | Join several callers in an HA-hosted audio conference. | [Conferences](docs/GROUPS.md#conference-group) |
| Transfers | Send an established call elsewhere with a blind transfer, or consult first from an attended-transfer-capable SIP phone. | [Transfers](docs/USER_GUIDE.md#forward-or-transfer-a-call) |
| Parking and directed pickup | Park an audio call and retrieve it by slot number, or answer another phone that is still ringing. | [Parking and pickup](docs/CALL_PARKING.md) |
| Voicemail | Give each phone a mailbox, greeting and no-answer rule. Listen through the card, HA Media or a SIP voicemail menu; use native actions to transcribe or deliver recordings. | [Voicemail](docs/VOICEMAIL.md) |
| Message waiting | Receive per-account new/old message counts through standard SIP MWI on compatible phones. | [Voicemail indicators](docs/VOICEMAIL.md#access-from-a-telephone) |
| Busy lamps and presence | Monitor eligible phones with standard SIP subscriptions, using the same HA call state as the cards and Dashboard. | [Busy lamps](docs/USER_GUIDE.md#busy-lamps-on-sip-phones) |
| DTMF and announcements | Send keypad digits to door stations and menus, or use received digits and spoken prompts in an HA call flow. | [Automation cookbook](docs/AUTOMATION_DIALPLAN.md) |
| Conversation recording | Save a conversation through HA actions or the optional card control. | [Recording](docs/CALL_RECORDING.md) |
| Named trunks | Connect independent provider or PBX lines and select the outgoing line by phone, action or default. | [Trunks](#optional-sip-trunk) |

Start with the [illustrated PBX walkthrough](docs/PBX_WORKFLOWS.md) for controls
and practical call flows. Parking is for established audio calls; audio
conferences do not carry video.

## How it works

Home Assistant is the SIP registrar, call router and media bridge. Browser
phones, standard SIP equipment and optional ESP phones use the same shared
directory and backend call model. An external PBX can connect through a trunk.

A logical phone supplies the persistent identity, extension and call settings.
Its receiving browser or Companion app supplies the microphone, speaker and
optional camera. A card, Dashboard or popup presents that same phone's call;
opening another view does not create another SIP or media lifecycle.

![Home Assistant as a local SIP and PBX hub](docs/images/home-assistant-local-sip-pbx.png)

Logical phones share the SIP listener and RTP pool. Browser and ESP phones
handle one active call at a time; registered SIP phones can hold a call and
start a consultation for an attended transfer. A phone remains occupied until
all its calls end, while calls to different phones remain independent.

See the [architecture](docs/ARCHITECTURE.md) for ownership and media, or the
[call flows](docs/CALL_FLOWS.md) for direct and HA-bridged sequences.

## Logical Home Assistant phones

The integration creates one ordinary Home Assistant browser phone on first
setup, named from Home Assistant's location. Add or remove phones
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

Omit `device_id` only when a preferred phone is configured or exactly one
compatible phone exists. Otherwise the action asks you to select the local
phone explicitly.

The card's idle `Options` panel includes a `Microphone anti-alias filter`.
It is enabled by default and stored per browser and logical phone. The setting
takes effect when the next call opens the microphone and only adds processing
when the negotiated transmit rate is lower than the browser capture rate.

The same panel selects the browser microphone, speaker and camera. These
preferences belong to the browser or Companion App session, are shared by all
VoIP Stack cards in that session and remain unchanged when no device is
selected. During a call, the tune button beside Hangup opens the same controls,
so a headset or camera can be changed without ending the call. Unsupported
speaker routing remains under operating-system control.

Each room-to-room media endpoint needs a distinct browser or Companion session.
Two cards in one browser tab can display two phones, but one tab still owns one
physical microphone, speaker and camera pipeline.

![Dashboard demo](docs/images/dashboard.gif)

_One Home Assistant dashboard can control browser and ESP phones alongside the
shared phonebook. Each phone retains its own identity and call state._

## Cards, dashboard and global call popups

There are two public Lovelace card types. Choose the calling phone by name in
the visual editor; Home Assistant stores its `device_id` for you.

| Card type | Mode | Purpose |
| --- | --- | --- |
| `custom:voip-stack-card` | `ha_softphone` | A browser or Companion phone with its own microphone, speaker and optional camera. Select an existing logical HA phone. |
| `custom:voip-stack-card` | `esp_mirror` | Control one physical ESP phone through its own call, answer and hangup actions. The ESP owns the call media. |
| `custom:voip-stack-card` | `phonebook` | A scrollable view of the shared directory. It does not create another phone. |
| `custom:voip-stack-dashboard-card` | No mode field | Shared extension status, contacts and call controls for the HA or ESP phone selected by `device_id`. |

For example, a Dashboard bound to an existing phone uses:

```yaml
type: custom:voip-stack-dashboard-card
device_id: <phone_device_id>
```

The Dashboard and compact card use the same phone, call state and controls.
Opening another view does not create another SIP account or media engine.

**Global call popups** are an optional presentation of an existing HA browser
phone. On that phone's HA Device page, choose **Visit**, or
open the phone card's **Options > Call popups**. Select the existing phone,
enable **Receive call popups on this device**, and save. This choice persists
in the current browser or Companion app; configure each receiving device
separately. It does not associate the phone with an HA user.

<table>
  <tr>
    <td width="50%"><img src="docs/images/voip-popup-settings.png" alt="Optional call-popup settings for an existing Home Assistant phone"/></td>
    <td width="50%"><img src="docs/images/voip-popup-ringing.png" alt="Incoming Casa call displayed over another Home Assistant view"/></td>
  </tr>
</table>

An incoming call can then open over another HA view without a phone card in
the foreground. **X**, **Esc** and **Back** minimize the call into a small
caller/timer bar; tap it to reopen. Use **Decline** or **Hang up** to end the
call. The popup shares the existing phone's audio, ringtone and Call-ID.

<img src="docs/images/voip-popup-minimized.png" width="113" alt="Minimized call with caller, duration and explicit hangup button"/>

<details>
<summary>Video popup demonstration</summary>

![Video in the global call popup](docs/images/voip-popup-video-demo.png)

_Demo of receiving a doorbell video call in the global popup, using an
illustrative gate-camera feed._

</details>

The HA frontend must remain running and have the required media permissions.
Logical ringing, routing and no-answer rules remain available when the browser
is closed, but browser audio and auto-answer require an active receiving client
with its card loaded or global popups enabled. Mobile systems can suspend that
client in the background.

The Companion app uses its existing WebView. An
[actionable notification](docs/AUTOMATION_DIALPLAN.md#actionable-doorbell-notification)
can open the intended phone and answer the identified call. This does not add
an operating-system telephone channel or native incoming-call screen.
See the [popup setup and controls](docs/USER_GUIDE.md#receive-calls-over-other-home-assistant-views).

## Home Assistant as a SIP video phone

Answer a SIP video doorbell or call another video phone from the HA phone
card, Dashboard or popup in a compatible browser or the Companion app.
Audio and video directions are negotiated independently: you can receive the
doorbell's camera while keeping your own camera off.

Enable **SIP video for the Home Assistant softphone** in the integration's
**Reconfigure** flow. Receiving video does not require camera permission.
**Allow browser camera transmission** exposes the per-phone **Send Camera**
choice; transmitting still requires permission in the receiving browser.

H.264, VP8 and RTP/JPEG use compatible encoded paths where possible. Optional
FFmpeg transcoding supports selected legacy receive codecs and conversion
between incompatible H.264/JPEG SIP legs. Enabling conversion does not force
all video through a decoder: a compatible encoded path stays preferred.
Audio remains usable when video is unavailable or deliberately disabled.

Compatibility depends on the negotiated codec, packetization, image geometry
and endpoint decoder. See the [codec matrix and privacy controls](docs/SIP_VIDEO.md)
for each browser, SIP and ESP path, including audio-first calls that add video.
An [ESP videophone](#esp-endpoints-and-media-roles) supplies its own camera and
screen; audio-only endpoints and conferences remain audio-only.

## Phonebook and routing

Home Assistant keeps one persistent phone directory and publishes its public
roster through `sensor.voip_phonebook` and the standard LDAP directory service.
Compatible managed ESP phones use LDAP for their shared phonebook. It combines:

- ESPHome VoIP identities, including offline managed phones;
- logical Home Assistant phones;
- configured local SIP accounts;
- manual contacts;
- Assist destinations;
- dynamic ring/conference groups;
- optional trunk-routed numbers.

Phones keep stable identities when their displayed name or extension changes.
A name or `extension` selects an internal destination; a `number` is normally
routed through a trunk. The resolver can also handle canonical SIP URIs.
Directory entries distinguish phones, groups and services. Contact metadata
contains no SIP passwords, and only eligible phone entries become automatic
BLF targets.

Phonebook names must be unique across phones, contacts and Assist pipelines,
even when their extensions differ. Duplicate names produce an error log and
a persistent Home Assistant notification identifying the conflicting entries.
Rename one destination; the notification clears when the phonebook updates.

LDAP provides directory records; live busy/idle and presence updates use SIP
subscriptions. A registered handset can subscribe to `dialog` for BLF,
`presence` for availability and `message-summary` for voicemail counts.
These are separate services; a directory entry does not by itself prove that a
phone is connected or that an external PBX extension is being monitored.

SIP routing identity and presentation identity remain separate. ESPHome node
names and account usernames provide stable URI users, while friendly names are
sent as standard SIP display names with spaces preserved. Incoming caller text
comes from the peer's `From` header, and the answering endpoint can publish its
resolved name through RFC 4916 connected identity.

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

## Optional SIP trunk

Local calls need no trunk. Add a named line to connect HA to a provider or another
PBX. Use the integration's **Add SIP trunk** menu
and edit each line separately. Keep **Register the trunk** enabled for account
registration, or disable it for a static trunk and configure its allowed
incoming SIP source IPs.

![Add SIP trunk in the VoIP Stack integration menu](docs/images/pbx-add-trunk-menu-dark.png)

Inbound mode can route immediately or collect an internal extension through
negotiated RTP `telephone-event`/compatible SIP INFO DTMF. Outbound contacts
with public numbers use the selected outgoing line. Explicit digits, no-digit fallback and
automation overrides have distinct, documented precedence.

Use **Set external call access** to allow or block trunk calls per phone.
See the [automation editor walkthrough](docs/AUTOMATION_DIALPLAN.md#control-external-calls-per-phone).

See [`docs/SIP_TRUNK.md`](docs/SIP_TRUNK.md) before exposing the listener beyond
a trusted network.

## Assist as a phone extension

There are two separate Assist-related features:

| Feature | Where it runs | How it starts |
|---|---|---|
| ESP Voice Assistant | On a full-experience ESP device | Local wake word, touch control or an ESPHome action. |
| Callable HA Assist | In Home Assistant through VoIP Stack | A SIP caller dials its phonebook name or extension. |

Enable **Include voice assistant** while configuring VoIP Stack, choose the
pipeline and assign an extension. ESPs, registered SIP phones, browser phones
and trunk callers can then call that Assist pipeline like any other contact.

This is a Home Assistant PBX feature. The SIP caller talks to the selected HA
Assist pipeline over the call. It does not start, replace or change the local
Voice Assistant running on a full-experience ESP device.

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

## Automations as dialplan

**Automations as dialplan** lets you define call behavior in Home Assistant's
normal automation editor. Create an Automation contact, then build a sequence:
**greeting, then forward to a phone or voice assistant**. Add a keypad
menu, presence condition or office-hours rule when you need it.

**Phonebook as dialplan** remains the default when no routing automation overrides it.
Native VoIP triggers select the
call for the actions automatically, including across waits and synchronous
scripts. No blueprint, generic event filter or Call-ID template is needed for
the common examples. A destination can be its phonebook name or extension.

Start with the [step-by-step greeting](docs/AUTOMATION_DIALPLAN.md#create-your-first-greeting-in-the-editor),
then add a [forward after the greeting](docs/AUTOMATION_DIALPLAN.md#forward-after-the-greeting).

- [Route calls during office hours](docs/AUTOMATION_DIALPLAN.md#route-to-reception-during-office-hours).
- [Forward only when a phone does not answer](docs/AUTOMATION_DIALPLAN.md#forward-an-unanswered-ha-call-to-assist).
- [Build a keypad menu](docs/AUTOMATION_DIALPLAN.md#build-a-small-keypad-menu).
- [Show received keys in HA notifications](docs/AUTOMATION_DIALPLAN.md#see-received-digits-in-persistent-notifications).
- [Run a gate action from in-call DTMF](docs/AUTOMATION_DIALPLAN.md#open-a-gate-with-in-call-dtmf).

Explicit extension digits entered during the trunk's initial DTMF menu retain
precedence over a generic routing override. With no applicable automation
selection, the configured phonebook route continues. Existing Event Entity
and state automations remain available; see the
[migration guide](docs/AUTOMATION_DIALPLAN.md#move-existing-automations-gradually).

The full [Automations as dialplan cookbook](docs/AUTOMATION_DIALPLAN.md) explains
call completion, fallback, time limits, concurrent callers and troubleshooting.

## ESP endpoints and media roles

ESP firmware is an optional part of the project, with maintained VoIP-only
and FULL profiles. It turns supported ESP32 boards into standards-based SIP
phones and full videophones. With a microphone, speaker, camera and display, a supported
videophone profile can make and receive audio and video calls, transmit its
camera and show the remote caller. Call Home Assistant phones, other ESP
phones, standard SIP phones and SIP door stations through the same system.

ESP phones support registered SIP accounts, multiple lines, LDAP contacts,
BLF states, MWI and DTMF for voicemail menus when enabled in their profile.
They can use VoIP Stack for Home Assistant or a
[compatible external PBX](#use-esp-firmware-with-an-existing-pbx).
Optional FULL firmware also combines local Assist, Micro Wake Word, music
and TTS with VoIP.

ESP media roles are derived from the configured components; they are not a
separate user mode.

| Derived role | Media | Typical use |
|---|---|---|
| `full_duplex` | microphone TX + speaker RX | Room phone, door station, wall panel |
| `mic_only` | microphone TX | Monitor or capture endpoint |
| `speaker_only` | speaker RX | Paging or announcement target |

An endpoint must have at least one real media direction. ESP VoIP uses PCM
for audio. Audio-only profiles omit video resources. A supported S3 display
can receive RTP/JPEG through the standalone JPEG renderer; an S3 camera can
supply a separate JPEG transmit path. P4 videophone profiles select JPEG or
H.264, with the FULL P4 profile using JPEG.

The PBX negotiates audio and video independently in each direction. It prefers
compatible formats before using supported conversion between different
contracts. An S3 JPEG sink does not decode H.264; HA can convert a negotiated
upstream H.264 stream to JPEG when video transcoding is enabled.

<table>
  <tr>
    <td align="center"><img src="docs/images/call-from-esp-to-homeassistant.gif" width="240"/><br/><b>ESP to Home Assistant</b></td>
    <td align="center"><img src="docs/images/ha-sip-video-call.gif" width="240"/><br/><b>SIP video call</b></td>
  </tr>
</table>

Audio component details live in the companion projects:

- [`esp_audio_stack`](https://github.com/n-IA-hane/esphome-audio-stack)
- [`esp_aec`](https://github.com/n-IA-hane/esphome-audio-stack/tree/main/esphome/components/esp_aec)
- [`esp_afe`](https://github.com/n-IA-hane/esphome-audio-stack/tree/main/esphome/components/esp_afe)
- [`voip_stack`](https://github.com/n-IA-hane/esphome-voip-stack)
- [`runtime_controller`](https://github.com/n-IA-hane/esphome-runtime-controller)

## Full ESP voice experience

The [`yamls/full-experience/`](yamls/full-experience/) profiles are complete
ESPHome voice devices, not merely larger SIP configurations. They combine the
VoIP endpoint with an independent local Voice Assistant stack and coordinate
all realtime consumers through one audio and runtime ownership model.

Maintained video profiles pause Micro Wake Word during a video call and resume
it afterward when Wake Word remains enabled and its normal start conditions
permit. This preserves the saved Wake Word setting.

The underlying audio stack is also reusable without VoIP. Custom ESPHome voice
devices can use it to share codec or I2S ownership, speaker reference, AEC/AFE
processing and a cleaned microphone stream between their own consumers.

The [illustrated microphone wiring guide](https://github.com/n-IA-hane/esphome-audio-stack/blob/main/README.md#microphone-wiring-and-delivery-illustrated)
shows single-mic, stereo L/R, TDM and codec layouts, with YAML for independent
AFE/BSS inputs or optional mono downmix before AEC/AFE.

<table>
  <tr>
    <td align="center"><img src="docs/images/assistant-animated.gif" width="220"/><br/><b>Animated assistant</b></td>
    <td align="center"><img src="docs/images/assistant-speaking.jpg" width="220"/><br/><b>Assistant response</b></td>
    <td align="center"><img src="docs/images/lvgl-audio-volume.jpg" width="220"/><br/><b>Runtime audio controls</b></td>
    <td align="center"><img src="docs/images/lvgl-hangup-reason.jpg" width="220"/><br/><b>Call end reason</b></td>
  </tr>
  <tr>
    <td align="center"><img src="docs/images/assistant-happy.jpg" width="220"/><br/><b>Positive mood</b></td>
    <td align="center"><img src="docs/images/assistant-neutral.jpg" width="220"/><br/><b>Neutral mood</b></td>
    <td align="center"><img src="docs/images/assistant-angry.jpg" width="220"/><br/><b>Negative mood</b></td>
    <td align="center"><img src="docs/images/afe-controls.png" width="220"/><br/><b>AFE controls in HA</b></td>
  </tr>
</table>

<table>
  <tr>
    <td align="center"><img src="docs/images/p4-touch-overview.jpg" alt="Native P4 screen capture with weather, Voice Assistant and the SIP dial pad" width="640" style="max-width: 100%; height: auto;"/><br/><b>P4 native screen: weather, Voice Assistant and VoIP</b></td>
    <td align="center"><img src="docs/images/ducking-barge-in.gif" width="260"/><br/><b>Ducking and barge-in</b></td>
  </tr>
</table>

The full profiles provide:

- Micro Wake Word on the cleaned post-AEC microphone stream, coordinated with
  calls and other runtime activity;
- a native ESPHome Voice Assistant started by wake word or touch;
- media playback, announcements, TTS, ringtones and optional Sendspin through
  one shared speaker path;
- ducking and barge-in, including interruption of a current assistant reply;
- VoIP calls that coexist with the assistant instead of creating a second
  microphone or speaker owner;
- AEC or AFE processing, with runtime controls and diagnostics where supported;
- coordinated LEDs, display pages, timers, call state and media activity
  through `runtime_controller`;
- headless operation on audio boards, plus full LVGL interfaces on supported
  displays;
- optional animated and mood-aware assistant artwork on display profiles.

The shared pipeline matters because Micro Wake Word, Voice Assistant and VoIP
all need the same cleaned user speech while music, TTS or a ringtone may still
be playing. Speaker output also supplies the phase-coherent reference used by
AEC instead of letting each feature open its own audio path.

![Shared music, TTS, wake word, Voice Assistant and VoIP audio pipeline](docs/images/shared-audio-aec-pipeline.png)

### Assistant artwork and avatars

Display profiles can select an assistant avatar with the `ai_avatar`
substitution. Each avatar directory may provide idle animation frames plus
listening, thinking, loading, error, timer and mood images. The selected assets
are resized for the target display during the build.

```yaml
substitutions:
  ai_avatar: my_assistant
```

The optional [`starfleet` community avatar](assets/images/assistant/starfleet/README.md)
was contributed by [rvdv01](https://github.com/rvdv01) in
[discussion #110](https://github.com/n-IA-hane/esphome-intercom/discussions/110).
Select it with `ai_avatar: starfleet`; maintained profiles keep their default.

This artwork represents the ESP device's own Voice Assistant state. It is
unrelated to the optional HA Assist pipeline that SIP callers can dial as an
extension.

See the [deployment guide](docs/DEPLOYMENT_GUIDE.md) for profile selection and
the companion [audio stack documentation](https://github.com/n-IA-hane/esphome-audio-stack)
for AEC, AFE, I2S and codec topology details.

### Add an ESP phone when you need one

Building the maintained firmware requires **ESPHome 2026.10.0 or newer**.

1. Choose the YAML for your exact board from the
   [device guide](yamls/README.md). Keep its supplied wiring; change GPIOs only
   when your physical wiring differs or you use a generic profile.
2. Add the flashed device through the normal ESPHome integration.
3. Call its phonebook name from another endpoint. An optional ESP mirror card
   controls that physical phone.

The full ESP experience is a separate firmware choice that adds local voice
and media features to an ESP endpoint.

| Device goal | Maintained starting point |
|---|---|
| ESP VoIP only | [`yamls/voip-only/`](yamls/voip-only/) |
| Local Voice Assistant + MWW + media + TTS + optional Sendspin + VoIP | [`yamls/full-experience/`](yamls/full-experience/) |
| Native ESPHome mic/speaker paths | [`yamls/voip-only/esphome-native/`](yamls/voip-only/esphome-native/) |
| New or unqualified hardware | [`yamls/experimental/`](yamls/experimental/) |

The reorganized firmware profiles select one device package and one
`firmware_features` list. Follow the [progressive package examples](packages/README.md#work-through-a-full-profile)
to remove Sendspin, build a voice-only device, or combine voice, calls and music.
Keep the selected release's package and component sources coordinated. The
current preview uses the official ESPHome
audio, mixer, resampler, player, Voice Assistant, wake-word and OTA providers,
with our Audio Stack/AEC/AFE, VoIP Stack and Runtime Controller. Follow the
[native provider migration](docs/BREAKING_CHANGES.md#development-firmware-native-audio-providers)
when carrying over old external-component lists. For an older copied configuration, follow the
[firmware migration](docs/BREAKING_CHANGES.md#development-firmware-new-device-packages)
instead of stacking new packages on top of the old preset.

The [user guide](docs/USER_GUIDE.md) continues from installation through cards,
calls, SIP accounts, groups, Assist, trunks and diagnostics. The
[deployment guide](docs/DEPLOYMENT_GUIDE.md) explains how to choose between
single-bus, dual-bus, lightweight AEC and full AFE profiles.

## Supported hardware

Ready profiles are examples of complete pin, codec and resource choices; they
are not a claim that every board with the same chip has the same wiring.

| Device/profile | Configuration | Status |
|---|---|---|
| Spotpear Ball v2 | [`spotpear-ball-v2-full-afe.yaml`](yamls/full-experience/single-bus/spotpear-ball-v2-full-afe.yaml) | Field tested |
| Waveshare ESP32-S3 Audio Board | [`waveshare-s3-full-afe.yaml`](yamls/full-experience/single-bus/waveshare-s3-full-afe.yaml) | Field tested |
| Waveshare ESP32-S3 Touch LCD 1.85C-BOX V2 | [`waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml`](yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml) | Supported FULL AEC/downmix or dual-microphone AFE |
| Waveshare ESP32-P4 Touch LCD, full JPEG videophone | [`waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml`](yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml) | Field tested |
| Waveshare ESP32-P4 Touch LCD, portrait | [`waveshare-p4-touch-full-afe-portrait.yaml`](yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml) | Experimental layout |
| Generic ESP32-S3, single bus | [`generic-s3-full-aec.yaml`](yamls/full-experience/single-bus/generic-s3-full-aec.yaml) | Reference profile |
| Generic ESP32-S3 with DVP JPEG camera | [`generic-s3-full-aec-camera.yaml`](yamls/full-experience/single-bus/generic-s3-full-aec-camera.yaml) | Reference wiring for Freenove FNK0085 N8R8; camera transmission |
| Generic ESP32-S3, single bus, 4 MB flash | [`generic-s3-full-lite-aec.yaml`](yamls/full-experience/single-bus/generic-s3-full-lite-aec.yaml) | Experimental; limited OTA headroom |
| Generic ESP32-S3, dual bus | [`generic-s3-full-aec.yaml`](yamls/full-experience/dual-bus/generic-s3-full-aec.yaml) | Reference profile |
| Native ESPHome mic/speaker | [`generic-s3-full-esphome-native.yaml`](yamls/full-experience/esphome-native/generic-s3-full-esphome-native.yaml) | Reference profile |

The complete hardware, memory and C6 firmware notes are in the
[deployment guide](docs/DEPLOYMENT_GUIDE.md#esp-devices).

S3 camera profiles can transmit JPEG video. Receiving JPEG on a supported S3
display requires the separate receive renderer and matching display package;
a camera profile alone does not supply that path. S3 does not decode H.264.
Supported P4 videophone profiles transmit their camera and display the remote
video. Devices with both a microphone and speaker retain bidirectional audio.
See the [SIP video guide](docs/SIP_VIDEO.md).

FULL profiles using `esp_aec` or `esp_afe` start in `sr_high_perf` and offer
`sr_low_cost` as the lower-memory alternative. The processor and mode selector
use the same initial-mode setting. VoIP-only profiles retain their separate
FD/VoIP choices and standalone callbacks; the native ESPHome profile adds no
external DSP mode. These defaults do not change microphone calibration or
wake-word gain. Where mode restoration is enabled, the restored SR choice is
applied before the processor starts, so the selector matches the running DSP.

The experimental Generic Full Lite AEC profile keeps Voice Assistant, wake
word, software AEC, VoIP, HTTP playback, TTS, timers and Runtime Controller.
It uses size optimization, requires PSRAM and leaves Sendspin disabled. The
preview uses a dedicated dual-OTA partition table supplied beside its YAML;
keep that CSV with the configuration. Moving from the stock layout requires
a serial factory flash, which resets stored preferences.
Check the actual compiled image against the selected partition table; older
build sizes do not establish current OTA headroom. Follow the
[firmware migration instructions](docs/BREAKING_CHANGES.md#development-firmware-new-device-packages)
when replacing a copied profile. The full 8 MB profile retains Sendspin.

[![Platform](https://img.shields.io/badge/Platform-ESP32--S3%20%7C%20ESP32--P4-blue.svg)](#supported-hardware)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-native-blue.svg)](https://www.home-assistant.io)
[![ESPHome](https://img.shields.io/badge/ESPHome-2026.10.0%2B-18bcf2.svg)](https://esphome.io)

### ESPHome components

Use the maintained YAML whenever possible. For the coordinated
firmware described here, a minimal custom external-component declaration is:

```yaml
external_components:
  - source: github://n-IA-hane/esphome-voip-stack@main
    components: [voip_stack]
  - source: github://n-IA-hane/esphome-audio-stack@main
    components: [esp_audio_stack, esp_aec]
```

Replace `esp_aec` with `esp_afe` only when the profile actually uses the full
AFE pipeline. The maintained profiles use coordinated `main` sources. Keep the
sources supplied with the selected profile rather than mixing release generations.

After a major ESPHome or component upgrade, clear that device's ESPHome build
cache before compiling. The [deployment guide](docs/DEPLOYMENT_GUIDE.md)
contains the complete component and cache instructions.

### Development preview firmware

For the current development preview, start from the maintained YAML for your
exact board and use its matching device package from `@dev`. Override all applicable
component sources together in the existing `substitutions` block:

```yaml
substitutions:
  ext_components_source: github://n-IA-hane/esphome-intercom@dev
  voip_stack_components_source: github://n-IA-hane/esphome-voip-stack@dev
  audio_stack_components_source: github://n-IA-hane/esphome-audio-stack@dev
  runtime_controller_components_source: github://n-IA-hane/esphome-runtime-controller@dev
  esp_video_renderer_components_source: github://n-IA-hane/esphome-video-renderer@dev
  camera_components_source: github://n-IA-hane/esphome-esp-video-camera@dev
  esp_screenshot_components_source: github://n-IA-hane/esphome-screenshot@dev
  intercom_idf_ref: dev
```

The renderer, camera and screenshot sources apply only when the selected
profile loads those components. `intercom_idf_ref: dev` selects the P4 PPA
rotation fix. For example, the P4 landscape FULL JPEG profile keeps its
existing package path and changes only its ref:

```yaml
packages:
  device: github://n-IA-hane/esphome-intercom/packages/devices/waveshare-p4-full-landscape.yaml@dev
```

Keep the selected board's wiring, secrets and feature list. The shipped
`@main` defaults remain the stable path; this block is an explicit preview
selection. Updating HACS does not rebuild or upload ESP firmware.

## Use ESP firmware with an existing PBX

An ESP running VoIP Stack can use manually configured REGISTER accounts with
a conventional PBX, or operate as a static SIP peer. Multiple named accounts
can coexist with the managed HA line; select the line for the call you want to
place. This does not require HA provisioning for the manual account.

Use [registered ESP accounts](docs/ESP_REGISTRATION.md) for credentials, line
selection and persistence. For the static example below, the PBX knows the
ESP's address, so that route does not require REGISTER or account credentials
on the firmware. A registered desk phone or softphone can then dial its extension.

For example, reserve extension **1001** on the PBX and associate it with the
ESP's fixed address, such as `192.168.1.60:5060`. On Asterisk, a dialplan entry
then calls the configured static endpoint:

```ini
exten => 1001,1,Dial(PJSIP/esp-bedroom,30)
 same => n,Hangup()
```

The endpoint/AOR configuration is included in the guide below. Baresip is one
way to test the calls; it is not required.

For calls in the other direction, add a PBX destination to the firmware's
existing phonebook:

```yaml
voip_stack:
  static_contacts:
    - name: "700"
      ip: 192.168.1.20
      port: 5060
      transport: udp
```

This calls extension `700` on the PBX at `192.168.1.20`. The PBX must also have
a static endpoint for the ESP and compatible audio settings. The device can
retain its Assist and media features through ESPHome, or use a VoIP-only profile.

[Set up an ESP with a classic PBX](docs/CLASSIC_PBX.md) includes the firmware
settings, Asterisk and FreeSWITCH static-extension examples, ordinary SIP-phone
calls, and the measured Asterisk 16 kHz microphone / 48 kHz playback setup.

## Upgrading

1. Read the target release notes and follow any migration instructions that
   apply to your installed version.
2. Update the integration through HACS and restart Home Assistant.
3. When the card changes, reload the dashboard. Reset the frontend cache if the
   browser or Companion app continues to show the previous card.
4. Rebuild and upload ESPHome firmware when adopting firmware-side changes.
   Follow the release's component references and clean-build instructions where
   specified. Updating HA through HACS does not flash the ESP devices.

Reconfiguration and edits to existing automations or YAMLs are needed only when
the release instructions identify a relevant change.

For release history, use the [changelog](CHANGELOG.md) and
[release notes](https://github.com/n-IA-hane/esphome-intercom/releases).

## Documentation

For custom firmware composition, see [Modular device packages](packages/README.md).

Start with the practical [user guide](docs/USER_GUIDE.md) for normal setup and
daily operation. Use the [automation cookbook](docs/AUTOMATION_DIALPLAN.md) for
redirect, fallback, DTMF and guarded concurrent routing, and the
[SIP feature reference](docs/WHATS_NEW_2026_8_1.md) for the established SIP and PBX
capabilities.

| Topic | Document |
|---|---|
| Choose and install a profile | [Deployment guide](docs/DEPLOYMENT_GUIDE.md) |
| Upgrade safely | [Breaking changes](docs/BREAKING_CHANGES.md) |
| Services, selectors and side effects | [Home Assistant services](docs/SERVICES.md) |
| Automations and contextual routing | [Automation cookbook](docs/AUTOMATION_DIALPLAN.md) |
| Ring and conference groups | [Groups](docs/GROUPS.md) |
| Phone mailboxes, MWI and message automations | [Voicemail](docs/VOICEMAIL.md) |
| SIP video codecs and browser privacy | [SIP video](docs/SIP_VIDEO.md) |
| Provider/PBX registration | [SIP trunk](docs/SIP_TRUNK.md) |
| Dahua registered or static door station | [Dahua setup and audio comparison](docs/DAHUA.md) |
| Names, extensions and route precedence | [Dial-plan resolver](docs/DIALPLAN_RESOLVER.md) |
| Shared directory, LDAP and ESP/HA phonebook | [Phonebook protocol](docs/PHONEBOOK_PROTOCOL.md) |
| Registered ESP lines | [ESP accounts](docs/ESP_REGISTRATION.md) |
| Current versions and past releases | [Changelog](CHANGELOG.md) |
| Expected signaling and media paths | [Call flows](docs/CALL_FLOWS.md) |
| Runtime ownership and architecture | [Architecture](docs/ARCHITECTURE.md) |
| Every option, trigger and condition | [Reference](docs/reference.md) |
| Logs, captures and qualification | [Testing and debug](docs/TESTING_AND_DEBUG.md) |
| Common failures | [Troubleshooting](docs/troubleshooting.md) |
| All documentation | [Documentation index](docs/README.md) |

## Testing

The repository maintains automated tests plus real SIP, RTP, browser, trunk
and hardware qualification tools. A green unit suite is not
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

For SIP call setup or hangup problems, the **`voip_stack.capture_sip`** action
can produce a Wireshark-readable trace directly from HA, including HA OS,
without SSH or tcpdump. Start it, reproduce the problem, then stop and download
the PCAP from the action response. See the
[step-by-step SIP capture guide](docs/troubleshooting.md#capture-sip-signaling-from-home-assistant-including-ha-os)
for examples, memory limits and what the trace can diagnose. Requires a release
containing this action.

## Before opening an issue

**Report the parts you actually use.** If the issue involves only HA, browser
cards or SIP equipment without an ESP, provide the HA-side information below
and skip the ESP requirements. ESP dumps apply only to ESP-related faults;
Audio Diagnostics additionally requires Audio Stack on that device.

Reproduce the issue with the current maintained release/profile and report the
exact versions actually running, including ESP firmware when an ESP is involved.
An updated YAML
on your computer does not update a device until it is rebuilt and uploaded.

Include:

- Steps to reproduce, expected behavior, actual behavior and how often it happens.
- HA and VoIP Stack versions; for ESP devices, ESPHome and component tags/commits.
- Exact board, microphone/speaker and codec models, relevant YAML and custom wiring.
- The call path: who calls whom, directly or through HA, SIP UDP/TCP, and PBX or
  peer software/version. Include negotiated codecs and `ptime` if known.
- HA integration diagnostics and logs around the failure, plus the ESP boot log.
- For an updated ESP profile, complete **VoIP Diagnostics** log output during the
  fault and after hangup. Add **Audio Diagnostics** when using Audio Stack.
  Open the ESPHome logs before pressing the buttons, use INFO or a more verbose
  logger level, and include whole diagnostic blocks, not only the last line.
- A [SIP capture](docs/troubleshooting.md#capture-sip-signaling-from-home-assistant-including-ha-os)
  for HA-handled signaling problems. It does not capture RTP media or SIP exchanged
  directly between ESP devices. Further captures may be needed for those paths.

To request an ESP dump from HA, open **Developer Tools > Actions**, select
**Button: Press** (`button.press`) and choose that device's **VoIP Diagnostics**
or **Audio Diagnostics** button. Open the ESPHome device logs first: the dump is
printed there, not returned by the HA action. For missing buttons in custom
firmware, use the [YAML definitions](docs/troubleshooting.md#collect-a-runtime-snapshot-from-an-esp)
and rebuild/upload. The `voip_stack.dump_diagnostics` and
`esp_audio_stack.dump_diagnostics` names are ESPHome actions, not HA services.

For audio faults, state which direction is silent or distorted. For video faults,
state whether the call began with video or video was enabled later. For custom
Audio Stack hardware, include whether the problem persists with AEC/AFE disabled
and whether the bus is shared or split, if you can test those cases.

Use this structure, replacing the example details with what you observed:

```text
Title: ESP receives audio but the browser caller hears silence
Versions: [exact installed HA, integration, ESPHome and component versions]
Hardware: [board, microphone, speaker, codec and relevant wiring]
Call path: HA browser card -> Generic S3, through HA, SIP UDP
Steps: call, answer, speak in both directions, hang up from HA
Expected: bidirectional audio and both endpoints idle after hangup
Actual: ESP plays audio; browser hears silence; hangup works
Frequency and test time: [repetitions, timestamp and timezone]
Attachments: sanitized YAML, HA diagnostics, HA/ESP logs, full diagnostic blocks,
             and a SIP capture when relevant
```

Never publish SIP passwords, authentication data, API keys or tokens. Review
configuration files, logs and captures before attaching them.

If you ignore these instructions and open an "it does not work" issue without
reproduction details or supporting logs, I'll get pissed off like there's no
tomorrow and close it as incomplete.

## Support the project

If VoIP Stack is useful to you, [consider sponsoring development](https://github.com/sponsors/n-IA-hane). Community support helps cover development tools and test hardware. The project remains free and open source.

For bug reports and hardware feedback, follow the
[issue-reporting instructions](#before-opening-an-issue) above.

## Contributing

Contributions should preserve standards-based SIP/SDP/RTP behavior and the
single authoritative call lifecycle. Avoid endpoint-specific timing
workarounds when a transaction, dialog, media or ownership invariant can solve
the underlying problem.

Before submitting a change, run the repository test environment documented in
[`docs/TESTING_AND_DEBUG.md`](docs/TESTING_AND_DEBUG.md).

### AI-assisted contributions

Vibe coding is welcome here. I support AI-assisted development and consider it
an invaluable tool, provided it is used thoughtfully.

Asking an AI to "add this feature" or "fix this bug" and submitting the result
is not enough. A change may solve the immediate problem while introducing
regressions elsewhere, especially in a project where audio, video and call
handling share limited resources.

Understand the proposed changes, review their wider effects, and test the
affected functionality before opening a pull request. Explain what you tested
and what remains unverified. AI can help write the code, but careful review
and testing remain the contributor's responsibility.

## License

Project-owned code is licensed under the MIT License; see
[`LICENSE`](LICENSE).

External Espressif components and optional system codecs keep their own
licenses. They are consumed as upstream dependencies and are not copied into
the project license.

> [!NOTE]
> VoIP Stack is designed for trusted local networks. It is not an emergency
> telephone service.
