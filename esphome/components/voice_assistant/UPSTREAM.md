# ESPHome Voice Assistant fork upstream record

Upstream baseline: ESPHome `dev` commit
`5f6a910e2d6e41d3716668a66e5dff8cca25f2ea`, component path
`esphome/components/voice_assistant`.

Checked with:

```bash
git diff --no-index ../esphome-pr-work/esphome/components/voice_assistant esphome/components/voice_assistant
```

## Local patch

1. `tts_playback_start_timeout`

   Files:
   - `__init__.py`
   - `voice_assistant.h`
   - `voice_assistant.cpp`

   Reason: slow TTS engines can take longer than ESPHome's fixed 2 second
   playback-start timeout. The fork keeps upstream's 2 second default, exposes a
   YAML option, and maintained full voice packages set it to 35 seconds.

   Upstream path: viable as a narrow configuration option preserving existing
   default behavior.

## Current diff summary

```diff
__init__.py
+ CONF_TTS_PLAYBACK_START_TIMEOUT schema/codegen option

voice_assistant.h
+ setter and tts_playback_start_timeout_ member

voice_assistant.cpp
+ configurable deadline while waiting for playback to start
+ retain the upstream 2 second deadline after playback has started
+ preserve completed or aborted playback across late TTS events
+ deliver the streaming-start hook before submitting the media URL
```

After updating ESPHome, re-run the diff above and update this file before
release.
