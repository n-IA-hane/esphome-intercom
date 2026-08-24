# ESPHome speaker fork upstream record

Upstream baseline: ESPHome `dev` commit
`5f6a910e2d6e41d3716668a66e5dff8cca25f2ea`, component path
`esphome/components/speaker`.

Checked with:

```bash
.venv/bin/esphome version
git diff --no-index ../esphome-pr-work/esphome/components/speaker esphome/components/speaker
```

## Local patches

1. `pause_releases_pipeline`

   Files:
   - `media_player/__init__.py`
   - `media_player/speaker_media_player.h`
   - `media_player/speaker_media_player.cpp`

   Reason: Home Assistant pause should release the media pipeline on combined
   media/TTS/intercom devices when explicitly configured. This keeps HA media
   controls user-compatible while freeing the ESP audio graph for TTS, timers
   and intercom receive audio.

   Upstream path: viable as an optional `speaker.media_player` pause policy.
   The patch is narrow and preserves upstream default behavior.

2. Decoder-source accepted event

   File:
   - `media_player/audio_pipeline.cpp`

   Reason: replaces a fixed `delay(10)` while releasing the reader's
   `shared_ptr` with an event bit set when the decoder has attached to the raw
   ring buffer. The timeout remains only as a fail-soft bound during teardown.

   Upstream path: viable as a scheduler cleanup independent of Intercom.

## Current diff summary

```diff
media_player/__init__.py
+ CONF_PAUSE_RELEASES_PIPELINE schema/codegen option

media_player/speaker_media_player.h
+ pause_releases_pipeline_ member and setter

media_player/speaker_media_player.cpp
+ PAUSE and toggle-pause stop the media pipeline when the option is enabled

media_player/audio_pipeline.cpp
+ DECODER_MESSAGE_ACCEPTED_SOURCE event bit
+ reader waits on that event instead of sleeping 10 ms
+ decoder sets the event after add_source()
```

After updating ESPHome, re-run the diff above and update this file before
release.
