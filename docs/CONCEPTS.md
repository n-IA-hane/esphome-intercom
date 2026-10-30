# Phones, contacts and calls

You can make your first call without reading a SIP manual. These are the names
you will see in the editor, and when they matter.

## The everyday terms

| Term | What it means | Example |
| --- | --- | --- |
| Phone | Something that places or receives calls and has its own state. | The HA browser phone named Reception, a SIP handset or an ESP. |
| Card | The dashboard controls for a phone. It is not another phone by itself. | Two cards can show Reception; the browser that answers owns its audio. |
| Calling phone | The local phone that will place a call. Select it by name. | Reception. |
| Destination | Who you want that phone to call. | Kitchen, extension 101, a group or an external number. |
| Contact | A phonebook entry telling the PBX how to reach a destination. | A named door station or delivery driver's number. |
| Extension | An optional local dial-plan number. Each destination needs a distinct one. | 101 for Kitchen. |
| SIP account | Credentials a normal SIP phone uses to register with HA. | An account for a desk phone or Zoiper. |
| Trunk | A connection from HA to a provider or another PBX. | A work telephone line. It is not a room phone. |
| Automation contact | A callable destination handled by HA automations; also a backend announcement caller. | Welcome or Wakeup-Caller. |
| Mailbox | The messages stored for one phone by HA's voicemail feature. | Kitchen's unanswered-call messages. |
| Ring group | Several phones ring; the first answer wins. | Ring Kitchen and Office together. |
| Conference | Several callers share an audio conversation. | Everyone joins the same named room. |

![Controls choose a local phone and a destination through HA](images/phone-control-path.svg)

## What do I actually select?

For **Reception calls Kitchen**, select **Reception** in **Calling phone** and
enter **Kitchen** in **Destination**. Select names in HA's visual pickers; you do
not need to find or type device IDs. Home Assistant stores those references.

An Automation contact is different: choose **Calling Automation contact** when
HA should originate and play TTS or a recording by itself, without browser
microphone access. Use either a calling phone or an Automation source for that
call. See the [illustrated action editor](USER_GUIDE.md#choose-the-calling-phone-in-the-editor)
and [outgoing announcement recipe](AUTOMATION_DIALPLAN.md#place-a-scheduled-announcement-call).

The HA phone created during setup takes its initial name from your HA instance.
That name is an ordinary phone name, not a separate universal `HA` destination.
Choose the displayed phone in the picker.

## Where the work happens

Home Assistant owns its phonebook, routing defaults, voicemail and call
applications. Cards show those phones and send controls to the backend. Each
phone still has its own card; you do not need a single central popup.

An ESP is optional. With VoIP firmware it is a physical SIP phone. A Full ESP
profile can also run its own wake-word Voice Assistant and media player. That
local assistant is separate from calling an Assist extension through the PBX.

The ESP mirror card controls the ESP's microphone and speaker. An HA softphone
card uses the browser or Companion WebView's media devices. Selecting an ESP
mirror does not turn your phone's microphone into that ESP's microphone.

If an ESP calls another IP directly, HA's routing rules do not control that
call. Use an HA-managed route when voicemail, forwarding or another PBX feature
must apply. ESP firmware can also work as a direct or registered endpoint of a
[classic PBX](CLASSIC_PBX.md).

## When you need the protocol terms

| Term | Meaning in this project |
| --- | --- |
| SIP | Call signaling: invite, ring, answer, change or end the call. |
| SDP | The negotiated media description, including codecs, directions and addresses. |
| RTP | The packets carrying the actual audio or video. |
| REGISTER | A SIP phone or registered trunk reports its reachable contact to its server. A static IP endpoint need not use it. |
| DTMF | The digits you send during a call, for a menu or a compatible door-release function. |
| MWI | The standard message-waiting indication. It reports counts, not message audio or a full message list. |
| BLF | A phone's busy-lamp display, based on SIP dialog-state subscriptions. |
| REFER | A SIP transfer request. It does not itself prove the other telephone can complete the transfer. |

The existing ESPHome API connection carries device controls and managed account
settings, using its configured encryption policy. Current managed firmware
reads the shared phonebook through LDAP; older firmware receives JSON updates.
SIP/RTP is a separate call path: a working API connection alone does
not prove that call signaling and media can reach the peer across networks.
See [network setup](DEPLOYMENT_GUIDE.md#network) when a phone rings but its audio
cannot travel in one or both directions.

## Advanced identifiers

The guides use visible names first. For integrations and complex automations,
`device_id` identifies the local HA device, `endpoint_id` identifies a logical
phone internally, and `call_id` identifies one call. They are not interchangeable
with a phone's extension or the remote destination.

Native VoIP automation triggers supply their own call context. A normal
single-call Automation sequence also keeps its own call reference. Explicit
IDs are useful for advanced concurrent workflows, not a prerequisite for an
ordinary Call, Speak and Hang up sequence. See the [action reference](SERVICES.md).
