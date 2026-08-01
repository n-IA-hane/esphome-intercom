# ESP JPEG video source

`esp_jpeg_video_source` adapts complete JPEG frames from
`esp_video_camera` to the encoded-video interface owned by `voip_stack`. It
does not decode, resize or re-encode a frame.

## Example

```yaml
esp_jpeg_video_source:
  id: p4_jpeg_video
  camera_id: p4_camera
  width: 800
  height: 800
  framerate: 15

voip_stack:
  video:
    codec: jpeg
    source: p4_jpeg_video
    sink: p4_video
```

## Options

| Option | Meaning |
| --- | --- |
| `id` | Encoded source ID. |
| `camera_id` | Required `esp_video_camera` using a JPEG-producing device. |
| `width` / `height` | Exact JPEG geometry advertised to SIP. Defaults to 400x400. |
| `framerate` | Maximum transmitted frame rate, default 10. |

The source keeps the camera frame borrowed only for the synchronous RTP copy.
`device: csi` is rejected because it exposes raw frames rather than JPEG access
units. Use `esp_h264_video_source` for that path.
