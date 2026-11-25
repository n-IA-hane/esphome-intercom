# Official components and stabilization

## Consolidation before the stable release

The current consolidation starts from Intercom `bf0461ad`, VoIP ESP
`d9dc9dc5`, Audio Stack `d4a872b7`, Runtime Controller `68dd0606` and renderer
`edda01a9`. These revisions identify the starting point, not a completed
qualification. Existing account identities, credentials, device/entity IDs,
automations and public firmware settings must survive the work.

The work proceeds through baseline regressions and CI checks, SIP input and
authorization fixes, shared directory ownership and UI corrections, then
protocol/media/call-control separation and the firmware subsystems. Each
extraction preserves the current resource owner and cleanup barrier. Decoder,
SDK and driver upgrades are separate changes.

CI reports editorial checks independently from the call and browser jobs.
Release verification also reads the authenticated GitHub run and every job:
the commit and source ref must match the candidate, and all required jobs
must have completed successfully. A user-requested diagnostic installation
before that point is an unqualified candidate with a preserved rollback.

The release verifier requires `--ci-run-id` and `--ci-ref` in addition to the
package, qualification and archive arguments. Do not substitute a successful
run for an older commit or a different source channel. Passing software checks
does not replace the physical audio, display and teardown checks below.

The public firmware profiles use the stable ESPHome 2026.11.0 builder.
Qualification tooling pins its official source revision,
`1feda1bcaa861a5f2e933f2c65d599e44f4b132b`, in
`requirements-firmware-test.txt`. Use a profile's existing YAML in Device
Builder. The shared packages select the additional components it needs.

HTTP and Micro Wake Word now come directly from immutable official ESPHome
commits containing merged PR22787 and PR22781, respectively. Their local
runtime copies are removed. Voice Assistant and `speaker_source` retain their
bounded adaptations on `373224c141dceadbb67ace263040cbfc802c5968`.
Source revisions, remaining deltas and removal conditions are recorded in
[`esphome/upstream_sources.json`](../esphome/upstream_sources.json).

`qualification/sources.json` is the common component catalog. The candidate lock
records both package versions and the installer receipt containing the ESPHome
commit. Firmware manifests also retain the resolved SDK/dependency identities,
configuration and binary hashes. A version string alone is insufficient.

## Build the preview recipes

To reproduce the builder used by CI, run these commands from the repository root:

```sh
uv venv .venv-preview
uv pip install --python .venv-preview/bin/python -r requirements-firmware-test.txt
.venv-preview/bin/esphome config path/to/your-board.yaml
```

Replace the last argument with your board YAML, then use the same ESPHome
executable for `compile` and `upload`. The requirements file pins the official
ESPHome commit used to qualify these recipes.

## December release plan

Use one public YAML per board and feature profile. Validate every public
configuration with the stable 2026.11 builder. Reuse representative builds and
hardware results for the features they actually exercised. Record the exact
component revisions in the release manifest so the released build is reproducible.

When ESPHome 2026.12.0 stable ships, check the **released tag** for each
backported fix. Where the native component covers it, remove that component
from the shared package's `external_components` list and retest the same board
YAML. Removing only the Git revision would still load an external component.
Ship the verified cleanup in a patch release with its tested ESPHome minimum.
The original VoIP Stack 2026.12.0 tag will retain its backports for users
building with ESPHome 2026.11.

## Work and exit conditions

The table lists exit conditions, not a claim that every item is complete.
Coordinated publication and the remaining physical checks are in progress.
The JPEG checks below cover both the recommended SDK and an explicit forward
SDK qualification. They do not establish optical panel behaviour, acoustic
quality or H.264 readiness.

| Work | Completion condition |
| --- | --- |
| MWW | Official merge `26c2b61c405ad004e08c6472de08ed264e97ea3e`; eleven model-lifecycle cases and fixed historical negative control |
| VA | Local idle/reentrant-ordering delta remains until PR22782 or equivalent is qualified |
| HTTP | Official merge `ce3f2f4c2cb98bed6e23176832afdce40be6661b`; completed-worker and reentrant completion contracts |
| Speaker player | Local terminal-error and replacement behavior remains pending equivalent official support |
| GMF | Official manager selected, ESP-SR unchanged, creation/reconfiguration/teardown qualified |
| S3/P4 rendering | Protocol-neutral frame input; VoIP adapters owned by VoIP; no parallel renderer |
| P4 display | Declared minimal upstream-aligned MIPI owner supplies serialized transforms/readback; complete the scoped physical regressions |
| Screenshots | Complete visible composition including video, using public APIs without a private MIPI dependency |
| HA | Indexed listeners, correct Store removal, shared native form controls and persistent strategy bindings |
| Runtime/audio metadata | Native bounded queues and truthful capture metadata with explicit unknown timestamps |
| Publication | Exact qualified component graph promoted, changelogs and migration instructions current |

