# VoIP test matrix

This matrix is mandatory before claiming the VoIP refactor complete. A test
passes only when protocol logs, backend state, card rendering and device state
agree. Counters alone are not proof of audible bidirectional audio.

## Qualification status for the current candidate

A result belongs to its recorded source revision, dependency set, firmware and
route. `PASS` applies only to the named evidence layer. `FAIL` preserves a
reproduced witness. `PENDING` means that the case has not completed on the final
candidate, including cases blocked by an unavailable device or missing peer.
An earlier pass must never be copied into a later candidate by inference.

The 2026.11.1-dev qualification started from snapshot `f2ba8b73`. Full software,
HA-runtime and browser gates use isolated coordinated sources with recorded file hashes;
subsequent fixes require a new recorded source set. The live matrix below remains
PENDING until each selected route has a complete result. The five original
failures remain separate from the generic-account and NAT acceptance cases.

The completed automated qualification covers 3,003 software test cases. This
includes 264 successful native host cases retained only after verifying unchanged
compiled inputs, toolchain and generation helpers; changed and dependent modules
were rerun. The earlier complete HA execution passed 1,098 tests and 11 subtests,
and the real-browser fixture suite passed 84 tests. Its test/style deltas passed
171 software tests and 13 subtests plus 22 HA wire tests. Original failing
results remain preserved separately from their successful reruns.

Standalone host suites passed Audio 455, Runtime Controller 103, Video Renderer
44 and Camera contracts 5 tests. The initial keepalive candidate passed 1,001
native VoIP tests against each pinned audio dependency, 4.0.1 and 4.1.0. The later screenshot refusal-diagnostic
change passed its complete 30-test component suite separately. These results
have recorded source and dependency lineage; they are local qualification, not
a claim about an unpublished commit's GitHub CI or an unperformed hardware case.

The generic-peer discovery and MWI candidate passes 1,050 native tests per audio
dependency, the complete 1,121-test HA gate with 11 subtests, and 366 affected
pure protocol tests with 75 subtests. Its capability response uses a distinct
SDP origin. Existing MWI dialogs complete unsubscribe after deregistration
through their matched identity; spoofed and new unauthorized dialogs remain
rejected. Browser source files and native host generation inputs are unchanged.
Final firmware D1/MWI hardware retests remain PENDING.

An additional RAW bridge envelope regression reproduced a legal 20 ms packet
being dropped or forwarded beyond a 10 ms destination's limit. The bounded
forwarding guard now uses the existing decode/reframe path when receive bounds
or packet size require it. Its immutable overlay passes 112 pure tests and four
subtests plus 131 affected HA tests and three subtests. The original RED remains
preserved; checks require identical samples, correct sequence/timestamps and
unchanged compatible Opus encoded forwarding.

A physical LAN witness using a real P4 as an ordinary SIP account subsequently
passed browser-to-P4 48/16 kHz mono at 10 ms with JPEG in both directions. The
reverse P4-to-LAB call on that earlier firmware exchanged audio/video successfully at 16/16 kHz; the
required advanced 48/16 kHz reverse profile remains unqualified. Both directions
had contiguous PCM packets, decoded JPEG, measured MWW pause/resume and complete
port cleanup. Registration refresh and deregistration were also observed. This
does not qualify physical NAT, acoustic quality or every device/UI route.

