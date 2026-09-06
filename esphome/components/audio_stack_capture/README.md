# Audio qualification observer

This optional component observes the existing Audio Stack task. It does not
create an I2S reader or a playback engine. Production profiles do not enable it.
The matching Audio Stack build must provide `USE_ESP_AUDIO_STACK_CAPTURE` hooks.

The capture mask selects hardware RX (2), processed microphone (4), completed
DAC frames (8), and speaker PCM (16). Mask 8 transfers only completion metadata;
it proves played-frame cadence, not waveform quality. Waveform evidence must
come from an independent peer or from an explicitly qualified PCM capture.

`deferred_transfer: true` buffers a bounded recording in PSRAM and then sends
it over TCP. It postpones transfer until the recording ends, not until the
call ends. Buffer size must cover the selected rates, channels, samples and
record headers. Check the returned drop counters before interpreting a trace.

Measured limitation on WS3 Opus: transferring a roughly 1 MB raw capture during
a call delayed microphone transmission and caused queue drops, even with this
task at priority 1. A successful capture is therefore not proof of an
unperturbed call. Do not use bulk raw transfers to qualify continuous media.
Use bounded DAC metadata plus independent peer WAV/PCAP for the same interval,
and retain the observer's timing in the evidence. No audio-task timing or
priority adjustment is justified by an observer-induced failure.

This follows the separation between collection and transport explained in
[ESP-IDF application tracing](https://docs.espressif.com/projects/esp-idf/en/v5.5.2/esp32s3/api-guides/app_trace.html).
