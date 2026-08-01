# Testing and debug

This project has enough call paths that manual spot checks are not enough.
Use this page as the standard regression checklist before release-level
changes.

## Automated local tests

Run the HA integration suite:

```bash
cd <checkout>/esphome-intercom
./.venv/bin/python -m pytest tests -q
```

The complete local gate also runs Ruff and parses every shipped JavaScript
module:

```bash
./.venv/bin/python -m ruff check custom_components scripts tests
find custom_components/voip_stack/frontend -name '*.js' -print0 \
  | xargs -0 -n1 node --check
```

Some tests deliberately lock down source-level routing contracts; they catch
accidental branch removal but do not prove a real SIP transaction, browser
codec or RTP path. Release evidence must include the real matrix below.

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

Build the release asset from the repository root:

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

`.github/workflows/release.yml` builds and uploads the archive when a GitHub
release is published. It can also target an existing tag through a manual
dispatch. Manual dispatch preserves an existing asset by default; enable its
explicit replacement input only after the locally built ZIP has passed the
contract test, because GitHub CLI replacement deletes the old asset before
uploading the new one.

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
- `purge_devices` with a high `min_unavailable_hours` as a no-op.

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

The **Debug mode** option in the VoIP Stack config flow enables optional
SIP/RTP diagnostics and media captures; it does not by itself change Home
Assistant's logger level. Disable both forms of debug after collecting the
trace, because logs and captures can contain call metadata or conversation
audio.

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

When RTP relay debug is enabled, HA writes WAV captures under:

```text
~/.cache/voip_stack_debug/
```

The filenames include source/destination call IDs and side labels. Use these
when a call connects but audio direction, volume or format negotiation is
unclear.

Captures are opt-in through `debug_mode`. HA automatically records up to 15
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
disable debug mode after the test and remove retained artifacts according to
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

- the ESP-SR feed task supplies complete input/reference frames;
- the fetch task blocks on the feed semaphore and then calls ESP-SR fetch;
- stop or reconfigure explicitly wakes the blocked task;
- I2S reads/writes block on DMA/backpressure;
- finite timeouts are reserved for lifecycle and fault bounds, not periodic
  checks for possible audio work.

This follows Espressif's documented
[ESP-SR feed/fetch model](https://docs.espressif.com/projects/esp-sr/en/latest/esp32p4/audio_front_end/README.html),
[ESP-ADF event/ring-buffer pipeline](https://docs.espressif.com/projects/esp-adf/en/latest/api-reference/framework/audio_pipeline.html)
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
  direct browser dialogs cover compatible video add/remove, while bridge
  topology/codec changes are rejected without disturbing existing media;
- ring group caller cancel before answer;
- ring group first-answer-wins;
- conference join/leave;
- group membership changes reflected in the phonebook.