Temporary providers must name their official base, essential differences,
upstream proposal and removal condition in `esphome/upstream_sources.json`.
The model-reload correction was merged in
[ESPHome PR22781](https://github.com/esphome/esphome/pull/22781) on 24 November
2026 UTC. Packages select its exact official merge until the minimum supported
released builder contains the fix. No local MWW provider remains. The
noncontinuous idle correction remains proposed in
[ESPHome PR22782](https://github.com/esphome/esphome/pull/22782).
Remove the provider and external selection together after qualification of an
official revision containing every remaining correction. A published proposal
is not equivalent to its inclusion in a stable release.

The HTTP completion correction was merged on 20 November 2026 UTC in
[PR22787](https://github.com/esphome/esphome/pull/22787). Its official merge
replaces the byte-identical local HTTP runtime. The broader player
terminal-error and replacement policy is a
[follow-up contribution to PR22118](https://github.com/esphome/esphome/pull/22118#issuecomment-6914576261),
not a duplicate PR or an accepted extension of that narrower startup fix.
The H.264 dependency remains an explicit candidate fork on official base
`08a0b658`, with the default-off DB-buffer Kconfig delta; its public branch is
proposed in [Espressif PR28](https://github.com/espressif/esp-h264-component/pull/28).
Default-SDK P4 boot and duplex wire media are qualified;
the optional vendor Unity case is not presented as hardware-executed.

### Remaining exceptions and their removal gates

| Source | Status on 25 November 2026 | Removal gate |
| --- | --- | --- |
| `audio_http` | Official immutable component pin; no local runtime delta | Minimum supported released builder contains the merged fix and completion/restart tests pass |
| `micro_wake_word` | Official immutable component pin; no local runtime delta | Minimum supported released builder contains the merged fix and reload/reset contracts pass |
| `voice_assistant` | Local two-site lifecycle patch; PR22782 open | Noncontinuous IDLE and callback restart ordering match, with Speaker/MediaPlayer completion covered |
| `speaker_source` | Local terminal-error and replacement handling; contribution to open PR22118 | Current/pending ERROR cleanup, replacement ownership and repeat policy all match; startup-only PR behavior is insufficient |
| `mipi_dsi` | Local display-owner capability; PR22789 open | Official owned transform, serialized UI/video writes and composed readback, plus physical P4 qualification |
| Espressif H.264 | Pinned fork with DB-buffer placement option; PR28 open | Official equivalent preserves internal DMA budget and passes codec/SDK/hardware qualification |
| P4 SDK backports | Verified build-local official PPA/SDMMC patches | The recommended SDK includes the fixes and passes rotated display/camera workloads |
| GMF AFE | Official immutable dependency, no local vendor delta | Dependency upgrade is a separate DSP qualification, not a fork-removal task |

Test snapshots of official providers and authentic wake-word model assets live
under `tests/fixtures`, with hashes checked before use. They do not participate
in production component selection and require no network or manually prepared
source tree. Positive MWW tests execute the actual streaming-model owner with
a substituted inference backend; they establish fresh-input and lifetime
contracts, not acoustic recognition accuracy. The old official revision is a
fixed negative control, so updating the installed builder cannot make a correct
upstream provider fail merely because it no longer reproduces the old bug.

## P4 source and measurement checkpoint

The independent renderer sends decoded RGB/YUV through the MIPI display's
public owned transform, not through a P4 LVGL video image. The driver composes
rotation once and serializes video with UI drawing and coherent readback.
Its base is official `c6f330458a2da2f64788487486bd6d0f8749fafd`, aligned with
the recorded ESPHome driver. S3 retains LVGL image presentation. SIP/RTP adapters
remain in VoIP Stack.

Public P4 profiles follow ESPHome's recommended SDK without a source pin. The
S3 candidate retains its separately qualified official IDF 5.5.6/dd47 source pin;
that historical choice is not described as an unpinned recommended SDK.
Default v5.5.6 tag `880a203a` and forward v6.0.3 tag `76f5dedd` require the
recorded official SDMMC/PPA corrections as build-local backports. The forward
SDK6 tag build and its digital-media check used the same MIPI, Renderer and
Screenshot source as the recommended-SDK candidate. This does not pin public
profiles to SDK6.

Two default-SDK receive-video checks with a 320x240, 10 FPS JPEG source measured
9.696 and 9.941 display-owner commits per second in approximately eight-second
steady windows. The first strict browser probe passed. The repeat reported
24 startup audio-playout drops and remained a strict failure; both calls
completed call-scoped cleanup. This is a bounded digital-media result, not an
all-green audio or visible-panel claim.

On 16 November, the true SDK 6.0.3 tag plus the same official backports completed
one matching Casa-to-P4 JPEG call. Its steady window measured 9.824 display-owner
commits per second, with approximately 10 FPS JPEG on the wire in both directions,
zero RTP loss, zero browser playout drops and zero underruns. Captured microphone
PCM matched the browser recording; the opposite direction retained the known
extra all-zero boundary frames. Controls were unchanged at Master 0 and all
call-scoped resources were released. This short comparison demonstrates the
measured workload, not a statistical speedup or an optical panel test.

The device was restored afterwards to the archived recommended-SDK 5.5.6
candidate. The user was absent, so transient physical white flashes remain
unverified even though active and idle screenshots succeeded. Screenshots do
not replace direct observation of scanout. H.264 remains a separate profile and
qualification.

The standalone P4 encoded-frame producer also compiled and linked against
Core 373 and the recommended SDK 5.5.6 with the same prepared MIPI/Renderer source.
Its selected components contain no VoIP, API, network, audio or camera provider.
The exact ELF, map and generated source were archived, then the build was
cleaned with ESPHome. This proves build-time independence, without a firmware
upload or another physical qualification claim.

## Real upgrade and call checks

On 17 November, an existing laboratory installation running stable Core
2026.11.1 upgraded its persisted integration configuration from 6.4 to 7.1.
The component converted three browser phones into SIP accounts in place,
preserving subentries, devices, entities and nine saved card configurations.
Existing SIP credentials were unchanged; the component generated the three
missing browser-account passwords once. A second actual HA process start
preserved the migrated configuration and credentials.

The same integration source completed a call from a registered bareSIP client
answered in the existing Lovelace card, followed by a separate bidirectional
L16 call. The latter retained browser PCM in both directions and two audio
streams independently decoded from the packet capture. Call resources returned
to quiescence before reuse. The peers were local SIP clients, with browser
audio output disabled; this checks digital media rather than acoustic quality.
The original laboratory storage and service state were restored afterwards.

The Chromium suite also passed 138 cases, including decoder recovery after an
H.264 error. Its explicit browser catalog now includes that regression file;
the catalog guard passes as well. These checks complement the live HA calls.

## Home Assistant stable release checkpoint

**Open checkpoint: 2026-12-02.** Verify the official stable release includes
Core PRs [#190351](https://github.com/home-assistant/core/pull/190351) (Repair
ownership by subentry) and [#192364](https://github.com/home-assistant/core/pull/192364)
(Voice Support audio processing). Both changes are in development Core at
`fddf359f543346a9b7cc0068e8e4dc19944dea13`; they are absent from 2026.11.1.

The integration minimum remains stable Core 2026.11.1 for ordinary features.
Repair ownership uses the subentry argument only when the official API supports
it, retaining entry ownership otherwise. Telephone Assist waits for first speech
with pre-roll before starting Core STT, then delegates utterance completion to
the existing Core pipeline. New Voice Support processing belongs to that Core
pipeline, rather than a second integration segmenter.

Qualify the newer Core behaviour separately in the laboratory. Keep home
installations on stable Core. Once the compatible
stable release is available, rerun the affected tests, qualify upgrade with
existing accounts/dashboard bindings, and complete the home update. If either
change is deferred, retain this checkpoint with the exact missing dependency.
No automatic home update is scheduled by this document.

## Qualification and resources

Complete related edits before grouped tests and firmware builds. Compile only
the affected profiles, on persistent storage, and retain one verified copy of
each useful unique firmware/evidence artifact. Retire completed generated build
trees and obsolete checkouts so unrelated experiments cannot fill the disk.

Compare actual presentation FPS, drops, decode/present time, surfaces/copies,
internal memory, largest free block, PSRAM, audio continuity and LVGL response
against the same workload on the retained reference firmware. Software, media
transport and physical display evidence establish different claims.

The ordinary account invariant remains: an enabled account is reachable with
a valid SIP registration or a persistent HA binding. Browser visibility is
independent of that binding. Preserve account/device/entity identities and
configured policies during every upgrade.
