> **VoIP Stack for Home Assistant does not require ESPHome or ESP devices.** You can use the integration with browser softphones and standard SIP phones.
>
> **For ESP firmware builds only: ESPHome 2026.10.0 or newer is required.**
>
> Audio Stack, ESP VoIP Stack, Runtime Controller and the maintained device YAMLs now follow ESPHome 2026.10's audio interfaces. Upgrade your ESPHome builder before compiling this firmware; older builder versions are no longer supported.
>
> ESPHome 2026.10 includes substantial audio improvements and several of our accepted contributions. Thank you to the ESPHome team for your work, reviews and accepted PRs! <3

# VoIP Stack 2026.11.0
## It's starting to look like a damn PBX.

Changes since stable **2026.10.1**.

**ESP firmware migration:** the shared packages have been reorganized. Start from the updated YAML for your exact board and bring over your settings. [Migration checklist](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/BREAKING_CHANGES.md#development-firmware-new-device-packages).

VoIP Stack is getting a little too confident. After all the work we've been putting into it, it's starting to look suspiciously like a proper PBX.

**This time, we have a lot of new stuff to show you.**

First, thank you for the growing interest in this project. It is great to see how you use it, receive concrete feedback and discover applications I hadn't thought of. Your suggestions are helping shape where it goes next.

Some features came directly from your requests. Others were added because, apparently, leaving a perfectly reasonable intercom alone wasn't an option.

The goal is becoming more ambitious: useful PBX tools inside Home Assistant, for homes, offices, shared buildings and more demanding installations. Small and medium-sized business scenarios are an interesting direction to explore too.

Meanwhile, the house can transfer callers, take messages and keep someone waiting while you move to another room. **A decent improvement over shouting "IT'S FOR YOU" through three walls.**

![VoIP Stack phone card](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/88385dd3af840320af4cc281e779c5a5a3aa7c78/docs/images/pbx-phone-idle-green.png)

## The PBX tools are ready when you need them

Parking and voicemail no longer have separate global enable switches. Calls, transfer, pickup, BLF/MWI and the other PBX tools share the same backend; there is no separate Lite/Advanced engine.

Configure parking numbers and timers, choose a voice for spoken voicemail menus, and create mailboxes only for the phones that need them. Initial setup does not require a TTS provider. Existing phone settings, recordings and mailboxes are preserved. Turning off an individual mailbox still explicitly deletes that mailbox and its messages.

A previous global `enabled: false` for parking or voicemail is deliberately removed by migration. Review feature codes and per-phone routing/resource settings if you previously disabled those applications; no mailbox or recording is created automatically.

Unused facilities do not start hold audio or open an unused media archive. Closing the last idle card also releases its shared activity subscription. Existing numbers take priority over conflicting default feature codes, with HA Repairs explaining what needs attention.

## We have call transfer!

Make or answer a call, then pass the conversation to the person or room that should actually deal with it: a colleague, another Home Assistant softphone or a SIP phone, within the supported transfer paths.

During the call:

1. Press **Transfer**.
2. Choose a contact, or switch to **Keypad** and enter a destination.
3. Press **Transfer** to confirm.

The card reuses the contact list and keypad you already know. Opening the transfer view does not transfer anything until you confirm.

"I'll put you through" is finally a feature, not just a threat.

![Choose a transfer destination](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/88385dd3af840320af4cc281e779c5a5a3aa7c78/docs/images/phone-transfer-dark.png)

**The HA card currently provides blind transfer only.** You send the caller to the destination without speaking to that person first. It does not offer a second consultation call.

**Attended transfer is available from compatible SIP handsets or softphones supporting REFER/Replaces:** hold the caller, call the other extension, speak to that person, then complete the transfer. The answered destination dialog is retained rather than calling that person again.

HA automations can use the transfer action too. A failed preparation keeps the original conversation available when its caller is still present. Transfer support depends on the endpoints and media path. For a browser-to-browser call handed to a SIP phone or trunk, disable both local cameras first; active local video is not supported on that handoff. Endpoint and media limitations still apply; see the current snapshot limitations below.

[Transfer instructions and supported combinations](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/USER_GUIDE.md#forward-or-transfer-a-call)

## Park here, pick up over there

Want to continue on another phone? **Park the call.**

Press **P** during an established audio call. The PBX assigns a slot, such as **701**, which appears in the card. Dial that slot from the same phone or another device to resume the conversation.

The caller waits. You move. Nobody has to carry the wall-mounted kitchen tablet upstairs.

![Parking control during an audio call](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/88385dd3af840320af4cc281e779c5a5a3aa7c78/docs/images/pbx-phone-audio-call-green.png)

![Call parked in slot 701](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/88385dd3af840320af4cc281e779c5a5a3aa7c78/docs/images/pbx-phone-parked-green.png)

<details>
<summary>📷 Retrieve the parked call</summary>

Dial the slot shown by the card:

![Dial the assigned parking slot](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/88385dd3af840320af4cc281e779c5a5a3aa7c78/docs/images/pbx-phone-pickup-green.png)

The conversation resumes on the phone that called the slot:

![Conversation resumed after retrieval](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/88385dd3af840320af4cc281e779c5a5a3aa7c78/docs/images/pbx-phone-retrieved-green.png)

</details>

SIP phones can also park by transferring an answered call to the access code **700**. The card button and transfer to 700 are alternative ways to park. **The assigned slot, such as 701, is the number used to retrieve the call.**

Optional hold audio uses HA Media. With no hold media configured, the parked caller waits in silence. A timeout can return the call to the original phone or another configured destination.

Parking is available by default, with configurable access and slot numbers. It currently supports audio calls, not active video.

[Parking and retrieval guide](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/CALL_PARKING.md)

## Answer someone else's ringing phone

**Directed pickup** lets you answer a call still ringing on another phone, including a member of an audio ring group.

Use the native **Pick up call** action and select the phones by name. SIP phones can also use the documented codes, such as `pickup.Kitchen` or **`*8101`** for extension 101.

The original caller keeps the same call. There is no need to hang up and try again.

Useful when a colleague has stepped away. Or when they're still at their desk but have spiritually left the building.

![Pick up another ringing phone](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/88385dd3af840320af4cc281e779c5a5a3aa7c78/docs/images/pickup-ringing-action-dark.png)

## Trunks and contacts, configured visually

SIP trunks and contacts can be added and managed through the integration's visual configuration.

Open **Settings → Devices & services → VoIP Stack** and use the controls to add the entry you need. Each trunk has its own readable name, configuration, associated device and readiness entity.

![Add SIP trunk from the integration menu](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/88385dd3af840320af4cc281e779c5a5a3aa7c78/docs/images/pbx-add-trunk-menu-dark.png)

### Trunk configuration has moved

Provider settings are **no longer entered in the main VoIP Stack setup or reconfiguration flow**.

Use **Add SIP trunk**, available from the integration menu and its shortcut. To edit a line, open that named trunk's configuration.

An existing enabled single trunk is migrated automatically, preserving credentials, incoming routing and the outgoing default. **Do not delete the integration or recreate your working trunk to migrate it.**

If an automation used the old trunk-status entity, select the current entity on the named trunk device rather than assuming its entity ID is unchanged.

[Migration instructions](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/BREAKING_CHANGES.md#pbx-development-branch-separate-sip-trunk-entries)

## Multiple SIP lines, with independent rules

Connect multiple providers or PBXs and configure registration, transport, credentials, codecs and incoming destinations independently.

Static trunks without REGISTER were already available. **Managing multiple independent lines is the new part.**

![Configure a named SIP trunk](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/88385dd3af840320af4cc281e779c5a5a3aa7c78/docs/images/pbx-add-trunk-dark.png)

The outgoing line is selected in this order:

1. The trunk explicitly chosen in the Call or Forward action.
2. The calling phone's preferred outgoing trunk.
3. The integration default.

The first line becomes the default automatically. You can change it when adding another line. **Use default** follows the current default selection.

An unavailable selected line produces an error instead of silently using another provider. Removing the default trunk creates an HA Repair asking you to choose a replacement.

Incoming routing is independent: one line can ring Kitchen, another Office. A called number matching the phonebook takes precedence over the fallback destination.

Audio and video codec lists are configurable per trunk. An empty video list makes the line audio-only; selecting a codec does not install additional transcoding support.

[Trunk configuration and examples](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/SIP_TRUNK.md)

## Your phone, your forwarding rules

In **Options → Call routing**, choose what should happen:

- always forward to another destination;
- use an alternative when busy or in Do not disturb;
- forward when the phone is unavailable;
- set a ringing timeout followed by **Hang up**, **Forward** or **Voicemail**.

![Phone routing rules](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/88385dd3af840320af4cc281e779c5a5a3aa7c78/docs/images/pbx-phone-routing-green.png)

Leave the timeout unset if you do not want an additional PBX ringing deadline. SIP/network timeouts and caller cancellation still apply.

Settings belong to the phone in the backend and remain effective when the card is closed. ESP mirror cards edit these same policies. **Calls sent directly to an ESP's IP that bypass HA do not pass through these routing rules.**

Automations can take control of their own call. A routing action does so; an automation that merely sends a notification leaves the default handling intact. Ring groups retain their group policy while several members are ringing.

[Phone routing walkthrough](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/PHONE_ROUTING.md)

## Voicemail for each phone

Each phone can have its own **mailbox**.

Create a mailbox on the phone and configure when calls should reach it. Select an HA TTS provider, language and optional voice in the Voicemail voice panel for spoken telephone menus. Recording a message with the configured greeting/tone does not require TTS; spoken menu navigation does.

For example: let Kitchen ring for 20 seconds, then send the call to voicemail.

**Enabling a mailbox does not automatically enable forwarding to voicemail.** Those are separate settings.

![Configure a phone mailbox](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/88385dd3af840320af4cc281e779c5a5a3aa7c78/docs/images/mailbox-action-dark.png)

The card displays the voicemail control when messages exist. See the caller and date, listen to a message or delete it. During a conversation, that button leaves room for the call controls. Listening and deletion use the same call and DTMF menu as a telephone.

From a phone:

- **`*97`** opens the identified phone's own mailbox, without a PIN.
- **`*98`** or `mailbox.<phone>` accesses another mailbox using its PIN.
- **1** selects the next message, **2** deletes, **3** repeats and **#** hangs up.
- `voicemail.<phone>` or `*60<extension>` leaves a message directly.

SIP phones can receive standard **MWI/message-summary** notifications. An identified ESP can call its mailbox through HA too; that does not automatically add a message-list screen to its physical display.

Messages remain in HA Media. The **Voicemail received** trigger supplies the existing file reference for notifications and other automations. Optional transcription uses an installed HA STT provider.

**Disabling a mailbox deletes its messages and folder.** Re-enabling it starts with an empty mailbox. Removing the phone also removes its associated mailbox.

[Voicemail setup, keypad commands and automations](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/VOICEMAIL.md)

## Record a conversation when you need it

Enable **Show recording controls** in the card editor, then use **Record call** and **Stop recording** during a supported conversation. HA actions provide the same controls.

Recording is **off until explicitly started**. Hangup finishes it and the mixed WAV file is saved in HA Media. The recording-finished event can continue an automation.

The recorder uses the existing call media path. A recording error stops recording, not the conversation.

The current scope is supported two-party calls whose media passes through HA. Direct calls bypassing HA and conferences are outside that scope.

[Recording guide](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/CALL_RECORDING.md)

## Native Home Assistant phone automations

Call, forward, transfer, park, pick up, speak, play audio and hang up using native HA actions.

Phone-state and mailbox triggers, together with native conditions, let you build workflows in the visual editor. Select phones by name. Ordinary recipes do not need a wall of mysterious device IDs.

![Select the phone and destination in the visual action editor](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/88385dd3af840320af4cc281e779c5a5a3aa7c78/docs/images/call-action-fields-dark.png)

Softphones and automations use the same PBX operations.

An **Automation contact** can originate a call from the backend without an open browser or a physical source phone. Wait for an answer, deliver a TTS message or play an audio file, then hang up.

Your 07:30 wake-up call no longer needs a receptionist. Whether this improves humanity remains debatable.

![Choose the TTS provider and announcement text](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/88385dd3af840320af4cc281e779c5a5a3aa7c78/docs/images/tts-action-dark.png)

The additions include:

- selecting a named outgoing trunk for an announcement;
- playing WAV/MP3 audio from HA Media or an HTTP(S) URL;
- waiting without answering, leaving a call ringing before forwarding;
- persistent external-call permissions per phone, without blocking internal or incoming calls;
- reading phone state through a native action;
- picking up a call using the identity supplied by a native trigger, without constructing it manually.

<details>
<summary>📷 A native ringing trigger, condition and pickup action</summary>

![Native phone-state trigger](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/88385dd3af840320af4cc281e779c5a5a3aa7c78/docs/images/phone-state-trigger-dark.png)

![Native phone-state condition](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/88385dd3af840320af4cc281e779c5a5a3aa7c78/docs/images/phone-state-condition-dark.png)

![Pick up call action](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/88385dd3af840320af4cc281e779c5a5a3aa7c78/docs/images/pickup-action-dark.png)

</details>

These additions include requests [#136](https://github.com/n-IA-hane/esphome-intercom/issues/136), [#138](https://github.com/n-IA-hane/esphome-intercom/issues/138), [#117](https://github.com/n-IA-hane/esphome-intercom/issues/117) and [#119](https://github.com/n-IA-hane/esphome-intercom/issues/119).

[Automation recipes](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/AUTOMATION_DIALPLAN.md)

## Shared phone state, BLF and card activity

The backend exposes call state to HA actions, triggers and standard SIP **BLF/dialog** subscriptions. Mailbox notifications remain separate through MWI.

Occupied parking slots appear in the card's side list. Other phone activity is shown without moving the central call buttons. The current interlocutor is not repeated as Busy, and empty lists take no space. ESP mirror cards also exclude the current conversation from the side activity list, so the same ringing status is not shown twice. Calls belonging to other conversations remain visible.

ESP-to-ESP calls that completely bypass the PBX are not HA-owned SIP dialogs and do not necessarily have the same visibility in its projections.

The future tiled operator dashboard remains a separate project. **It is not included in this release.**

## A card that fits Home Assistant better

Audio and video controls use consistent icons, accessible labels and tooltips. The card follows native HA theme surfaces, borders and shadows more closely, including translucent themes.

![Phone options](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/88385dd3af840320af4cc281e779c5a5a3aa7c78/docs/images/pbx-phone-options-green.png)

The bottom video-call bar remains responsive, with controls arranged without covering the call duration. The keypad closes after dialing; open it again when you need DTMF during a call.

![Video received from a Mobotix door station through FreeSWITCH](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/88385dd3af840320af4cc281e779c5a5a3aa7c78/docs/images/pbx-mobotix-video-green.png)

<details>
<summary>📷 DTMF keypad during the video call</summary>

![DTMF keypad during the video call](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/88385dd3af840320af4cc281e779c5a5a3aa7c78/docs/images/pbx-mobotix-keypad-green.png)

</details>

## Improved FreeSWITCH and FusionPBX interoperability

The new guide explains the native media settings involved in audio-only calls, video calls and changes during an established conversation: late negotiation, codec inheritance, codec mixing and propagation of stream changes through re-INVITEs.

The aim is to negotiate the actual participants' capabilities, without maintaining a contact list labelled "has a camera" or "doesn't have a camera." These are standard FreeSWITCH mechanisms, not a special VoIP Stack protocol.

Provider-facing profiles, early media and other PBX applications still need appropriate configuration.

For NAT scenarios, a trunk can specify a reachable media IPv4 address without changing LAN calls or SIP registration. Correct RTP/RTCP mappings are required. **Automatic DDNS handling for this option is not included.**

[FreeSWITCH/FusionPBX guide](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/FREESWITCH.md)

## Rename entities without breaking your phones

Phone lookup, routing and access checks follow Home Assistant's entity registry when an entity ID changes.

Shared phonebook and call-event entities can also be renamed without breaking automatic card discovery. ESPHome entities retain their device ownership.

You can tidy up your entity names without accidentally disconnecting the receptionist.

Addresses [#145](https://github.com/n-IA-hane/esphome-intercom/issues/145).

## Czech support and localized Assist responses

Thanks to **@hruba202** for the Czech integration translations and voice-command contribution in [#140](https://github.com/n-IA-hane/esphome-intercom/pull/140).

The integration strings include the newer PBX and named-trunk settings. Assist commands use shared localized response templates with resolved contact names and appropriate errors.

Thanks also to **@Gafielt** for the German commands and response-localization work in [#137](https://github.com/n-IA-hane/esphome-intercom/pull/137). Contributor attribution is retained.

Custom sentence examples must be installed separately; HACS does not copy them into your HA configuration. This translation does not include the separate dashboard card or voicemail IVR prompts.

[Assist commands and installation](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/ASSIST_INTENTS.md)

## Everyday behavior and reliability improvements

There are also changes you should notice mainly because something annoying stops happening:

- **Settings saves:** ringing-timeout edits remain visible until the backend confirms them.
- **Voicemail:** supported default-language selection and resource cleanup after storage-scan errors.
- **Audio startup:** reserve RTP before outgoing INVITE and prepare browser processors earlier.
- **Companion Android:** correct the audio-output transition that could hide the first word of an IVR greeting on the verified device, without adding a fixed greeting delay or requiring a modified APK.
- **Superseded browser setup:** an older cancelled audio preparation no longer incorrectly fails the current call.
- **Video changes:** preserve negotiated audio while video is added, removed or renegotiated; Send Camera controls transmission separately from reception.
- **Call labels:** display the actual destination rather than the previously selected idle contact. Service acknowledgements no longer replace the complete call snapshot.
- **Duplicate extensions:** report conflicts through HA Repairs and reject ambiguous numbers.
- **SIP transfers:** match progress notifications to the correct transfer so an old notification cannot complete a newer one.
- **Attended transfers:** clean up failed replacements and roll back staged resources while preserving an original conversation that is still valid.
- **Group pickup:** confirm the recovering phone before retiring other answerers; rejected attempts do not create duplicate members or false ringing.
- **Route decisions:** accepted forwarding or cancellation retains priority when pickup preparation fails while another branch answers.
- **Session timers:** a successfully refreshed call is not terminated using its old expiry time.
- **TCP and trunks:** improve dialog ownership, registration cleanup, UDP NAT-signaling maintenance and availability immediately after adding the first trunk.
- **Dahua:** retain negotiated receive mappings independently of transmit selection and scope compatibility behavior to the explicit peer profile ([#115](https://github.com/n-IA-hane/esphome-intercom/issues/115)).

The experimental native-Companion calling/menu prototype has been retired from distribution. **The normal dashboard softphone inside Companion remains available.** Update automations that referred to a retired prototype endpoint.

## ESPHome component updates

Updating HA and updating ESP firmware are separate operations. The following additions require rebuilding and uploading the relevant device profile.

### Pick the functions and keep the wiring together

The maintained device YAMLs now include **one device package** and an editable **`firmware_features` list**:

```yaml
firmware_features: [voice, wake_word, voip, music, timers, status_led, diagnostics]
```

This example keeps Assist, wake word, calls, HTTP music and timers without Sendspin. Edit the existing list in the board YAML, rather than adding another list in an overlay. ESPHome selects the corresponding consumers, controls, callbacks and presentation before compilation. Shared player/mixer dependencies are composed once.

Unknown feature names, such as `voiip`, now fail configuration validation with the supported choices. A feature whose dependency is missing produces an explicit warning: for example, `sendspin` requires `music`. The check does not silently enable dependencies or add runtime tasks.

Removing music still allows Assist to speak. Removing VoIP removes the call controls and bindings. AFE/AEC/native audio remains the board profile's separate choice; adding a screen or video feature name does not give a headless board new hardware.

The board package owns wiring, the audio profile owns capture/playback, feature packages own their behavior, and presentation packages own geometry. The catalog shows exactly where each supplied profile leads. Generic Full Lite keeps Sendspin excluded from its default 4 MB configuration; check the actual OTA image size after rebuilding.

**Start from the refreshed YAML for your exact model and revision.** Keep the device name, secrets, network/API/OTA settings, custom pins and wanted behavior. Do not paste the old FULL preset or whole player/mixer blocks over the new device package. Existing leaf includes remain for custom configurations with their documented prerequisites.

[Step-by-step package examples](https://github.com/n-IA-hane/esphome-intercom/blob/main/packages/README.md) · [Device catalog](https://github.com/n-IA-hane/esphome-intercom/blob/main/packages/DEVICE_CATALOG.md) · [Migration checklist](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/BREAKING_CHANGES.md#development-firmware-new-device-packages)

### Official ESPHome audio providers, with our components around them

Maintained firmware now uses the official ESPHome **audio, resampler, mixer,
player, Voice Assistant, Micro Wake Word, microphone, speaker and OTA** providers.
Their local copies are retired, together with the HTTP MIME, Sendspin-source and
DSP-placement adapters. Audio Stack/AEC/AFE, ESP VoIP Stack and Runtime Controller
keep their existing responsibilities and public interfaces.

Board wiring, microphone downmix/BSS, codec calibration, PCM formats, UI and
application features remain in the device packages. Music, announcements and
VoIP retain their separate mixer inputs. The P4 display adaptation and AFE's GMF
vendor dependency are separate; this does not claim that every vendor fork has
been removed.

**For copied YAMLs, start from the current board example.** Remove the retired
external-provider names, `tts_playback_start_timeout`, MWW `task_core` and
`ring_buffer_duration`, and private resampler `task_core`/`task_priority` fields.
The standard providers use their own scheduling, timeout and Stop behavior.
[Native provider migration](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/BREAKING_CHANGES.md#development-firmware-native-audio-providers)

### ESP VoIP Stack: blind audio transfers over UDP and TCP

The ESP can receive a blind SIP REFER, call the supplied destination and report the outcome to the PBX. The original conversation stays available during destination preparation. A successful answer switches peers; a failed attempt preserves the original conversation if its caller is still present.

UDP/TCP handoffs and retransmissions belonging to retired dialogs are handled without adding a second audio engine. A remote hangup of the original conversation does not automatically cancel an accepted transfer; a local hangup on the ESP cancels the pending operation.

The receiver's current scope is **blind audio transfer to IPv4 SIP addresses**. It does not include video transfers, hostname targets or attended transfers with Replaces. Additional authentication challenges from a replacement destination fail explicitly; the receiver does not retry with unrelated credentials.

The replacement keeps the active transmit format. The documented replacement path uses L16/16 kHz in both directions; ordinary calls can negotiate 48 kHz reception with 16 kHz transmission.

### Choose an outgoing line for one ESP call

HA can select a trunk for a particular ESP-originated call. The request carries its own identity, so cancelling it cannot terminate a later conversation. Ordinary ESP phonebook calls retain their existing behavior.

### Asterisk and FreeSWITCH with ESP extensions

The classic PBX guide now explains both calling an ESP and **making outgoing calls from the ESP through the PBX**.

It covers static SIP endpoint configuration, inbound and outbound routing, Asterisk PJSIP and FreeSWITCH examples, codec choices and supported PCM combinations with different receive/transmit rates.

The documented setup uses static ESP peers without REGISTER and can operate independently of Home Assistant's PBX.

[Conventional PBX setup and outgoing calls](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/CLASSIC_PBX.md)

### Audio Stack: restart playback safely after a stop

Audio Stack rejects new writes until a pending buffer reset completes, allowing the producer to retain those samples and retry. This prevents new audio from being discarded by an earlier reset, leaving playback waiting for samples that will never be played.

The shared packages now use the official ESPHome mixer and player lifecycle. Their maintenance sequence stops the playback sources before the hardware output. The Audio Stack reset correction addresses one reproduced path investigated in [#129](https://github.com/n-IA-hane/esphome-intercom/issues/129); the separate long-uptime investigation remains open.

Speaker writes now accept the complete PCM frames that fit, including when a decoder supplies a block larger than the buffer. Unaccepted samples remain with the producer for retry. The standard ESPHome resampler now supplies the playback conversion path. Audio Stack retains bounded, frame-aligned writes and keeps rejected PCM with its producer.

An optional **read-only ES8311 diagnostic package** adds a manually triggered register dump. It does not write codec registers or add periodic polling.

### Refreshed audio dependencies and cleaner stop/restart

The coordinated firmware refresh integrates **GMF AI Audio 1.1.1**, explicitly pins **ESP-SR 2.6.0**, and uses official ESPHome's **esp-audio-libs 4.0.1** and **micro-FLAC 0.3.0**. ESP-SR 2.6.0 was already present in the previous resolved firmware locks; the change makes that selection explicit. Sendspin follows ESPHome 2026.10 and uses **0.9.3**, with native encryption and pairing.

Immediate processed-PCM delivery is preserved. GMF's updated shutdown ordering and stack placement are integrated with the existing Audio Stack owner. Writes rejected during shutdown honor their caller's wait budget instead of letting a mixer repeatedly retry without yielding.

Playback Stop and natural end-of-file use the official player's lifecycle. Music and announcements remain independently controlled. HTTPS playback includes the certificate bundle needed by the source.

One shared OTA lifecycle stops the selected sources, suspends the speaker/display in order and restores readiness after an interrupted update. An old delayed pause cannot run after recovery and leave the screen or audio stuck. Recovery does not restart an old song or originate a call.

The Box FULL AFE baseline has been exercised with calls, overlapping media and announcements, real HA announcements, Stop/restart, native Sendspin and completed/interrupted OTA. Earlier headless Waveshare and Spotpear checks belong to their recorded firmware snapshots; the native Box result is not blanket qualification of every board or long-uptime workload.

### Runtime decisions you can actually follow

The Runtime Controller guide now starts with music, an incoming call and the return to music. Small examples introduce activities, events and priorities before custom adapters. Optional debug dumps identify the activity that selected each output policy, using the existing resolver.

A longer hardware YAML is not evidence of a second state machine: pins, images and screen geometry still need definitions. The new device composition removes repeated wiring, while the controller keeps competing activities coordinated.

### Native ESPHome cameras and custom LED states

The JPEG source accepts native ESPHome camera IDs and requests the next image outside the previous image callback. Hanging up does not need to stop another web/API stream from the same camera.

Hardware-specific and call-direction checks remain relevant, including HA-to-S3 camera use.

Runtime Controller also adds an optional empty LED preset with explicit state styling for custom device behavior. Existing presets retain their defaults.

Thanks to the contributors behind [ESP VoIP Stack #6](https://github.com/n-IA-hane/esphome-voip-stack/pull/6) and [Runtime Controller #1](https://github.com/n-IA-hane/esphome-runtime-controller/pull/1).

[Camera setup and current limits](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/SIP_VIDEO.md)

## Waveshare 1.85C-BOX V2: one YAML, your choice of processing

The new unified FULL YAML keeps the choices at the top:

```yaml
substitutions:
  audio_processor: aec
  mic_mix: average
  voip_transport: udp
```

The default combines both microphones into standalone mono AEC. Choose
`audio_processor: afe` and `mic_mix: passthrough` for dual-microphone AFE/BSS;
AFE with `average` instead receives mono. The package selects the matching
backend, controls and channel count without copying the board wiring. The old
AEC/AFE entrypoints remain valid with their previous microphone choices.

Assist, VoIP, HTTP playback, Sendspin, timers and the circular interface remain
configured. Both processing paths share this board's codec/reference/amplifier
calibration. All eight native 360x360 animation poses are retained. FULL AFE
uses ESPHome's official SIZE optimization to fit the existing OTA slots.

The updated AFE was checked on this Box with real calls, local music and speech,
48/16 kHz announcements, independent stops/restarts and both hangup sides.
Raw/processed recordings and cancellation off/on comparisons confirm reduced
playback leakage in these windows. They do not qualify natural barge-in,
independent near-end speech, every enclosure or long uptime.

[Get the unified YAML](https://github.com/n-IA-hane/esphome-intercom/blob/main/yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml)
| [Read the processing and hardware notes](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/WAVESHARE_V2_PROCESSING.md)

## Espressif ESP32-S3-BOX-3: dual-microphone FULL AFE

A separate experimental FULL AFE profile enables both ES7210 microphones, speech enhancement and a software echo reference through Audio Stack on **Espressif's ESP32-S3-BOX-3**.

The existing contributor-tested single-microphone AEC profile remains available. Physical microphone mapping, acoustic behavior and operation with the complete workload still need device checks.

**This is Espressif's BOX-3, not the Waveshare 1.85C-BOX V2.** Apparently, naming every device "some variation of box" was a collective industry decision.

[Experimental profile](https://github.com/n-IA-hane/esphome-intercom/tree/main/yamls/experimental/esp32-s3-box-3) · [Community discussion #116](https://github.com/n-IA-hane/esphome-intercom/issues/116)

## More practical documentation

The illustrated user guide walks through transfer, parking, ringing-call pickup, voicemail, announcements and native HA triggers, conditions and actions.

Phone pickers use readable names. Ordinary examples start in the visual editor; detailed configuration remains available for advanced setups. The README and firmware guides now progress from a first call or an unchanged board profile to feature selection, shared voice/music/calls, custom settings and finally hardware/runtime internals.

[Illustrated user guide](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/USER_GUIDE.md) · [PBX workflows](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/PBX_WORKFLOWS.md)

## Further firmware changes since stable 2026.10.1

### One board YAML, selectable microphone processing

- **New optional dual-microphone downmix** works with TDM inputs and codec-free
  I2S Left/Right microphones. Audio Stack owns channel extraction and averaging;
  AEC/AFE keep their normal mono or dual-microphone contracts.
- The Waveshare 1.85C BOX V2 now has one short FULL entrypoint with
  `audio_processor: aec|afe`, `mic_mix: average|passthrough` and
  `voip_transport: udp|tcp`. Shared packages select the matching processor,
  controls and microphone count. Existing AEC/AFE filenames remain compatible.
- Standalone AEC can receive the two-microphone average. AFE can receive two
  separate channels for BSS or a mono average. Inconsistent manual overrides
  fail ESPHome's native YAML validation before C++ compilation.
- Both Box backends share the verified microphone/reference/amplifier
  calibration. FULL AFE uses the official SIZE optimization to retain all
  features and native 360x360 assets within the existing OTA slots.
- The Box FULL AFE passed bounded real SIP/music/announcement/stop/restart
  checks and off/on acoustic playback comparisons. Natural wake-word accuracy,
  independent near-end speech/double-talk and long uptime remain separate checks.
- Optional analog PGA controls and bounded PCM capture make raw microphones,
  reference and processed output inspectable. They are compiled only when
  configured. Same-rate conversion avoids unused channel scratch.
- Runtime I2S failure now reconciles audio adapters and retires stale PCM.
  ESP VoIP preserves a still-ringing REFER replacement after the original
  INVITE ends, and properly closes incoming calls whose media preparation fails.
  Microphone-only and speaker-only firmware builds are checked separately.

[Unified Box YAML](https://github.com/n-IA-hane/esphome-intercom/blob/main/yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml)
| [Processing choices and measured limits](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/WAVESHARE_V2_PROCESSING.md)
| [General microphone delivery and tuning](https://github.com/n-IA-hane/esphome-audio-stack/blob/main/docs/AUDIO_TUNING.md)

These changes affect ESP firmware packages and documentation. HA backend/card
logic is unchanged by this update. Updating HACS alone does not install ESP
firmware.

### Microphone wiring and delivery: illustrated configuration guide

The full guide is included here and in the [Audio Stack README](https://github.com/n-IA-hane/esphome-audio-stack/blob/main/README.md#microphone-wiring-and-delivery-illustrated).

New to Audio Stack? The [annotated starting example](https://github.com/n-IA-hane/esphome-audio-stack/blob/main/README.md#1-start-with-a-microphone-and-speaker)
shows a **48 kHz bus and speaker with a 16 kHz microphone**, explaining
each field inline. A [complete YAML](https://github.com/n-IA-hane/esphome-audio-stack/blob/main/examples/00-generic-i2s-duplex.yaml)
is provided; microphone conversion does not limit the speaker to 16 kHz.

<details>
<summary>Show wiring diagrams, YAML choices and supported combinations</summary>

## Microphone wiring and delivery, illustrated

**Choose the physical inputs first, then choose what Audio Stack delivers to
the processor.** AEC and AFE do not need separate microphone drivers:

```text
                    [Physical microphone(s)]
                                │
                                ▼
              [Audio Stack: capture/select/convert]
                                │
                       Choose ONE delivery
            ┌───────────────────┴───────────────────┐
            ▼                                       ▼
      [passthrough]                             [average]
     1 or 2 channels                          mono (A+B)/2
            │                                       │
            └───────────────────┬───────────────────┘
                                ▼
                       [AEC / AFE / none]
                                │
                                ▼
                        [Mono microphone]
                                │
                    ┌───────────┼───────────┐
                    ▼           ▼           ▼
                   MWW       Assist       VoIP
```

The following YAML blocks are **fragments to merge into an existing board
configuration**, not complete firmwares. Keep its pins, codecs, clocks and
component declarations. Include the selected processor in `external_components`
and declare it once. GPIOs and TDM mappings below are examples, not universal
wiring instructions. [Complete codec-free stereo example](https://github.com/n-IA-hane/esphome-audio-stack/blob/main/examples/05-stereo-i2s-downmix.yaml)
and [complete TDM example](https://github.com/n-IA-hane/esphome-audio-stack/blob/main/examples/04-dual-mic-downmix.yaml) are available.


**Example GPIOs, not universal board pins:** the codec-free diagrams use
`GPIO5 = BCLK`, `GPIO6 = WS`, `GPIO4 = DIN` and `GPIO7 = DOUT`, matching the
complete stereo I2S example. The TDM example uses MCLK/BCLK/WS/DIN/DOUT on
GPIO12/13/14/15/16 and I2C SDA/SCL on GPIO11/10. The ES8311 example instead
uses GPIO5/6/7/4/8 for MCLK/BCLK/WS/DIN/DOUT and GPIO47/48 for I2C.
These are alternative example layouts. Use the actual board schematic, common
ground and module supply requirements; do not combine their pin maps.

### A. one digital I2S microphone

```text
ESP GPIO5 (BCLK) ─────────────► mic BCLK
ESP GPIO6 (WS) ───────────────► mic WS
ESP GPIO4 (DIN) ◄────────────── mic DATA
                                L/R selects Left

Selected Left samples ──► mono AEC / mono AFE / no processor
```

```yaml
esp_audio_stack:
  rx_slot_mode: stereo
  mic_channel: left
  mic_mix: passthrough
```

Reading stereo slots can be necessary even with only one physical microphone.
Select the occupied slot with `mic_channel`; use `right` if that is how the
microphone is wired. Without `rx_mic_slots`, Audio Stack selects one microphone.
For a microphone that supports a true mono frame, `rx_slot_mode: mono` is also
available with the matching hardware format.

### B. two compatible digital microphones on one data line

```text
                             ┌──► mic A BCLK
ESP GPIO5 (BCLK) ────────────┤
                             └──► mic B BCLK

                             ┌──► mic A WS
ESP GPIO6 (WS) ──────────────┤
                             └──► mic B WS

mic A DATA (Left) ───────────┐
                             ├──► ESP GPIO4 (DIN)
mic B DATA (Right) ──────────┘

RX frame: [ Left = mic A │ Right = mic B ]
```

This requires microphones whose datasheets permit sharing DATA: each must
release the line outside its selected slot. Strap one to Left and the other
to Right. Do not join two outputs that drive the line at the same time.

```yaml
esp_audio_stack:
  rx_slot_mode: stereo
  rx_mic_slots: [left, right]
```

No input codec or TDM mode is needed. Left/Right describes **two captured
microphones**, not a requirement for stereo speakers. The configured slot
order determines the processor's microphone order.

### C. keep both microphones independent for AFE/BSS

```text
I2S Left / TDM mic slot A ───► mic A ──┐
                                       ├──► AFE (two mics, SE/BSS) ──► mono
I2S Right / TDM mic slot B ──► mic B ──┤
Playback reference ────────────────────┘
```

```yaml
esp_audio_stack:
  rx_slot_mode: stereo
  rx_mic_slots: [left, right]
  mic_mix: passthrough
  processor_id: afe

esp_afe:
  id: afe
  mic_num: 2
  se_enabled: true
```

For TDM, replace the two `rx_*` fields with the board's `tdm_mic_slots`, as
shown below. AFE receives two separate, interleaved microphone channels and
the playback reference. Its public output remains mono. Averaging first would
remove the channel separation needed by this dual-microphone processing.

### D. average both microphones in Audio Stack

```text
mic A ──┐
        ├──► Audio Stack: (A + B) / 2 ──► one mono channel
mic B ──┘
```

```yaml
esp_audio_stack:
  sample_rate: 48000
  output_sample_rate: 16000
  rx_slot_mode: stereo
  rx_mic_slots: [left, right]
  mic_mix: average
```

Audio Stack delivers the mean of both physical microphones as one ordinary
mono source. The same `mic_mix: average` works with two TDM microphone slots.
It runs in the existing conversion pass, without an extra task, wait or DSP
queue. The bus can remain at 48 kHz while the processor receives 16 kHz.
Choose a processor in E, or omit `processor_id` to use the average without DSP.

`passthrough` remains the default and excludes averaging from compilation.
It does not implicitly combine two captured microphones. Averaging is not
beamforming, and opposite-polarity signals can cancel each other.

### E. average both microphones before mono AFE or AEC

The physical wiring and Audio Stack downmix are the same in both cases.
Choose **one** processor below. Keep the playback reference separate from the
microphone average; use the reference route actually configured for your board.

```text
AEC option:
[Mic A, Mic B] ──► [Audio Stack average] ──► [esp_aec] ──► mono mic
                                                 ▲
[Playback reference] ────────────────────────────┘

AFE option:
[Mic A, Mic B] ──► [Audio Stack average] ──► [esp_afe] ──► mono mic
                                                 ▲
[Playback reference] ────────────────────────────┘
```

**Option 1: standalone mono AEC.**

```yaml
esp_audio_stack:
  sample_rate: 48000
  output_sample_rate: 16000
  rx_slot_mode: stereo
  rx_mic_slots: [left, right]
  mic_mix: average
  processor_id: aec

esp_aec:
  id: aec
  sample_rate: 16000
  mode: sr_low_cost
```

**Option 2: mono AFE.**

```yaml
esp_audio_stack:
  sample_rate: 48000
  output_sample_rate: 16000
  rx_slot_mode: stereo
  rx_mic_slots: [left, right]
  mic_mix: average
  processor_id: afe

esp_afe:
  id: afe
  mic_num: 1
  se_enabled: false
```

AFE sees an ordinary single-microphone source, just as it would with one
physical microphone. Its enabled AEC, NS, VAD and other stages retain their
normal behavior. It does not become the standalone AEC component. Native YAML
validation rejects `mic_mix: average` with `mic_num: 2` or dual-mic SE/BSS.

For unprocessed averaged capture, omit `processor_id` and the processor
declaration. The microphone then includes any echo from the local speaker.

### F. tDM microphones, with a codec or another compatible TDM source

```text
ESP GPIO12 (MCLK) ────────────► codec MCLK
ESP GPIO13 (BCLK) ────────────► codec BCLK
ESP GPIO14 (WS) ──────────────► codec WS
ESP GPIO15 (DIN) ◄───────────── TDM DATA
ESP GPIO16 (DOUT) ────────────► output DAC DATA

mic A ────────► ADC input ──┐
mic B ────────► ADC input ──┼──► one TDM DATA line ──► ESP DIN
Speaker feedback ─► ADC ────┘

Example RX frame:
┌────────────┬────────────┬────────────┬────────────┐
│ slot 0     │ slot 1     │ slot 2     │ slot 3     │
│ mic A      │ reference  │ mic B      │ unused     │
└────────────┴────────────┴────────────┴────────────┘
       │            │            │
       ▼            ▼            ▼
     Mic A         REF         Mic B

Mic A + Mic B: independent channels OR mono average.
REF: separate AEC/AFE reference input, never averaged.
```

```yaml
esp_audio_stack:
  tdm_total_slots: 4
  tdm_mic_slots: [0, 2]
  use_tdm_reference: true
  tdm_ref_slot: 1
  tdm_tx_slot: 0
  mic_mix: average  # Or passthrough for dual-microphone AFE.
```

Use either mono processor configuration in E for an average, or C for two
separate channels. The same capture, conversion and delivery path is reused.
With one TDM microphone, use `tdm_mic_slots: [0]`, `mic_mix: passthrough` and
a mono processor.

An ES7210 can digitize analog microphones and serialize them this way. A
compatible native TDM source can also provide slots without an input codec.
Audio Stack does not turn ordinary I2S microphones into TDM devices.

Slot numbers follow the actual board frame, not the order of samples packed
into DMA memory. If no physical reference is wired, set
`use_tdm_reference: false` and use the software reference. Do not copy a
hardware-reference slot or analog gain from another board's schematic.

### G. analog microphones connected through a hardware codec

```text
ESP GPIO11 (SDA) ◄────────────► codec SDA
ESP GPIO10 (SCL) ─────────────► codec SCL

Analog mic A ─────┐
                  ├──► [Codec ADC] ──► STD I2S or TDM ──► Audio Stack
Analog mic B ─────┘         ▲
                            │
ESP I2C ────────────────────┘ codec setup and analog gain
```

Use the supported codec driver and the digital layout it actually outputs:
one selected mic, two STD slots or TDM slots. Once captured, separate channels
and averaging work the same way as in C, D and E. The codec must really expose
those channels; its name or analog input count alone does not establish a
supported two-channel serial layout. This is an external ADC path, not direct
analog capture through an ESP GPIO.

### H. eS8311 microphone plus DAC feedback is not a dual-mic input

```text
ESP GPIO4 (DIN) ◄───────────── ES8311 I2S RX

ES8311 I2S RX frame
┌──────────────────────┬──────────────────────┐
│ Left: microphone ADC │ Right: DAC reference │
└───────────┬──────────┴───────────┬──────────┘
            │                      │
            │                      │
            └─────► [AEC/AFE] ◄────┘
                        │
                        ▼
                 mono microphone
```

```yaml
esp_audio_stack:
  num_channels: 2
  use_stereo_aec_reference: true
  reference_channel: right
  mic_mix: passthrough
```

Configure `no_dac_ref: false` in both ES8311 codec blocks, as described in
[the stereo-reference section](https://github.com/n-IA-hane/esphome-audio-stack/blob/main/README.md#5-use-the-codec-stereo-channel-as-a-reference).
Do not add `rx_mic_slots` or average these slots: the second signal is playback,
not another microphone. A hardware-reference flag cannot create a physical
feedback connection that the codec or board does not provide.

### I. one shared RX/TX bus or two separate buses

```text
SHARED BUS (I2S port 0):
Mic DATA ──► GPIO4 (DIN)  ──► [RX]
Amp DATA ◄── GPIO7 (DOUT) ◄── [TX]
Both devices: GPIO5 (BCLK), GPIO6 (WS).

SPLIT RX/TX BUSES:
Mic DATA   ──► GPIO4 (DIN) ──► [I2S port 0 RX]
Mic clocks ◄── GPIO5 (BCLK), GPIO6 (WS)
Amp DATA   ◄── GPIO7 (DOUT) ◄── [I2S port 1 TX]
Amp clocks ◄── GPIO10 (BCLK), GPIO11 (WS)
```

For standard I2S, keep the chosen microphone selection and `mic_mix`, then
replace top-level bus pins with:

```yaml
esp_audio_stack:
  rx_bus:
    i2s_num: 0
    i2s_bclk_pin: GPIO5
    i2s_lrclk_pin: GPIO6
    i2s_din_pin: GPIO4
  tx_bus:
    i2s_num: 1
    i2s_bclk_pin: GPIO10
    i2s_lrclk_pin: GPIO11
    i2s_dout_pin: GPIO7
```

Both buses use the stack's configured sample rate. These are separate capture
and playback buses, **not two independent microphone receivers**. One Audio
Stack owns both. Split-bus TDM is not currently supported.

### J. choose the playback reference independently of microphone delivery

```text
Software reference: playback PCM ───────────┬──► I2S TX ──► speaker
                                            └──► reference ──► AEC/AFE

Hardware reference: codec feedback or wired ADC slot ──────► AEC/AFE
```

Without hardware feedback, `aec_reference: ring_buffer` is the default software
route; `previous_frame` is the other supported choice. With physical TDM or
ES8311 stereo feedback, select the matching hardware route instead. Never
average the playback reference into the microphones.

### Supported combinations and present limits

| Physical microphones | Delivery | Processor input |
| --- | --- | --- |
| One selected STD I2S or TDM microphone | Passthrough | Mono AEC, mono AFE or no DSP |
| Two STD L/R microphones sharing DATA | Passthrough | Two independent AFE/BSS inputs |
| Two STD L/R microphones sharing DATA | Average | Mono AEC, mono AFE or no DSP |
| Two selected TDM microphone slots | Passthrough | Two independent AFE/BSS inputs |
| Two selected TDM microphone slots | Average | Mono AEC, mono AFE or no DSP |
| ES8311 mic plus DAC reference | Separate roles | One mic plus reference, never dual-mic BSS |

The current public microphone endpoint delivers **mono**. Keeping two input
channels for AFE does not expose a stereo recording endpoint; optional raw PCM
capture can inspect both input channels separately from that public endpoint.
Selecting two microphones with a mono processor and passthrough uses the first
selected microphone, not an implicit average.

The current implementation supports **one or two selected physical microphones**.
It does not provide three-or-more-microphone AFE, PDM/USB acquisition or two
independent DIN lines. Two DIN receivers would require their own supported
acquisition and sample-alignment design; setting `mic_mix` or calling them TDM
cannot create that support.

For analog gain controls and optional recordings before/after processing, see
[Audio tuning and PCM capture](https://github.com/n-IA-hane/esphome-audio-stack/blob/main/docs/AUDIO_TUNING.md).

</details>

### ESP32-P4 on the new SDK

- Restore camera startup on pre-v3 ESP32-P4 boards with ESPHome 2026.10 and
  IDF 5.5.6. Keep ISP conversion and CSI passthrough consistent, without
  replacing the upstream camera source or adding another media pipeline.
- Fix video-enabled builds affected by the ESPHome `select` namespace.
- Optional framebuffer captures now synchronize DMA-written pixels before
  reading them, avoiding misleading screenshots with stale or missing controls.
- The FULL JPEG check covers real calls, video added and removed during a call,
  concurrent playback, Sendspin and recovery after an interrupted OTA. Its
  10 FPS setting is a limit: measured camera TX was 3.8-5.0 FPS under this
  concurrent workload. H.264 and acoustic echo performance are separate checks.
  See the [P4 hardware notes](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/P4_HARDWARE_AND_MEDIA.md#esphome-202610-physical-check).

### Music Assistant and ESPHome 2026.10

- Use the official ESPHome **2026.10.0** release and its recommended ESP-IDF
  **5.5.6**. Remove the 2026.9 compatibility branches for ring-buffer handles,
  speaker mute/silence and older automation bindings.
- Sendspin now uses ESPHome's native **0.9.3** client, with encrypted connections,
  persistent identity and PIN pairing. Update Music Assistant to **2.10.7 or
  newer**. Complete the speaker's **Setup** in Music Assistant and read the
  temporary **Sendspin Pairing Code** from its HA device. Native **Sendspin
  Confirm Pairing** and **Sendspin Cancel Pairing** controls are available when
  requested by the flow. Normal reboots retain the pairing.
- Keep the native default unpaired-access policy. To allow only paired servers,
  add `sendspin: {unpaired_access: false}` to the firmware configuration.
- Use official mixer, player and resampler ownership, with no local queue,
  cancellation barrier or task-creation shim. Existing input separation and
  maintenance actions remain in their shared packages.
- ESP VoIP Stack uses the released ring-buffer contract and explicitly qualifies
  the POSIX socket wait call, avoiding the new precompiled-header namespace
  collision. Its SIP/RTP lifecycle and negotiation remain the same.

Updating HACS or Music Assistant alone does not update an ESP's firmware.
Follow the [firmware migration and pairing guide](https://github.com/n-IA-hane/esphome-intercom/blob/main/packages/README.md#pair-sendspin-with-music-assistant).

### Further audio and runtime improvements

- FULL AEC/AFE presets now start in **SR HIGH** and offer SR LOW. Separate
  VoIP-only profiles retain FD choices. Existing board-specific microphone and
  echo-reference calibration is preserved.
- Positive **Mic Gain** now applies constant gain, preserving the beginning of
  quiet speech after silence. Review saved positive settings for clipping when
  upgrading. Micro Wake Word remains the official ESPHome provider.
- Options has a centered title and uses swipe navigation without a Back
  button. The shared round-device button cancels an active or pending Assist
  interaction before handling phone-page controls, keeping an established call
  active. The Box V2 hardware RESET button cannot be reassigned in firmware.
- Optional [playback diagnostics](https://github.com/n-IA-hane/esphome-intercom/blob/main/docs/playback_diagnostics.md)
  observes native announcements and hardware completion without automatic
  recovery. It helps capture the state of a stalled pipeline before rebooting.
- Shared committed call state now drives browser phones and PBX projections, including no-answer routing, voicemail handoff and browser application ownership.
- FULL firmware packages use native Runtime Controller observers for Assist, wake word, timers and HA connectivity. Existing user callbacks remain available. VoIP-only profiles keep direct VoIP callbacks and do not acquire a Runtime Controller dependency.
- Coordinated ESP updates retain our Audio Stack, AFE and VoIP fixes while selecting official playback providers. Playback completion follows DMA descriptor identity; reference silence/partial frames and AFE acquisition boundaries keep their ownership. Delayed SIP offers, stereo DC filtering and mixed-codec reception use their corrected contracts.
- Sponsorship notices are now short, optional links instead of large introductory appeals.

Updating the HA integration through HACS does not flash ESP devices. Rebuild the relevant firmware to receive the coordinated ESP changes. Native source startup and Stop retain ESPHome's behavior and are not claimed to be free of main-loop latency.


## Current snapshot limitations

The HA card offers blind transfer, not an attended consultation interface. Parking and directed pickup currently support audio, not active video. ESP REFER has the IPv4/audio limits described above.

This update includes the backend correction for selecting the retained peer when either side initiates a bridged transfer. Independent SIP-peer coverage also checks rejected transfers and legacy peers without REFER. This is not blanket qualification of every physical ESP/PBX combination. A failed transfer must not be treated as successful merely because the command was accepted.

The experimental board profiles and Spotpear long-uptime/announcement investigation retain their stated limits. Wake-word recognition during playback remains a separate investigation. This release does not claim identical behavior across every handset, provider and PBX configuration.

## Install or update 2026.11.0

1. Open **VoIP Stack** in HACS and choose **Redownload**.
2. Select **2026.11.0** and download the release.
3. Restart Home Assistant.
4. Reload the dashboard or reopen Companion.
5. Review the trunk migration and each line's readiness.

**HACS does not update ESP firmware.** Rebuild and upload the appropriate profile to install component-side changes.

The coordinated component releases are published separately:

- [ESP VoIP Stack 2026.11.0](https://github.com/n-IA-hane/esphome-voip-stack/releases/tag/v2026.11.0)
- [ESP Audio Stack 2026.11.0](https://github.com/n-IA-hane/esphome-audio-stack/releases/tag/v2026.11.0)
- [Runtime Controller 2026.11.0](https://github.com/n-IA-hane/esphome-runtime-controller/releases/tag/v2026.11.0)
- [Pinned GMF AI Audio 1.1.1 integration](https://github.com/n-IA-hane/esp-gmf/releases/tag/v1.1.1-afe-dev)

HA backend/card package: the attached `voip_stack.zip` is built from the exact
commit identified by this development tag. The attached `coordinated-sources.json`
records the component revisions. ESP package and component changes are installed
by rebuilding firmware; HACS installs the HA integration separately.

## Help us reproduce a problem

Include the installed version, devices and destinations involved, exact steps, expected and actual behavior, the direction of any audio/video problem, and relevant logs.

For an ESP, include the YAML profile, board and firmware version. Where available, the **VoIP Diagnostics** and **Audio Diagnostics** buttons produce the summaries to attach. Open ESPHome logs first; these are ESPHome diagnostic actions, not separate HA services.

"The door station calls HA; I see video but cannot hear audio from the door station" is already useful information.

"Doesn't work" is a feeling. An understandable one, but difficult to debug.

Do not publish passwords, tokens or SIP captures containing credentials.

[How to report a useful issue](https://github.com/n-IA-hane/esphome-intercom/blob/main/README.md#before-opening-an-issue)

## Thank you

Thanks to everyone suggesting ideas, trying unusual configurations, attaching readable logs and coming back to tell us whether a correction worked.

And go thank **@kfix** too, you ungrateful lot. 🖤

That donation practically funded an entire month of VoIP Stack development. Apparently, "open source" still hasn't convinced the bills to pay themselves.

Seriously, thank you for helping keep this project moving.

Thanks also to **@DunklerPhoenix** for the feature suggestions and real-world feedback that helped improve audio announcements. Let us know how the Halloween pranks go. 🖤

Keep asking for things. The intercom wants its own office now anyway.
