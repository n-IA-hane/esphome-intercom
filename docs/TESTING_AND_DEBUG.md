# Testing and debug

Regression tests cover reported compatibility failures. Include the relevant
protocol and device scenarios when validating a change.

This project has enough call paths that manual spot checks are not enough.
Use this page as the standard regression checklist before release-level
changes.

## Automated local tests

Run the regular suite through the project runner:

```bash
cd <checkout>/esphome-intercom
./scripts/test_suite.sh software-full
```

The repository `.venv` intentionally does not install Home Assistant. Run the
HA integration tests with `./scripts/test_suite.sh ha`; that mode selects
`../ha-voip-lab/.venv/bin/python`, or the interpreter supplied through
`HA_PYTHON`. Do not invoke HA test modules with the repository `.venv`.

CI checks Python with Ruff. The complete local gate also parses every shipped
JavaScript module:

```bash
./.venv/bin/ruff check custom_components scripts tests
find custom_components/voip_stack/frontend -name '*.js' -print0 \
  | xargs -0 -n1 node --check
```

### Repeat a coordinated CI source set

The software CI job uploads `coordinated-sources.json`, containing the actual
backend commit and the four component commits checked out for that run.
Default runs continue following the coordinated `dev`/`main` branches. To repeat
an exact run, select its backend revision and supply the complete artifact JSON
as the manual workflow's `component_manifest` input.

For an isolated local checkout, use an empty component parent directory:

```bash
python scripts/coordinated_sources.py --root /path/to/component-parent \
  --manifest coordinated-sources.json --output replayed-sources.json
```

The script rejects partial manifests, mutable commit references, mismatched
backend revisions and existing component directories. It never resets a local
checkout. This checkout manifest complements the local `candidate-lock.json`,
which also records toolchain and dependency identities.

The ESP component's `contract` job uploads `native.xml`. Its permanent REFER
socket regressions exercise production response dispatch and retired-transaction
replay with real loopback UDP/TCP sockets. These tests substitute scheduling and
hardware media, so their results do not qualify physical audio or video. A full
board build and an explicitly authorized hardware session are separate gates.

## Candidate qualification

Live device qualification is a separate, explicitly started operation. CI runs
software, HA-runtime and documentation checks on pushes and pull requests, but
does not call your physical phones or flash devices. The local qualification
tools resolve the current commits of `esphome-intercom`,
`esphome-voip-stack`, `esphome-audio-stack` and
`esphome-runtime-controller`, record them in `candidate-lock.json`, and use
local component checkouts for candidate firmware builds.

The complete plan includes `software-full`, the real Home Assistant runtime,
coverage, mutation, isolated peers, a real browser, firmware compilation and
the selected S3/P4 hardware jobs. Keep published references in maintained YAMLs
unchanged and create a disposable local-source variant for candidate builds.
Test operators must record the resolved commits with the resulting evidence:
a green CI run is not a physical-device qualification.

The browser startup regression delays worklet downloads in real Chromium and
checks the first rendered samples after preparation, using silent virtual
devices. A separate SIP/UDP test checks that initial PCMU frames survive an
80 ms browser-readiness handshake. These checks cover frontend preparation and
the RTP/WebSocket boundary; they do not qualify a physical phone's audio route
or promise lossless playback after an arbitrarily long browser suspension.

This software and compile gate still does not claim physical interoperability.
A release candidate also needs the applicable live browser, SIP peer and ESP
matrix below, with both peers observed and call-scoped resources returned to
their pre-test baseline.

Some tests deliberately lock down source-level routing contracts; they catch
accidental branch removal but do not prove a real SIP transaction, browser
codec or RTP path. Release evidence must include the real matrix below.

The peer-live runner also changes and restores DND and auto-answer on one real
HA browser phone. Select that phone explicitly, either as argument 2 or through
the self-hosted runner variable:

```bash
HA_PYTHON=/path/to/ha/.venv/bin/python \
  ./scripts/run_peer_live_qualification.sh evidence/results <endpoint_id>

VOIP_QUALIFICATION_POLICY_ENDPOINT_ID=<endpoint_id> \
HA_PYTHON=/path/to/ha/.venv/bin/python \
  ./scripts/run_peer_live_qualification.sh evidence/results
```

The command fails before testing when the selection is absent. It never assumes
the historical `default` endpoint.

