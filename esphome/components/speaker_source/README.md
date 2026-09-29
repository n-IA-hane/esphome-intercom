# ESPHome playback adapter

Based on the stable ESPHome `release` branch, version `2026.9.0`, commit
[`c6e4c87e525dd343e470d8dea368957a002f6505`](https://github.com/esphome/esphome/tree/c6e4c87e525dd343e470d8dea368957a002f6505/esphome/components/speaker_source).
ESPHome has no `main` branch. Compare future updates against the stable release,
not unreleased `dev` changes. Volume and mute behavior match this stable baseline.
The original ESPHome license is included in this directory: runtime sources use
GPLv3, Python configuration uses MIT.

This adapter keeps the native configuration and speaker interfaces. It is
selected by the shared playback compatibility package for full media profiles.
The playback accounting changes and dependency compatibility are described in
[Sendspin compatibility](../sendspin_compat/README.md).

Stop commands also target a source that is still opening its HTTP stream.
An explicit stop cancels that pending request and does not advance the playlist.

Worker shutdown also guards ESP-IDF's `pthread_join` against unrelated task
notifications. ESPHome uses task notifications to wake its main loop, including
while an HTTP reader is stopping. The adapter checks the worker's actual exit
state before releasing its resources. It adds no task, media buffer or polling
loop and preserves the upstream source outside that completion check.

Upstream proposals for the remaining compatibility changes:

- [Playback accounting](https://github.com/esphome/esphome/pull/19499).
- [Pending-source cancellation](https://github.com/esphome/esphome/pull/19501).
- [ESP-IDF join completion](https://github.com/espressif/esp-idf/pull/19123).

Recheck these differences when updating the stable ESPHome baseline. An open
proposal is not a replacement for a released upstream fix.

## Diagnose a stalled control queue

A full control queue now logs its first waiting command and the state of both
playback pipelines. Keep the complete block: queue depth, active/target/pending/
stopping source states, speaker state and pending frame count. The head command
codes are 0 (replace URI), 1 (enqueue URI), 2 (advance playlist), 3 (play current)
and 4 (player command).

During an investigation, the same snapshot can be requested from a main-loop
lambda with `id(speaker_media_player).dump_diagnostics()` (use your player ID).
For example, an optional `interval` automation can request it every 60 seconds.
Use logger level INFO or more verbose. The snapshot does not clear the queue,
stop playback, or log media URLs. It adds no periodic work unless you configure it.

A source's pending frame count alone is not proof of a blocked output: after its
IDLE notification, final output callbacks may no longer be attributed to that
source. Compare it with speaker state and the mixer/output buffers.
