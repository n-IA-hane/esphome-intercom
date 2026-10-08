# Dahua SIP door stations

Use the Dahua compatibility profile when a door station expects an H.264 media
section even when the Home Assistant browser is only receiving video. The
profile also selects standard SIP audio formats at 20 ms instead of treating
an unregistered door station as a generic directional PCM endpoint.

This configuration is available in the development preview after 2026.10.1.
It requires no Python patch, fabricated User-Agent, or `registered: true` flag.

## Dahua registered with Home Assistant

For a door station configured to register with VoIP Stack, retain its existing
SIP account. The registrar identifies a `Dahua UAC/...` User-Agent automatically.
Calling that registered contact keeps its current audio offer, including
Dahua PCM/16000 where detected. Enable SIP video and leave **Send camera** off
when the browser should only receive the door station's image.

The DHI-VTO2211G-WP reporter confirmed received H.264 and bidirectional audio
for incoming calls. For outgoing calls to the automatically registered contact,
received audio is fixed but speech at the VTO remains under investigation.
The available SIP/media logs do not prove what its decoder does with the
outgoing PCMU. The comparison procedure below isolates the audio offer.

## Static door station with its own SIP server

Keep the VTO's built-in SIP server configuration when it is needed for VTH or
DMSS. A contact profile does not change those device settings.

1. In **Settings > Devices & services > VoIP Stack**, choose **Add contact**,
   then an ordinary **Contact**, not an Automation contact.
2. Enter a unique name and the device's complete **SIP URI**. Include its real
   extension, host, port and transport.
3. Select **SIP compatibility profile: Dahua**.
4. Start with **Dahua audio compatibility: Standard SIP codecs**.
5. Enable SIP video in VoIP Stack and video capability for the calling browser
   phone. Leave **Send camera** off for receive-only video.

The same fields are available when reconfiguring the contact. Equivalent
Developer tools action (replace the example URI with your device's address):

```yaml
action: voip_stack.add_contact
data:
  name: Front door
  sip_uri: "sip:8001@192.0.2.20:5060;transport=udp"
  sip_profile: dahua
  dahua_audio: standard
```

This is a direct call to the specified URI. Registering an optional trunk does
not by itself move this contact's calls onto that trunk.

The profile supplies common SIP audio at 20 ms. With video enabled, it offers
receive-only H.264 packetization mode 0 using the already validated handling of
Dahua answers without `fmtp`. Global video settings and the source phone's
video capability still apply. No camera capture is started for receive-only
calls. Physical confirmation of this configurable static-contact path on the
VTO3211D-P-S2 remains necessary.

## A Dahua destination reached through a trunk

Configure HA's trunk registration/authentication against the VTO's SIP server
as required by that device. Then create a contact with its **Number**, leaving
SIP URI and Address empty, so the existing dialplan selects the trunk:

```yaml
action: voip_stack.add_contact
data:
  name: Front door via PBX
  number: "8001"
  sip_profile: dahua
  dahua_audio: standard
```

Call the contact by name. Its explicit Dahua profile supplies the audio/video
capabilities while the trunk retains its normal routing and authentication.
The profile belongs to this destination, not the trunk: ordinary numbers and
other contacts using that same trunk retain their existing behavior. A trunk
must be enabled and ready. The automatic-video exception on this route requires
an explicitly Dahua contact; a bare dialed number has no such profile.

## Audio choices and a controlled comparison

| GUI choice | Action value | What is offered |
| --- | --- | --- |
| Auto by User-Agent | `auto` | Keep existing detection. A real Dahua User-Agent enables PCM; without one, use standard SIP codecs. |
| Standard SIP codecs | `standard` | Offer standard codecs, excluding proprietary Dahua PCM. |
| Standard + Dahua PCM | `pcm` | Add PCM/16000 to the standard offer without requiring a User-Agent. |

Explicit `standard` or `pcm` requires `sip_profile: dahua`. These choices do
not force PCMU or PCM as the transmit codec. The remote SDP answer still drives
transmit selection, and HA accepts the negotiated receive formats independently.
Returning both settings to Auto removes the overrides.

To compare the automatic-contact problem without modifying that registration:

1. Copy its active SIP URI exactly, including port and transport.
2. Create two ordinary contacts with different names and that same URI.
3. Select Dahua on both. Choose `standard` for one and `pcm` for the other.
4. Use the same browser phone, microphone, volume and **Send camera: off**.
5. Call each contact, answer and speak the same short non-private phrase in
   both directions. Note which side can hear it. Hang up, then call again.

This changes the presence of proprietary PCM in the audio offer while keeping
the Dahua video profile and destination the same. The manual contact that was
already working can remain as a separate baseline. A difference in the result
would implicate the device's handling of that offer, but would not by itself
prove that it requires symmetric codecs.

Never reinterpret payload 97 solely by its number. It can mean PCM/16000 or
L16 at a different rate, depending on the SDP. An unnegotiated mapping remains
rejected. Incoming VTO calls keep their existing negotiation; these contact
settings select offers when HA originates a call to the contact.

## Verification and remaining hardware checks

Software tests compare the reported automatic and manual audio paths over UDP:
nonzero browser PCM is encoded to PCMU, independently decoded at the peer, and
checked for sample content, payload size, sequence, timestamps and destination.
Both paths produce identical transmit samples while receiving PCM or PCMU.
This verifies HA's test-path output; it does not prove that a physical VTO
renders those packets or that the reporter's browser supplies the same samples.

On the physical devices, please check:

- Both directions of speech for the standard/PCM offer comparison.
- Receive-only video on the static direct and actual trunk routes.
- Hangup initiated at the VTO, which the latest captures did not verify.
- An immediate second call after each direction of hangup.

Keep the SIP capture and media log for each named scenario. The integration's
**Capture SIP signaling** action excludes RTP. If transmission is still silent,
a short additional RTP capture of a non-private test phrase is needed to verify
the wire audio, destination and timing in the affected installation. Review
captures before sharing them; SIP authentication and real conversations must
not be published.

## Protocol basis and interoperability evidence

[RFC 3264 sections 6.1 and 7](https://www.rfc-editor.org/rfc/rfc3264.html)
allow different negotiated codecs in the two directions. The answer's first
codec guides HA's transmission; it does not restrict the peer to that same
receive codec. Payload mappings remain directional, including during updates.

[Asterisk's asymmetric_rtp_codec policy](https://docs.asterisk.org/Latest_API/API_Documentation/Module_Configuration/res_pjsip/#asymmetric_rtp_codec)
can follow a compatible received codec for transmission. That is a deliberate
interoperability policy, not proof that every Dahua requires symmetry.
[FreeSWITCH codec negotiation](https://developer.signalwire.com/freeswitch/media-and-codecs/codecs/)
also provides per-profile and per-call offer controls. Our explicit contact
choices follow that separation: choose advertised capabilities per destination
and continue validating the actual offer/answer exchange.

[Dahua lists G.711 and PCM for the VTO2211G-WP](https://www.dahuasecurity.com/products/discontinued-products/video-intercoms/vto2211g-wp).
An [Asterisk community investigation](https://community.asterisk.org/t/dahua-vto4202f-p-s2-asterisk-dahua-vth5422h-no-video/94956)
also reports working G.711 without PCM on a different Dahua setup. These are
reasons to provide a standard-codec comparison, not evidence that the remaining
one-way audio on the reported firmware is fixed. Older forum reports involving
reordered SDP media sections or RTP/RTCP port collisions must be checked against
the actual capture rather than applied as generic Dahua workarounds.
