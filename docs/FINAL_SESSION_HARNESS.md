# Grouped final-session harness

This is preparation and host qualification tooling. It performs no physical
action on import. The plan CLI only writes `prepared`, with physical/acoustic
`not_run`; it has no live execution switch. Existing G0 admission remains in
`qualification/evidence.py`, `qualification/registry.py` and
`scripts/run_hil_qualification.py`. A preparation document cannot satisfy G0.
Unsupported HIL scenarios still fail closed until their actual measured
producers and validators exist. No host receipt becomes physical evidence.

## Final grouped matrix

Use the same complete, identified P4 and S3 LCD FULL candidate, preserving
Assist, MWW, audio and display. Bind the actual MAC/build, source lock, candidate
lock and firmware artifacts before any setter or call. An API identity check
identifies the physical qualification target; it must never choose SIP routing
or codecs. Do not substitute Lite or add another build only for one scenario.

| Route | Required media and identity |
| --- | --- |
| Home HA to/from ESP | Home managed `ha`, default line `ha`, Master0 preserved; independently decode HA TX48k/10 ms and ESP TX16k/10 ms with explicit D1 SDP. |
| P4 to/from S3 | Actual peer dialogs, both terminal owners, watchdog window from the real configured contract, cleanup, immediate distinct redial. |
| Manual VoIP Stack HA SIP account to/from physical ESP | User-created ordinary account, `GenericSipAccount` / `sip_account`, standard AoR and SDP, audio and video in both directions, both terminal owners and redial. No Provisioner or ESPHome integration in the test PBX. |
| Asterisk QA to/from P4 and S3 FULL | Existing QA7101/7102 and peers7103/7104, trunk6002; L16/16k/20 ms plus supported10/16 ms alternatives, independent directional PCM and full teardown. |
| ESP1 to HA to registered Asterisk trunk to Asterisk-registered ESP2, and reverse | Duplex audio mandatory; correlate both PBX legs and actual per-leg SDP/RTP. If Asterisk delivers H.264, HA converts it to JPEG for S3. S3 never receives an H.264 decode contract. Preserve passthrough when formats match. |
| Real Lovelace LAB Dashboard | Actual monitor/shared engine; ordinary tile call, answer, pickup, hangup, remount/reload; browser PCM plus independent SIP-peer PCM, both legs, EOF and cleanup. |

The manual test account has user ownership and a separate Native line such as
`manual_ha`; creation remains deferred until the user requests it. Home's `ha`
account stays active and default. Asterisk remains an additional interoperability
case. Credentials stay in private files, never command arguments, logs or plan
JSON. Do not register household ESPs through LAB's ESPHome integration.

For both FULL ESPs on Asterisk, enable real MWI and BLF code in the final
candidate, including `registration.message_waiting: true` and explicit QA
contact `account_id`. Require SUBSCRIBE/NOTIFY plus actual Native getter and UI
ringing, in-call and idle state. Deposit QA voicemail, observe QA waiting/counts,
enter the real menu with RFC4733, measure playback, delete the QA message,
observe QA waiting-no, then quiescence. No mailbox creation/deletion or PBX
configuration action is performed by this preparation tool.

An empty-account MWI getter may choose another waiting mailbox. Record Home's
baseline and verify `get_message_waiting("qa")` and QA's Message-Account. A
pre-existing Home waiting indicator cannot prove a QA transition. Preserve Home
messages and settings. Master0 playback may prove decoded transport/native sink
progress; it does not prove listening or audible output. Acoustic acceptance
requires its separate authorized experiment.

## Existing owners and reusable collectors

`tools/qualify_esp_session.py` selectively recovers Q's domain-keyed state and
capture checks. It contains no automatic silence, provisioning, account removal,
device/PBX restart, demo roster or IMAP path. Supply the existing actor's API
client to `EspObserver`; exact MAC/build verification precedes its services.
Its connection and subscriptions are closed even on identity failure.

