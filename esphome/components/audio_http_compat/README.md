# HTTP WAV compatibility

Use ESPHome 2026.9.0 or newer for the native `audio_http` component, including
its `persistent_ring_buffer` option. This component only enables recognition
of the `audio/vnd.wave` MIME type in micro-decoder 0.4.0 for WAV playback.

The build adapter replaces the existing detector translation unit without
modifying downloaded sources. It adds no runtime task, queue or audio buffer.
It is inactive when WAV is disabled or the upstream detector already supports
this MIME type. Include `packages/audio/http_media_codecs.yaml` to select WAV,
MP3 and this compatibility adapter together.
