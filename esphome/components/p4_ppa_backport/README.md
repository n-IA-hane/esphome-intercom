# ESP32-P4 PPA build backport

This helper applies only official ESP-IDF commit
[`9da8ff53`](https://github.com/espressif/esp-idf/commit/9da8ff53f751f6682f69897d886f3a9946deed31)
to the exact ESP-IDF v5.5.6 and v6.0.3 source files recorded in
`p4_ppa_backport.json`. The source references are the actual official tag commits,
not an arbitrary checkout whose directory happens to carry the version number.
It fixes cross-core PPA engine-idle admission and stale blocking-completion
semaphores. It does not change the public PPA API or add a different engine.

The CMake hook copies four C files and their private header into this build's
binary directory. It verifies the official patch and before/after SHA256 values,
then substitutes only those sources on the existing `esp_driver_ppa` target.
Target settings, source settings, source basenames and public headers remain
owned by ESP-IDF. The installed SDK and `IDF_PATH` are never edited.

Other chips, frameworks and SDK versions add no build hook. A reverse dry-run
recognizes this official fix when already present and skips all copying. An
unrecognized source in either supported version fails configuration instead of guessing another patch.
Remove the helper when the normal supported SDK contains the fix.

```yaml
p4_ppa_backport:
```

Builds require Git, already used by ESPHome for source dependencies. This is a
temporary, explicit official backport, not a permanent SDK version override.
