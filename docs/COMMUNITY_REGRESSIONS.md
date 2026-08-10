# Community regression index

This index ties reported failures to repeatable regression evidence. A unit or
replay test proves only the named software contract. Hardware interoperability
is listed separately and is never inferred from Home Assistant returning to
`idle`.

| Issue | Protected behavior | Automated evidence | External evidence | Remaining boundary |
| --- | --- | --- | --- | --- |
| [#68](https://github.com/n-IA-hane/esphome-intercom/issues/68) | An unchanged roster is not pushed repeatedly, while a reconnected device can be rehydrated | `test_phonebook_pushes_only_changed_content_and_rehydrates_one_device` | None current | Keep open until reboot and reinstall are repeated on physical ESPHome phones. The phonebook contract is unchanged in this release. |
| [#85](https://github.com/n-IA-hane/esphome-intercom/issues/85) | Dahua digest registration, TCP flow reuse, PCM/16000 negotiation and call teardown | `test_sip_registrar.py`, `test_sip_tcp_profile.py`, `test_sdp_pcm_profile.py`, `test_sip_video_peer.py` | VTO2111D reporter confirmed stable registration, bidirectional calls and H.264 | Other Dahua firmware and iOS browser media still need device-specific qualification. |
| [#88](https://github.com/n-IA-hane/esphome-intercom/issues/88) | ESP to ESP RTP survives beyond the media watchdog in both directions | `test_live_voip_qualification_contract.py`, scenario `esp_to_esp_bidirectional` | P4 and Waveshare S3 passed both directions, both hangup owners and post-call quiescence | Repeat on each newly supported hardware audio path. |
| [#89](https://github.com/n-IA-hane/esphome-intercom/issues/89) | Browser to browser calls use the local bridge and share the normal lifecycle | `test_softphone_originate.py`, `test_voip_backend_route_contract.py`, frontend runtime suites | Isolated Home Assistant lab passed browser media handoff and dashboard reload | Browser permission policies remain browser and origin dependent. |
| [#93](https://github.com/n-IA-hane/esphome-intercom/issues/93) | `device_id` selects either an ESPHome or HA phone without leaking the internal adapter | service endpoint and HA runtime suites | ESP and HA action paths passed with service responses | Firmware must expose the complete ESPHome call-control action set. |
| [#94](https://github.com/n-IA-hane/esphome-intercom/issues/94) | Arbitrary PCMA input chunks are reframed into complete Assist frames without assuming 20 ms RTP | `test_sdp_pcm_profile.py`, including 320 byte input chunks | Reporter trace established the original 10 ms packet pattern and the reporter confirmed FRITZBox to Assist on hardware | Requalify on a new FRITZBox media packetization profile. |
| [#95](https://github.com/n-IA-hane/esphome-intercom/issues/95) | FRITZBox REGISTER uses a bare registrar URI and star dial strings reach the trunk unchanged | registrar, URI, dial-plan and FRITZBox replay suites | Reporter confirmed end-to-end audio and `**621` dialing | Requalify if registrar routing or destination normalization changes. |

For a fix to close a row's remaining boundary, preserve the candidate lock,
peer observations, SIP or RTP evidence where applicable, and post-call resource
snapshot. A passing model test alone is not sufficient.
