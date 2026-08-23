# ESPHome Voice Assistant fork upstream record

Upstream baseline: ESPHome 2026.8.0 tag, component path
`esphome/components/voice_assistant`.

Checked with:

```bash
diff -ru test_captures/esphome-2026.8.0-src/esphome/components/voice_assistant esphome/components/voice_assistant
```

## Local patch

1. `tts_playback_start_timeout`

   Files:
   - `__init__.py`
   - `voice_assistant.h`
   - `voice_assistant.cpp`

   Reason: slow TTS engines can take longer than ESPHome's fixed 2 second
   playback-start timeout. The fork keeps upstream's 2 second default, exposes a
   YAML option, and maintained full voice packages set it to 10 seconds.

   Upstream path: viable as a narrow configuration option preserving existing
   default behavior.

## Current diff summary

```diff
__init__.py
+ CONF_TTS_PLAYBACK_START_TIMEOUT schema/codegen option

voice_assistant.h
+ setter and tts_playback_start_timeout_ member

voice_assistant.cpp
+ start_playback_timeout_ uses the configured value instead of hardcoded 2000
```

After updating ESPHome, re-run the diff above and update this file before
release.