Screenshot capture requires observed ID and status before the action.
Busy/Capturing refuses immediately. Historical Ready does not complete a new
request: both ID and Ready must be fresh. Repeated Error and Busy fail without
waiting for an ID. The exact URL, HTTP200, MIME, matching ETag, complete JPEG,
expected geometry and decoded pixels belong to that capture. The ID/status are
checked again after download. Inject a real native scene checkpoint before and
after capture; a changed scene fails without publishing an artifact. A capture
without that checkpoint explicitly reports `scene_verified: false` and cannot
prove page semantics. Unchanged idle pixels are valid for a new capture ID.

`decode_pcap_l16()` reuses the maintained SDP/RTP analyzer and checks the capture
with capinfos, selects one explicit tuple/SSRC/PT, and independently decodes the
actual big-endian L16 payload into WAV. It checks payload size against clock and
packet time, RTP sequence/timestamp continuity and a nonzero observation window.
Decode both directions separately. A waveform's nonzero RMS does not establish
semantic content, acoustic quality, hardware sample integrity or listening.
Retain reference/correlation or listening evidence where those are claimed.

REGISTER, MWI, dialog-info and RFC4733 capture helpers validate transaction or
subscription identity and reject wrong accounts, stale versions and duplicate
terminal digits. They are capture oracles, not SIP clients. LDAP still requires
an actual successful query generation, atomic Native rows, preserved manual
entries, stable BLF identity and real UI captures. Do not fabricate a Native
diagnostic/API marker when the identified firmware does not expose an observer.

`run_owned_case()` aggregates the existing actor's cleanup and authoritative
quiescence snapshot. Action failure stays failed even after successful cleanup;
cleanup failure preserves the primary error. Cancellation still runs cleanup.
An existing non-idle baseline is never modified. Public idle alone is insufficient.

## Dashboard mode and later execution

`tools/qualify_dashboard_session.py` provides `DashboardSession` to the existing
real browser driver. The outer driver owns browser/authentication, fixture views,
SIP peers, capture writer and the LAB wrapper. Supply actual owner/peer state,
the authoritative runtime cleanup barrier, `stage_peer`, measured media and
`cleanup_peer` callbacks. Ordinary DOM button clicks drive the shipped Dashboard.
Cleanup can hang up only its observed Call-ID; an unknown/foreign dialog is
refused while the existing peer owner still receives its cleanup callback.

Before loading the page, verify served Card, engine, Dashboard/model/view hashes
against the candidate. Use the actual maintained `BrowserAudioCapture`, which
records negotiated PCM; `validate_browser_pcm()` reads and decodes both streams.
Its required `request_ids` set must come from CDP socket URLs observed for this
Call-ID. Samples from an earlier call cannot satisfy the current call's window.
When a registrar rewrites Contact, pass both the expected request Contact and
the explicitly observed accepted Contact to the registration capture oracle.
Do not use Q's incompatible `capture_pcm=False` wrapper or replace getUserMedia,
play, render or capability discovery to make a test green. The passive snapshot
reads published store data and DOM only. Its host test loads the actual shipped
Dashboard model and rejects observer side effects. This function test is not
real Lovelace qualification.

LAB execution later uses the existing `scripts/with_ha_lab_candidate.sh`, exact
unit `home-assistant-voip-lab.service`, loopback18123/SIP15060 and existing private
peer configuration. The wrapper restores service state, symlink and package.
The preserved real Card/HA/baresip driver is a reference, not current evidence
for this candidate or for Dashboard. Never run the retired service shorthand.
No LAB start, Lovelace modification or call occurs during harness preparation.

Keep one call-scoped PCAP per generation, capture PID/readiness/stop status,
capinfos output, SDP, both decoded WAV directions, Native/HA snapshots and fresh
screenshots. Hash inputs inside the existing evidence root and bind
plan/head/candidate/run/Call-ID. Require both leg cleanup, owner/relay/socket/RTP
reservation release and media EOF before redial. Root's G0 validates the actual
measured artifacts. No `status: passed` string or packet counter replaces them.
