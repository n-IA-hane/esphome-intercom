# I2S Audio Duplex - Full-Duplex I2S for ESPHome

True simultaneous microphone and speaker operation on a single I2S bus for audio codecs.

## Why This Component?

Standard ESPHome `i2s_audio` creates separate I2S instances for microphone and speaker, which works for setups with separate I2S buses. However, audio codecs like **ES8311, ES8388, WM8960** use a single I2S bus for both input and output simultaneously.

```
Without i2s_audio_duplex:
  Mic and Speaker fight for I2S bus → Audio glitches, half-duplex only

With i2s_audio_duplex:
  Single I2S controller handles both directions → True full-duplex
```

## Features

- **True Full-Duplex**: Simultaneous mic input and speaker output
- **Single I2S Bus**: Efficient use of hardware resources
- **AEC Integration**: Built-in support for echo cancellation
- **Volume Control**: Software gain for mic and speaker
- **Callback System**: Stream mic data to multiple consumers
- **Hardware Optimized**: Uses ESP-IDF native I2S drivers

## Use Cases

- **Audio Codec Devices**: ES8311, ES8388, WM8960, MAX98090, etc.
- **Intercom Systems**: Full-duplex conversation
- **Voice Assistants**: Listen while providing audio feedback
- **Video Conferencing**: Simultaneous talk and listen
- **Recording Studio Monitors**: Record while playing backing track

## Requirements

- **ESP32** or **ESP32-S3** (tested on S3)
- Audio codec with shared I2S bus (ES8311 recommended)
- ESP-IDF framework

## Installation

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/n-IA-hane/esphome-intercom
      ref: main
    components: [i2s_audio_duplex]
```

## Configuration

```yaml
i2s_audio_duplex:
  id: i2s_duplex
  i2s_lrclk_pin: GPIO45      # Word Select (WS/LRCLK)
  i2s_bclk_pin: GPIO9        # Bit Clock (BCK/BCLK)
  i2s_mclk_pin: GPIO16       # Master Clock (optional, some codecs need it)
  i2s_din_pin: GPIO10        # Data In (from codec ADC → ESP mic)
  i2s_dout_pin: GPIO8        # Data Out (from ESP → codec DAC speaker)
  sample_rate: 16000
  aec_id: aec_component      # Optional: link to esp_aec
```

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | ID | Required | Component ID |
| `i2s_lrclk_pin` | pin | Required | Word Select / LR Clock pin |
| `i2s_bclk_pin` | pin | Required | Bit Clock pin |
| `i2s_mclk_pin` | pin | -1 | Master Clock pin (if codec requires) |
| `i2s_din_pin` | pin | -1 | Data input from codec (microphone) |
| `i2s_dout_pin` | pin | -1 | Data output to codec (speaker) |
| `sample_rate` | int | 16000 | Audio sample rate (8000-48000) |
| `aec_id` | ID | - | Optional esp_aec component for echo cancellation |

## Pin Mapping by Codec

### ES8311 (Xiaozhi Ball, AI Voice Kits)
```yaml
i2s_audio_duplex:
  i2s_lrclk_pin: GPIO45   # LRCK
  i2s_bclk_pin: GPIO9     # SCLK
  i2s_mclk_pin: GPIO16    # MCLK (required)
  i2s_din_pin: GPIO10     # SDOUT (codec → ESP)
  i2s_dout_pin: GPIO8     # SDIN (ESP → codec)
  sample_rate: 16000
```

### ES8388 (LyraT, Audio Dev Boards)
```yaml
i2s_audio_duplex:
  i2s_lrclk_pin: GPIO25   # LRCK
  i2s_bclk_pin: GPIO5     # SCLK
  i2s_mclk_pin: GPIO0     # MCLK (required)
  i2s_din_pin: GPIO35     # DOUT
  i2s_dout_pin: GPIO26    # DIN
  sample_rate: 16000
```

### WM8960 (Various Dev Boards)
```yaml
i2s_audio_duplex:
  i2s_lrclk_pin: GPIO4    # LRCLK
  i2s_bclk_pin: GPIO5     # BCLK
  i2s_mclk_pin: GPIO0     # MCLK (required)
  i2s_din_pin: GPIO35     # ADCDAT
  i2s_dout_pin: GPIO25    # DACDAT
  sample_rate: 16000
