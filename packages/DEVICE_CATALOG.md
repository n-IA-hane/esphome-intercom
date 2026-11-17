# Device package catalog

Each row links a user-facing firmware YAML to its complete device composition.
The device package keeps its hardware/backend selection and consumer parameters
in one place. Shared feature modules own behavior; presentation layouts own
geometry. The original board diagrams remain in the user-facing YAMLs.

The list records the supplied defaults, not hardware certification. Experimental
and untested profiles keep their status. Native microphone-only and speaker-only
profiles keep that direction. Voice PE selects our overlay while its official
firmware remains the owner of its audio and assistant.

| Firmware YAML | Device package | Default features |
| --- | --- | --- |
| [esp32-s3-box-3-full-aec](../yamls/experimental/esp32-s3-box-3/esp32-s3-box-3-full-aec.yaml) | [`devices/esp32-s3-box-3-aec.yaml`](devices/esp32-s3-box-3-aec.yaml) | `voice, wake_word, voip, music, sendspin, timers, display, status_led, sensor_dock, diagnostics` |
| [esp32-s3-box-3-full-afe](../yamls/experimental/esp32-s3-box-3/esp32-s3-box-3-full-afe.yaml) | [`devices/esp32-s3-box-3-afe.yaml`](devices/esp32-s3-box-3-afe.yaml) | `voice, wake_word, voip, music, sendspin, timers, display, status_led, sensor_dock, diagnostics` |
| [home-assistant-voice-pe-voip](../yamls/experimental/home-assistant-voice-pe/home-assistant-voice-pe-voip.yaml) | [`devices/home-assistant-voice-pe-voip.yaml`](devices/home-assistant-voice-pe-voip.yaml) | `voip` |
| [waveshare-s3-touch-lcd-1.85c-box-v2-full-aec](../yamls/experimental/waveshare-s3-touch-lcd-1.85c-box-v2/waveshare-s3-touch-lcd-1.85c-box-v2-full-aec.yaml) | [`devices/waveshare-s3-box-v2-aec.yaml`](devices/waveshare-s3-box-v2-aec.yaml) | `voice, wake_word, voip, music, sendspin, timers, display, diagnostics` |
| [waveshare-s3-touch-lcd-1.85c-box-full-afe](../yamls/experimental/waveshare-s3-touch-lcd-1.85c/waveshare-s3-touch-lcd-1.85c-box-full-afe.yaml) | [`devices/waveshare-s3-box-v1-aec.yaml`](devices/waveshare-s3-box-v1-aec.yaml) | `voice, wake_word, voip, music, sendspin, timers, display, diagnostics` |
| [waveshare-s3-touch-lcd-1.85c-box-voip-only-afe](../yamls/experimental/waveshare-s3-touch-lcd-1.85c/waveshare-s3-touch-lcd-1.85c-box-voip-only-afe.yaml) | [`devices/waveshare-s3-box-v1-phone-afe.yaml`](devices/waveshare-s3-box-v1-phone-afe.yaml) | `voip, display` |
| [generic-s3-full-aec](../yamls/full-experience/dual-bus/generic-s3-full-aec.yaml) | [`devices/generic-s3-dual-aec.yaml`](devices/generic-s3-dual-aec.yaml) | `voice, wake_word, voip, directory, blf, mwi, music, sendspin, timers, status_led, diagnostics` |
| [generic-s3-full-esphome-native](../yamls/full-experience/esphome-native/generic-s3-full-esphome-native.yaml) | [`devices/generic-s3-native.yaml`](devices/generic-s3-native.yaml) | `voice, wake_word, voip, directory, blf, mwi, music, sendspin, timers, status_led, diagnostics` |
| [generic-s3-full-aec](../yamls/full-experience/single-bus/generic-s3-full-aec.yaml) | [`devices/generic-s3-single-aec.yaml`](devices/generic-s3-single-aec.yaml) | `voice, wake_word, voip, directory, blf, mwi, music, sendspin, timers, status_led, diagnostics` |
| [generic-s3-full-aec-camera](../yamls/full-experience/single-bus/generic-s3-full-aec-camera.yaml) | [`devices/generic-s3-single-aec-camera.yaml`](devices/generic-s3-single-aec-camera.yaml), configurable shared-I2S audio and native DVP JPEG camera | `voice, wake_word, voip, directory, blf, mwi, music, sendspin, timers, status_led, diagnostics, video` |
| [generic-s3-full-lite-aec](../yamls/full-experience/single-bus/generic-s3-full-lite-aec.yaml) | [`devices/generic-s3-lite-aec.yaml`](devices/generic-s3-lite-aec.yaml) | `voice, wake_word, voip, directory, blf, mwi, music, timers, status_led, diagnostics` |
| [spotpear-ball-v2-full-afe](../yamls/full-experience/single-bus/spotpear-ball-v2-full-afe.yaml) | [`devices/spotpear-ball-v2-full-afe.yaml`](devices/spotpear-ball-v2-full-afe.yaml) | `voice, wake_word, voip, directory, blf, mwi, music, sendspin, timers, status_led, display, diagnostics` |
| [waveshare-p4-touch-full-afe-landscape-videophone-jpeg](../yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-landscape-videophone-jpeg.yaml) | [`devices/waveshare-p4-full-landscape.yaml`](devices/waveshare-p4-full-landscape.yaml) | `voice, wake_word, voip, directory, blf, mwi, music, sendspin, timers, display, diagnostics, video` |
| [waveshare-p4-touch-full-afe-portrait](../yamls/full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml) | [`devices/waveshare-p4-full-portrait.yaml`](devices/waveshare-p4-full-portrait.yaml) | `voice, wake_word, voip, directory, blf, mwi, music, sendspin, timers, display, diagnostics` |
| [waveshare-s3-full-afe](../yamls/full-experience/single-bus/waveshare-s3-full-afe.yaml) | [`devices/waveshare-s3-full-afe.yaml`](devices/waveshare-s3-full-afe.yaml) | `voice, wake_word, voip, directory, blf, mwi, music, sendspin, timers, status_led, diagnostics` |
| [waveshare-s3-touch-lcd-1.85c-box-v2-full](../yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full.yaml) | [`devices/waveshare-s3-box-v2.yaml`](devices/waveshare-s3-box-v2.yaml), selectable AEC/AFE, microphone mix and SIP transport | `voice, wake_word, voip, directory, blf, mwi, music, sendspin, timers, display, diagnostics` |
| [waveshare-s3-touch-lcd-1.85c-box-v2-full-afe](../yamls/full-experience/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-full-afe.yaml) | [`devices/waveshare-s3-box-v2-afe.yaml`](devices/waveshare-s3-box-v2-afe.yaml) | `voice, wake_word, voip, directory, blf, mwi, music, sendspin, timers, display, diagnostics` |
| [generic-s3-full-afe](../yamls/untested/generic-s3-full-afe.yaml) | [`devices/generic-s3-afe.yaml`](devices/generic-s3-afe.yaml) | `voice, wake_word, voip, music, sendspin, timers, status_led, diagnostics` |
| [generic-s3-voip](../yamls/voip-only/dual-bus/generic-s3-voip.yaml) | [`devices/generic-s3-phone-dual.yaml`](devices/generic-s3-phone-dual.yaml) | `voip, directory, blf, mwi, status_led, diagnostics` |
| [generic-s3-voip-esphome-native-full-duplex](../yamls/voip-only/esphome-native/generic-s3-voip-esphome-native-full-duplex.yaml) | [`devices/generic-s3-phone-native.yaml`](devices/generic-s3-phone-native.yaml) | `voip, directory, blf, mwi, status_led, diagnostics` |
| [generic-s3-voip-esphome-native-mic-only](../yamls/voip-only/esphome-native/generic-s3-voip-esphome-native-mic-only.yaml) | [`devices/generic-s3-phone-microphone.yaml`](devices/generic-s3-phone-microphone.yaml) | `voip, directory, blf, mwi, status_led, diagnostics` |
| [generic-s3-voip-esphome-native-speaker-only](../yamls/voip-only/esphome-native/generic-s3-voip-esphome-native-speaker-only.yaml) | [`devices/generic-s3-phone-speaker.yaml`](devices/generic-s3-phone-speaker.yaml) | `voip, directory, blf, mwi, status_led, diagnostics` |
| [generic-s3-voip](../yamls/voip-only/single-bus/generic-s3-voip.yaml) | [`devices/generic-s3-phone-single.yaml`](devices/generic-s3-phone-single.yaml) | `voip, directory, blf, mwi, status_led, diagnostics` |
| [generic-s3-voip-camera](../yamls/voip-only/single-bus/generic-s3-voip-camera.yaml) | [`devices/generic-s3-phone-single-camera.yaml`](devices/generic-s3-phone-single-camera.yaml), standalone AEC phone and native DVP JPEG camera | `voip, directory, blf, mwi, status_led, diagnostics, video` |
| [spotpear-ball-v2-voip](../yamls/voip-only/single-bus/spotpear-ball-v2-voip.yaml) | [`devices/spotpear-ball-v2-phone.yaml`](devices/spotpear-ball-v2-phone.yaml) | `voip, directory, blf, mwi, display, status_led, diagnostics` |
| [waveshare-p4-touch-videophone-h264](../yamls/voip-only/single-bus/waveshare-p4-touch-videophone-h264.yaml) | [`devices/waveshare-p4-phone-h264.yaml`](devices/waveshare-p4-phone-h264.yaml) | `voip, directory, blf, mwi, display, video` |
| [waveshare-p4-touch-videophone-jpeg](../yamls/voip-only/single-bus/waveshare-p4-touch-videophone-jpeg.yaml) | [`devices/waveshare-p4-phone-jpeg.yaml`](devices/waveshare-p4-phone-jpeg.yaml) | `voip, directory, blf, mwi, display, video` |
| [waveshare-s3-voip](../yamls/voip-only/single-bus/waveshare-s3-voip.yaml) | [`devices/waveshare-s3-phone.yaml`](devices/waveshare-s3-phone.yaml) | `voip, directory, blf, mwi, status_led, diagnostics` |
| [waveshare-s3-touch-lcd-1.85c-box-v2-voip](../yamls/voip-only/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-voip.yaml) | [`devices/waveshare-s3-box-v2-phone.yaml`](devices/waveshare-s3-box-v2-phone.yaml) | `voip, directory, blf, mwi, display, diagnostics` |

`esp32-s3-box-3-additions.yaml` remains a compatibility overlay for existing
BOX-3 configurations; it is not a separate firmware entrypoint. New BOX-3 device
packages select `display` and `sensor_dock` independently.

The complete feature/dependency contract and legacy entrypoint mapping are in
[the package guide](README.md). Shared fragments should not be selected by guessing
which filenames look related to a feature.