The same peer-live command also executes the two explicit-extension precedence
cases from `tools/inbound_routing_qualification.py`. Their separate JSON
artifact must contain both exact scenario IDs. A generic green SIP matrix or
only one extension case cannot satisfy the DTMF routing contract.

Important groups:

- `tests/test_voip_backend_route_contract.py`: static contracts for SIP route
  branches and service registration.
- `tests/test_sip_*.py`, `tests/test_sdp_pcm_profile.py`,
  `tests/test_rtp_profile.py`, `tests/test_roster_resolver.py` and
  `tests/test_router_contract.py`: resolver, registrar, RTP relay and protocol
  behavior.
- `tests/test_group_call_matrix.py`: PBX-style ring/conference group matrix.
- `tests/test_conference.py`: conference mixer and lifecycle primitives.

For an explicitly authorized live deployment, use the repository helper
instead of copying the component by hand:

```bash
tools/deploy_ha_voip_stack.sh
```

It stages the exact local component, creates a timestamped remote backup,
installs through the configured SSH alias, restarts Home Assistant and waits
for a terminal service state. Environment overrides are documented by
`tools/deploy_ha_voip_stack.sh --help`.

## HACS release archive

For local inspection, build the release asset from the repository root:

```bash
./.venv/bin/python scripts/build_hacs_zip.py
sha256sum voip_stack.zip
```

The output is a flat, reproducible archive whose integration files, including
`manifest.json`, are at the ZIP root. The builder rejects hidden files,
symlinks, debug captures and unknown file types, and adds the repository's MIT
`LICENSE`. Run `tests/test_hacs_contract.py` before publishing it.

When `hacs.json` has `zip_release: true`, every HACS-visible release must expose
an asset named exactly `voip_stack.zip`. Publish or backfill that flat asset
before making the setting visible on the default branch; a differently named
or nested ZIP is not a HACS release asset.

The CI workflow tests software, HA behavior and the documentation. Publishing
a release asset remains a separate operation. After the applicable checks,
build and validate the archive, then upload that exact file explicitly:

```bash
./.venv/bin/python scripts/build_hacs_zip.py
./.venv/bin/python -m zipfile -t voip_stack.zip
sha256sum voip_stack.zip
gh release upload <tag> voip_stack.zip
```

For a manual installation of this same flat archive, extract it into the
integration directory rather than directly into `/config`:

```bash
mkdir -p /config/custom_components/voip_stack
unzip -o voip_stack.zip -d /config/custom_components/voip_stack
```

After extraction, `/config/custom_components/voip_stack/manifest.json` must
exist. Restart Home Assistant and hard-refresh dashboards containing the card.

## Real SIP matrix

The development environment can run local SIP endpoints against the real HA
instance. The useful matrix is:

- create multiple SIP endpoint accounts;
- register them to HA over SIP/TCP;
- call by name;
- call by extension;
- call HA by name;
- call HA by extension;
- change HA extension and verify immediate phonebook/dial-plan update;
- verify registered endpoint calls do not fall into `route_requested`.

Expected route evidence:

- endpoint-to-endpoint:
  `SIP TX INVITE <callee>@<registered-contact-host>:<port>`;
- HA target:
  `HA softphone state=ringing`;
- no `SIP route requested` for registered endpoint calls to normal roster
  targets.

## SIP video matrix

Enable video only on the HA softphone and use a standard SIP peer. Cover at
least:

- incoming audio-only call after video has been enabled;
- outgoing audio-only call after video has been enabled;
- incoming H.264 `sendrecv` with non-black browser canvas and outbound camera
  access units;
- direct H.264, VP8 and JPEG receive;
- H.264 and VP8 browser camera transmit;
- optional H.263, H.263-1998 and H.265 receive through FFmpeg;
- bidirectional H.264-to-JPEG and JPEG-to-H.264 fallback between two HA-owned
  SIP legs, with direct relay retained for any already compatible direction;
- outgoing video, including dashboard reloads during ringing and after media
  has connected;
- remote `sendonly`, proving receive-only video does not request a camera;
- remote `recvonly`, proving receive failure cannot stop camera transmit;
- incompatible video rejected with `m=video 0` while compatible audio remains;
- camera permission denied while incoming video and browser audio remain live;
- RTP/AVP compatibility plus RTP/AVPF compound RR/SDES and negotiated PLI/FIR
  recovery after attach;
