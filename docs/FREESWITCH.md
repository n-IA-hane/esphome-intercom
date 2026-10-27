# FreeSWITCH and FusionPBX audio/video interoperability

VoIP Stack can call audio phones and video door stations through a
FreeSWITCH-based PBX. The PBX negotiates separately with the calling phone
(the A-leg) and the destination (the B-leg). Its media policy determines how
the result on one leg reaches the other.

This guide records a tested internal-profile configuration using native
FreeSWITCH settings. It does not require a list of video-capable contacts or
a VoIP Stack-specific protocol. Apply it to the relevant call route and verify
it against the services that your PBX already provides.

## The four settings and their roles

| Setting | Location in the tested configuration | Purpose |
| --- | --- | --- |
| `media_mix_inbound_outbound_codecs=false` | Persistent global variable | Avoid mixing additional outbound codec preferences into the caller's offer. |
| `inbound-late-negotiation=true` | SIP profile receiving the initial call | Defer media selection until the dialplan can apply the bridge policy. |
| `inherit_codec=true` | Calling leg, before `bridge` | Let the called leg's negotiated result inform the media selected for the caller. |
| `rtp_pass_codecs_on_stream_change=true` | Dialplan, exported to both legs before `bridge` | Propagate mid-call media changes, such as adding video with a re-INVITE. |

The tested PBX already had late negotiation enabled on its `internal` profile.
Its provider-facing `external` profile was not qualified with these changes.
The names describe SIP profiles, not necessarily whether a client is physically
inside or outside the LAN: a remotely registered HA trunk may use `internal`.

The persistent global setting is represented as:

```xml
<X-PRE-PROCESS cmd="set" data="media_mix_inbound_outbound_codecs=false" />
```

The receiving SIP profile contains:

```xml
<param name="inbound-late-negotiation" value="true" />
```

The relevant dialplan applies these actions before its existing bridge:

```xml
<action application="set" data="inherit_codec=true" />
<action application="export" data="rtp_pass_codecs_on_stream_change=true" />
```

These are fragments for the existing configuration, not a replacement dialplan.
Keep permitted audio/video codecs appropriate to your endpoints. Do not disable
transcoding or force one global codec list just to make one test succeed.
Early media can establish the calling leg before bridging, so an IVR or a
`183` response needs its own test rather than assuming the same outcome.

## Save the configuration that FusionPBX actually executes

Use FusionPBX's persistent settings and Dialplan Manager. In the tested setup,
the two dialplan actions belong to the domain variables before the destination
bridges. Saving database details alone did not update the compiled dialplan XML.
Verify the generated XML and the corresponding channel variables on a new call.

When maintaining a deployment programmatically, use its native dialplan
compilation and cache mechanisms. `reloadxml` alone does not prove that a stale
FusionPBX-generated dialplan or directory cache has been regenerated. Profile
parameter changes also require the appropriate profile reload procedure;
reloading XML is not equivalent to restarting a SIP profile.

See the [FusionPBX Dialplan Manager documentation](https://docs.fusionpbx.com/en/latest/dialplan/dialplan_manager.html).

## What was verified

The tested configuration produced these results:

- A video offer to an audio phone resulted in an audio-only call.
- A video destination retained H.264.
- Zoiper added video to an established audio call; FreeSWITCH propagated the
  re-INVITE to Home Assistant and negotiated H.264 on both legs.
- The persistent domain policy replaced the temporary per-extension setting.

The HA-side test also received and displayed actual Mobotix video through that
PBX with the browser webcam off. SDP acceptance alone is not proof that images
are arriving: inspect RTP and the displayed result as well.

![Actual Mobotix video received by the HA phone](images/pbx-mobotix-video-green.png)

For another deployment, separately test upgrades initiated from either side,
video removal, hold/resume, early media, transfers, queues, IVRs, conferences
and recording. The report does not certify every combination or provider.

## Two fixes in Home Assistant

The 2026.11.0 release also corrects H.264 delivery to the browser. Standalone
SPS/PPS parameter blocks are cached for the next picture, rather than submitted
to WebCodecs as empty pictures. This prevents a decoder failure when the PBX
switches from an initial frame to the camera stream.

NAT reachability is separate from codec negotiation. A remote PBX must have a
reachable media address and RTP/RTCP ports from the beginning of the stream.
Losing the initial SPS/PPS can leave video undecodable even while audio works.
See [outgoing media through NAT](SIP_TRUNK.md#outgoing-media-through-nat).
The current address override is a literal IPv4 address; automatic DDNS handling
is not implemented by that option.

## Sources

- [FreeSWITCH media handling and late negotiation](https://developer.signalwire.com/freeswitch/media-and-codecs/handling/).
- [FreeSWITCH channel variables](https://developer.signalwire.com/freeswitch/reference/channel-variables/).
- [Media-change handling in the tested FreeSWITCH source revision](https://github.com/signalwire/freeswitch/blob/3f13ad1b1d323e435757fc43a2bb829d4ea5b9a8/src/switch_core_media.c).
- [Codec inheritance in the same revision](https://github.com/signalwire/freeswitch/blob/3f13ad1b1d323e435757fc43a2bb829d4ea5b9a8/src/switch_ivr_originate.c).
