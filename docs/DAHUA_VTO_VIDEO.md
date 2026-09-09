# Optional Dahua VTO video compatibility

Existing Dahua support remains enabled as before. Do not change a working configuration merely because it uses a Dahua door station.

Some VTO firmware answers an audio-only call with an additional video section, or answers a receive-only video offer with `sendrecv`. Those answers do not follow the SDP offer/answer rules. A missing H.264 `fmtp` is different: the standard defines defaults, including packetization mode 0.

For the outgoing browser-to-VTO case reported in issue #115, the optional `dahua_vto` contact profile offers video from the beginning, using H.264 `42000a`, packetization mode 0 and `sendrecv` signaling. The browser is separately denied camera transmission. This is a receive-video compatibility profile, not a way to enable two-way browser camera transmission.

Enable SIP video in the integration, then use the **Optional SIP video compatibility** field of the `voip_stack.add_contact` action. Select **Dahua VTO (receive video only)** for the affected contact. The action field is `sip_video_profile: dahua_vto`. Leave **Default** for other devices, including ESP phones. The profile does not apply to trunk calls or local HA/ESP endpoints.

If creating a separate video contact, give it a distinct name and the door station's actual SIP URI. The action also supports updating a manual contact by its existing ID; retain its existing address and other settings when doing so. Replacing the whole phonebook is unnecessary.

This does not loosen SDP validation globally, change incoming Dahua calls, or alter the default H.264/JPEG/VP8 formats. The previously supported Dahua audio behavior remains separate.

The new profile is software-tested against the reported SDP behavior. Confirmation on the actual DHI-VTO2211G-WP and its firmware is still required. Please test received video, audio in both directions, hangup from both sides and a second call. Report any failure in [issue #115](https://github.com/n-IA-hane/esphome-intercom/issues/115).