- exact-codec RTP and RTCP relay between two standard SIP legs;
- trunk DTMF selection into a video-capable logical HA phone, including a
  missing-extension rejection with no retained session or RTP reservation;
- per-phone state isolation: the selected phone reaches `in_call`, unrelated
  phones remain idle and every involved phone returns to idle after teardown;
- compact 6-column, default, wide and tall card geometry with long caller text;
- clean local and remote hangup with the video RTP port and browser owner
  released;
- caller CANCEL while ringing and repeated mixed-codec calls with zero active
  sessions, dialogs, media owners, transcoders and cleanup tasks afterwards.

With backend debug enabled, record
`media_debug.runtime_resources.resource_counts` before, during and after the
call. The final snapshot must set `call_scoped_quiescent: true` and report zero
sessions, legs, pending routes/invites, media owners, active audio/video
sessions and allocated RTP ports. Compare long-lived listener/trunk task counts
to the idle baseline rather than assuming they should disappear.

The Playwright probe records runtime evidence rather than relying on a source
string check:

```bash
mkdir -p test_captures
export HA_URL="https://home-assistant.example/dashboard/voip"
export PLAYWRIGHT_STORAGE_STATE="$HOME/.cache/ha-playwright-state.json"
./.venv/bin/python tools/sip_video_browser_probe.py \
  --out test_captures/voip-video-result.json
```

Wait for `READY_FOR_VIDEO_CALL`, then start a deterministic audio/video caller:

```bash
./.venv/bin/python tools/sip_video_peer.py \
  --host home-assistant.example \
  --port 5060 \
  --target HA \
  --codec vp8 \
  --direction sendrecv \
  --out test_captures/voip-video-peer.json
```

See [SIP Video](SIP_VIDEO.md) for an outgoing probe,
the current codec profile and deliberate limitations.

### P4 to trunk media gate with PCAP

For changes to RTP framing, pacing, codec conversion, trunk bridging or
audio-to-video re-INVITE, run the physical P4 against a real Wildix endpoint
and collect the packet capture from the HA host:

```bash
out="test_captures/p4-wildix-$(date -u +%Y%m%dT%H%M%SZ)"
./scripts/run_p4_wildix_pcap_gate.sh "$out" --audio-only

out="test_captures/p4-wildix-video-$(date -u +%Y%m%dT%H%M%SZ)"
./scripts/run_p4_wildix_pcap_gate.sh "$out"
```

The first command qualifies bidirectional audio without renegotiation. The
second starts with audio and adds bidirectional video through re-INVITE. The
gate fails unless the call runner succeeds, the capture is readable, at least
four RTP streams are observed and every audio stream has zero sequence loss.
Audio streams must also remain within 25 percent of the RTP cadence declared
by their SDP clock and timestamp progression. Video sequence gaps are reported
but are not a default failure across direction-changing re-INVITEs, because RTP
produced while a destination leg is inactive is intentionally not forwarded.
Use `tools/rtp_pcap_evidence.py --max-video-loss N` for a call interval whose
video direction is continuously active. The gate stores
`summary.json`, `ha-full.pcap`, `capinfos.txt`,
`tcpdump.log` and `rtp-evidence.json` under the selected directory.

PCAP is required for tests whose oracle is SIP transaction order, RTP
direction, sequence continuity, timestamp cadence or packet loss. It is not a
substitute for browser rendering counters, decoded media, physical audio or
post-call resource quiescence, which remain separate required oracles.

## Service matrix

Exercise all public services with temporary data:

- schema list contains every expected service;
- `set_dnd`;
- `set_ha_softphone_settings`;
- `create_account`, `disable_account`, `enable_account`,
  `rotate_account_password`, `list_accounts`,
  `remove_account`;
- `add_contact`, `remove_contact`, `set_contacts`, `clear_contacts`,
  `export_phonebook`, `push_phonebook`;
- `call` and `forward` to a registered endpoint;
- `answer`, `decline`, `hangup` against pending SIP calls;
- `select_inbound_destination` against an initial `route_requested`
  occurrence;
- `route` against an advanced forced route request;
- `set_deadline` and `cancel_deadline` with current and stale call revisions;
- `purge_devices`, verifying that HA rejects it without removing any Device.

