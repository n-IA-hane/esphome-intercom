# User guide

This guide describes the normal VoIP Stack workflow from installation to the
first local, SIP and trunk calls. Protocol and developer details are linked at
the end.

## Install Home Assistant VoIP Stack

1. Open HACS and download **VoIP Stack**.
2. Restart Home Assistant.
3. Open **Settings > Devices & services > Add integration**.
4. Select **VoIP Stack**.
5. Keep SIP port `5060` and RTP base port `40000` unless another service uses
   them.
6. Set **Advertise host** only when automatic detection does not produce an
   address reachable by every phone.

The first setup creates one ordinary Home Assistant browser phone. Its default
name comes from the Home Assistant location name, for example `Casa`. It is a
real logical phone, not a temporary setup object, and can receive calls even
before more phones are added.

## Add the dashboard phone

1. Add the **VoIP Stack card** to a dashboard.
2. Select **Home Assistant phone** mode.
3. Select the phone Device created during setup, for example `Casa`.
4. Save the card.
5. Grant microphone permission when the browser asks. Grant camera permission
   only when this phone must transmit video.

The card saves one public identity, the Home Assistant `device_id`. Incoming
calls for that phone move every connected card bound to the same Device to
`ringing`. The browser that answers becomes the media owner.

Example YAML:

```yaml
type: custom:voip-stack-card
mode: ha_softphone
device_id: 0123456789abcdef0123456789abcdef
```

Use the card editor to select the Device. Do not copy a Device Registry ID from
another installation.

## What `device_id` and `endpoint_id` mean

For every public phone action:

```text
device_id   = the local phone performing the action
destination = the remote party to call
```

The same action works whether the local phone is a Home Assistant browser phone
or a compatible ESPHome phone:

```yaml
action: voip_stack.call
data:
  device_id: 0123456789abcdef0123456789abcdef
  destination: Waveshare S3 Audio
```

`endpoint_id` remains an internal stable identity used to correlate a logical
phone, SIP session, media owner and WebSocket snapshot. It is useful to the
backend because a Device can be temporarily unavailable while its logical
endpoint and active session still exist. Users do not save it in card YAML and
do not pass it to phone actions.

If `device_id` is omitted, VoIP Stack uses the explicitly preferred phone. If
no preferred phone is configured but exactly one compatible phone exists, that
phone is used. Otherwise the action asks for a phone selection instead of
guessing.

Choose the preferred phone from **Settings > Devices & services > VoIP Stack >
Configure > Home Assistant phones**. Removing or renaming the original `Casa`
phone is allowed after another preferred phone has been selected.

## Add an ESPHome phone

1. Start from a maintained profile under [`yamls/`](../yamls/).
2. Change only the documented substitutions, network secrets and hardware pins.
3. Compile and flash the device.
4. Add it through the normal ESPHome integration.
5. Wait for VoIP Stack to discover its endpoint and publish it in the phonebook.

Normal custom profiles should include the semantic phone package:

```yaml
packages:
  voip_ha_phone: !include packages/voip/ha_phone.yaml
```

It provides the entities, phonebook subscription and native ESPHome actions
required by Home Assistant:

```text
start_call
answer_call
decline_call
hangup_call
```

An ESP mirror card selects the existing ESPHome Device. VoIP Stack does not
create or own a duplicate Device.

## Make local calls

The destination may be a phonebook name, extension, group, SIP URI or public
number.

From a card, select a contact or enter the destination and press Call.

From an automation:

```yaml
action: voip_stack.call
data:
  device_id: 0123456789abcdef0123456789abcdef
  destination: Kitchen
```

From an ESPHome phone, choose a contact and invoke its normal call action. The
ESP calls the selected destination through the route published by Home
Assistant. Direct ESP-to-ESP SIP remains possible when the phonebook contains a
direct route.

The public actions are:

| Action | Purpose |
| --- | --- |
| `voip_stack.call` | Start a call from the selected local phone |
| `voip_stack.answer` | Answer a pending call |
| `voip_stack.decline` | Reject a pending call |
| `voip_stack.hangup` | End the selected call |
| `voip_stack.forward` | Redirect a pending or ringing HA-owned call |
| `voip_stack.transfer` | Transfer an established call with SIP REFER |
| `voip_stack.set_dnd` | Change DND on an HA phone |
| `voip_stack.set_auto_answer` | Persist Auto Answer for an HA phone |
| `voip_stack.set_send_video` | Persist default camera transmission |

## Add more Home Assistant phones

Open **Settings > Devices & services > VoIP Stack > Add phone > Home Assistant
browser phone**.

Create one phone per independent place or browser session, for example:

- `Casa`, extension `666`;
- `Test`, extension `667`;
- `Reception`, extension `200`.

Each phone receives its own Device, call state, DND, Auto Answer, extension,
groups and video settings. Bind each card to the intended Device. Two cards in
one browser tab do not create two physical microphones or cameras, so use a
separate browser or Companion session for simultaneous independent media.

