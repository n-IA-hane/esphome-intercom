# Use a VoIP Stack firmware with a classic PBX

A normal SIP desk phone or softphone can call an ESP through a conventional
PBX. The ESP can call back through the same PBX when the dialplan permits it.
Baresip below is a reproducible test client, not a required part of the system.

```text
SIP phone or softphone <--> Asterisk / FreeSWITCH <--> ESP extension 1001
      registered                 routing               fixed IP
```

An ESP running `voip_stack` can be a local SIP phone for Asterisk or another PBX
that supports static SIP peers. The PBX knows the ESP's address from its
configuration; the ESP does not send REGISTER or need a SIP username/password.

You can use this alongside the device's ESPHome and Assist features. The PBX
handles telephone routing; Home Assistant can continue providing the device's
other functions. A VoIP-only firmware can also operate without Home Assistant.

## Example layout

This audio-only example uses:

| Device or destination | Address or number |
| --- | --- |
| Asterisk | `192.168.1.20`, SIP UDP `5060` |
| ESP bedroom phone | `192.168.1.60`, SIP UDP `5060`, RTP base UDP `40000` |
| Bedroom extension on the PBX | `1001` |
| PBX echo-test extension | `700` |

Replace the example addresses with your own and keep the ESP address fixed,
for example through a DHCP reservation. Both devices must be able to reach
the SIP and negotiated RTP addresses. Registration is not required to exchange
INVITE, answer and BYE messages.

## Asterisk: add the ESP as a static extension

Merge these sections into `pjsip.conf`. Reuse an existing UDP transport if one
already listens on the required address/port; do not create a duplicate listener.

```ini
[transport-udp-esp]
type=transport
protocol=udp
bind=0.0.0.0:5060

[esp-bedroom]
type=endpoint
transport=transport-udp-esp
context=from-esp-bedroom
aors=esp-bedroom
disallow=all
allow=slin16:10
direct_media=no
use_ptime=yes

[esp-bedroom]
type=aor
contact=sip:1001@192.168.1.60:5060

[esp-bedroom-identify]
type=identify
endpoint=esp-bedroom
match=192.168.1.60/32
```

The AOR supplies the ESP's static destination. `identify` matches incoming
requests from that ESP to the endpoint and its dialplan context. There is no
`auth`, `outbound_auth` or `registration` section for this peer. A password
challenge would fail because the ESP component does not implement SIP Digest.

`direct_media=no` keeps Asterisk in the audio path, allowing it to bridge or
transcode to other phones without moving the ESP's media with a re-INVITE.
`use_ptime=yes` follows the negotiated packet time. These settings are local to
this endpoint and do not change your other phones.

In `extensions.conf`, add an echo test reachable from the ESP:

```ini
[from-esp-bedroom]
exten => 700,1,Answer()
 same => n,Echo()
 same => n,Hangup()
```

To let an existing PBX phone call the ESP, add this extension to the dialplan
context already used by that phone:

```ini
exten => 1001,1,Dial(PJSIP/esp-bedroom,30)
 same => n,Hangup()
```

Reload the endpoint and dialplan configuration using your normal Asterisk
administration procedure. Transport changes may require an Asterisk restart.
Useful checks in the Asterisk CLI are:

```text
pjsip show endpoint esp-bedroom
pjsip show aor esp-bedroom
pjsip show contacts
dialplan show from-esp-bedroom
```

A static contact does not wait for registration. Add further PBX destinations
by extending the ESP's contact list and the permitted dialplan in
`from-esp-bedroom`. Other PBX phones can keep using their normal registration.

### Add a conventional SIP phone for the first call

An existing registered desk phone can dial `1001` as soon as its context contains
the route above. For a self-contained test, add account `6002` to `pjsip.conf`:

```ini
[6002]
type=endpoint
transport=transport-udp-esp
context=from-phones
aors=6002
auth=6002-auth
disallow=all
allow=ulaw,alaw
direct_media=no

[6002]
type=aor
max_contacts=1
remove_existing=yes

[6002-auth]
type=auth
auth_type=userpass
username=6002
password=YOUR_SIP_PASSWORD
```

Choose your own password and use it on the phone. In `extensions.conf`:

