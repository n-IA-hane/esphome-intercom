# ESP H.264 video source

`esp_h264_video_source` converts raw ESP32-P4 CSI frames into H.264 access
units. PPA performs crop, rotation and scaling in hardware. Espressif
`esp_h264` 1.3.6 performs the hardware encode.

## Example

```yaml
esp_video_camera:
  id: p4_camera
  i2c_id: internal_i2c
  device: csi
  resolution: 800x800
  max_framerate: 25
  rotation: 270

esp_h264_video_source:
  id: p4_h264_video
  camera_id: p4_camera
  width: 400
  height: 400
  framerate: 10
  bitrate: 800000
  gop: 10
```

## Options

| Option | Meaning |
| --- | --- |
| `id` | Encoded source ID. |
| `camera_id` | Required raw `esp_video_camera` using `device: csi`. |
| `width` / `height` | Encoder output geometry. Both must be multiples of 16. |
| `framerate` | Target encoded frame rate, default 10. |
| `bitrate` | Target encoder bitrate in bits per second. |
| `gop` | Distance between key frames, default 30. |

The source probes the hardware path during setup and reuses its PPA client,
YUV and encoded buffers during calls. Managed H.264 libraries and their large
buffers are included only when this component and `codec: h264` are selected.
