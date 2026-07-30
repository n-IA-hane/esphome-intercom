# Component provenance

The working `esp_video_camera` baseline in this tree was initially shared by
GitHub user `Psix-anp` as `esp_video_camera.zip` in:

https://github.com/n-IA-hane/esphome-intercom/discussions/18#discussioncomment-17787290

The attachment was described by its author as an "AI fork component". It keeps
`@youkorr` as its ESPHome code owner and is derived from the ESPHome camera work
proposed in:

https://github.com/esphome/esphome/pull/16944

Local changes made after importing that snapshot are limited to integration,
hardware qualification and fixes needed by this project's P4 firmware. Do not
remove this provenance when moving the component to a standalone repository.

The local P4 SIP-video integration also adds a single synchronous borrowed
RGB565-frame consumer. It fans the already transformed camera frame into an
optional compile-time H.264 source without opening the sensor twice. The
existing JPEG handoff still allocates one owned copy because ESPHome's
`CameraImage` assumes ownership and frees that buffer; safely pooling those
copies would require a camera API ownership change rather than a local reuse
shortcut.

The discussion attachment did not contain a standalone license or its own Git
history. Before publishing this component independently, confirm its licensing
and history with the contributor and prefer contributing through a repository
maintained by them.
