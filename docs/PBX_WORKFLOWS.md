# Using the PBX features

This page covers the updated **2026.11.0** release. If you downloaded an
earlier build with the same version label, redownload it in HACS, restart HA
and reload the dashboard. Version 2026.10.1 did not include these additions.

Home Assistant owns the phones, calls and settings. A card is the control
panel for one phone. Closing the card does not remove that phone's forwarding
rules or mailbox. ESP hardware is optional.

## What would you like to do?

| Goal | Where to start |
| --- | --- |
| Add a second provider or external PBX | Integration menu > Add SIP trunk |
| Choose which line a phone uses | Phone card > Options > Call routing |
| Send unanswered calls elsewhere | Enable Ringing timeout and choose an outcome |
| Take messages when nobody answers | Create the phone's mailbox and no-answer rule; configure a voice for spoken menus |
| Listen to your messages | Voicemail in the HA phone card, or dial `*97` |
| Continue a conversation on another phone | Park it, then dial its assigned slot |
| Hand an answered call to another person | Transfer from the card, or consult first from a compatible SIP handset |
| Answer a different phone that is still ringing | Dial `pickup.<phone>` or `*8` followed by its extension |
| Save a conversation | Enable recording controls or use Start recording in an automation |
| Change routing according to presence or time | Use native VoIP triggers and actions in an HA automation |

## Add and choose telephone lines

A trunk is a connection to a provider or another PBX. A phone is an endpoint
that places and receives calls. Adding a trunk does not create another room
phone or replace the phone named after your Home Assistant instance.

Open **Settings > Devices & services > VoIP Stack**. In the integration's
three-dot menu, choose **Add SIP trunk**. HA also offers a shortcut with the same
name at the top of its integration page.

![VoIP Stack integration menu with Add SIP trunk](images/pbx-add-trunk-menu-dark.png)

