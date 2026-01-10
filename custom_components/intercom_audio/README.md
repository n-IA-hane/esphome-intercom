# Intercom Audio - UDP Audio Streaming for ESPHome

Real-time UDP-based audio streaming component for ESP32 devices. Enables device-to-device and device-to-server audio communication.

## Features

- **UDP Streaming**: Low-latency audio over WiFi
- **Multiple Modes**: Full-duplex, TX-only, RX-only
- **Flexible Audio Sources**: Works with i2s_audio_duplex, standard mic, or speaker
- **AEC Support**: Optional echo cancellation integration
- **Dynamic Endpoints**: Runtime-configurable remote IP/port
- **Jitter Buffer**: Smooth playback despite network variations
- **Packet Statistics**: TX/RX counters for monitoring
- **ESPHome Actions**: Start/stop via automations

## Use Cases

This component works for many audio streaming applications:

- **Intercom Systems**: ESP-to-ESP or ESP-to-Server calls
- **Baby Monitors**: One-way audio with optional talk-back
- **Surveillance**: Audio monitoring from cameras/sensors
- **Public Address**: Send announcements to ESP speakers
- **Voice Logging**: Stream audio to recording server
- **Home Automation**: Voice triggers and responses
- **Accessibility**: Remote audio for hearing assistance

## Installation

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/n-IA-hane/esphome-intercom
      ref: main
    components: [intercom_audio]
```

## Operating Modes

### Mode 1: Full Duplex with i2s_audio_duplex
Best for codecs like ES8311 that share one I2S bus.

```yaml
i2s_audio_duplex:
  id: i2s_duplex
  # ... pins config

intercom_audio:
  id: intercom
  duplex_id: i2s_duplex
  listen_port: 12346
  remote_ip: "192.168.1.100"
```

### Mode 2: Full Duplex with Separate Mic + Speaker
For setups with separate I2S buses (INMP441 + MAX98357A).

```yaml
microphone:
  - platform: i2s_audio
    id: mic
    # ... config

speaker:
  - platform: i2s_audio
    id: spk
    # ... config

intercom_audio:
  id: intercom
  microphone_id: mic
  speaker_id: spk
  listen_port: 12346
  remote_ip: "192.168.1.100"
```

### Mode 3: TX Only (Microphone Only)
For devices that only send audio (baby monitor, surveillance mic).

```yaml
microphone:
  - platform: i2s_audio
    id: mic
    # ... config

intercom_audio:
  id: intercom
  microphone_id: mic        # Only microphone, no speaker
  listen_port: 12346
  remote_ip: "192.168.1.100"
```

### Mode 4: RX Only (Speaker Only)
For devices that only receive audio (announcement speakers, PA systems).

```yaml
speaker:
  - platform: i2s_audio
    id: spk
    # ... config

intercom_audio:
  id: intercom
  speaker_id: spk           # Only speaker, no microphone
  listen_port: 12346
  remote_ip: "192.168.1.100"
```

## Configuration

```yaml
intercom_audio:
  id: intercom
  duplex_id: i2s_duplex           # OR microphone_id + speaker_id
  aec_id: aec_component           # Optional: echo cancellation
  listen_port: 12346              # UDP port to receive audio
  remote_ip: "192.168.1.100"      # Destination IP (static or lambda)
  remote_port: 12346              # Destination port
  buffer_size: 8192               # Jitter buffer size in bytes
  prebuffer_size: 2048            # Bytes to buffer before playback
  on_start:                       # Triggered when streaming starts
    - logger.log: "Streaming started"
  on_stop:                        # Triggered when streaming stops
    - logger.log: "Streaming stopped"
