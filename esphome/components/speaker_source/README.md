# ESPHome playback adapter

Based on ESPHome 2026.9.0. The original ESPHome license is included in this
directory: runtime sources use GPLv3, Python configuration uses MIT.

This adapter keeps the native configuration and speaker interfaces. It is
selected by the shared playback compatibility package for full media profiles.
The playback accounting changes and dependency compatibility are described in
[Sendspin compatibility](../sendspin_compat/README.md).

Stop commands also target a source that is still opening its HTTP stream.
An explicit stop cancels that pending request and does not advance the playlist.
