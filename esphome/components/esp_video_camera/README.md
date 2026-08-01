# ESP video camera

`esp_video_camera` exposes Espressif's `esp_video` V4L2 pipeline as a native
ESPHome camera entity and as a borrowed-frame source for the P4 SIP video
adapters. The maintained CSI path targets ESP32-P4. USB UVC can be enabled
explicitly for supported ESP-IDF builds.

The component uses managed Espressif components. Camera sensor drivers, ISP,
IPA tuning and the hardware JPEG device are not reimplemented here. See
[PROVENANCE.md](PROVENANCE.md) for origin and attribution.

## Example

```yaml
esp_video_camera:
  id: p4_camera
  name: P4 Camera
  i2c_id: internal_i2c
  device: jpeg
  resolution: 800x800
  jpeg_quality: 10
  max_framerate: 15
  rotation: 270
```

Use `device: jpeg` for the native camera entity and the JPEG SIP source. Use
`device: csi` for raw frames consumed by `esp_h264_video_source`.

## Options

| Option | Meaning |
| --- | --- |
| `id` | Component and camera entity ID. |
| `i2c_id` | Required SCCB/I2C bus used by the sensor. |
| `device` | `jpeg`, `csi`, `uvc`, `uvc0` through `uvc9`, or a `/dev/videoN` path. Default `jpeg`. |
| `resolution` | `auto`, `QVGA`, `VGA`, `480P`, `720P`, `1080P`, or `WIDTHxHEIGHT`. |
| `jpeg_quality` | Hardware JPEG quality from 1 through 63. |
| `max_framerate` | Capture limit from 0.1 through 60 frames per second. |
| `rotation` | 0, 90, 180 or 270 degrees. Compressed UVC frames cannot be rotated here. |
| `xclk_pin` / `xclk_frequency` | Optional sensor clock pin and frequency. |
| `enable_xclk` | Let this component initialize XCLK. |
| `enable_uvc` | Include the Espressif USB UVC host dependency and device. |

## Frame ownership

CSI and JPEG consumers borrow one V4L2/MMAP frame during their synchronous
callback. They must finish reading it before returning. The capture task sleeps
when no camera or SIP consumer is active. H.264 and JPEG profiles use different
device modes and must not share one firmware.