```

## Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | ID | Required | Component ID |
| `duplex_id` | ID | - | i2s_audio_duplex for single-bus codecs |
| `microphone_id` | ID | - | Standard microphone component |
| `speaker_id` | ID | - | Standard speaker component |
| `aec_id` | ID | - | Optional esp_aec for echo cancellation |
| `listen_port` | int | 12346 | UDP port to listen on (1024-65535) |
| `remote_ip` | string/lambda | "" | Remote device IP address |
| `remote_port` | int/lambda | 12346 | Remote device port (1024-65535) |
| `buffer_size` | int | 8192 | Jitter buffer size (min 2048) |
| `prebuffer_size` | int | 2048 | Pre-buffer before playback (< buffer_size) |
| `on_start` | automation | - | Actions when streaming starts |
| `on_stop` | automation | - | Actions when streaming stops |

## Built-in Sensors

```yaml
sensor:
  - platform: intercom_audio
    intercom_audio_id: intercom
    tx_packets:
      name: "TX Packets"
    rx_packets:
      name: "RX Packets"
    buffer_fill:
      name: "Buffer Fill"

text_sensor:
  - platform: intercom_audio
    intercom_audio_id: intercom
    mode:
      name: "Audio Mode"    # "Full Duplex", "TX Only", "RX Only"

switch:
  - platform: intercom_audio
    intercom_audio_id: intercom
    streaming:
      name: "Streaming"     # Control streaming on/off
    aec:
      name: "Echo Cancellation"
```

## ESPHome Actions

### Start Streaming
```yaml
button:
  - platform: template
    name: "Start Call"
    on_press:
      - intercom_audio.start:
          id: intercom
          remote_ip: "192.168.1.100"
          remote_port: 12346
```

### Stop Streaming
```yaml
button:
  - platform: template
    name: "End Call"
    on_press:
      - intercom_audio.stop:
          id: intercom
```

### Reset Counters
```yaml
button:
  - platform: template
    name: "Reset Stats"
    on_press:
      - intercom_audio.reset_counters:
          id: intercom
```

## Dynamic Remote IP/Port

The remote endpoint can be set dynamically using lambdas:

```yaml
text_sensor:
  - platform: template
    id: target_ip
    name: "Target IP"

number:
  - platform: template
    id: target_port
    name: "Target Port"
    min_value: 1024
    max_value: 65535

intercom_audio:
  id: intercom
  duplex_id: i2s_duplex
  listen_port: 12346
  remote_ip: !lambda 'return id(target_ip).state;'
  remote_port: !lambda 'return (uint16_t)id(target_port).state;'
```

## Lambda Access

```cpp
// Start streaming to specific endpoint
id(intercom).start("192.168.1.100", 12346);

// Start using configured lambda values
id(intercom).start();

// Stop streaming
id(intercom).stop();

// Check state
bool streaming = id(intercom).is_streaming();
auto state = id(intercom).get_state();  // IDLE, STARTING, STREAMING, STOPPING

// Get statistics
uint32_t tx = id(intercom).get_tx_packets();
uint32_t rx = id(intercom).get_rx_packets();
size_t buffer = id(intercom).get_buffer_fill();

// Reset counters
id(intercom).reset_counters();

// Get mode string
const char* mode = id(intercom).get_mode_str();  // "Full Duplex", "TX Only", etc.

// Volume and gain control
id(intercom).set_volume(0.8f);
id(intercom).set_mic_gain(4);  // Gain for 32→16 bit conversion

// AEC control
id(intercom).set_aec_enabled(true);
bool aec_on = id(intercom).is_aec_enabled();
```

## Complete Example: Baby Monitor (TX Only)

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/n-IA-hane/esphome-intercom
      ref: main
    components: [intercom_audio]

i2s_audio:
  - id: i2s_mic
    i2s_lrclk_pin: GPIO3
    i2s_bclk_pin: GPIO2

microphone:
  - platform: i2s_audio
    id: baby_mic
    i2s_audio_id: i2s_mic
    i2s_din_pin: GPIO4
    adc_type: external
    bits_per_sample: 32bit
    channel: left

intercom_audio:
  id: baby_monitor
  microphone_id: baby_mic
  listen_port: 12346
  remote_ip: "192.168.1.100"    # Parent unit IP
  remote_port: 12346
  on_start:
    - light.turn_on:
        id: status_led
        red: 0%
        green: 100%
        blue: 0%
  on_stop:
    - light.turn_off: status_led

switch:
  - platform: intercom_audio
    intercom_audio_id: baby_monitor
    streaming:
      name: "Baby Monitor Active"

button:
  - platform: template
    name: "Start Monitoring"
    on_press:
      - intercom_audio.start:
          id: baby_monitor
```

