# ESPHome speaker fork

This directory carries a small project-local fork of ESPHome's `speaker` and
`speaker.media_player` components.

The fork exists for custom YAMLs that still use ESPHome's
`platform: speaker` media player and need media pause to release the playback
pipeline cleanly. Maintained full-experience YAMLs now use the source-based
`speaker_source` media path instead; see
`packages/media_player/full_mono_48k.yaml`.

## What changed

The public addition is:

```yaml
media_player:
  - platform: speaker
    pause_releases_pipeline: true
```

When `pause_releases_pipeline` is `false`, pause keeps the media pipeline alive
and pauses decoder output. That is close to upstream ESPHome behavior.

When `pause_releases_pipeline` is `true`, pause stops the media pipeline and
keeps the media player in a paused state from Home Assistant's point of view.
On resume, media starts through the normal media command path. This prevents a
paused radio/media stream from keeping decoder/network/speaker pipeline state
around while another audio owner needs the output graph.

The fork also avoids a short stale-media burst when a stopped paused pipeline is
unpaused or replaced by a new media item. Stop/unpause is deferred until the
pipeline has actually reached `STOPPED`.

## Why it is needed here

The maintained full-experience YAMLs run several speaker users together:

- media/radio playback;
- Voice Assistant TTS;
- timer alarm sounds;
- intercom receive audio;
- mixer/resampler sources above `esp_audio_stack`.

Home Assistant's normal media pause semantics are useful for users, but on small
ESP32 audio devices a paused pipeline should not keep heavy pipeline resources
or an output path half-owned while TTS or intercom starts. Releasing the media
pipeline on pause made the combined media + TTS + intercom path stable on the
maintained S3/P4 profiles without changing user-visible media controls.

## Scope

This fork is intentionally narrow:

- it is only pulled by YAMLs that list `speaker` in `external_components`;
- it does not replace `esp_audio_stack`;
- it does not own I2S or codec hardware;
- it preserves ESPHome's normal `speaker` and `media_player` YAML shape;
- native voip-only YAMLs that do not use `platform: speaker` media playback
  do not need it.

## Maintained usage

Current maintained full-experience YAMLs do not require this fork for their
main media path. They use:

```yaml
packages:
  speaker_mixer: !include packages/audio/full_mono_mixer_48k.yaml
  speaker_media_player: !include packages/media_player/full_mono_48k.yaml
```

That package uses `platform: speaker_source`, not `platform: speaker`.

Legacy custom YAMLs that still use ESPHome's speaker media player can set:

```yaml
pause_releases_pipeline: true
```

when they need media pause/resume to coexist with TTS, timers and intercom.

## Upstream compatibility

The fork should be treated as a compatibility layer until ESPHome upstream has a
matching pause mode. Keep local changes minimal and rebase against upstream
ESPHome before changing unrelated media-player behavior.
