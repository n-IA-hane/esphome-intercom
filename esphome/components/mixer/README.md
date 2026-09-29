# ESPHome playback adapter

Based on ESPHome 2026.9.0. The original ESPHome license is included in this
directory: runtime sources use GPLv3, Python configuration uses MIT.

This adapter keeps the native configuration and speaker interfaces. It is
selected by the shared playback compatibility package for full media profiles.
The playback accounting changes and dependency compatibility are described in
[Sendspin compatibility](../sendspin_compat/README.md).

The adapter also includes ESPHome's [pending mixer restart fix](https://github.com/esphome/esphome/pull/19368).
When a new playback starts while the previous mixer task is shutting down,
cleanup preserves its start request for the next loop instead of discarding it.