## Complete Example: Announcement Speaker (RX Only)

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/n-IA-hane/esphome-intercom
      ref: main
    components: [intercom_audio]

i2s_audio:
  - id: i2s_spk
    i2s_lrclk_pin: GPIO6
    i2s_bclk_pin: GPIO7

speaker:
  - platform: i2s_audio
    id: pa_speaker
    i2s_audio_id: i2s_spk
    i2s_dout_pin: GPIO8
    dac_type: external

intercom_audio:
  id: pa_system
  speaker_id: pa_speaker
  listen_port: 12346           # Listens for incoming audio
  buffer_size: 16384           # Larger buffer for smoother playback
  prebuffer_size: 4096
  on_start:
    - output.turn_on: amp_enable
  on_stop:
    - output.turn_off: amp_enable

output:
  - platform: gpio
    id: amp_enable
    pin: GPIO46

sensor:
  - platform: intercom_audio
    intercom_audio_id: pa_system
    rx_packets:
      name: "Received Packets"
    buffer_fill:
      name: "Buffer Level"
```

## Complete Example: Full Intercom System

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/n-IA-hane/esphome-intercom
      ref: main
    components: [intercom_audio, i2s_audio_duplex, esp_aec, mdns_discovery]

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

mdns_discovery:
  id: discovery
  service_type: "_intercom._udp"
  scan_interval: 30s

intercom_audio:
  id: intercom
  duplex_id: i2s_duplex
  aec_id: aec
  listen_port: 12346
  remote_ip: !lambda 'return id(selected_ip).state;'
  remote_port: !lambda 'return (uint16_t)id(selected_port).state;'
  buffer_size: 8192
  prebuffer_size: 2048
  on_start:
    - output.turn_on: speaker_enable
    - light.turn_on:
        id: status_led
        red: 0%
        green: 100%
        blue: 0%
  on_stop:
    - output.turn_off: speaker_enable
    - light.turn_off: status_led

text_sensor:
  - platform: template
    id: selected_ip
    name: "Selected IP"

number:
  - platform: template
    id: selected_port
    name: "Selected Port"
    min_value: 1024
    max_value: 65535
    initial_value: 12346
```

## Protocol Details

### Audio Format
- **Sample Rate**: Configurable (8000, 16000, 32000, 48000 Hz)
- **Bit Depth**: 16-bit signed PCM
- **Channels**: Mono
- **Packet Size**: ~512 bytes (256 samples)
- **Packets/Second**: ~62 at 16kHz

### Network
- **Protocol**: UDP (connectionless, low latency)
- **Port Range**: 1024-65535 (unprivileged)
- **Bandwidth**: ~128 kbps at 16kHz mono

## Troubleshooting

### No Audio Received
1. Check firewall allows UDP on listen_port
2. Verify remote_ip is reachable (ping)
3. Check both devices use same port
4. Monitor RX packet counter - should increase

### Audio Choppy/Delayed
1. Increase buffer_size (try 16384)
2. Increase prebuffer_size (try 4096)
3. Check WiFi signal strength
4. Reduce sample_rate if bandwidth limited

### Echo Issues
1. Add esp_aec component and link via aec_id
2. Increase AEC filter_length
3. Reduce speaker volume

### One-Way Audio
1. Verify both devices have correct remote_ip pointing to each other
2. Check firewall on both sides
3. Confirm mode supports bidirectional (need mic+speaker or duplex)

### High CPU Usage
1. Lower sample_rate to 8000 or 16000
2. Disable AEC if not needed
3. Check for other tasks competing for CPU

## Performance Notes

- **Latency**: ~50-100ms typical (buffer + network)
- **CPU**: 5-15% depending on sample rate and AEC
- **Memory**: ~20KB for buffers and task stack
- **Task Priority**: 9 (runs on Core 1 to avoid WiFi conflicts)

## Validation Rules

- `prebuffer_size` must be less than `buffer_size`
- `buffer_size` minimum is 2048 bytes
- Port must be 1024-65535
- Must have at least one audio source (duplex, mic, or speaker)
- Cannot mix `duplex_id` with `microphone_id`/`speaker_id`

## License

MIT License
