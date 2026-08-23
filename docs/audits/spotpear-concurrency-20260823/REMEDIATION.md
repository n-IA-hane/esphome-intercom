# Spotpear concurrency remediation

Date: 2026-08-23

## Root cause

The failure was caused by the ESPHome 2026.8 SPI ESP-IDF adapter passing LVGL
display buffers in PSRAM without `SPI_TRANS_DMA_USE_PSRAM`. ESP-IDF 5.5 then
allocated and freed an internal DMA bounce buffer for every 4092 byte display
block. During Sendspin plus VoIP plus TTS, those repeated internal allocations
exhausted and fragmented the DMA heap until SPI transfers failed.

This was confirmed from the ESP-IDF 5.5 `setup_dma_priv_buffer()` path and from
real Spotpear serial evidence. It was not an audio codec, SIP, AEC, AFE or MWW
ownership defect.

## Fix

- The local SPI adapter uses `SPI_TRANS_DMA_USE_PSRAM` for external buffers.
- Aligned transfers use 4032 byte chunks, the largest 64 byte multiple below
  the IDF transaction limit.
- Aligned buffers also use `SPI_TRANS_DMA_BUFFER_ALIGN_MANUAL`, preventing IDF
  from allocating a private bounce buffer.
- Spotpear preallocates two 32 KiB HTTP rings, uses a 64 KiB Sendspin ring and
  places the two 12 KiB VoIP audio task stacks in PSRAM.
- MWW, Sendspin, TTS, VoIP, AFE and AEC remain enabled.
- WS3 and P4 do not load the Spotpear SPI fork.

## Exact candidate

```text
esphome-intercom:           ce20b60c52339b24c60a6624c03cfebb1142e1c6
esphome-voip-stack:         8021412f590d5dd9692456dd696cc3f5b257c849
esphome-audio-stack:        07041f51b1b63929f84c75d4a216ea117f9b1b68
esphome-runtime-controller: a41b84d871b4f6ef7a8bd8b02190ff647460b83f
ESPHome:                    2026.8.0
ESP-IDF:                    5.5.5
```

## Real hardware results

Spotpear was tested with Sendspin music playing while the qualification runner
established one call in each direction between Spotpear and WS3. A real FLAC
TTS stream was injected during each call. The final promoted configuration
reported two in-call transitions, two cleanup transitions to idle and two FLAC
streams. Serial contained no SPI transmit error, invalid argument, watchdog,
panic or reset.

The same source fix had already completed eight calls and seven TTS streams in
the preceding repeated candidate runs with no SPI errors. A JTAG peak snapshot
also showed MWW, AEC, audio, VoIP, mixer, resampler and decoder tasks present
during the concurrent load.

WS3 was compiled, uploaded by OTA and passed the direct bidirectional call
matrix after reboot. Device volumes were kept at 1 percent and music was
stopped after every scenario.

## Compile gates

```text
Spotpear full AFE:
  image 7590939 bytes, DIRAM 179035 bytes, app slot 93.4 percent used

WS3 full AFE:
  image 3432755 bytes, DIRAM 165863 bytes, app slot 42.2 percent used

P4 full landscape JPEG:
  image 13968452 bytes, DIRAM 174764 bytes, app slot 84.6 percent used

P4 VoIP-only H.264:
  image 3987056 bytes, DIRAM 173820 bytes, app slot 24.1 percent used
```

P4 was compile-gated only because no P4 hardware was available. No current P4
runtime or presentation claim is made by this remediation.

## Software gates

```text
software-full: 1606 passed, 4 deselected, 138 subtests passed
Home Assistant: 77 passed
peer suites: 179 passed, 9 subtests passed
JavaScript runtime: 14 passed, 1596 deselected
```

One host timing assertion initially measured 11.14 ms against a 10 ms limit.
The isolated witness passed five consecutive runs and the full rerun passed.
No threshold or production code was changed.

## Distribution status

All changes are committed locally. Nothing was pushed, tagged, released or
posted externally. The maintained YAML continues to point to `@dev`; remote
compilation will remain incomplete until the coordinated local commits are
published in dependency order with explicit user authorization.
