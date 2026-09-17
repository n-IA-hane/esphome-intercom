# Sendspin compatibility

This optional component is loaded by the full media profiles, alongside the
native ESPHome mixer and speaker-source adapters. Audio-only VoIP profiles do
not load it.

For the Sendspin C++ 0.7.2 dependency used by ESPHome 2026.9.0, it fixes:

- Worker stacks requested in PSRAM must also have byte-access capability.
  Failed thread configuration is reported, and the creating task's previous
  pthread configuration is restored after worker creation.
- A partially late encoded chunk reaches Sendspin's existing decoded-prefix
  trimming logic instead of losing the entire chunk before decoding.

The adapter compiles one replacement translation unit for each enabled role.
It does not create another player, audio queue or synchronization clock, and
leaves disabled artwork/visualizer roles excluded. The dependency's source is
not modified in the package cache. Source checks stop the build if the relevant
upstream implementation changes, so a dependency update requires review rather
than silently applying an obsolete patch.

The mixer credits only frames actually contributed by each source. The
speaker-source player reserves pending frames before its blocking speaker
write, allowing completion callbacks during that write to be counted correctly.
Both retain the standard ESPHome speaker interfaces.