Always restore:

- HA softphone extension and group settings;
- DND off;
- manual phonebook contacts;
- temporary SIP accounts removed;
- no pending HA softphone call.

## Home Assistant logs

To include the integration's DEBUG messages in Home Assistant logs, add this
top-level block to `configuration.yaml` and restart Home Assistant:

```yaml
logger:
  default: info
  logs:
    custom_components.voip_stack: debug
```

The **Debug mode** option enables detailed SIP/RTP diagnostics; it does not
change Home Assistant's logger level and it does not record conversations.
**Media capture** is a separate explicit option. Disable both after collecting
the required evidence because logs contain call metadata and captures contain
conversation audio.

Useful filters:

```bash
journalctl -u home-assistant.service --since "10 minutes ago" --no-pager |
  grep -E "SIP RX INVITE|SIP TX INVITE|SIP TX 180|SIP route requested|SIP bridge registered|HA softphone state|registered user"
```

Run that command on a host using the documented systemd service layout. On
Home Assistant OS, containers or other installations, use **Settings → System
→ Logs** or the installation's supported log command instead of assuming a
host name or log-file path.

Look for:

- `SIP bridge registered` after outbound bridge setup;
- `SIP TX 180 Ringing` immediately after `answer_ha` pending calls;
- `SIP TX INVITE <target>@<contact>` for registered endpoint routes;
- `SIP route requested` only for explicit automation fallback scenarios.

## Phonebook inspection

```bash
export HA_URL="https://home-assistant.example"
read -rsp "Home Assistant long-lived access token: " HA_TOKEN; echo
curl -fsS -H "Authorization: Bearer $HA_TOKEN" \
  "$HA_URL/api/states/sensor.voip_phonebook" |
  jq -r '.attributes.roster_json' | jq '.contacts[] | {id,name,extension,sip_uri,metadata}'
unset HA_TOKEN
```

Do not commit tokens, private host names, IP addresses or secret-file paths to
the repository.

Use this after every group/extension/account change. The phonebook is the
source of truth for dialing.

## Runtime snapshots

ESP devices expose useful SIP snapshots as sensors:

- `sensor.<device>_voip_state`
- `sensor.<device>_voip_endpoint`
- `sensor.<device>_voip_sip_snapshot`
- `sensor.<device>_voip_last_reason`
- `text.<device>_voip_ring_groups`
- `text.<device>_voip_conference_groups`
- `switch.<device>_voip_conference_ring`

For HA-side runtime, inspect call events, softphone state events and
`sensor.voip_phonebook`.

## Audio debug

When private media capture is explicitly enabled, HA writes WAV captures under:

```text
~/.cache/voip_stack_debug/
```

The filenames include source/destination call IDs and side labels. Use these
when a call connects but audio direction, volume or format negotiation is
unclear.

`debug_mode` controls detailed logs and metrics only. The separate
`media_capture` option records up to 15
seconds in each direction for a Home Assistant softphone WebSocket session and
up to 8 seconds for each leg of an RTP relay. The directory is created with
mode `0700`, names are sanitized, and pruning keeps at most the newest 24 files
and 64 MiB in total. Related WAV/JSON files are published and retained as one
capture group, so a failed writer or retention pass never leaves half of a
session. At most four capture groups may be waiting for disk I/O across all
calls; further snapshots are dropped instead of growing an unbounded executor
queue. Inspect `debug_capture_pending_writes` and
`debug_capture_dropped_writes` in the debug snapshot when evidence is missing.
WAV/JSON data can still contain private conversation audio and call metadata:
disable media capture after the test and remove retained artifacts according to
the deployment's privacy policy.

## Serial and device debug

For ESP debug:

- serial logs show component setup, SIP state, audio stack state and reset
  causes;
- JTAG snapshots are useful when a device is responsive enough to expose
  runtime state but audio or FSM state is inconsistent;
- keep volumes low for automated tests, but do not set them to zero when
  validating real audio paths.

For AFE or I2S lifecycle work, keep the production hot path event-driven:

- Espressif's AFE manager owns the feed/fetch workers;
- the wrapper read callback supplies queued microphone/reference PCM;
- the result callback delivers processed PCM through the bounded output stream;
- stop or reconfigure retires work through the current session owner;
- I2S reads/writes block on DMA/backpressure;
- finite timeouts are reserved for lifecycle and fault bounds, not periodic
  checks for possible audio work.