```ini
[from-phones]
exten => 1001,1,Dial(PJSIP/esp-bedroom,30)
 same => n,Hangup()
```

For calls in the reverse direction, add this entry to the existing
`[from-esp-bedroom]` context:

```ini
exten => 6002,1,Dial(PJSIP/6002,30)
 same => n,Hangup()
```

The phone registers as `6002`; the ESP does not register. Asterisk routes
`1001` to the ESP's static AOR and `6002` to the registered phone's contact.

## Configure the ESP

Start with a maintained firmware for your actual board. Keep its microphone,
speaker, audio processing, GPIOs and hardware setup. Merge the following into
its existing `voip_stack:` block rather than adding a second component:

```yaml
voip_stack:
  extension: "1001"
  transport: udp
  sip_port: 5060
  rtp_port: 40000
  use_ha_as_first_contact: false
  audio:
    tx:
      sample_rate: 16000
      pcm_format: s16le
      channels: 1
      frame_ms: 10
    rx:
      sample_rate: 16000
      pcm_format: s16le
      channels: 1
      frame_ms: 10
  static_contacts:
    - name: "700"
      ip: 192.168.1.20
      port: 5060
      transport: udp
```

The contact's name is the destination SIP user in this simple example:
selecting `700` calls `sip:700@192.168.1.20:5060`. It is a PBX extension, not the
ESP's own number. The ESP's `extension: "1001"` identifies the local phone.

The wire format is **L16 PCM, 16 kHz, mono, 10 ms**. ESPHome uses `s16le` for the
local PCM format; VoIP Stack performs the standard L16 wire conversion. Asterisk
calls the corresponding codec `slin16`. This does not change the board's I2S
clock: retain its existing resampler/audio stack configuration. Extra formats
already present in your profile may remain offered; Asterisk below accepts only
`slin16` for this endpoint.

Optional ESPHome buttons for testing:

```yaml
button:
  - platform: template
    name: PBX echo test
    on_press:
      - voip_stack.call:
          target: "700"
  - platform: template
    name: Answer PBX call
    on_press:
      - voip_stack.answer_call:
  - platform: template
    name: End PBX call
    on_press:
      - voip_stack.stop:
```

These are **ESPHome actions**, not HA service YAML. Merge them into an existing
`button:` list. Your board's existing answer/hangup controls can be used instead.

### Choose who supplies the phonebook

`use_ha_as_first_contact: false` only changes the initial contact selection; it
does not disable the HA-managed phonebook.

For an ESP whose telephone contacts are configured entirely in firmware, add
`ha_integration: false` to `voip_stack:`. The ordinary ESPHome API and Assist
can still be used independently. This disables VoIP Stack's automatic HA phone
entities and roster reception, so adapt any board UI that explicitly depends
on those entities before choosing this mode.

If you want VoIP Stack on HA to keep discovering and controlling this phone,
leave its HA integration enabled and put the PBX destinations in HA's managed
phonebook as direct SIP contacts. Use a SIP URI such as
`sip:700@192.168.1.20:5060;transport=udp` and leave **Bridge via HA** disabled for
the direct path. This avoids competing firmware and HA definitions for the
same contact.

For an installation with no HA API client, either omit `api:` or set its
`reboot_timeout: 0s`; otherwise ESPHome's normal API timeout can reboot the
phone. Keep the API and its existing encryption settings when HA uses it.

## First call with ordinary phone settings

On a desk phone, configure SIP server `192.168.1.20`, user `6002` and the
password you chose, then dial `1001`. No custom phone software is required.

For the same test with baresip, put this account in `~/.baresip/accounts`:

```text
<sip:6002@192.168.1.20:5060;transport=udp>;auth_user=6002;auth_pass=YOUR_SIP_PASSWORD
```

Start baresip with its usual audio devices and wait for registration. This
account deliberately does not force an audio codec. With the Asterisk account
above, normal G.711 audio is negotiated on the phone leg.

```text
/dial sip:1001@192.168.1.20
/hangup
```

Answer on the ESP and speak in both directions before hanging up. Add a static
ESP contact named `6002`, with PBX address `192.168.1.20` and SIP port `5060`, to
call the registered phone from the ESP. Asterisk converts between the phone's
G.711 audio and the ESP's L16 PCM; the two legs need not use the same codec.

