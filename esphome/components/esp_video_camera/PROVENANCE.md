# Component provenance

The working `esp_video_camera` baseline in this tree was initially shared by
GitHub user `Psix-anp` as `esp_video_camera.zip` in:

https://github.com/n-IA-hane/esphome-intercom/discussions/18#discussioncomment-17787290

The contributor now maintains the standalone component and its release history
at:

https://github.com/Psix-anp/esphome-esp-video-camera

The initial attachment was described by its author as an "AI fork component".
It keeps `@youkorr` as its ESPHome code owner and is derived from the ESPHome
camera work proposed in:

https://github.com/esphome/esphome/pull/16944

Local changes made after importing that snapshot are limited to integration,
hardware qualification and fixes needed by this project's P4 firmware. They
have been split into reviewable upstream pull requests:

https://github.com/Psix-anp/esphome-esp-video-camera/pulls

Keep the local snapshot until those changes are merged and the resulting
upstream revision is requalified on the P4. The upstream repository carries the
standard ESPHome split license: MIT for Python and other non-runtime files,
GPLv3 for C and C++ runtime code.

The local P4 SIP-video integration also adds a single synchronous borrowed
RGB565-frame consumer. It fans the already transformed camera frame into an
optional compile-time H.264 source without opening the sensor twice. The
existing JPEG handoff still allocates one owned copy because ESPHome's
`CameraImage` assumes ownership and frees that buffer; safely pooling those
copies would require a camera API ownership change rather than a local reuse
shortcut.

Do not remove this provenance while the project carries the local snapshot.
Future camera changes should be proposed to the contributor's repository first,
then consumed here from a pinned, hardware-qualified upstream revision.