Waiting for input or output capacity can be normal. Correlate task state with
PCM progress and queue ownership before identifying a bottleneck.

This follows Espressif's documented
[ESP-SR feed/fetch model](https://docs.espressif.com/projects/esp-sr/en/latest/esp32p4/audio_front_end/README.html),
[GMF AI Audio 1.1.1 manager](https://components.espressif.com/components/espressif/gmf_ai_audio/versions/1.1.1/readme)
and [blocking I2S API](https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/peripherals/i2s.html).
Search for `vTaskDelay`, short timed semaphore waits and periodic wake loops
before accepting an audio-path change.

Physical ESPHome speakers should have a bounded `timeout` when a mixer is the
upstream producer. A mixer can drain and stop its source tasks without an
immediate final stop reaching the hardware speaker; an unbounded physical
speaker then keeps the I2S TX side and full-duplex AFE reference alive on
silence. The timeout is ESPHome's documented bus-release contract, not an
audio scheduler. Verify after stopping media that the audio stack returns from
duplex to microphone-only and that subsequent telemetry windows report no
silent speaker-underrun fills.

Use targeted runtime snapshots for counters and JTAG/GDB snapshots for task
state, core ownership and stack margin. JTAG halts disturb real-time audio, so
never treat a JTAG capture as an audio-quality test. Full SystemView/AppTrace
instrumentation must be memory-qualified first; if it consumes enough internal
or DMA RAM to cause AFE, SPI or I2S failures, those failures are instrumentation
artifacts until reproduced on the production firmware.

The checked-in capture and summary tools keep this workflow reproducible:

```bash
./.venv/bin/python tools/jtag_snapshots.py --help
./.venv/bin/python tools/analyze_jtag_snapshots.py --help
```

### Render the on-device options page

Use the actual expanded configuration with ESPHome's official host snapshot
display. The renderer imports the production page, fonts, theme and settings
callbacks. It replaces hardware data owners with host fixtures, without an
audio or SIP stack:

```bash
esphome config device.yaml > device-expanded.yaml
./.venv/bin/python tools/render_lvgl.py \
  --input device-expanded.yaml --page settings_continuous_page \
  --width 240 --height 240 \
  --output test_captures/options-preview \
  --build-parent /tmp/options-preview-build
```

Use the complete config dump here, without `--no-defaults`, so native generated
entities are available to the host fixtures. Do not use `--show-secrets`.
Box V2 uses 360 by 360 pixels; P4 uses `settings_p4_page` and the selected
orientation's dimensions. `--prepare-only` writes the host configuration
without compiling or running it.

Inspect the top, middle and end snapshots, including the physical circular
boundary on round displays. Check the centered Options title, wrapped labels,
values and units, optional controls and the final VoIP section. A host
snapshot proves the rendered layout, not panel operation, touch propagation,
speaker audio or DSP behavior. Gesture tests need real LVGL pointer input;
calling a gesture handler directly does not exercise hit testing or bubbling.

For round profiles with VoIP, add `--pointer` to exercise the production
startup page registry, page selector and LVGL pointer input. Check the bounded
Assist -> Contacts -> Dialer -> Options sequence: UP advances, DOWN goes back,
and neither end wraps. Inside Options, test navigation from the fixed header
or exposed area outside the scrolling body, and verify that dragging inside
the body scrolls without leaving the page. During a call, a recognized swipe
must not become a hangup, while a normal hangup tap must still work. These
host checks do not replace validation of the physical touchscreen.

Options has no Back button. P4 enters Options with an upward swipe and leaves
with a downward swipe from the fixed header or another navigation area.

Settings must update from their existing owners' notifications and refresh on
page entry. Verify that opening or refreshing the page causes no writes to
the controls, that a rejected command restores the actual value, and that a
dragged slider is not overwritten by a concurrent state notification. Changes
to playback metadata alone must not redraw an unchanged volume control.

### ESP32-S3 profiling without application probes

`tools/esp_profile.py` collects DEBUGPC samples and whole-core PMU counters
through stock OpenOCD `irscan`/`drscan` commands, following the ESP32-S3 JTAG
NAR protocol in the qualified Espressif release `v0.12.0-esp32-20260831`.
The ESP32-S3 target does not expose the generic `xtensa dm` command, whose
APB register addresses also differ from NAR register numbers. It never invokes
the `profile` command, which can fall back to halting the target. It also
rejects arbitrary Tcl commands, halted cores, zero PCs, PMU ownership conflicts
and overflowing counters. Only one collector can own a local endpoint.
During raw scans it temporarily suspends OpenOCD's background target polling
and verifies its restoration on exit. This controls the debugger only;
the ESP keeps executing, with its running state checked through DSR.

Start with `doctor`, which checks local files without contacting hardware:

```bash
./.venv/bin/python tools/esp_profile.py doctor \
  --elf firmware.elf --manifest build-identity.json --openocd /path/to/openocd
```

The build manifest contains `elf_sha256`, `firmware_sha256`, `device_mac`,
`profile`, `scenario_contract` and `source_revisions`. Each source revision
records its full `head`, a `tree_sha256` of the component contents and its
`dirty` state. `running_identity` binds the ELF hash to an independently
verified upload/device identity record through `evidence` and
`evidence_sha256`. A filename or the newest ELF in a build directory is not
proof that it matches the running firmware.

Keep the original waveform, volumes, gains, DSP mode, enabled models and UI
load in `scenario_contract`; these must match when comparing revisions.
Use a fresh output directory for each independent measurement:

```bash
./.venv/bin/python tools/esp_profile.py record --mode pc \
  --elf firmware.elf --manifest build-identity.json --scenario music-48k \
  --seconds 60 --hz 100 --output test_captures/profile/music-48k/run-1
./.venv/bin/python tools/esp_profile.py analyze \
  test_captures/profile/music-48k/run-1 --elf firmware.elf \
  --addr2line /path/to/xtensa-esp32s3-elf-addr2line
```

The two cores are sampled sequentially with host timestamps and slightly
varying intervals. Results are **shares of observed PC samples**, including
unresolved addresses, not exact task CPU percentages or per-function latency.
PMU mode uses windows of at most five seconds; select `--event instructions`,
`data_stalls`, `instruction_stalls` or `waiti`. It restores the previously
unowned counters and requires `apptrace_enabled: false` in the manifest.
These aggregate stalls are not flash/PSRAM cache-miss counts.

`compare` accepts lists of baseline/candidate `analysis.json` files. It
requires matching acquisition/workload contracts and unique capture identities.
At least five independent windows per revision are needed for its run-based
bootstrap interval. Repeating the same file does not create new measurements.

SystemView is a separate diagnostic-build mode, using official FreeRTOS hooks
with nonzero overhead. It requires `apptrace_enabled: true`,
`systemview_enabled: true`, `--mode sysview --seconds 10`, `--idf-root`,
`--idf-python`, the actual `--toolchain` prefix and `--openocd-log` pointing
to the live endpoint's regular logfile at INFO verbosity or above. Host-only
`echo` markers verify that this file belongs to the endpoint and delimit the
active interval after native START and before native STOP. The collector saves
the appended log with its hash, inode and original offset; missing markers,
rotation, truncation, CRC retries or recovery invalidate the capture. A halt,
resume or reset inside the active interval also invalidates it. Native
attach/detach halts outside those markers do not alone reject the capture.
The primary acquisition error is preserved if STOP or log qualification also
fails. Raw per-core files and
parser errors are retained. `analyze` excludes attach/detach edges, rejects
overflow or a nonzero parser exit, and requires the hash-bound accepted log.
Older captures without this evidence cannot qualify timing retroactively.
On ESP32-S3 with ESP-IDF 5.5.6, the official AppTrace implementation documents
trace-memory corruption under concurrent writes. OpenOCD can recover a CRC
failure by halting and resuming the CPU even when incomplete-block and lost-byte
counts remain zero. A successful parser and zero transport loss therefore do
not establish uninterrupted execution. These checks reject that observer
failure; they do not remove tracing overhead or qualify this mode on hardware.
See the pinned [OpenOCD CRC recovery](https://github.com/espressif/openocd-esp32/blob/b0edad625b5edc0a64d52a641a7bd02e3a56c37d/src/target/espressif/esp_xtensa_apptrace.c#L231)
and [ESP-IDF 5.5.6 S3 trace-memory workaround](https://github.com/espressif/esp-idf/blob/v5.5.6/components/app_trace/app_trace_membufs_proto.c#L153).
Acquisition also requires zero incomplete
transport blocks and lost bytes, and a verified final STOPPED state. Use the
official shared ESP_TIMER timestamp source for dual-core captures; independent
CPU clocks are not synchronized by the parser. It reports ready-to-run waits separately
from scheduled residency. Residency includes untraced interrupts; it is not
exclusive task CPU time. Qualify trace overhead and RAM usage before relying
on a diagnostic build. PMU and AppTrace must not own the counters concurrently.

SystemView admission also requires three independently collected, hash-bound
JSON receipts. Add `boot_confirmation`, `post_prefit_evidence` and
`runtime_preflight_evidence` to the manifest, each containing `evidence` and
`evidence_sha256`. The collector reads these files; it does not upload firmware,
connect to the ESPHome API or perform a memory-mapping halt for you.

The boot receipt records the uploaded ELF/binary hashes, target MAC, build,
the actual `Boot seems successful; resetting boot loop counter` log with its
host timestamp, and a matching API identity obtained after that message.
The post-preflight receipt records another matching API identity and a fresh
requested audio-diagnostic `uptime_ms`. Do not estimate boot time from the
ordinary Uptime sensor replayed on an API connection: its value can be cached.
The runtime receipt records `verified_at`, `elf_sha256` and the independently
read `live_app_elf_sha256`, together with the task/queue mapping used for analysis.

Boot-time estimates tolerate five seconds for deferred diagnostic reporting.
A detected reboot is allowed only when the matching live app identity and
runtime pointers were observed after that new boot. Otherwise discard the old
runtime mapping and obtain new evidence before recording. A successful boot
message precedes ESPHome's unchecked IDF mark-valid return; it is not a direct
partition-state read. The expected live app surviving a subsequent reboot is
separate evidence of persistence. PC/PMU captures retain their existing identity
contract; historical captures are not rewritten by this admission gate.

Do not reuse hardware instruction TRAX on a SystemView image merely because
its recorder is STOPPED. The compiled RTOS hooks still poll AppTrace's down
channel, including shared TRAX protocol registers, when recording is disabled.
Restoring the memory mux alone does not restore that protocol state. Use a
separate, cold-booted TRAX-only diagnostic image with AppTrace/SystemView and
their hooks absent. On ESP32-S3, enable the official `ESP32S3_TRAX` and
`ESP32S3_TRAX_TWOBANKS` options, then verify the resulting 32 KiB memory
reservation before use. These observer modes must have separate firmware and
resource qualification; a failed mixed-observer run is not a production AFE
regression by itself.

Complete source audits and local regression gates before uploading a profiling
candidate. Collect dumps/PCM separately from the main measurement windows.
Keep captures private under `test_captures/`; profiling does not replace
independent continuity, acoustic or wake-word hit-rate tests.

On ESPHome 2026.7 and newer native ESP-IDF builds, custom partition tables use
`esp32.partitions`; `board_build.partitions` under `platformio_options` is not
the authoritative native-build setting. A partition-layout change on an
installed device is a separate migration from a normal application OTA and
must be planned and validated explicitly.

When testing real devices, cover:

- HA to ESP;
- ESP to HA;
- ESP to ESP;
- registered SIP endpoint to ESP;
- registered SIP endpoint to HA;
- HA/card to registered SIP endpoint;
- unknown, unregistered SIP endpoint to HA;
- unknown, unregistered SIP endpoint to each ESP;
- ESP in-dialog hold/re-INVITE receives `488` while the established call and
  later BYE remain functional;
- HA-owned UPDATE/re-INVITE hold/resume and supported audio changes commit once;
  direct browser dialogs cover compatible video add/remove;
- an audio-only SIP bridge can add video through a paired destination
  re-INVITE, including codec conversion, source rejection, BYE during setup and
  immediate redial after transcoder cleanup;
- ring group caller cancel before answer;
- ring group first-answer-wins;
- conference join/leave;
- group membership changes reflected in the phonebook.


## Independent SIP peer for PBX development tests

The PBX branch tests transfer, voicemail/MWI, parking, pickup and recording with
baresip 4.6.0. Baresip is a test peer, not a VoIP Stack dependency. The CI build
uses pinned source revisions and only the required audio/signaling modules.

With CMake, a C compiler, OpenSSL, zlib and libsndfile development files installed:

```sh
scripts/build_sip_test_peer.sh "$PWD/test_captures/sip-peer"
PATH="$PWD/test_captures/sip-peer/bin:$PATH" \
VOIP_TEST_BARESIP_MODULE_PATH="$PWD/test_captures/sip-peer/lib/baresip/modules" \
HA_PYTHON=/path/to/ha-test-venv/bin/python scripts/test_suite.sh ha --keep-going
```

Use your actual HA test interpreter in the last line. The peer is installed only
in the supplied directory; system packages and running PBXs are not replaced.
The override also selects its matching modules. Real peer tests must not be
reported as hardware validation for a physical phone or ESP.


### SIP consolidation regressions

The focused protocol tests exercise REFER correlation, independent event dialog
lifetime, authenticated retries, parsing, request saturation and subscription
routing. The saturation test occupies 24 real request handlers, then exchanges
final responses, PRACK, NOTIFY, BYE and CANCEL over UDP and TCP. The routing test
uses a separate TCP listener for the Contact or proxy destination.

```bash
.venv/bin/python -m pytest -q tests/test_refer_correlation.py \
  tests/test_sip_refer_parser.py tests/test_sip_control_load.py \
  tests/test_sip_subscription_routing.py tests/test_sip_replaces.py
```

The Home Assistant transfer suite starts three independent baresip processes.
It covers audio transfers, video added or removed during replacement, transfer
while a consultation is being put on hold, and compensation after an injected
handoff failure. Use the Home Assistant test environment for this suite:

```bash
HA_PYTHON=/path/to/ha-test-environment/bin/python
"$HA_PYTHON" -m pytest -q tests/test_sip_phone_transfers_ha.py
```

A short repeated-call test runs by default. For a one-hour, 100-cycle run on the
same backend and registered peers:

```bash
mkdir -p test_captures
VOIP_CONSOLIDATION_SOAK_CYCLES=100 \
VOIP_CONSOLIDATION_SOAK_SECONDS=3600 \
VOIP_CONSOLIDATION_SOAK_REPORT="$PWD/test_captures/sip-soak.jsonl" \
"$HA_PYTHON" -m pytest -q tests/test_sip_phone_transfers_ha.py \
  -k long_lived_transfer_runtime --timeout=3720
```

Each cycle establishes a call, starts a consultation, performs an attended
transfer, observes bidirectional RTP and hangs up. It requires empty call and
RTP reservation registries before the next cycle. The JSONL report records
application subscription ownership, notification tasks, cached responses, open
file descriptors and process task counts. Pin and record the component tree
before starting; do not edit it during the run. A short run is not evidence of
one-hour stability, and RTP packet counts alone do not certify decoded audio or
video presentation.

For an independent Asterisk/PJSIP transfer check, install Asterisk, baresip and
FFmpeg, then run:

```bash
.venv/bin/python scripts/run_asterisk_transfer_lab.py \
  --output test_captures/asterisk-transfer
```

The output directory must be new. The runner starts a private Asterisk process
with loopback SIP transports and two baresip phones. It calls through Asterisk,
checks a received PCMU tone, transfers the call and checks the independently
decoded audio at both phones. It stops its processes before writing a successful
`result.json`. Asterisk's installed documentation data must be readable; use
`--asterisk-data` to select an accessible data directory if required by the
package's permissions. It does not modify the installed PBX configuration or
contact an external provider. This check validates the SIP client and transfer
path; the separate HA suite validates services, session ownership and UI-facing
state.


Use `--mode attended` to exercise consultation followed by Replaces, and
`--authenticate` to require Digest credentials generated for that run. The
password is removed from the saved PBX configuration after shutdown. With
`--transport tcp`, the HA client leg uses TCP while Asterisk routes the handset
legs over UDP. The lab leaves endpoint transport selection to the URI, following
[Asterisk's transport selection rules](https://docs.asterisk.org/Configuration/Channel-Drivers/SIP/Configuring-res_pjsip/PJSIP-Transport-Selection/),
so an endpoint setting cannot force TCP dialog notifications onto a UDP transport.
For example:

```bash
.venv/bin/python scripts/run_asterisk_transfer_lab.py \
  --mode attended --authenticate --transport tcp \
  --output test_captures/asterisk-attended-tcp
```
