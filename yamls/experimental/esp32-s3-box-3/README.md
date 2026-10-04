# ESP32-S3-BOX-3 experimental profiles

These files target Espressif's ESP32-S3-BOX-3, not the Waveshare 1.85C-BOX V2.
The maintainer does not own this board. Keep a copy of your working firmware
and configuration before testing.

| Profile | Microphones | Processing | Validation |
| --- | --- | --- | --- |
| [Full AEC](esp32-s3-box-3-full-aec.yaml) | MIC1 | Lightweight AEC | Original profile tested by contributor mwhdc, see [#80](https://github.com/n-IA-hane/esphome-intercom/issues/80) |
| [Full dual-mic AFE](esp32-s3-box-3-full-afe.yaml) | MIC1 and MIC2 | AFE echo cancellation and speech enhancement | Experimental candidate for [#116](https://github.com/n-IA-hane/esphome-intercom/issues/116); physical validation pending |

The dual-mic profile reuses the existing Audio Stack implementation. It enables
ES7210 ADC inputs MIC1/MIC2 (`mic_selected: 0x03`) and captures the two standard
I2S slots with `rx_mic_slots: [left, right]`. AFE consumes both microphones and
a software playback reference, then supplies one processed mono microphone to
Assist, Micro Wake Word and VoIP. This configuration does not use a hardware
TDM reference slot.

The microphone wiring is documented in Espressif's
[BOX-3 main-board schematic](https://github.com/espressif/esp-box/blob/master/hardware/SCH_ESP32-S3-BOX-3_V1.0/SCH_ESP32-S3-BOX-3-MB_V1.1_20230808.pdf).
The codec driver maps the two-channel standard-I2S output to MIC1/MIC2;
confirm both physical channels using the level sensors below.

The initial AFE mode is `fd_low_cost`, with both microphones, AEC and speech
enhancement enabled. Dual-mic speech enhancement takes priority over standalone
noise suppression in ESP-SR. AGC is disabled in this initial profile, as in the
maintained dual-mic reference configuration. The inherited codec gain is 30 dB;
its acoustic suitability on this board still needs measurement.

## What to test

1. Preserve your device name, secrets and board-specific customizations. Build
   the dual-mic YAML with ESPHome 2026.9.0 or newer. It uses the maintained stable
   component sources; no experimental GMF manager patch is required.
2. After flashing, confirm the device stays connected and the new firmware
   remains installed instead of rolling back.
3. Look for **Microphone Left Level** and **Microphone Right Level** in the
   ESPHome device entities. They measure the raw slots before AFE. With the
   speaker quiet, speak near each microphone and check that both respond.
4. Test wake word and Assist, then VoIP speech in both directions. Check echo
   during simultaneous speech/playback, hang up and immediately place a second
   call. Verify Assist still works afterwards.
5. Test ordinary media playback and the display/touch functions you use. Start
   at a moderate speaker level; avoid increasing all gains together.

Use the included **Audio Diagnostics** and **VoIP Diagnostics** buttons while
viewing device logs. If something fails, attach the complete boot log, diagnostic
blocks, exact component/compiler versions and relevant YAML with secrets removed.
Report the first failing step, not just whether SIP reached `in_call`.

Dual-mic processing adds CPU and memory load. A successful build does not prove
stable simultaneous operation of AFE, wake word, media and VoIP on this board.