Scope packet evidence to its Call-ID and media generation. A combined capture
may reuse payload types and ports across calls; preserve an aggregate oracle
FAIL and apply the [session-scoping workflow](TESTING_AND_DEBUG.md#scope-rtp-evidence-to-one-sip-session)
before qualifying individual calls. Isolated Linux NAT runs cover 1500- and
1280-byte MTUs. Complete advanced UDP OPTIONS responses exceed the 1280-byte
path and fragment there; physical ESP/lwIP fragmentation remains PENDING.

| Original failure | Permanent regression or maintained witness | Current status and remaining acceptance |
|---|---|---|
| A registered directional endpoint received a generic stereo offer before SIP transmission. | [Offer construction](../tests/test_softphone_originate.py), `test_registered_peer_offer_preserves_directional_profile`. | Fixed in `324d0193`; scoped software and earlier browser-to-P4 media passed. Final-candidate generic SIP account, reverse direction and full route matrix are PENDING. |
| A rejected browser start erased the selected phone and made retry report a false busy state. | [Card runtime](../tests/test_frontend_card_runtime.py), `test_card_runtime_follows_authoritative_sip_state_and_call_identity`, including failed start and retry. | Fixed in `324d0193`; the original implementation failed the new assertion and an existing HA lab browser reproduced the loss of identity. Final-candidate failed SIP start followed by ordinary UI retry is PENDING. |
| Adding video overwrote the audio/video port reservation and leaked the original ports. | [Video intent](../tests/test_softphone_video_intent.py), `test_enabling_video_keeps_audio_reservation_owned_until_client_cleanup`, and [real SIP camera cycles](../tests/test_camera_intent_wire_ha.py). | The original candidate reproduced the leak. Snapshot `f2ba8b73` contains the bounded fix and scoped green regression. Live audio-to-video, video removal, Hangup and immediate redial remain PENDING. |
| A P4 JPEG send/receive call transmitted browser video while P4-to-HA RTP stayed at zero. | [SIP video browser probe](../tools/sip_video_browser_probe.py), correlated with the running P4 camera, encoder and SIP/RTP evidence. | The initial physical witness reached a V4L2 buffer-request failure (`VIDIOC_REQBUFS`, `ENOSPC`) before producing JPEG frames. A later identified FULL candidate exchanged real PCM/JPEG in both directions. Complete browser continuity and final route qualification remain OPEN; packet continuity, playback cadence and acoustic quality have separate outcomes. |
| P4 Options content or its gesture ownership prevented scrolling and swipe exit. | [Real LVGL pointer test](../tests/test_lvgl_host_render.py), `test_p4_options_pointer_scrolls_content_and_exits_at_top`, and [settings composition](../tests/test_lvgl_settings_composition.py). | Fixed in `28cf8241`; host pointer checks cover both orientations and the firmware was prepared/uploaded. Physical touch, deployed pixels and the complete screen regression remain PENDING. |

The two documentation failures in CI for `88018550` are separate: the screenshot
redirect title violated sentence case and its moved illustration was left
unreferenced. That run had 2 failed tests, 2,931 passed tests and 219 passed
subtests in the software job. Its HA, browser and Box build jobs passed, but none
of those results covers the final candidate. The documentation corrections must
pass [documentation contracts](../tests/test_documentation_contract.py) and the
full software gate before the candidate is called green.

### Qualification tool findings

The first complete HA gate on the expanded candidate executed 1,057 tests and
11 passing subtests: 1,043 tests passed and 14 failed, with no skipped tests. Eight failures covered
voicemail, one covered parking and five covered inbound capability negotiation.
The failures are preserved with their corrected contracts and reruns. In
particular, a real browser-to-own-mailbox call switched to TCP for its larger
INVITE, but local caller correlation inspected only the UDP transport. The
bounded correction checks the owned TCP writer's local tuple as well; real
mailbox calls and matched/mismatched UDP/TCP regressions pass in the complete HA gate.
Six other voicemail cases exposed the receive envelope: HA advertised a 10 ms
preference and a 20 ms maximum, then rejected valid 20 ms PCM. The expanded
fixtures retain both 10 and 20 ms packets and the original exact recorded PCM
assertions. The candidate restores the caller's own advertised receive maximum
when the answer omits that attribute; the complete HA gate covers both packet lengths.
A preferred packet interval is not a strict
duration limit, and the two directions can differ, as specified by
[RFC 3264](https://www.rfc-editor.org/rfc/rfc3264.html#section-6.1) and
[SDP packet-time attributes](https://www.rfc-editor.org/rfc/rfc8866.html#section-6.5).

The parking fixture's real peer advertised audio-only capabilities through
OPTIONS. It correctly received no video reservation, despite a separate endpoint
flag. Both this known state and the interval before capability discovery must
remain covered. The added unknown-capability case exposed a separate real
G.711 failure: an ordinary peer answered the advertised 10 ms offer, but HA
rejected its compatible codec answer. The bounded common-codec candidate passes
real 10/20 ms peer, parking and complete HA/protocol checks.

Ten subsequent re-INVITE/image-attribute tests exposed an inconsistent shared
fixture: its initial SDP advertised a maximum packet time, but its manually
constructed dialog omitted that bound. The central confirmed-dialog fixture now
retains the format from its initial SDP, with an independent packet-bound check.
Complete format equality, rollback, compensation and teardown assertions remain
in place; all affected modules pass their complete rerun.

The complete native VoIP gates initially passed 977 tests per audio dependency
profile, while 24 cases could not compile: two host fixture callbacks retained
the old owning string signature after the production hook changed to a borrowed
string view. Their corrected fixtures execute all 24 cases successfully. The
repeated complete gates each pass 1,001 tests, with no skipped, failed or error
results, against the pinned audio dependencies 4.0.1 and 4.1.0. Source import
audits reject live-parent checkout dependencies. These are native host results;
a fixture compile error or a host pass is not physical-device qualification.

The video probe's old outbound helper wrote private card fields and called the
private start method. It has been replaced with visible Keypad, input and Call
controls driven by Playwright; browser tests require trusted input/click events
and reject private-state shortcuts. Camera controls use the same normal UI path.

`--send-camera` selects the initial preference; it does not prove a mid-call
change. The explicit `--camera-toggle-cycle` starts with the browser camera off,
requires a connected audio dialog, enables/disables/re-enables sending, uses ordinary
Hangup, requires quiescence and redials as a new dialog. It preserves the
original camera preference. `--camera-cycle-direction sendonly` is the default
for a receiving peer without a camera. `sendrecv` covers a camera peer, including
an initial receive-only video leg, and requires actual video RTP, receive,
decode and rendering progress. An explicitly allowed dark image still requires
real decoded/rendered frames. UI command order and [actual SIP re-INVITE/ownership](../tests/test_camera_intent_wire_ha.py)
are separate oracles. The wire fixture receives the initial ACK before the three
public camera actions, preserves 48/16 kHz mono at 10 ms, and checks both dialog
generations release every lease. It makes no RTP/media-counter claim. The full
browser/runtime cycle remains PENDING until recorded.

Screenshot image admission is an independent resource change. The standalone
component keeps `preallocate_buffers: true` as its default. With `false`, only
worker/mutex and encoder workspace are reserved at boot; image temporaries are
admitted per capture in the configured pool. Allocation failure preserves the
published ID/JPEG and existing readers, without changing quality, dimensions or
memory capabilities. Raw pixels and coverage are released after encoding;
published/leased JPEGs retain their ownership. The expanded component host suite
has 30 passing tests, including the complete component with sanitizers and a
concurrent HTTP reader. An identified Box candidate with on-demand allocation
passed the SDK build, OTA and official boot confirmation; three fresh 360-pixel
JPEG captures passed ID/ETag, decode and image review checks. Physical OOM and the
complete gallery remain PENDING. No camera-memory reservation is inferred from
this option.

An earlier Box browser run reported 149 discarded 10 ms playback frames while
HA RTP drop counters remained zero. That is 1.49 seconds of queued browser PCM,
separate from time-scale adaptation. The case remains FAIL. The old recording
lacked fresh worklet statistics at its early sample, so it does not locate the
first discard or distinguish startup from steady state. The cause remains OPEN.
Keep thresholds unchanged and correlate monotonic/wall clocks, AudioContext
state/time, actual WebSocket PCM geometry and bounded wire WAV capture before a
controlled comparison. Muted synthetic capture gives no acoustic qualification.

A later saved browser result incorrectly reported success after a hold sample
recorded 152 discarded frames. A subsequent document/AudioContext recreation
reset the final counters to zero. The corrected [timeline oracle](../tests/test_playwright_auth.py)
keeps loss deltas across every observed counter generation within the same SIP
Call-ID, records missing observations and rejects any earlier loss despite later
recovery. It also preserves running render-clock deficits of at least half a
second against the observer's monotonic clock. Replaying the original saved
result now fails with its 152 frames and the original first-loss sample intact.
Document navigation and cold AudioContext timing are browser observations;
their causes remain separate from ESP, RTP and acoustic qualification.

## Evidence layers and execution gates

| Gate | Required observation | Limit of that result |
|---|---|---|
| Source and host software | Exact coordinated revisions; Ruff, JavaScript parsing, configuration/code generation, regression and host-runtime tests; nonempty successful JUnit. | Extracted methods, substituted scheduling and compile success do not establish a real call or physical media. |
| Home Assistant and independent peer | Supported HA APIs, production SIP parser/dialog/media owners, an independent peer and real loopback sockets; decoded media where the case requires it. | A controlled peer or in-process HA instance is not an ESP behind NAT or a physical device. |
| Real browser | Existing Lovelace UI, ordinary controls, the page-owned engine, actual WebAudio/WebSocket media, backend SIP/RTP and complete cleanup. | Synthetic capture and muted output prove transport/media processing, not microphone, speaker or acoustic quality. |
| Hardware and panel | Identified running firmware and target, real device call route, independent SIP/RTP and camera/audio/panel observations. | A framebuffer is not panel readback; packet counters are not acoustic proof. |

Freeze one complete candidate before the broad run. Keep compiler output on
persistent disk and captures in the explicitly selected disposable evidence
directory. Independent software suites may run in parallel. Shared HA labs,
physical phones, registration changes and firmware work require one serialized
owner. Before and after every live case require no active call, leg, relay,
media owner, transcoder or allocated RTP port, with `call_scoped_quiescent=true`.
If a case fails, preserve its exact identity and evidence, research the owning
contract and state one falsifiable hypothesis before preparing another bounded
candidate. Do not repair a failed witness with a peer-specific route, a second
media lifecycle or longer retry delays.

## Full functional acceptance matrix

The following rows expand the automatic coverage catalog into runtime
obligations. Every runtime row is PENDING for the final candidate until its
result names the actual peers, route, direction, registration mode and media.
An unavailable target remains PENDING, with its reason recorded.

| Case family | Required runtime witness | Existing regression or runner |
|---|---|---|
| Direct, unregistered calls | HA/browser-to-ESP, ESP-to-HA/browser and ESP-to-ESP, both directions over each supported SIP transport, without requiring REGISTER. Cover P4, Box V2, WS3 Audio and available Spotpear targets. | [Protocol tests](../tests/test_sip_protocol.py), [live qualification](../tools/live_voip_qualification.py), [browser matrix](../tools/ha_softphone_matrix.py). |
| Registered managed and generic phones | Repeat both call directions with managed registration, then with a real ESP registered as a normal SIP account. The generic case must not depend on ESP endpoint kinds, managed capabilities, legacy endpoint text or injected discovery hints. Capabilities genuinely advertised by the real peer on the SIP/SDP wire, including D1, remain valid input. | [Registrar tests](../tests/test_sip_registrar.py), [offer tests](../tests/test_softphone_originate.py), [registration engine](https://github.com/n-IA-hane/esphome-voip-stack/blob/dev/tests/test_sip_registration.py). |
| Generic-account audio geometry | On HA-to-ESP and ESP-to-HA calls require the actual negotiated directional 48/16 kHz mono PCM contract with 10 ms packets. Check SDP, payload types, packet sizes, clocks and decoded media independently; a generic 20 ms software offer is not this witness. | [SDP profiles](../tests/test_sdp_pcm_profile.py), [RTP profiles](../tests/test_rtp_profile.py), [SIP client sockets](../tests/test_sip_client_socket.py). |
| NAT and registration flow | In an isolated NAT topology verify authenticated REGISTER, received/rport, Contact and TCP-flow reuse, refresh, expiry, reconnect and remote reachability. Test ordinary OPTIONS keepalive (`registration.keepalive_interval`, default 15s, 0 disables), idle expiry and changed mappings without shortening registration expiry. Then prove RTP in both directions, source validation, Hangup/CANCEL and zero remaining ports/sockets before redial. | [Registrar flow tests](../tests/test_sip_registrar.py), [shared local signaling flow](../tests/test_local_sip_flow_ha.py), [isolated NAT runner](../tools/ha_voip_lab/sip_nat_lab.py), [registration transport](https://github.com/n-IA-hane/esphome-voip-stack/blob/dev/tests/test_sip_registration_transport.py), [keepalive regression](https://github.com/n-IA-hane/esphome-voip-stack/blob/dev/tests/test_sip_registration_keepalive.py), [SIP client sockets](../tests/test_sip_client_socket.py). |
| Duplex media and initial video | Measure browser/HA and device RTP in both directions on every applicable route. Repeat initial JPEG/H.264 or other supported video profiles, including bidirectional video for the normal-account ESP. Record capture, encode, transport, decode and presentation separately. | [Browser media probe](../tools/sip_video_browser_probe.py), [video relay tests](../tests/test_sip_video_relay.py), [real-browser video](../tests/test_frontend_video_browser.py). |
| Media changes | Audio to video, camera off/video removal, hold/resume and supported codec/direction changes. Include accept, reject, supersede, cancellation and another call reusing retired ports; Hangup must release only the current owner's resources. | [Video intent](../tests/test_softphone_video_intent.py), [SIP protocol](../tests/test_sip_protocol.py), [transfer video changes](../tests/test_sip_phone_transfers_ha.py). |
| Terminal state and reuse | Each side ends an established call; caller cancels; receiver declines; DND/busy and no-answer complete. Repeat failed start then retry, late responses and immediate redial with the same phone identity and no stale media. | [Card runtime](../tests/test_frontend_card_runtime.py), [engine runtime](../tests/test_frontend_engine_runtime.py), [outbound lifecycle](../tests/test_outbound_lifecycle_runtime.py), [browser matrix](../tools/ha_softphone_matrix.py). |
| Ring and conference groups | First-answer wins, individual decline, all busy/unavailable, caller cancel, simultaneous answers, join/leave and membership updates; preserve both media directions and retire losing legs. | [Group matrix](../tests/test_group_call_matrix.py), [conference](../tests/test_conference.py), [ring-group live runner](../tools/ring_group_live_matrix.py). |
| Pickup, parking, hold and transfer | Pickup only a confirmed ringing leg; park/retrieve; hold/resume; blind and attended transfer, cancel/failure rollback and replacement. Include browser, registered phone and standard PBX legs. | [Pickup](../tests/test_group_pickup_regressions_ha.py), [parking](../tests/test_parking_lot_ha.py), [phone transfers](../tests/test_sip_phone_transfers_ha.py), [Asterisk runner](../scripts/run_asterisk_transfer_lab.py). |
| Voicemail, DTMF and phone services | No-answer deposit; enter own mailbox, play and delete with negotiated RTP events and SIP INFO; MWI counts/subscription lifecycle; BLF/presence and connected identity. Verify live UI/contact updates after each operation. | [Voicemail](../tests/test_voicemail_ha.py), [DTMF](../tests/test_sip_rtp_dtmf.py), [BLF](../tests/test_pjsip_blf_ha.py), [presence](../tests/test_phone_list_presence.py), [native phone clients](../tests/test_phone_clients_interop_ha.py). |
| ESP runtime and shared audio | Observe FSM transitions, VA start/stop, MWW intent, ringing, media playback and VoIP arbitration. Video pauses/resumes MWW through the maintained policy without changing the user's enable choice. Use only triggers present in the identified firmware. | [Runtime target model](../tests/test_runtime_controller_target_model.py), [runtime UI](../tests/test_runtime_ui_connectivity.py), [video/MWW policy](../tests/test_esp_video_renderer_mww_policy.py), coordinated Audio Stack and Runtime Controller suites. |
| Contacts, Options and displays | Real device phonebook discovery, navigation, selected destination, status/BLF, Options controls, scroll and top-boundary exit. Repeat P4 orientations, Box, WS3 LCD and available Spotpear layouts. Native screenshots and real touch have independent results. | [Phone-service guide](ESP_PHONE_SERVICES.md), [settings composition](../tests/test_lvgl_settings_composition.py), [LVGL pointer/render tests](../tests/test_lvgl_host_render.py), [screenshot guide](ESP_SCREENSHOT.md). |
| Standard PBX interoperability | Direct/static and registered PBX routes, generic SIP accounts, both call directions, media, transfer, DTMF, MWI/BLF and teardown. Include strict registration/authentication behavior and supported provider/trunk legs without using managed-ESP shortcuts. | [Classic PBX guide](CLASSIC_PBX.md), [Asterisk runner](../scripts/run_asterisk_transfer_lab.py), [phone transfers](../tests/test_sip_phone_transfers_ha.py), [inbound routing](../tools/inbound_routing_qualification.py). |

The LDAP C++ interoperability test proves the directory client contract; it does
not qualify the generic SIP-account row. Similarly, managed directional-offer
unit tests and a controlled softphone cannot substitute for the normal-account
physical ESP, NAT or PBX witnesses. Acoustic tests and household runtime actions
remain explicitly coordinated operations, even when software gates are green.

## Global gates

- No ESP firmware compile or OTA before local implementation and host checks.
- `voip-pcm/1` profile documented and kept in sync with code.
- Record the selected PCM or optional compressed-codec profile explicitly.
  Optional P4 video is compile-gated to one encoded codec, JPEG or H.264, and is
  absent from audio-only firmware.
- Direct ESP SIP remains available without REGISTER. Optional ESP PBX accounts
  support registration and Digest authentication. Qualify managed HA accounts
  and normal generic SIP accounts separately; registration must not become a
  universal prerequisite for a valid direct inbound INVITE.
- SIP signaling available on UDP and TCP; RTP audio remains UDP in the current
  phone profile.
- `Via` and `Contact` include correct transport and host:port.
- Trunk REGISTER uses the registrar domain as Request-URI while `To` and `From`
  retain the account address of record. Qualify this against both a strict
  FRITZBox registrar and the configured provider/PBX.
- SDP never emits `a=fmtp:<pt> ptime=N`; only `a=ptime` and `a=maxptime`.
- Default payload limit rejects `48000:s16le:1:20`.
- Native 48 kHz mono signed-16 PCM profiles use 10 ms frames, 960 payload
  bytes, and remain below the default 1200-byte RTP payload budget.
- Canonical directory entries are data-driven. Maintained native phone services
  use LDAP for contacts and SIP OPTIONS/SDP for media capabilities. Legacy JSON
  delivery is a compatibility path, not the generic SIP discovery contract.
  Direct ESP and softphone entries retain their actual SIP transport.
- ESP endpoint declarations never include a separate `sip` protocol column.
  SIP is implicit; the only transport choice is SIP/TCP or SIP/UDP signaling.
- The phonebook is an outbound dial plan, not an inbound caller allowlist.
  Unknown/unregistered callers remain valid live-test sources.
- SIP URI users remain stable routing identifiers while quoted display names
  preserve spaces. Incoming caller text comes from `From`; connected callee
  identity follows RFC 4916 when both peers support the in-dialog UPDATE.

## Local contract tests

Run after implementation is complete:

- `./scripts/test_suite.sh software-full --keep-going`
- `HA_PYTHON=/path/to/ha-test-venv/bin/python ./scripts/test_suite.sh ha --keep-going`
- `./scripts/test_suite.sh browser --keep-going`
- `./.venv/bin/python tools/voip_dev_check.py`
- `./.venv/bin/python tests/support/qualification_matrix.py --validate --summary`
- `./scripts/run_virtual_device_tests.sh --all`
- `./scripts/run_virtual_device_tests.sh --repeat 1000 --seed 1234 --scenario terminal-no-late-green`

`run_virtual_device_tests.sh` uses the deterministic contract simulator. It is
not a virtual ESP device and it is not proof of physical audio timing.

`tests/support/qualification_matrix.py` is the generated transport/codec and
usage catalog for its enumerated families. The full functional acceptance table
above also owns registration, NAT, PBX and device-runtime obligations outside
those generated families. A new public mode needs its permanent regression and
execution entry before it can be marked covered; catalog size is not execution
evidence.

The generated matrix currently contains 325 usage scenarios and 2,162
transport/codec combinations. These rows are coverage obligations, not a claim
that a single physical-device run exercises every Cartesian combination.

Required simulator scenarios:

- `ha-to-esp-answer-hangup`
- `ha-to-esp-cancel`
- `ha-to-esp-auto-answer`
- `ha-to-esp-dnd-decline`
- `esp-to-ha-answer-hangup`
- `esp-to-ha-decline`
- `esp-to-ha-busy-while-ringing`
- `esp-to-ha-busy-while-in-call`
- `esp-to-esp-direct-decline`
- `esp-to-esp-via-ha-decline`
- `esp-busy-while-ringing-direct`
- `esp-busy-while-ringing-via-ha`
- `esp-busy-while-in-call-direct`
- `esp-busy-while-in-call-via-ha`
- `ha-softphone-card-contract`
- `esp-mirror-card-contract`
- `phonebook-json-shared`
- `softphone-codec-only-488`
- `sip-auth-unsupported`
- `ha-softphone-remote-cancel`
- `terminal-no-late-green`

## HA service matrix

For every service, assert HA event bus output, logs and resulting entity/card
state.

- `voip_stack.call` without source: HA originates to roster destination.
- `voip_stack.call` with an ESP `device_id`: selected
  ESP originates the call through its own `start_call` action.
- `voip_stack.call destination=Kitchen`: resolves roster name.
- `voip_stack.call destination=sip:Kitchen@IP:5060;transport=tcp`: direct
  SIP URI.
- `voip_stack.call destination=0574...`: requires HA SIP bridge route, no direct
  ESP call.
- `voip_stack.call destination=...`: HA SIP UA originates using roster
  resolver.
- `voip_stack.call` with a requested Home Assistant action response returns the
  authoritative endpoint/call snapshot; the current card originates through
  the standard `call_service` WebSocket command. The removed private start
  command is absent and must not acquire a compatibility adapter.
- `voip_stack.answer`: pending inbound SIP receives `200 OK` only
  after local media setup path is ready.
- `voip_stack.decline`: pending inbound SIP receives configured final
  response and terminal reason.
- `voip_stack.hangup`: handles pending INVITE, active client, relay and
  server dialog idempotently.
- `voip_stack.add_contact`, `remove_contact`, `set_contacts` and `clear_contacts`:
  update the canonical manual contacts, validate serialized input and reflect
  the result through current LDAP phone services and supported compatibility
  delivery. Never require the legacy endpoint text sensor on a native phone.
- `voip_stack.push_phonebook`: diagnose supported compatibility delivery and
  record every updated/skipped endpoint; verify native LDAP results separately.
- `voip_stack.set_dnd`: updates the HA softphone DND store, emits event,
  and causes inbound calls to receive `486 Busy Here` with DND reason.
- `voip_stack.forward`: source leg, destination leg, busy destination and
  self-forward rejection all publish terminal/forward events.
- `voip_stack.route`: resolves an outstanding explicit route request and
  rejects missing/stale requests deterministically.
- `voip_stack.export_phonebook`: returns the current canonical roster only in
  the administrator's service response, without mutating or broadcasting it.
- `voip_stack.set_ha_softphone_settings`: updates extension and group settings,
  refreshes the roster and leaves unrelated settings intact.
- `voip_stack.purge_devices`: rejects every request and never removes a Device
  owned by ESPHome.
- `voip_stack.create_account`, `remove_account`, `rotate_account_password`,
  `enable_account`, `disable_account`, and `list_accounts`:
  validate credential lifecycle, one-time secret handling, registrar refresh
  and redacted exports.

## Live device call matrix

Run after HA deployment and only after local tests pass. Exercise every powered
device in the qualification environment and record unavailable devices as not
run, never as passed or failed. Collect HA logs, ESP logs and sampled entity
snapshots.

- HA softphone card calls WS3 over SIP TCP.
- HA softphone card calls Spotpear over SIP TCP.
- HA softphone card calls WS3 over SIP UDP if contact transport is UDP.
- HA softphone card cancels before answer; ESP returns idle, no green/spin.
- HA softphone card hangs up while in_call; ESP receives BYE and goes idle.
- ESP answers HA call; RTP packet counters increase both directions and audible
  browser-to-ESP audio is immediate.
- ESP declines HA call; HA softphone/card show declined reason.
- ESP DND rejects HA call; HA card/log show DND.
- ESP auto-answer accepts HA call and card goes in_call.
- WS3 calls HA; HA card rings, Answer works, audio is immediately
  bidirectional.
- Spotpear calls HA; HA card rings, Answer works, audio is immediately
  bidirectional.
- Browser without `navigator.mediaDevices` cannot send Answer, leaves the
  remote dialog ringing and displays an actionable local-media error.
- ESP calling HA then ESP hangs up; HA card returns idle/disconnected.
- ESP calling HA then HA declines; ESP last reason updates.
- WS3 calls Spotpear direct TCP SIP.
- Spotpear calls WS3 direct TCP SIP.
- WS3 calls Spotpear direct UDP SIP.
- Spotpear calls WS3 direct UDP SIP.
- WS3 calls Spotpear through HA bridge.
- Spotpear calls WS3 through HA bridge.
- Second caller hits HA while HA softphone is ringing: second caller gets busy.
- Second caller hits HA while HA softphone is in_call: second caller gets
  busy.
- Second caller hits ESP while ESP is ringing: second caller gets busy.
- Second caller hits ESP while ESP is in_call: second caller gets busy.
- An offer without a codec supported by the selected endpoint receives `488`.
  Test PCM-only and configured compressed-codec profiles independently.
- Unsupported authentication challenges terminate with an explicit reason;
  configured registered accounts must complete their supported Digest flow.
- Unknown, unregistered SIP peer calls HA by name and extension; routing is not
  rejected merely because the source is absent from the phonebook.
- Unknown, unregistered SIP peer calls WS3 and Spotpear directly with compatible
  SDP; each endpoint rings or returns only its normal busy/DND response.
- Established ESP calls receive a hold/codec re-INVITE: the ESP sends `488`,
  the original RTP/dialog stays usable, and a later BYE cleans up.
- Established HA-owned calls receive compatible UPDATE and re-INVITE offers:
  hold/resume, direction, RTP endpoint and supported audio-format changes
  commit once; rejected or retransmitted offers do not duplicate the commit.
- An initial INVITE without SDP receives a local offer in `200 OK`, answers in
  ACK, reaches a real registered SIP peer through the normal route, survives
  source and destination hangup, and returns all call resources to baseline.
- HA video hold/resume retains its negotiated codec/direction contract and
  camera authorization. Supported video addition/removal commits atomically;
  rejected or superseded changes preserve the prior dialog, media and ownership.
- HA/Baresip legs negotiate Opus at 48 kHz where offered and supported; a
  separate L16 48 kHz case verifies high-rate PCM without implying that ESP
  endpoints support compressed codecs.
- A FRITZBox-style PCMA call that advertises 16 ms but sends 10 ms RTP packets
  is reframed without decode drops and produces complete Assist input frames.
- A caller with display name `Dio Cane` remains exactly `Dio Cane` on the
  receiving card and ESP. Spaces must not become underscores, and roster data
  must not replace the peer's valid `From` display name.
- A caller that dials a numeric extension receives the selected endpoint's
  canonical name through connected identity when RFC 4916 is supported.
- A Wildix-routed call delivers `0123456789*#` in order through the negotiated
  DTMF transport, with every digit visible in the HA event and automation log.
- P4 H.264 and JPEG profiles each complete incoming and outgoing audio/video
  calls with bidirectional media, clean BYE/CANCEL handling and no audio gaps.
- H.264-to-JPEG and JPEG-to-H.264 HA bridge fallback passes both media
  directions. Already compatible directions remain direct and hangup leaves no
  FFmpeg process, socket, owner or call resource.
- An audio-only WS3 offer never starts a video session or inherits video from a
  parallel P4/card call.
- Direct P4-to-WS3 and WS3-to-P4 calls remain audio-only, bypass HA media and
  cleanly return both endpoints to idle.

## Real HA qualification runners

- `tools/live_voip_qualification.py --all --allow-trunk`: HA, powered ESP,
  ring/conference, DND, self-busy and external cancellation paths.
- `tools/ring_group_live_matrix.py`: ESP/browser winners, individual decline,
  caller cancellation and bidirectional browser video.
- `tools/ha_softphone_matrix.py`: remote hangup, reload while ringing, manual
  and automatic answer, decline, forward/resume, multiple browsers, registered
  SIP and automation fallback.
- `tools/inbound_routing_qualification.py`: trunk default routing, native
  automation override, DTMF valid/invalid/no-digits, Assist forwarding and
  cancellation during the DTMF window.

The real matrix must also include registered SIP client to HA, HA to registered
client, registered client to registered client, Wildix to HA, Wildix to P4 and
the reverse directions supported by each endpoint. Video-capable legs verify
both direct codec relay and the bounded H.264/JPEG fallback. Audio-only legs
must remain audio-only.

An external-call qualification must include a completed PSTN call, not only
ringing followed by CANCEL: answer the remote phone, require the audio dialog
and RTP counters to remain active, then hang up normally. Repeat with an
audio/video offer and an audio-only gateway answer so omission of trailing
video cannot tear down the accepted audio leg. Separately verify that a
video-capable endpoint can accept the offered video and that mid-dialog video
addition still works.

For inbound DTMF, test both negotiated transports. An offer containing
`telephone-event` validates provisional RFC 4733 collection. An offer without
it validates confirmed-dialog SIP INFO collection. Do not force an RTP-only
test profile against an SDP offer that contains no named-event payload and then
classify the missing digit as a PBX failure.

## Card visual matrix

Use Playwright/Chromium screenshots plus HA events/logs.

- `/lovelace/default_view`: HA softphone card shows roster contacts, excludes
  HA self and never says `No endpoint` when `sensor.voip_phonebook` has
  callable contacts.
- HA softphone card ringing after ESP INVITE: Answer and Decline buttons are
  clickable and perform HA softphone actions.
- HA softphone card after remote BYE/CANCEL: returns idle from backend event,
  not from local optimistic state.
- `/lovelace/default_view` or the configured VoIP dashboard: ESP mirror card
  mirrors WS3 only.
- `/lovelace/default_view` or the configured VoIP dashboard: ESP mirror card
  mirrors Spotpear only.
- ESP mirror next/prev press ESP buttons and update ESP destination.
- ESP mirror Call presses ESP call button; no browser audio backend starts.
- ESP mirror Answer/Decline/Hangup press ESP controls.
- ESP mirror incoming/ringing/in_call/declined/busy states match ESP entity
  snapshots.

## LED/presentation matrix

Sample LED entity effect/color at <=100 ms while calls transition.

- Outgoing/calling: orange blink.
- Incoming/ringing: red blink.
- Streaming: fixed green.
- Decline/cancel/timeout before answer: returns idle/off, never green/spin even
  for one sample.
- BYE from remote while in_call: returns idle without transient incoming or
  media-player spin.
- Media/MWW active then incoming VoIP call: VoIP owns audio and presentation
  remains consistent.

## Required artifacts

- JSON/JUnit reports in the explicitly selected per-run evidence directory;
  see [temporary output and persistent build rules](TESTING_AND_DEBUG.md#temporary-qualification-outputs).
- Playwright screenshots for both dashboards in idle, ringing and in_call.
- HA log excerpt around every call leg.
- ESP serial/API logs around every direct/bridge call.
- SIP transcript samples for UDP and TCP.
- RTP stats: selected payload type, rate, frame_ms, tx/rx packets, drops,
  wrong payload type, wrong size.
- Final table: scenario, expected, observed, artifacts, pass/fail.