Each trunk now has a separate named entry, a device and a readiness entity.
Provider credentials are no longer edited in the integration's main
**Reconfigure** flow. Use the configuration control beside the named trunk.
See the [migration instructions](BREAKING_CHANGES.md#pbx-development-branch-separate-sip-trunk-entries)
before updating an existing installation.

![First step for adding a named SIP trunk](images/pbx-add-trunk-dark.png)

The first line becomes the default automatically. The screenshot shows adding
another line, which also offers **Use as the default outgoing trunk**.
A call can choose a line explicitly; otherwise the phone's preference applies,
then the integration's default. **Use default** follows the current default,
rather than permanently selecting the line that happened to be default before.
Deleting the default does not silently move calls onto another provider.

Incoming routing is independent. For example, set line A's fallback destination
to Kitchen and line B's to Office. Calls whose called number already matches a
phonebook destination use that match first. Choose the real phone name in the
picker, including the phone named after your HA instance.

Each line also has its own permitted audio and video codecs. An empty video
list makes that trunk audio-only. **Send Camera** only controls your camera:
turning it off does not prohibit receiving the other party's video on a
video-capable line. Codec selection does not add transcoding capabilities.

For registered lines, readiness indicates the client's usable registration
state. A static line does not REGISTER; its ready state must not be read as a
successful provider registration. The entity also exposes registration mode,
transport and the last SIP status code. See [SIP trunks](SIP_TRUNK.md).

## Decide what happens when a phone does not answer

Open that phone's card, then **Options > Call routing**. The same settings are
available through **VoIP Stack: Configure phone routing**, using HA's device
picker. This includes ESP mirror cards: the policy is stored in HA.

![Phone options and expanded call-routing settings](images/pbx-phone-routing-green.png)

The screenshot shows a 20-second voicemail rule being prepared before saving.
Enable **Ringing timeout** to choose how long this phone rings and what happens
afterwards. The outgoing-line selector chooses a specific trunk or follows the
integration default. The Save button becomes available when you edit the rule.

For example, to let Kitchen ring for 20 seconds and then take a message:

1. Enable Kitchen's mailbox as described below.
2. Enable **Ringing timeout** and enter **20 seconds**.
3. Choose **Voicemail** under **After timeout**.
4. Leave the mailbox destination empty to use Kitchen's own mailbox.

Choose **Forward** and another phone name or extension to ring that phone
instead. Choose **Hang up** to end the attempt. Forwarding every incoming call,
forwarding when busy/DND and forwarding when unavailable are separate rules.
Leave rules unset if you do not want them. No ringing timeout means no extra
PBX application deadline; caller cancellation and SIP/network timeouts still
apply.

An automation can override these defaults for its own call. A call-control
action such as Forward or Send to voicemail takes control; an automation that
only sends a notification does not. You do not need a second controller in the
card. Ring groups keep their group policy instead of allowing each member's
personal voicemail to answer while the others are still ringing.
See [phone routing](PHONE_ROUTING.md) for precedence and examples.

## Configure and use voicemail

**Voicemail** is the feature. A **mailbox** is one phone's message inbox, where
its messages are stored. There is no separate mailbox server to install.

Voicemail is available by default. Choose an HA TTS entity, language and optional
voice under **Reconfigure > Voicemail voice** for spoken menus. Enable
**Enable this phone's mailbox** in the desired phone card's Options. This also
works for an ESP mirror card. Messages are stored on HA, not in the ESP's flash.
Enabling the mailbox alone does not redirect unanswered calls: configure the
no-answer rule separately. Disabling **Enable this phone's mailbox** permanently
deletes that mailbox, its messages, PIN and settings. Re-enabling starts empty.

On an HA softphone, **Voicemail** appears when messages exist. Open it to see the
caller and date, select a message to listen, or use Delete for that message.
The card uses the phone's ordinary call and DTMF controls. It does not replace
an unrelated active call to perform a mailbox operation.

A SIP handset can use `*97` as its voicemail button's destination. An identified
ESP can also dial `*97` through the HA PBX to hear its own messages. The same
spoken menu is used. Press `1` for the next message, `2` to delete, `3` to repeat,
or `#` to hang up. A digit interrupts the spoken prompt immediately.
To access another phone's mailbox, use `*98` or `mailbox.<phone>` and its PIN.

Do not confuse listening with leaving a message: `voicemail.<phone>` or
`*60<extension>` goes directly to recording a new message for that phone.
A routing automation can use **Send to voicemail** after a spoken greeting.
Do not append Hang up: voicemail then owns recording and termination.

SIP message-waiting indicators use MWI (`message-summary`). MWI reports counts,
not a downloadable list of messages. ESP display integration for a message list
is not implied by enabling its HA mailbox. Removing a phone removes its mailbox
and saved voicemail; do not remove and recreate a phone merely to rename it.

The **Voicemail received** automation trigger supplies the saved file reference.
You can send that file through a suitable notification/email integration, or
transcribe it with an existing HA STT provider first. STT is optional and does
not replace the TTS needed for menus. See [voicemail](VOICEMAIL.md) for ready-to-use
automations and all keypad commands.

## Park a call or answer another ringing phone

Parking is available by default. Customize it under **Reconfigure > Call parking**. The default access
number is `700`; available slots start at `701`.

Suppose you are talking on Kitchen and want to continue on Office:

1. While on the call, press **Park** in the HA phone card. A SIP handset can
   transfer the call to **700** instead. These are two ways to perform the same
   parking operation, not two consecutive steps.
2. Note the assigned slot, for example **701**. Your phone is released while
   the other party remains connected to HA.
3. From Office, dial **701**. You retrieve that parked conversation.

You do **not** park in 701 and then transfer to 700. `700` asks the PBX to park;
`701` identifies a particular slot. A timeout can return the call to the phone
that parked it, or another configured destination.

Directed pickup is different: if Kitchen (extension 101) is still ringing,
dial **`*8101`** or **`pickup.Kitchen`** from Office to answer it there. No parking
step is needed. Current parking and directed pickup support audio calls and
reject active video before moving the call. Directed pickup targets a ringing phone, including a member of a ring group.
Groups containing only HA browser phones use the same pickup confirmation as
mixed SIP/browser groups. See [parking and pickup](CALL_PARKING.md).

## Transfer, monitor and record calls

For a simple transfer, choose the destination using the existing transfer
controls. For a consultation transfer on a compatible SIP handset, hold the
first call, call the other phone, then complete the transfer. This requires the
handset's REFER/Replaces support. Busy-lamp subscriptions use standard SIP
`dialog` events; voicemail indicators use the separate `message-summary` event.
See [the user guide](USER_GUIDE.md#forward-or-transfer-a-call).

To record an answered conversation, enable **Show recording controls** in the
HA softphone card editor, then use **Record call** and **Stop recording**. Showing
the buttons does not start recording automatically. HA actions offer the same
controls; hangup also finishes the file. Saved conversation recordings are
separate from voicemail messages.

HA must carry the audio. Direct device-to-device calls that bypass HA cannot
use this recorder, and conference recording is not supported. The recording
finished event supplies the saved file for further automation. See
[conversation recording](CALL_RECORDING.md).

## Keep ordinary call automations simple

The published preview already supports backend Automation callers, TTS and
recorded media playback. They are useful building blocks, not new requirements
for the phone features above. A scheduled automation can call a destination,
wait for the answer, play a message and hang up without an open browser.

Actions in the same ordinary Automation-call sequence follow its call; native
VoIP triggers also provide their own call context. You do not need to find the
last active call or copy a changing Call-ID for these standard workflows.
With multiple providers, the Call action can additionally select the outgoing
trunk. See the [automation cookbook](AUTOMATION_DIALPLAN.md).
