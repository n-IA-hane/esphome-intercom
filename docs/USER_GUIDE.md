# User guide

This guide describes the normal VoIP Stack workflow from installation to the
first local, SIP and trunk calls, then explains the everyday PBX controls.
ESP hardware is optional: you can start with a browser phone and ordinary SIP
phones. For the difference between a phone, card, contact and trunk, see
[basic concepts](CONCEPTS.md).

## Find the task you need

| I want to... | Start here |
| --- | --- |
| Choose which phone places a call | [Calling phone picker](#choose-the-calling-phone-in-the-editor) |
| See all phones and recover a ringing call | [VoIP Dashboard](#use-the-voip-dashboard) |
| Add a provider or another PBX | [SIP trunks](#configure-a-trunk) |
| Send an unanswered call somewhere else | [Ringing timeout and forwarding](#choose-what-happens-when-nobody-answers) |
| Move an answered call to someone else | [Transfer a call](#forward-or-transfer-a-call) |
| Leave and listen to messages | [Voicemail](#voicemail) |
| Continue a conversation on another phone | [Parking and pickup](#parking-and-picking-up-calls) |
| Call a phone and play an announcement | [Announcements](#make-an-announcement-without-a-browser) |
| React when a phone rings or its mailbox changes | [Native automation tools](#phone-state-in-automations) |

The screenshots use Home Assistant's visual editors. Choose phones by their
names; HA stores the identifiers for you. After updating the integration,
restart HA and reload the browser to load its updated cards and actions.

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
3. Choose the phone created during setup from the visual device picker,
   for example `Casa`. Select it by name; you do not need to look up its ID.
4. Save the card.
5. Grant microphone permission when the browser asks. Grant camera permission
   only when this phone must transmit video.

After saving, the card controls the phone you selected. Incoming calls for that phone
move every connected card bound to the same Device to `ringing`. The browser
that answers becomes the media owner.

## Read the phone card

The same icons are used in audio and video mode.

![Idle phone with keypad and options icons](images/pbx-phone-idle-green.png)

| Control | What it does |
| --- | --- |
| Keypad (nine dots) | Enter a number, contact name or SIP URI. During a call, send DTMF digits. The icon changes to Contacts when it can return you to the phonebook. |
| Voicemail (two linked circles) | Open this phone's messages. It appears when messages are available and the phone is idle, not during dialing, ringing or an active call. |
| Transfer (handset with arrow) | Open the existing contact/keypad view to choose where to send the current call. Confirm with **Transfer**. |
| P | Park the established audio call. The returned slot is the number to dial to retrieve it. |
| Options (sliders) | Open this phone's answer, DND, mailbox, audio/video and routing settings. |
| Call / Hang up | Start the selected call or end the current conversation. In video mode, use the handset icon in the translucent bottom bar. |

![Phone options on the same green-themed dashboard](images/pbx-phone-options-green.png)

Options includes auto-answer, DND, mailbox, ringtone and media-device settings.
**Send Camera** controls your outgoing camera; it does not prevent receiving
video. Disabling **Enable this phone's mailbox** deletes its messages and settings.

Occupied parking slots appear as a left-aligned bullet list. Other phones that
are calling, ringing or busy appear on the right, with the bullets at the right
edge and the text in its normal reading order. These are backend call states,
shared with SIP BLF; external PBX extensions are not automatically monitored.
The phone you are currently speaking to is already shown in the call status
and is omitted from the activity list.

The lists do not move or resize Call, Answer, Decline or the toolbar icons.
Lists fit the space beside the call controls and scroll when necessary.
Empty lists occupy no space. They are hidden while
the keypad, options or video view needs that space. Voicemail stays in its own
button and is not duplicated in these lists.

![An established audio call, with Parking available](images/pbx-phone-audio-call-green.png)

The keypad closes when you start or answer a call. Open it again if an IVR or
door station needs DTMF. On short video cards it scrolls above the call bar.

![Live video received from a Mobotix door station](images/pbx-mobotix-video-green.png)

This is a real call through a FreeSWITCH-based PBX with the local webcam off.
The image arrives from the door station. **P is disabled during video calls**:
parking currently supports audio calls. Switching off Send Camera alone does
not make a call audio-only when the peer is sending video.

![DTMF keypad open during the same video call](images/pbx-mobotix-keypad-green.png)

For parking and retrieval, follow the [parking walkthrough](CALL_PARKING.md).
For forwarding, ringing timeouts, mailbox setup and line selection, see
[PBX workflows](PBX_WORKFLOWS.md).

## Rename Home Assistant entities

You can change a VoIP Stack entity ID in **Settings > Devices & services >
Entities**, for example from `sensor.casa_call_state` to `sensor.living_room_call`.
The entity stays linked to the same phone. Its extension, settings and active
call do not change. Access checks use the current entity identity, not its old
name.

The shared phonebook and call-event entities can also be renamed. VoIP Stack
finds them through Home Assistant's entity registry, and cards using automatic
phonebook selection follow the new ID. You do not need to recreate the phone.

If you explicitly entered an entity ID in a card's YAML, a template or an
external automation, check that reference after renaming. VoIP Stack does not
rewrite user-authored YAML or retain the old ID as an alias.

This works with the existing remove-and-add lifecycle and Home Assistant's
in-place entity renaming introduced in Core 2026.11. The minimum supported
Home Assistant version remains 2026.7.0.

## Choose the calling phone in the editor

In Home Assistant's automation editor or **Developer tools > Actions**, the
**Calling phone** field in the Call action is a device picker: search for the
phone's name and select it.
No YAML editing or identifier lookup is needed. Enable the optional **Calling
phone** field, open **Select a device**, and choose the phone by name. Home
Assistant stores the selection for you.

![Find VoIP Stack: Call in the HA action picker](images/call-action-picker-dark.png)

![Choose the calling phone by its readable name](images/call-phone-picker-dark.png)

![Calling phone and Destination filled in the visual action editor](images/call-action-fields-dark.png)

In these screenshots, **Casa** is the calling phone and **Kitchen** is the
example destination. Nothing is executed just by filling these fields. In an
automation, save the action; in Developer tools, **Perform action** actually
starts the call. The optional Automation contact and outgoing trunk fields can
stay unset for an ordinary internal phone call.

**Calling phone** chooses who places the call. **Destination** chooses who receives it.
For example, select `Reception` in **Calling phone** and enter `Kitchen` in
**Destination**: Reception calls Kitchen. Selecting Kitchen as the calling
phone would reverse their roles.

This is a Device selection, rather than an entity ID such as `sensor.*`.
The picker presents the phone's readable name, just like other Home Assistant
selectors.

<details>
<summary>For advanced users: identifiers in saved YAML</summary>

If you inspect the saved YAML, these fields have different jobs:

```text
device_id   = the local phone performing the action
destination = the remote party to call
```

The same action works whether the local phone is a Home Assistant browser phone
or a compatible ESPHome phone.

`endpoint_id` remains an internal stable identity used to correlate a logical
phone, SIP session, media owner and WebSocket snapshot. It is useful to the
backend because a Device can be temporarily unavailable while its logical
endpoint and active session still exist. Users do not save it in card YAML and
do not pass it to phone actions.

</details>

If **Calling phone** is left unselected, VoIP Stack uses the explicitly preferred phone. If
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

Custom profiles use ESPHome 2026.10.0 or newer and enable native API actions:

```yaml
api:
  custom_services: true
```

The `voip_stack` component supplies discovery, phonebook reception and these
call-control actions automatically, without a VoIP HA package:

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

From an automation, use the visual editor:

1. Add the **VoIP Stack: Call** action.
2. In **Calling phone**, select the phone that should place the call by its name.
3. In **Destination**, enter who to call, for example `Kitchen`.
4. Save the automation.

You can make the same selection in **Developer tools > Actions** to try it.
Leaving **Calling phone** empty uses the preferred or sole compatible phone, as
explained above.

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

Numeric extensions must be unique across HA phones, SIP accounts, ESPHome
phones, manual contacts and Assist. If discovery introduces a duplicate,
**Settings > System > Repairs** lists the extension and the conflicting
names. Calls to that ambiguous number are rejected instead of selecting a
phone or falling through to a trunk. Give each destination a different
extension; for an ESPHome phone, change its extension on the device. The
repair clears automatically after the phonebook updates. Unique names and
explicit SIP addresses remain usable while you resolve the conflict.

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

## Dahua door stations

For registered or static Dahua devices, see the [Dahua setup guide](DAHUA.md).
The contact editor exposes the SIP profile and audio compatibility choices;
you do not need to change Python code or mark a static contact as registered.

## Configure a trunk

A local-only installation needs no trunk. To connect a provider, FRITZ!Box or
another PBX:

1. Open **Settings > Devices & services > VoIP Stack**.
2. Open the integration's three-dot menu and choose **Add SIP trunk**. The
   integration page also has an **Add SIP trunk** shortcut.
3. Name the line and choose whether the peer requires REGISTER.
4. Enter its server, transport and credentials, or the allowed incoming networks
   for a static trunk. Set an outbound proxy only when required by the peer.
5. Choose the allowed codecs and the receiving phone under **Fallback destination**.
6. Enable digit collection only if callers should choose an internal extension.

![Open the integration menu and choose Add SIP trunk](images/pbx-add-trunk-menu-dark.png)

![Name the line and choose whether it registers](images/pbx-add-trunk-dark.png)

Leave **Register the trunk** enabled when the provider gives you a SIP account.
Disable it only for a peer configured to accept a static connection. A static
trunk needs a precise incoming IP allowlist; its Ready indicator means it is
configured for that mode, not that a REGISTER succeeded.

Each trunk is a separate HA entry with its own device and readiness entity.
Edit it through the configuration control beside that named trunk, not through
**Reconfigure > Trunk** on the main integration. The first line becomes the
outgoing default; additional lines can be selected per phone or per call.
Incoming destinations are configured independently of that outgoing choice.

See the illustrated [PBX walkthrough](PBX_WORKFLOWS.md), detailed
[trunk guide](SIP_TRUNK.md) and
[migration instructions](BREAKING_CHANGES.md#pbx-development-branch-separate-sip-trunk-entries).

Public numbers and service strings such as `*` or `**621` are sent to the trunk
without destructive normalization. Names and internal extensions are resolved
through the phonebook first.

Home Assistant trunk and standard SIP legs support UDP, TCP and verified TLS.
ESP endpoints intentionally remain lightweight local SIP/RTP phones and do not
terminate SIP TLS or SRTP.

## Choose what happens when nobody answers

Open the phone's card and choose **Options > Call routing**. These are the
phone's settings in the backend: they still apply when its card is closed.
The ESP mirror card exposes the same phone-routing policy.

![Phone routing options, including an optional ringing timeout](images/pbx-phone-routing-green.png)

For a phone that should ring for 20 seconds and then take a message:

1. Enable its mailbox as described in the next section.
2. Enable **Ringing timeout** and enter **20 seconds**.
3. Choose **Voicemail** under **After timeout**.
4. Leave the mailbox destination empty to use this phone's mailbox, or choose
   another phone with an enabled mailbox.
5. Press **Save**.

Choose **Forward** and a destination instead to try another phone. Choose
**Hang up** to finish the unanswered call. Rules for forwarding every call,
forwarding while busy/DND, and forwarding while unavailable are separate.
Leave them unset when you do not need them. With no ringing timeout, VoIP Stack
adds no application-level no-answer deadline; caller cancellation and SIP
network timeouts still apply.

An automation that accepts control with a routing action can override the
phone's fallback for that call. Merely sending a notification does not take
control. For example, a native **Call ringing timeout** trigger can use
**Send to voicemail** or **Forward call**. See [routing precedence and
examples](PHONE_ROUTING.md).

## Forward or transfer a call

| Situation | Control to use |
| --- | --- |
| The phone is still ringing and another destination should answer | **Forward call**, a routing rule, or an automation |
| You have answered and want another person to take over | **Transfer** |
| You want to put the caller aside and resume elsewhere yourself | **Park call**, then call the assigned slot |

### Transfer from the card

1. During the conversation, press the **Transfer call** handset-and-arrow icon.
2. Select a contact. To enter an extension or SIP address, switch to **Keypad**.
3. Press **Transfer** to confirm.
4. On success, your phone becomes free. If the transfer fails, read the error
   and keep using the existing call; do not assume the destination answered.

![Choose a destination in the card's transfer view](images/phone-transfer-dark.png)

The card reuses the same contact and keypad view as normal dialing. Opening the
transfer view does not send the call anywhere until you confirm.

A blind transfer of a browser-to-browser call can select another browser or
route the remaining participant to a SIP phone or trunk. For a browser-to-SIP
or trunk handoff, turn off both local cameras first: active local video is not
supported on that handoff. A failed preparation leaves the original
conversation available.

### Transfer from a SIP handset or automation

For an attended transfer on a SIP handset, hold the caller, call the other
extension, speak to that person, then complete the transfer. The handset must
support REFER with Replaces. The destination retains its answered SIP dialog;
it is not called a second time. On failure, use the handset's normal resume or
hangup controls. SIP consultation calls can coexist; browser and ESP controls
retain their single-call behavior.

In an HA automation, choose **VoIP Stack: Transfer call** and the destination.
Native VoIP call triggers supply the current call. For a manually configured
action, the participating **Phone** picker is under **Advanced options**.
An explicit call ID is only needed for advanced selection between concurrent
calls. See the [action reference](SERVICES.md#voip_stacktransfer).

For two SIP audio calls already controlled by HA, the backend can join the
remaining participants locally when appropriate, including static SIP peers.
This is not a guarantee that every handset supports every REFER target or media
change. The updated ESP development firmware supports blind audio transfers
between IPv4 SIP endpoints over UDP or TCP. Rebuild and install the firmware;
updating HA alone cannot add this support to an ESP. Spotpear Ball V2 FULL has
been tested with HA browser phones in all four UDP/TCP transport combinations,
including busy, timeout and cancellation. The replacement call retains its
active TX codec, so the tested Spotpear transfer uses L16/16 kHz both ways.
Video transfers, hostname targets and attended REFER with Replaces are outside
this ESP receiver's supported scope. The original call remains available when
a transfer fails, provided its caller has not already hung up.

## Voicemail

PBX functions are available by default. There are no global parking or voicemail
enable switches. Configure resources as needed; registering a function does not
create mailboxes, record calls or start playback.

A **mailbox** is the message inbox belonging to one phone. Voicemail is the PBX
feature that answers a call, plays a greeting and records the caller.

### Enable the mailbox and its routing rule

1. For spoken mailbox menus, open **VoIP Stack > Reconfigure > Voicemail voice**
   and choose an HA TTS provider, language and voice. Message deposits use a
   recording tone or the mailbox greeting and do not require TTS.
2. Open the phone card's **Options** and enable **Enable this phone's mailbox**.
3. If unanswered calls should leave a message, configure **Call routing >
   Ringing timeout > Voicemail** and save it.

You can also use **VoIP Stack: Configure voicemail** in the action editor and
select the phone by name. Selecting a mailbox does not itself redirect calls.

![Configure a phone's mailbox through the native HA action](images/mailbox-action-dark.png)

Disabling the mailbox deletes that phone's mailbox, settings and messages.
Removing its HA phone or ESP endpoint also removes its mailbox. Conversation
recordings are separate and are not deleted by disabling voicemail.

### Listen, delete or use a SIP voicemail key

The card shows **Voicemail** while the phone is idle and has messages. Open it
to see the callers and recording dates. Select a message to listen; use its
Delete control to delete that message. The softphone calls the same voicemail
menu used by a SIP handset and sends the keypad commands. Its list remains open.

| From a phone connected to this PBX | What happens |
| --- | --- |
| Dial `*97`, or configure the handset's voicemail key to use it | Open that identified phone's own mailbox without a PIN |
| Dial `*98` | Choose another mailbox by extension and enter its PIN |
| Press `1` during playback | Next message |
| Press `2` during playback | Delete the selected message |
| Press `3` during playback | Repeat the selected message |

Another phone cannot open a mailbox unless that mailbox has a PIN configured.
MWI updates a compatible SIP handset's new-message indicator; it does not
transfer a list of audio files to the phone.

### Notify or transcribe after a message arrives

Use the native **Voicemail received** trigger to act on a newly saved recording.
For example, add **Transcribe voicemail** and then your notification action if
an HA speech-to-text provider is available. STT is optional and belongs to the
automation, not to the telephone menu. The recording is stored once in Home
Assistant Media and can be reused by the notification.

Use **Phone mailbox changed** when you want to react to message-count changes,
including messages being read or deleted. These are different triggers: a count
change does not necessarily mean a new recording arrived. See the
[voicemail guide](VOICEMAIL.md) for media access and complete examples.

## Use the VoIP Dashboard

Add **VoIP Dashboard** from Home Assistant's card picker. In its visual editor,
choose the phone the card belongs to and an optional title. The phone picker
stays in the editor; the card header always describes that selected phone.
The dashboard resource loads with the ordinary VoIP Stack card resource.

The YAML card type is `custom:voip-stack-dashboard-card`. The editor saves
the selected phone as `device_id`, just like the softphone card.

The header represents the selected phone. The upper grid contains the other
browser, ESP and registered SIP phones. Saved contacts and automation
destinations appear below. Icons identify the destination type and its
availability. The grid adapts to the card width and scrolls vertically when
the content exceeds the available height. Glass surfaces and text colors
follow the active Home Assistant theme.

For an operator console, give its section the full view width in Home
Assistant's layout editor. The grid grows to three, four or five columns as
space increases; narrow cards use one or two columns. Resize the card height
to choose how many rows are visible before scrolling.

| Tile | Meaning and action |
| --- | --- |
| Available | Call this destination from the selected phone when that phone is free. |
| Blue, ringing | Shows the caller. Use **Pick up** to recover an eligible ringing call on the selected phone. |
| Red, in call | Shows the connected party. Open the tile for the individual conversations. |
| On hold | Shows held conversations alongside the active conversation. |
| DND or offline | Shows the endpoint's reported availability and Do not disturb setting. |
| Mailbox badge | Shows the count of new voicemail messages. |

The header describes the selected phone's incoming or outgoing call. Answer,
Decline and Hang up act on that phone and call. With an ESP selected, audio
stays on the ESP. With a browser phone selected, the dashboard uses the same
page-level media engine as the ordinary softphone card; microphone permission
is requested when placing or answering a call. The dashboard answers browser
calls with audio. Use the phone card for camera and video controls.

You can keep both cards on one view. Closing a card releases its display
listeners while an active call remains owned by the media engine. Monitoring
another browser phone leaves its presence and microphone untouched.

Held and active conversations are separate entries. A transfer label requires
an explicit transfer relationship from the PBX. For a conventional handset
which has only held one call and started another, the dashboard shows those
two facts until a transfer operation is known. External contacts have no live
BLF status. Registered SIP phones can be monitored and called; select a browser
or ESP phone as the dashboard's calling phone.

The dashboard receives an initial snapshot and then changed endpoints over
Home Assistant's WebSocket. It shares the PBX observer with SIP BLF, with
permission checks for the monitored endpoints. A reconnect replaces the
snapshot before actions become available again.

## Busy lamps on SIP phones

A registered SIP phone can monitor another phone with a normal BLF key. Choose
BLF in the handset's key settings and use the monitored phone's username or
extension as the destination, through its VoIP Stack account.

VoIP Stack supports `Event: dialog` subscriptions and sends versioned
`application/dialog-info+xml` notifications. They reflect HA-owned calls:
ringing, established conversations and return to idle. A phone with two active
dialogs stays busy until both end. The state comes from the same call registry
used by the backend and card; no periodic status polling is added.

This is separate from the voicemail MWI indicator, which uses `message-summary`,
and online presence, which uses `presence`. Removing the monitored phone ends
its BLF subscription with `noresource`. Subscriptions can request up to one hour
and must refresh before expiry.

## Parking and picking up calls

### Continue a conversation on another phone

Parking is available by default. During an audio call,
press **P** on the card. The phone is released and the left-hand list shows the
assigned slot, for example **701**.

![The occupied slot shown after parking a call](images/pbx-phone-parked-green.png)

On the phone where you want to continue, open the keypad, enter **701** and
press **Call**. The original card can retrieve it too. The slot disappears when
the conversation resumes.

![Dial the assigned slot from the receiving phone](images/pbx-phone-pickup-green.png)

A SIP handset can park by transferring to **700** instead of pressing P.
**These are two ways to park, not two steps.** Once parked, call the reported
slot to retrieve it. Do not transfer it to 700 again. HA's slots are separate
from another PBX's own parking extensions; a trunk must route those numbers to
HA if they are used from the other PBX.

### Answer a phone that is still ringing

Choose **VoIP Stack: Pick up call** in the HA action editor:

1. **Recovering phone:** where you want to answer, for example Office.
2. **Ringing phone:** the phone currently ringing, for example Kitchen.
3. **Wait for answer:** how long to wait for the recovering phone to answer.
4. Leave **Parking slot** empty for a ringing-call pickup.

![Pick up a ringing call on another phone using the device pickers](images/pickup-ringing-action-dark.png)

A SIP or ESP recovering phone rings and must answer. For a browser phone, keep
its card open and its audio connection ready. If another group member answers
first, that answer wins. A failed pickup leaves the original ringing call
available. The advanced call and leg fields are only needed when the selected
phone has more than one eligible call.

To retrieve a parked call through this action, choose **Recovering phone** and
**Parking slot** instead of Ringing phone. From a telephone keypad, use the slot
number, `pickup.<phone>`, or `*8` followed by the ringing extension, such as
`*8101`. These are this PBX's dial-plan codes, not universal SIP codes.

For a native ringing-trigger automation, you can omit Ringing phone: the
trigger already identifies the call. Follow the
[complete visual recipe](#recover-a-ringing-call-from-an-automation).
For return timeouts and supported media, see [parking and pickup](CALL_PARKING.md).

## Recording a conversation

Use **VoIP Stack: Start call recording** with the participating phone selected, then
**Stop call recording**, or enable **Show recording controls** in that phone card's editor.
A hangup also finishes the recording. The conversation continues if recording
fails or reaches its configured limit. Recordings are separate from voicemail
and do not turn on message-waiting indicators.

The [recording guide](CALL_RECORDING.md) describes file access, automation events
and supported call paths. Recording is optional and off until requested.

## Make an announcement without a browser

An Automation contact can originate an audio call from the HA backend. No
microphone, open card or ESP caller is required.

For a weekday wake-up call, create an enabled Automation contact named
`Wakeup-Caller`. Then create an HA automation with a **Time** trigger at
**07:30** and a **Time** condition allowing Monday through Friday. Add these
actions in order:

1. **VoIP Stack: Call:** enable **Calling Automation contact**, enter
   `Wakeup-Caller`, set Destination to the target phone or SIP account, and
   choose **Wait for answer**, for example 30 seconds. Leave Calling phone unset.
2. **VoIP Stack: Speak to the caller:** choose the installed TTS provider and
   enter your message.
3. **VoIP Stack: Hang up:** keep the current call supplied by this execution.

![Choose the TTS provider and announcement text in the action editor](images/tts-action-dark.png)

Call waits for an answer before the next action runs. Speak waits for the
announcement to finish transmitting before Hang up runs. If the destination
is busy or never answers, the call action fails and releases its call; the
announcement does not play during ringing.

For a sound file, use **Play audio to the caller** and its media picker in
place of Speak. Both playback actions leave hangup to the next action, so you
can chain several announcements. Keep the sequence in the same automation
execution to use its call automatically. You do not need a call-ID template
for this ordinary sequence.

The same sequence can be triggered by a delivery notification or door sensor.
See the [automation cookbook](AUTOMATION_DIALPLAN.md) for incoming calls, menus,
error handling and complete YAML examples.

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

## Updating an existing installation

1. Read the target release notes and apply migration instructions relevant to
   your installed version.
2. Download the update through HACS and restart Home Assistant.
3. Reload the dashboard when the card changes. Reset the browser or Companion
   frontend cache if it still displays the previous card.
4. Rebuild ESPHome firmware when adopting firmware-side changes, using the
   component versions and build instructions specified for that release.

Use **Reconfigure**, change card bindings or edit existing automations only when
the release notes call for it or you want to change your setup. Current examples
use the visual phone picker and supported action fields; see [Services](SERVICES.md)
when adapting older YAML.

## Use ESP firmware with a classic PBX

ESP phones can use a static SIP peer configuration with a traditional PBX,
without REGISTER. See [Use a VoIP Stack firmware with a classic PBX](CLASSIC_PBX.md)
for Asterisk and FreeSWITCH static extensions, conventional SIP phones, ESP
contacts, audio formats and call tests. This is a
firmware-to-PBX path; HA can continue providing the device's Assist features.

## Phone state in automations

Use native triggers for changes and native conditions for decisions. These
choices appear in the ordinary Home Assistant automation editor; you do not
need an event-type string or a template to detect a ringing phone.

| Tool | Use it when... |
| --- | --- |
| **Phone state changed** trigger | A particular phone starts ringing, connects, goes on hold or returns to idle |
| **Phone mailbox changed** trigger | The phone's new/read message counts change |
| **Call picked up** trigger | A pickup has completed, rather than merely been attempted |
| **Phone is in state** condition | Continue only if the selected phone is idle, ringing, in a call, held, calling or unavailable |
| **Phone has a ringing call** condition | Check for a call that can still be answered |
| **Phone has voicemail** condition | Check for unread messages, or any messages when unread-only is disabled |
| **Get phone state** action | Read a snapshot into an action response for a more advanced script |

**Idle** and **Unavailable** are different. A phone can also have DND enabled;
reading its state does not turn DND off. Calls on an external PBX are not assumed
idle simply because HA cannot see them. These tools describe phones and calls
managed by this VoIP Stack instance.

### Recover a ringing call from an automation

Example: Kitchen is ringing, and you want Office to recover the call if Office
is free. First ensure Office is available and DND is off. If Office is a browser
phone, open its card so its audio connection is ready.

1. Open **Settings > Automations & scenes > Create automation**, then create an
   empty automation.
2. Under **When**, choose **Add trigger**. In the trigger chooser, select
   **By type** if it initially shows targets. Search for **Phone state changed**.
3. Select **Kitchen** as Phone. Enable **To** and choose **Ringing**. Leave From
   unset so any transition into ringing can start the automation.

![Native phone-state trigger with Kitchen and To Ringing selected](images/phone-state-trigger-dark.png)

4. Under **And if**, add **Phone is in state**. Select **Office** and **Idle**.

![Native condition requiring Office to be idle](images/phone-state-condition-dark.png)

5. Under **Then do**, add **VoIP Stack: Pick up call**. Select **Office** as
   Recovering phone and choose a Wait for answer duration. Leave Ringing phone,
   Parking slot and Advanced options unset: this native trigger already carries
   the ringing call.

![Pickup action with only the recovering phone selected](images/pickup-action-dark.png)

6. Save the automation. To test it, call Kitchen from a third phone. Office
   should receive the recovery attempt; answer it to complete the pickup.

The condition is an initial check, not a reservation. If another member answers
first or Office becomes busy, the backend resolves that race. A stale execution
cannot pick up a later call merely because it rings the same phone.

For a button, standalone script or a trigger from another integration, also
choose **Ringing phone** in Pick up call, or choose **Parking slot** for a parked
caller. A manual **Run actions** test does not reproduce the native ringing
trigger's call context. Use the complete test call above to validate the
triggered sequence.

### Read a snapshot when a script needs more detail

Select **VoIP Stack: Get phone state** and choose Phone with the device picker.
The response includes availability, DND, active calls and their participants,
and mailbox new/read counts. In an automation or script, give the action a
response variable when subsequent steps need those details.

![Read Office's state through the native HA action](images/get-phone-state-action-dark.png)

Reading state never answers, routes or ends a call. Use the native triggers
instead of repeatedly polling this action when you only need to react to a
change. See the [action reference](SERVICES.md#voip_stackget_phone_state) for
response fields and advanced concurrent-call selection.

## Detailed references

- [Automation cookbook](AUTOMATION_DIALPLAN.md)
- [Deployment guide](DEPLOYMENT_GUIDE.md)
- [Home Assistant actions](SERVICES.md)
- [Dial plan](DIALPLAN_RESOLVER.md)
- [Groups](GROUPS.md)
- [SIP trunk](SIP_TRUNK.md)
- [SIP video](SIP_VIDEO.md)
- [Troubleshooting](troubleshooting.md)
- [Architecture](ARCHITECTURE.md)

### Optional firmware audio tuning

Start with your maintained device YAML and its default AEC/AFE profile. For a
custom two-microphone delivery, analog PGA controls or a recording of individual
PCM stages, see [Audio Stack's tuning guide](https://github.com/n-IA-hane/esphome-audio-stack/blob/main/docs/AUDIO_TUNING.md).
These development options are opt-in; removing their YAML excludes the
corresponding capture/control code. The normal calling and Assist paths continue
to use the same microphone and speaker owners.
