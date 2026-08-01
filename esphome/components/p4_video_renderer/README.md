# P4 video renderer

`p4_video_renderer` is the encoded video sink for ESP32-P4 videophone
profiles. JPEG uses the P4 hardware decoder. H.264 uses Espressif tinyH264,
`esp_image_effects` and PPA before direct display presentation.

## Example

```yaml
p4_video_renderer:
  id: p4_video
  codec: h264
  width: 352
  height: 288
  framerate: 10
  max_decode_width: 352
  max_decode_height: 288
  display_id: main_display
  display_rotation: 270
  on_first_frame:
    - script.execute: show_video_page
  on_video_ended:
    - script.execute: show_phone_page
```

## Options

| Option | Meaning |
| --- | --- |
| `id` | Encoded sink ID. |
| `codec` | Required `jpeg` or `h264`. It must match `voip_stack.video.codec`. |
| `width` / `height` | Preferred receive geometry advertised in SDP. |
| `framerate` | Preferred receive frame rate. |
| `max_decode_width` / `max_decode_height` | Hard decode and surface allocation bounds. |
| `task_stacks_in_psram` | Place supported renderer task stacks in PSRAM. Frame buffers already use PSRAM. |
| `display_id` | Direct display used for H.264 presentation. Required when `codec: h264`. |
| `display_rotation` | Direct display rotation: 0, 90, 180 or 270 degrees. |
| `on_first_frame` | Runs when the first remote frame becomes presentable. |
| `on_video_ended` | Runs after the active remote video stream ends. |

H.264 geometry is derived once from the actual display area and validated again
before presentation. A changed or incompatible surface is dropped rather than
drawn with the wrong stride. JPEG and H.264 decoders, buffers and managed
libraries are selected at compile time and do not coexist in shipped profiles.
