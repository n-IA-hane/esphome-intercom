# VoIP test matrix

This matrix is mandatory before claiming the VoIP refactor complete. A test
passes only when protocol logs, backend state, card rendering and device state
agree. Counters alone are not proof of audible bidirectional audio.

## Global gates

- No ESP firmware compile or OTA before local implementation and host checks.
- `voip-pcm/1` profile documented and kept in sync with code.
- No ESP codecs beyond RTP PCM L16/L24.
- ESP endpoints use no Digest auth, send no `WWW-Authenticate`, and require no
  `Authorization` or `REGISTER`. HA may authenticate its optional trunk and
  local registrar accounts; neither mechanism gates all inbound INVITEs.
- SIP signaling available on UDP and TCP; RTP audio remains UDP in the current
  phone profile.
- `Via` and `Contact` include correct transport and host:port.
- SDP never emits `a=fmtp:<pt> ptime=N`; only `a=ptime` and `a=maxptime`.
- Default payload limit rejects `48000:s16le:1:20`.
- JSON roster entries are data-driven; direct ESP and softphone entries include
  explicit SIP transport or route through HA without user-authored `kind`.
- ESP endpoint declarations never include a separate `sip` protocol column.
  SIP is implicit; the only transport choice is SIP/TCP or SIP/UDP signaling.
- The phonebook is an outbound dial plan, not an inbound caller allowlist.
  Unknown/unregistered callers remain valid live-test sources.

## Local contract tests

Run after implementation is complete:

- `./.venv/bin/python -m pytest -q`
- `./.venv/bin/python tools/voip_dev_check.py`
- `./.venv/bin/python tests/support/qualification_matrix.py --validate --summary`
- `./scripts/run_virtual_device_tests.sh --all`
- `./scripts/run_virtual_device_tests.sh --repeat 1000 --seed 1234 --scenario terminal-no-late-green`

`run_virtual_device_tests.sh` uses the deterministic contract simulator. It is
not a virtual ESP device and it is not proof of physical audio timing.

`tests/support/qualification_matrix.py` is the authoritative automatic
coverage map. It must include every supported SIP transport pair, route mode,
direction, audio role, negotiated format, terminal failure and race condition.
Adding a new mode or public option requires extending that matrix first, then
implementing the runner coverage behind the generated scenario IDs.

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
- `voip_stack.add_contact`: updates central JSON and pushes to
  online ESPs.
- `voip_stack.remove_contact`: removes one manual contact by
  name and pushes the updated JSON.
- `voip_stack.set_contacts`: rejects invalid JSON roster and
  pushes valid JSON.
- `voip_stack.clear_contacts`: clears manual contacts and pushes updated
  JSON.
- `voip_stack.push_phonebook`: logs every pushed/skipped ESP.
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
- `voip_stack.purge_devices`: no-op and removal cases report exactly which
  unavailable devices were selected.
- `voip_stack.create_account`, `remove_account`, `rotate_account_password`,
  `enable_account`, `disable_account`, and `list_accounts`:
  validate credential lifecycle, one-time secret handling, registrar refresh
  and redacted exports.

## Live device call matrix

Run after HA deployment and only after local tests pass. Exercise every powered
device in the qualification environment and record unavailable devices as not
run, never as passed or failed. Collect HA logs, ESP logs and sampled entity
snapshots. The 2026-07-18 home qualification had only WS3 powered; Spotpear and
P4 were therefore outside that physical run.

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
- Codec-only softphone INVITE without L16/L24 receives `488`.
- SIP softphone/proxy 401/407 challenge terminates with unsupported-auth reason.
- Unknown, unregistered SIP peer calls HA by name and extension; routing is not
  rejected merely because the source is absent from the phonebook.
- Unknown, unregistered SIP peer calls WS3 and Spotpear directly with compatible
  SDP; each endpoint rings or returns only its normal busy/DND response.
- Established ESP calls receive a hold/codec re-INVITE: the ESP sends `488`,
  the original RTP/dialog stays usable, and a later BYE cleans up.
- Established HA-owned calls receive compatible UPDATE and re-INVITE offers:
  hold/resume, direction, RTP endpoint and supported audio-format changes
  commit once; rejected or retransmitted offers do not duplicate the commit.
- HA video hold/resume retains the directional H.264/VP8/JPEG contract and
  camera authorization. Adding/removing video or changing its codec receives
  `488` without damaging the current call.
- HA/Baresip legs negotiate Opus at 48 kHz where offered and supported; a
  separate L16 48 kHz case verifies high-rate PCM without implying that ESP
  endpoints support compressed codecs.

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

- `test_runs/` JSON from live matrix.
- Playwright screenshots for both dashboards in idle, ringing and in_call.
- HA log excerpt around every call leg.
- ESP serial/API logs around every direct/bridge call.
- SIP transcript samples for UDP and TCP.
- RTP stats: selected payload type, rate, frame_ms, tx/rx packets, drops,
  wrong payload type, wrong size.
- Final table: scenario, expected, observed, artifacts, pass/fail.