## Register a SIP client or IP phone

1. Enable the local registrar in **VoIP Stack > Configure**.
2. Create an account through **Add phone > SIP account**, or use the
   `voip_stack.create_account` action.
3. Configure the SIP client with the Home Assistant advertise host, SIP port,
   username and generated password.
4. Use UDP or TCP according to the account and network configuration.

The registered client appears in the central phonebook while at least one
Contact binding is active. Account passwords are returned only by the account
management action and are not placed in entity state.

## Configure groups

Use phone Device entities or phone settings to assign comma-separated groups.

- A **ring group** rings all eligible members. The first answer wins.
- A **conference group** joins participants to one HA audio mixer.
- **Ring for conference calls** controls whether a member rings when another
  participant starts that conference.

Call the group name exactly as shown in the phonebook.

## Make Assist callable

Open **VoIP Stack > Reconfigure**, enable **Include voice assistant**, choose a
pipeline and assign an extension. Assist then becomes a normal phonebook
destination.

Hanging up terminates the Assist media leg and its active pipeline work. It
does not leave a listening call session behind.

## Configure a trunk

Leave the trunk disabled for a local-only installation. To connect FRITZ!Box,
Wildix, another PBX or a provider:

1. Open **VoIP Stack > Reconfigure > Trunk**.
2. Enter server, port, transport, domain and credentials.
3. Set an outbound proxy only when the PBX requires one.
4. Select the default inbound destination.
5. Enable digit collection only when callers must choose an internal extension.

Public numbers and service strings such as `*` or `**621` are sent to the trunk
without destructive normalization. Names and internal extensions are resolved
through the phonebook first.

Home Assistant trunk and standard SIP legs support UDP, TCP and verified TLS.
ESP endpoints intentionally remain lightweight local SIP/RTP phones and do not
terminate SIP TLS or SRTP.

## Forward or transfer a call

Forward a call before it is established:

```yaml
action: voip_stack.forward
data:
  device_id: 0123456789abcdef0123456789abcdef
  call_id: current-call-id
  destination: Reception
```

Transfer an established call:

```yaml
action: voip_stack.transfer
data:
  device_id: 0123456789abcdef0123456789abcdef
  call_id: current-call-id
  destination: sip:desk@pbx.example
```

Secure SIP identities such as `sips:desk@pbx.example:5061;transport=tls` are
preserved across direct routing, outbound proxies and REFER targets.

## Audio and video calls

Browser phones negotiate supported audio codecs and can send or receive video.
ESP32-P4 profiles provide either JPEG or H.264 SIP video according to the YAML
selected at compile time.

An audio call can add compatible video through re-INVITE. The previous media
contract remains active until both dialogs accept the change. Closing the
camera or browser track does not independently rewrite the PBX call state.

For browser calls:

- microphone permission is required before Answer or Call can start media;
- camera permission is required only to transmit video;
- receiving a remote camera does not require local camera permission;
- reset the Companion frontend cache after upgrading the bundled card.

## Health, repairs and diagnostics

Use **Settings > System > Repairs** for actionable problems such as missing
ESPHome call-control actions, incompatible firmware contracts or enabled media
capture.

Use **Settings > System > Repairs > System information** or the integration's
System Health entry to inspect aggregate listener, trunk, endpoint, active-call
and RTP-resource status. Diagnostics redact credentials, private identities and
complete Call-IDs.

If a call fails:

1. check that both phone Devices are available;
2. check DND and Auto Answer;
3. verify the destination in the phonebook;
4. verify SIP and RTP firewall rules;
5. read [`troubleshooting.md`](troubleshooting.md);
6. collect diagnostics only after reproducing the failure.

## Upgrade safely

1. Read [`BREAKING_CHANGES.md`](BREAKING_CHANGES.md).
2. Download the update through HACS and restart Home Assistant.
3. Open **Reconfigure** once and review the integration options.
4. Open every card and confirm its selected phone Device.
5. Reset the browser or Companion frontend cache.
6. Rebuild ESPHome firmware when phone packages or component contracts changed.

Do not restore `endpoint_id` in card YAML and do not copy old action fields such
as `target`, `source` or `entity_id` into the current phone actions.

## Detailed references

- [Automation cookbook](AUTOMATION_DIALPLAN.md)
- [What is new in 2026.8.2-dev](WHATS_NEW_2026_8_2.md)
- [Deployment guide](DEPLOYMENT_GUIDE.md)
- [Home Assistant actions](SERVICES.md)
- [Dial plan](DIALPLAN_RESOLVER.md)
- [Groups](GROUPS.md)
- [SIP trunk](SIP_TRUNK.md)
- [SIP video](SIP_VIDEO.md)
- [Automation cookbook](AUTOMATION_DIALPLAN.md)
- [Troubleshooting](troubleshooting.md)
- [Architecture](ARCHITECTURE.md)
