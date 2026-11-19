# ESP32-P4 PPA build backport

This component backports two Espressif fixes to the Pixel Processing Accelerator
(PPA) driver in ESP-IDF v5.5.6 and v6.0.3.

- The first fix coordinates PPA engine access across cores and clears stale
  completion signals before starting a blocking operation.
  [Espressif engine fix](https://github.com/espressif/esp-idf/commit/9da8ff53f751f6682f69897d886f3a9946deed31)
- The DIG-734 fix uses the rotated rectangle to calculate the last DMA block.
  When that block holds less than the 1,536-bit FIFO, the driver bypasses its
  usual macroblock ordering so the DMA transfer can signal completion. The
  patch comes from the release branch corresponding to each SDK.
  [ESP-IDF 5.5 fix](https://github.com/espressif/esp-idf/commit/e106b16b9adf5af60785a3ee8032cf6e877edfe4),
  [ESP-IDF 6.0 fix](https://github.com/espressif/esp-idf/commit/8e4e73b0ddd98f2423490c45423d54fe2370d576)

A P4 watchdog dump taken when tapping Hang Up during a video call showed the
display task waiting on a 228 x 68 RGB565 redraw at scale 1 and 90 degrees.
For this rectangle, the released drivers calculate a 2,304-bit final block.
The fix accounts for rotation and calculates 256 bits, enabling the bypass.
The regression test compiles the geometry decision from each SDK and from
the source selected by the build hook. It also checks the unrotated redraw
and the concurrent 800 x 800 camera operation.

On an ESP32-P4 running IDF 5.5.6, tapping Hang Up during a two-way JPEG video
call triggered a watchdog reset. The core dump showed the display waiting for a
228 by 68 RGB565 rectangle rotated 90 degrees. In one repeat with the fix, the
same tap after 60 seconds of video ended the call. The device returned to idle
and the SIP peer acknowledged the hangup. The display completed all 627
updates recorded during that call.

The CMake hook identifies the SDK sources and patches by their SHA-256 hashes
in `p4_ppa_backport.json`. For a supported build that needs a fix, it copies
four C files and their private header into the build directory, applies the
missing patches and verifies the final hashes. The existing `esp_driver_ppa`
target compiles those sources with its original settings and public headers.
If both fixes are already present, it uses the SDK directly. An unrecognized
source that needs patching stops configuration.

Enable the component on ESP32-P4 using ESP-IDF. Builds require Git.

```yaml
p4_ppa_backport:
```
