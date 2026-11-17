# Round text

`lvgl_round_text` owns reusable text buffers for the ten visible thinking rows,
ten reply rows and reply page counter. The maintained round packages and the
first-generation Waveshare package use this same component. Their shared text
package follows the Voice feature: Music and Sendspin use other labels. Removing
Voice also removes this owner, its callbacks, header and cache preallocation.

```yaml
external_components:
  - source: ${ext_components_source}
    components: [lvgl_round_text]
lvgl_round_text:
  id: round_text
  buffers_in_psram: true
  debug: false
```

`buffers_in_psram` defaults to true. ESPHome's allocator prefers PSRAM when
available and falls back to internal RAM. False selects internal RAM. Setup
preallocates caches and disables the component loop. Existing source callbacks
supply text and bind the boot-created widgets; the existing script still owns
its 14-second pagination timer.

The wrapper accepts the existing widget arrays, fonts and chord widths through
`bind_thinking`, `bind_reply`, `set_thinking_font`, `set_reply_font`, `set_thinking`,
`set_reply` and `show_reply`. Text updates return false if storage cannot be
prepared, preserving the previous valid reply, visible labels and memory
footprint. Advance a
page index only after `show_reply` succeeds. `clear` detaches LVGL's borrowed
pointers before releasing oversized storage, while retaining normal caches.

| Cache | Initial retained capacity | Maximum retained capacity |
|---|---:|---:|
| Normalized reply arena | 2 KiB | 4 KiB |
| Reply intervals | 64 lines | 128 lines |
| Transaction/thinking scratch | 128 bytes | 1 KiB |
| Each of 20 visible row buffers | 64 bytes | 256 bytes |
| Page counter | 64 bytes | 64 bytes |

Caches grow by doubling. Larger responses use temporary overflow storage for
that response, with the same wrapping and complete long words, then release it
at clear. The scratch stages normalized input and intervals during an update;
a committed reply has one normalized arena and one interval table. No word
vector or candidate string is retained or allocated for each word.

Wrapping preserves the byte-length estimate before exact font measurement,
space/LF/CR/TAB separators, complete clipped words, thinking's starting row,
reply safety limit, empty-row padding and ten rows per page. The first-generation
profile passes its original chord count and integer width type. With
`LV_USE_ARABIC_PERSIAN_CHARS`, thinking retains LVGL's dynamic setter so shaping
keeps its original behavior; reply rows use persistent static buffers.

`debug: true` compiles allocation, copied-byte, font-measurement and label-update
counters. They are absent otherwise. No task, timer or polling owner is added.

The host oracle uses identified official LVGL 9.5 and real font measurements:

```sh
LVGL_HOST_SOURCE=/path/to/managed_components/lvgl__lvgl \
LVGL_HOST_BUILD_ROOT=/var/tmp/voip-builds/round-text \
python -m pytest tests/test_lvgl_round_text_runtime.py \
  --basetemp=/var/tmp/voip-builds/round-text-pytest
```

It compares the extracted previous algorithm, plain and Arabic/Persian builds,
UTF-8, whitespace, long/oversized text, pagination, clear/reuse, borrowed-pointer
updates, allocation failure and work after warmup. Public profile generation
is covered separately by `test_lvgl_round_text_config.py`. Host evidence does
not establish physical rendering, ESP CPU time or device memory savings.
