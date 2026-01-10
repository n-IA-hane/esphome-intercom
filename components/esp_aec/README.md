# ESP AEC - Acoustic Echo Cancellation for ESPHome

Hardware-accelerated echo cancellation using Espressif's ESP-SR library.

## What is Echo Cancellation?

When you have a speaker and microphone in the same device, the microphone picks up what the speaker plays. This creates an annoying echo for the remote party. AEC removes the speaker audio from the microphone signal in real-time.

```
Without AEC:
  You speak → Mic picks up YOUR voice + SPEAKER audio → Remote hears echo

With AEC:
  You speak → Mic picks up YOUR voice + SPEAKER audio
                                ↓
                    AEC subtracts speaker audio
                                ↓
              Remote hears only YOUR voice (clean)
```

## Features

- **Real-time Processing**: Sub-frame latency (~16ms)
- **Adaptive Filter**: Adjusts to room acoustics automatically
- **Configurable Quality**: Trade CPU for better echo removal
- **Hardware Optimized**: Uses ESP32-S3 vector instructions

## Use Cases

- **Intercom Systems**: Two-way audio without echo
- **Voice Assistants**: Clean wake-word detection while playing audio
- **Video Conferencing**: Full-duplex communication
- **Smart Speakers**: Talk while music is playing
- **Baby Monitors**: Two-way talk feature

## Requirements

- **ESP32-S3** with PSRAM (required for ESP-SR library)
- ESP-IDF framework (not Arduino)
- 8MB+ flash recommended

## Installation

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/n-IA-hane/esphome-intercom
      ref: main
    components: [esp_aec]
```

## Framework Setup

ESP-SR requires specific framework configuration:

```yaml
esp32:
  board: esp32-s3-devkitc-1
  framework:
    type: esp-idf
    sdkconfig_options:
      CONFIG_ESP32S3_DEFAULT_CPU_FREQ_240: "y"
      CONFIG_ESP32S3_DATA_CACHE_64KB: "y"
    components:
      - espressif/esp-sr^2.3.0

psram:
  mode: octal    # or quad
  speed: 80MHz
```

## Configuration

```yaml
esp_aec:
  id: echo_canceller
  sample_rate: 16000      # Must match audio components
  filter_length: 4        # 1-10, higher = better but more CPU
```

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | ID | Required | Component ID |
| `sample_rate` | int | 16000 | Audio sample rate (8000, 16000, 32000, 48000) |
| `filter_length` | int | 4 | AEC filter length (1-10) |

### Filter Length Guide

| Value | CPU Usage | Echo Removal | Use Case |
|-------|-----------|--------------|----------|
| 1-3 | ~10% | Basic | Low-power devices |
| 4-6 | ~20% | Good | General use (recommended) |
| 7-10 | ~35% | Excellent | High-quality audio, large rooms |

## Integration Examples

### With i2s_audio_duplex (Single I2S Bus)

```yaml
esp_aec:
  id: aec
  sample_rate: 16000
  filter_length: 4

i2s_audio_duplex:
  id: i2s_duplex
  i2s_lrclk_pin: GPIO45
  i2s_bclk_pin: GPIO9
  i2s_mclk_pin: GPIO16
  i2s_din_pin: GPIO10
  i2s_dout_pin: GPIO8
  sample_rate: 16000
  aec_id: aec              # Link AEC here
```

### With intercom_audio

```yaml
esp_aec:
  id: aec
  sample_rate: 16000
  filter_length: 4

intercom_audio:
  id: intercom
  microphone_id: mic
  speaker_id: speaker
  aec_id: aec              # Link AEC here
```

## Lambda Access

```cpp
// Check if AEC is initialized
if (id(aec).is_initialized()) {
  ESP_LOGI("aec", "AEC ready");
}

// Process audio frame manually (advanced)
// Normally handled automatically by audio components
int16_t mic_data[256];
int16_t speaker_ref[256];
int16_t output[256];
id(aec).process(mic_data, speaker_ref, output, 256);
```

## Runtime Control

AEC can be enabled/disabled at runtime via the audio component:

```yaml
switch:
  - platform: template
    name: "Echo Cancellation"
    lambda: 'return id(intercom).is_aec_enabled();'
    turn_on_action:
      - lambda: 'id(intercom).set_aec_enabled(true);'
    turn_off_action:
      - lambda: 'id(intercom).set_aec_enabled(false);'
```

## How It Works

1. **Reference Signal**: Speaker output is captured as "reference"
2. **Microphone Input**: Raw mic signal contains voice + echo
3. **Adaptive Filter**: Estimates room impulse response
4. **Subtraction**: Removes estimated echo from mic signal
5. **Output**: Clean voice signal

```
Speaker Output ──────────────────────┐
                                     │
                              ┌──────▼──────┐
Microphone ──────────────────►│   ESP-SR    │───► Clean Output
                              │     AEC     │
                              └─────────────┘
```

## Performance Notes

- **Latency**: ~16ms (one audio frame)
- **CPU**: 10-35% depending on filter_length
- **Memory**: Uses PSRAM for filter coefficients
- **Convergence**: 2-5 seconds to fully adapt to room

## Troubleshooting

### Echo Still Present
1. Increase `filter_length` (try 6-8)
2. Ensure speaker reference is correctly fed to AEC
3. Check sample rates match between all components
4. Reduce speaker volume (helps AEC converge)

### Audio Distortion
1. Decrease `filter_length`
2. Check for CPU overload (reduce other tasks)
3. Verify PSRAM is working correctly

### AEC Not Initializing
1. Verify ESP-SR component is included in framework
2. Check PSRAM configuration
3. Ensure sufficient flash size (8MB+)
4. Check logs for ESP-SR error messages

### Crackling/Dropouts
1. Audio task priority may be too low
2. WiFi/BLE activity interfering - pin audio to Core 1
3. Buffer sizes too small

## Memory Usage

| Component | Memory |
|-----------|--------|
| AEC Instance | ~50KB |
| Filter Coefficients | ~8KB per filter_length |
| Processing Buffers | ~4KB |

## Limitations

- ESP32-S3 only (requires ESP-SR)
- ESP-IDF framework required (not Arduino)
- Single-channel (mono) audio only
- Fixed sample rates (8k, 16k, 32k, 48k)

## License

MIT License

ESP-SR library is licensed under Espressif's terms.
