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

## Shared-output stop and restart

A forced stop can discard frames from more than one mixer input. The mixer now
stops its writer first, releases the transfer buffer and reconciles every source
timeline through its existing worker cleanup. Only then does it stop the hardware
output and permit restart. Source input buffers remain available for subsequent
playback. Discarded frames are not reported as played.

For ESP Audio Stack output, also use the development fix that rejects new writes
while its asynchronous ring reset is pending. Otherwise a new write can be
accepted and then discarded by the previous stop's reset. This combination has
passed targeted Spotpear mixed-media tests; it does not establish multi-day
stability for every configuration. See issue #129.
