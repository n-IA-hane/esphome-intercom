# Standard services for ESP phones

The 2026.11.1 development preview brings automatic SIP accounts, an LDAP
phonebook, SIP BLF and MWI, and IMAP voicemail to the coordinated ESP firmware.
Use matching `dev` components and the current device YAML when testing this
preview. The Home Assistant integration also serves ordinary SIP phones and
standard LDAP/IMAP clients.

## One phone, one identity

Add the ESP through the usual ESPHome integration. The maintained telephone
packages enable `voip_stack.ha_integration.auto_register`. Home Assistant
creates one account and password, saves them, then configures the ESP through
its existing native ESPHome API connection. Encryption follows that connection's
configuration. No additional pairing protocol or provisioning server is needed.

```text
ESPHome device online
         │
         ▼
HA creates the phone account once
         │ existing ESPHome API actions
         ▼
ESP receives SIP, LDAP and IMAP settings
         │
         ├──► SIP REGISTER, calls, BLF and MWI
         ├──► LDAP contact searches
         └──► IMAP mailbox and audio retrieval
```

The technical device name is the SIP login. The friendly device name shown in
Home Assistant is the caller display name and directory label. Renaming that
label preserves the password and mailbox. A reconnect reuses the same account.
The ESP remains one endpoint and one dashboard tile.

Removing the originating ESPHome integration or its device permanently revokes
the managed account, ends its calls/subscriptions and removes its directory
record, mailbox, recordings and IMAP identifiers. Going offline, restarting
or reloading an integration preserves the account and messages.

## Directory and contact states

Home Assistant assembles its routing phonebook directly into the shared
`PhoneDirectory`. LDAP reads those records; it has no separate contact database.
Offline phones stay in the directory until their owner is removed.

Open **VoIP Stack > Reconfigure > Phone directory and mailboxes** to set:

| Setting | Default |
|---|---|
| LDAP listener | TCP 1389 |
| Directory base DN | `ou=phonebook,dc=voip` |
| Anonymous searches | Disabled |
| IMAP listener | TCP 1143 |
| TLS | Disabled, optional certificate/key configuration |

Use the phone's SIP username/password for authenticated LDAP searches. The bind
DN is `uid=USERNAME,ou=accounts,ou=phonebook,dc=voip` with the default base DN.
A convenient phone filter is `(|(telephoneNumber=*)(labeledURI=*))`; display `cn`
and dial `telephoneNumber` or `labeledURI` according to the phone's capabilities.
`uid` is stable, `cn` is the visible name, and `ou` distinguishes ESP phones,
registered accounts, browser phones, contacts, automations and groups. Adjust
the configurable search filter on a traditional desk phone to these attributes.
The service supports LDAPv3 searches, filters and paged results. It is read-only;
edit contacts through the existing VoIP Stack configuration.

The ESP's vertical contact list uses one small pool of reusable rows. Each row
shows a type icon, name and BLF call state. Tap a normal row to call. Tap a
ringing row to perform directed pickup through the selected SIP account.
A ringing indicator animates while that list is visible.

For a custom display, `phone_list_view.visible_rows` reserves the widget pool
at boot. Cover the viewport with its configured row height and two scroll
buffer rows. The portrait P4 package reserves 9 contact rows and 11 voicemail
rows for its larger viewport. This pool is separate from the BLF subscription
limit and the IMAP message-page size.

BLF uses standard SIP `SUBSCRIBE`/`NOTIFY` with the `dialog` event package.
"Idle" means the watched resource has no active dialog, rather than proof of
network reachability. Unavailable or unsynchronized BLF is shown as unknown.
Display packages enable BLF with a pool of 16 contacts. Headless packages leave
that optional watch pool out; custom YAML can increase
`voip_stack.blf.max_subscriptions` to 64 and place its buffers in PSRAM.
PBX pickup prefixes are configurable (`pickup.` on VoIP Stack, commonly `**`
on other systems). Access remains subject to the PBX's pickup policy.

## Voicemail on the display

Enable the phone's mailbox through the existing VoIP Stack voicemail settings.
Standard SIP MWI controls the envelope above the status indicators. It appears
for new messages on the idle Assist/telephone views and stays hidden during
calls, ringing, settings and video. Tap the envelope to open the dedicated
mailbox page. The **Voicemail** entry in Contacts also opens read messages.

The page lists the caller, date, duration when supplied, and read state. Tap a
message to play it through the existing native announcement player. Stop flushes
that pipeline; Delete removes the selected message through IMAP. The arrow
controls move between bounded pages of messages. Swipe right to return.
A telephone/video call takes priority over voicemail playback and presentation.
The page works in both FULL and standalone telephone profiles; standalone
profiles retain their direct VoIP callbacks without Runtime Controller.

IMAP exposes `INBOX`, persistent UID/UIDVALIDITY, flags, IDLE, partial audio
fetches and UIDPLUS. `Seen` and deletion update the original media archive.
WAV content is generated from the existing message, cached within a fixed
budget and streamed in pieces. Listing metadata does not read the recording.
The server provides the standard `Content-Duration` field when known.

The SIP/DTMF voicemail menu remains available to other phones and PBXs. The
visual mailbox uses standard IMAP and SIP MWI, rather than HA-specific display
commands. An external PBX must supply its own compatible LDAP, BLF, MWI and
IMAP services; those capabilities vary by PBX and configuration.

## Custom firmware and other PBX systems

The standalone ESP component keeps direct SIP as its generic default. Automatic
HA provisioning is explicit:

```yaml
api:
  custom_services: true

voip_stack:
  id: phone
  # Keep the existing microphone, speaker and SIP configuration here.
  ha_integration:
    auto_register: true
```

The maintained device packages already include the complete service binding.
For a custom phone, configure `ldap_directory` and `imap_voicemail` only when
needed, and attach the latter to a native `speaker_source` pipeline. The
[ESP VoIP Stack documentation](https://github.com/n-IA-hane/esphome-voip-stack/tree/dev)
contains complete direct, registered and mixed-account examples, independent
LDAP/IMAP settings and memory/task controls.

Optional TLS requires `tls_support: true` in each ESP client at compile time.
Enable that firmware support before switching the HA listeners to TLS.
TLS uses the normal certificate trust configuration. A private CA can
be declared through the ESP client's `ca_certificate`; certificate validation
still applies. The Home Assistant services use manifest Python dependencies
and supported integration boundaries, including on Home Assistant OS.

## Updating an existing installation

1. Update the integration and coordinated ESP components to this preview.
2. Start from the current maintained YAML for your board, preserving identity,
   wiring and calibration. Rebuild and upload to install native phone services.
3. Let the existing ESPHome API configure the phone automatically. Its friendly
   name remains the caller identity.
4. Enable voicemail for the endpoint when wanted, then test its contact list,
   pickup and mailbox controls.

New managed firmware gets its phonebook through LDAP and discovers media
capabilities through SIP OPTIONS/SDP. The long **VoIP Endpoint** text sensor and
its serialization budget are legacy opt-ins for older integrations. Older ESP
firmware retains its existing JSON phonebook delivery while it is migrated.