```

## Built-in Controls

### AEC Switch
```yaml
switch:
  - platform: i2s_audio_duplex
    i2s_audio_duplex_id: i2s_duplex
    aec:
      name: "Echo Cancellation"
```

### Volume Controls
```yaml
number:
  - platform: i2s_audio_duplex
    i2s_audio_duplex_id: i2s_duplex
    mic_gain:
      name: "Mic Gain"
    speaker_volume:
      name: "Speaker Volume"
```

## Lambda Access

```cpp
// Start full duplex operation
id(i2s_duplex).start();

// Stop all audio
id(i2s_duplex).stop();

// Control mic/speaker independently
id(i2s_duplex).start_mic();
id(i2s_duplex).stop_mic();
id(i2s_duplex).start_speaker();
id(i2s_duplex).stop_speaker();

// Play audio to speaker
const uint8_t* pcm_data = ...;
size_t bytes = ...;
id(i2s_duplex).play(pcm_data, bytes);

// Check state
bool running = id(i2s_duplex).is_running();
bool mic_active = id(i2s_duplex).is_mic_running();
bool speaker_active = id(i2s_duplex).is_speaker_running();

// Volume control
id(i2s_duplex).set_mic_gain(1.5f);      // 0.0 - 2.0
id(i2s_duplex).set_speaker_volume(0.8f); // 0.0 - 1.0

// AEC control
id(i2s_duplex).set_aec_enabled(true);
bool aec_on = id(i2s_duplex).is_aec_enabled();
```

## Integration with intercom_audio

For intercom applications, link this component:

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
  aec_id: aec

intercom_audio:
  id: intercom
  duplex_id: i2s_duplex    # Uses i2s_audio_duplex for audio
  listen_port: 12346
  remote_ip: "192.168.1.100"
```

## Complete Example: Voice Assistant with Codec

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/n-IA-hane/esphome-intercom
      ref: main
    components: [i2s_audio_duplex, esp_aec]

i2c:
  sda: GPIO15
  scl: GPIO14

audio_dac:
  - platform: es8311
    id: codec_dac
    bits_per_sample: 16bit
    sample_rate: 16000

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
  aec_id: aec

switch:
  - platform: i2s_audio_duplex
    i2s_audio_duplex_id: i2s_duplex
    aec:
      name: "Echo Cancellation"

number:
  - platform: i2s_audio_duplex
    i2s_audio_duplex_id: i2s_duplex
    mic_gain:
      name: "Microphone Gain"
      min_value: 0
      max_value: 200
      step: 10
    speaker_volume:
      name: "Speaker Volume"
      min_value: 0
      max_value: 100
      step: 5
```

## When to Use This vs Standard i2s_audio

| Scenario | Use This Component | Use Standard i2s_audio |
|----------|-------------------|----------------------|
| ES8311/ES8388/WM8960 codec | Yes | No (won't work properly) |
| Separate INMP441 + MAX98357A | No | Yes (two I2S buses) |
| PDM microphone + I2S speaker | No | Yes (different protocols) |
| Need true full-duplex | Yes | Limited |
| USB audio | No | No (use different component) |

## Technical Notes

- **Sample Format**: 16-bit stereo (32 bits per frame)
- **DMA Buffers**: 8 buffers x 1024 bytes for smooth streaming
- **Task Priority**: 9 (below WiFi/BLE at 18)
- **Core Affinity**: Pinned to Core 1 to avoid WiFi interference

## Troubleshooting

### No Audio Output
1. Check MCLK connection (many codecs require it)
2. Verify codec I2C initialization (check logs)
3. Ensure speaker amp is enabled (GPIO control if applicable)

### Audio Crackling
1. Reduce sample_rate (try 8000 or 16000)
2. Check for WiFi interference (pin to Core 1)
3. Verify PSRAM is working if using AEC

### Echo Issues
1. Link esp_aec component via aec_id
2. Increase AEC filter_length (4-6 recommended)
3. Verify speaker reference is being captured

### Mic Not Working
1. Check din_pin wiring
2. Verify codec ADC is configured via I2C
3. Try increasing mic_gain

## Compared to Standard ESPHome

| Feature | i2s_audio_duplex | Standard i2s_audio |
|---------|-----------------|-------------------|
| Single-bus codecs | Full support | Half-duplex only |
| AEC integration | Built-in | Requires custom code |
| Simultaneous I/O | Native | Shared bus conflicts |
| Memory usage | ~8KB buffers | ~4KB per direction |

## License

MIT License