## Keep microphone and playback rates independent

Current development firmware also supports standard SDP with 16 kHz PCM sent
by the ESP and 48 kHz PCM received by it. Keep the maintained full profile's
existing audio capabilities; its AFE microphone remains at 16 kHz. For this
pre-release, select the component's development branch in the maintained YAML's
existing substitutions:

```yaml
substitutions:
  voip_stack_components_source: github://n-IA-hane/esphome-voip-stack@dev
```

Rebuild and upload the ESP. On the Asterisk endpoint, replace the single-codec
`allow` line with:

```ini
allow=slin48:10,slin16:10
asymmetric_rtp_codec=yes
```

Keep `direct_media=no` and `use_ptime=yes`. Asterisk can then send L16/48000 to
the ESP while receiving L16/16000. This requires the firmware's RX capabilities
to include both rates at 10 ms; an explicitly restricted 16 kHz profile stays
at 16 kHz. No private SDP attribute or special Asterisk patch is needed.

The registered phone has its own codec negotiation with Asterisk. For example,
a baresip phone can use G.711 at 8 kHz while the ESP uses PCM. Asterisk performs
the conversion between those legs. Converting an 8 kHz source to 48 kHz does
not restore frequencies absent from the original audio.

The ESP accepts only the receive payload mappings agreed for that call. If the
PBX sends another accepted PCM rate, conversion happens after packet reordering
in the existing receive task. The speaker keeps a stable sample rate. A format
already matching the speaker passes through without this conversion.

For a reproducible 48 kHz phone-side test, enable `module l16.so` in baresip's
`config`, and use this account:

```text
<sip:6002@192.168.1.20:5060;transport=udp>;auth_user=6002;auth_pass=YOUR_SIP_PASSWORD;audio_codecs=L16/48000/1,L16/16000/1
```

Also change the registered `6002` endpoint's Asterisk codec list to:

```ini
allow=slin48:10,slin16:10,ulaw,alaw
```

Call `1001` again. The intended ESP leg is **ESP microphone -> PBX: 16 kHz** and
**PBX -> ESP speaker: 48 kHz**, both at 10 ms. The phone leg can use L16/48000.
Check the SDP and `core show channel <channel-name>` instead of inferring the
wire rate from the I2S hardware rate. Ordinary desk phones can use their own
supported codecs; they do not need L16/48000 for basic calling.

## FreeSWITCH: add the ESP as a static extension

This example uses the vanilla XML directory and the `internal` Sofia profile.
The configuration was checked against FreeSWITCH sources and documentation;
the physical call matrix below was run with Asterisk, not FreeSWITCH.

Reserve extension `1001` for the ESP. In `directory/default/1001.xml`, use the
following definition instead of the unused vanilla sample user. If `1001` is
already a real phone, choose a free extension consistently throughout the
example.

```xml
<include>
  <user id="1001" cidr="192.168.1.60/32">
    <params>
      <param name="dial-string" value="{absolute_codec_string=L16@16000h@10i}sofia/internal/1001@192.168.1.60:5060"/>
    </params>
    <variables>
      <variable name="user_context" value="from-esp-bedroom"/>
      <variable name="effective_caller_id_number" value="1001"/>
      <variable name="effective_caller_id_name" value="ESP bedroom"/>
    </variables>
  </user>
</include>
```

`dial-string` is the static destination: a lookup of `user/1001@<domain>` dials
the ESP directly rather than looking for a REGISTER contact. The vanilla
`Local_Extension` dialplan already routes `1000` through `1019` this way.
For another number, add an explicit dialplan route in the callers' context:

```xml
<extension name="esp-bedroom">
  <condition field="destination_number" expression="^1001$">
    <action application="bridge" data="user/1001@$${domain}"/>
  </condition>
</extension>
```

Use either the existing vanilla route or the explicit route, not duplicates.
Place explicit routes before any catch-all rule that would consume the number.

The `cidr` identifies calls coming from the ESP. Retain the vanilla `domains`
ACL in `autoload_configs/acl.conf.xml`:

```xml
<list name="domains" default="deny">
  <node type="allow" domain="$${domain}"/>
</list>
```

