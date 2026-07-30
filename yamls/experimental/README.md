# Experimental YAMLs

Configurations for hardware topologies or integration experiments that have not
been promoted to release baselines. They compile or reflect the intended wiring,
but they are not release targets. Treat them as starting points for bring-up and
regression comparison.

## Contents

- [`home-assistant-voice-pe/`](home-assistant-voice-pe/) - Voice PE VoIP
  integration proof.
- [`waveshare-s3-touch-lcd-1.85c/`](waveshare-s3-touch-lcd-1.85c/) -
  community-tested Waveshare 1.85C V2 profiles.
- [`esp32-s3-box-3/`](esp32-s3-box-3/) - community-tested ESP32-S3-BOX-3
  full-AEC profile and optional hardware additions.

Dual-bus MEMS+amp profiles are no longer experimental; the maintained SIP phone
profiles live in [`../voip-only/dual-bus/`](../voip-only/dual-bus/) and
use `esp_audio_stack` `rx_bus` / `tx_bus`.

## Status

| YAML | Hardware | What's missing |
|------|----------|----------------|
| `home-assistant-voice-pe/home-assistant-voice-pe-voip.yaml` | Home Assistant Voice PE | runtime VoIP validation |
| `waveshare-s3-touch-lcd-1.85c/waveshare-s3-touch-lcd-1.85c-box-voip-only-afe.yaml` | Waveshare ESP32-S3-Touch-LCD-1.85C BOX V2 | tested by contributor; untested by maintainer |
| `waveshare-s3-touch-lcd-1.85c/waveshare-s3-touch-lcd-1.85c-box-full-afe.yaml` | Waveshare ESP32-S3-Touch-LCD-1.85C BOX V2 | maintainer hardware validation |
| `esp32-s3-box-3/esp32-s3-box-3-full-aec.yaml` | Espressif ESP32-S3-BOX-3 | tested by contributor; untested by maintainer |

## Contributing

If you flash one of these on the matching hardware and complete a successful
call, please open an issue or PR with logs from both ends so we can promote the
YAML to the tested tree.