The internal Sofia profile must apply it:

```xml
<param name="apply-inbound-acl" value="domains"/>
<param name="inbound-codec-prefs" value="$${global_codec_prefs},L16@16000h@10i"/>
```

The codec list retains the normal phone codecs and also accepts the ESP's
L16/16000 offer on incoming calls. The static dial string selects L16 for
outgoing calls to the ESP.

The ACL associates the ESP's source IP with directory user `1001`, without making
the ESP perform SIP authentication. Keep normal authentication for the other
phones. The user directory, static dial string and IP match have separate roles;
setting `register=false` on a gateway is not required for this direct endpoint.

For a test phone, create `directory/default/6002.xml`:

```xml
<include>
  <user id="6002">
    <params>
      <param name="password" value="YOUR_SIP_PASSWORD"/>
    </params>
    <variables>
      <variable name="user_context" value="default"/>
      <variable name="effective_caller_id_number" value="6002"/>
    </variables>
  </user>
</include>
```

Register a normal SIP phone or the ordinary baresip account shown above against
the internal profile, using its actual directory domain. In a vanilla LAN setup
this is usually the PBX address; replace it if your installation uses another
domain. Dial `1001` to reach the ESP through FreeSWITCH.

For calls from the ESP, create `dialplan/from-esp-bedroom.xml`:

```xml
<include>
  <context name="from-esp-bedroom">
    <extension name="registered-phone">
      <condition field="destination_number" expression="^6002$">
        <action application="bridge" data="user/6002@$${domain}"/>
      </condition>
    </extension>
    <extension name="echo-test">
      <condition field="destination_number" expression="^700$">
        <action application="answer"/>
        <action application="echo"/>
      </condition>
    </extension>
  </context>
</include>
```

Reload XML and the directory-derived ACL with `reloadxml` and `reloadacl` in
`fs_cli`. If you changed a Sofia profile, apply that change using your normal
profile restart procedure. Check `sofia status profile internal reg` for the
registered phone; the static ESP should not appear as a registered client.
For the example domain, `user_data 1001@192.168.1.20 param dial-string` in
`fs_cli` should return the static ESP destination.

FreeSWITCH keeps the media bridge in its normal path and can transcode the
registered phone's codec to L16 on the ESP leg. Do not enable media bypass for
this example. The 16/48 kHz measurement in this guide applies to Asterisk;
FreeSWITCH codec-selection policies should be verified separately.

References: [directory-backed ACLs](https://developer.signalwire.com/freeswitch/integration/acls/),
[dial strings](https://developer.signalwire.com/freeswitch/dialplan/dptools/),
[codec negotiation](https://developer.signalwire.com/freeswitch/media-and-codecs/codecs/).

## Check both call directions

1. From the ESP, call `700`. Verify that your voice returns through the echo
   test, then hang up from the ESP.
2. From an existing PBX phone, call `1001`. Answer on the ESP and check speech
   in both directions.
3. Hang up from the PBX phone, then repeat the call and hang up from the ESP.
4. Confirm both phones return to idle and Asterisk releases its call channels.

If signaling works but audio does not, inspect the negotiated codec, packet
time and RTP destination first. Use the ESP's `voip_stack.dump_diagnostics`
action while the call is active. A PBX endpoint restricted to G.711 will not
match this PCM example. Supported VoIP-only S3 profiles can alternatively use
Opus when it is compiled into the firmware and supported by the PBX.

On 9 October 2026, an Arch Linux Asterisk 23.5.0 lab exercised Spotpear Ball V2
with a registered baresip client, standard G.711 settings and explicit L16 PCM.
Tests covered calls in both directions, hangup from either endpoint, repeated
calls, 16 kHz microphone / 48 kHz receive negotiation and reception of an
alternate negotiated 16 kHz payload. RTP captures and microphone recordings
were checked, including known tones physically played by the speaker with AEC
temporarily disabled, then restored. These results qualify that setup, not all
PBX versions, phone models or boards.

[Asterisk PJSIP configuration reference](https://docs.asterisk.org/Configuration/Channel-Drivers/SIP/Configuring-res_pjsip/PJSIP-Configuration-Sections-and-Relationships/)
