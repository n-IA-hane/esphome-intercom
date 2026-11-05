# ESP audio codecs, rates and packet times

The maintained FULL packages declare PCM explicitly. You do not need to change
anything to use them with VoIP Stack in Home Assistant. Optional codecs are
compiled only when they are declared in `voip_stack.audio.codecs`.

## The FULL PCM defaults

The FULL package already supplies this block. It documents the defaults; adding
a second `codec: pcm` entry to that package is rejected. When composing a custom
package, edit its existing PCM declaration. The G.722 example below adds a new
codec alongside the supplied PCM declaration.

```yaml
voip_stack:
  audio:
    codecs:
      - codec: pcm
        tx:
          # Fewer RTP packets than 10 ms, with the same microphone sample rate.
          - {sample_rate: 16000, frame_ms: 20}
          - {sample_rate: 16000, frame_ms: 10}
        rx:
          # Prefer 48 kHz from HA. At 20 ms this would exceed the payload budget.
          - {sample_rate: 48000, frame_ms: 10}
          # Common PCM alternatives for compatible phones.
          - {sample_rate: 16000, frame_ms: 20}
          - {sample_rate: 16000, frame_ms: 10}
```

Lists express preferences, followed by explicitly supported alternatives.
Home Assistant should send 48 kHz/10 ms toward this ESP and receive
16 kHz/20 ms from it when both sides support that profile. The 16 kHz receive
alternatives allow a compatible peer to avoid unnecessary transcoding.

PCM mono S16 produces 960 audio bytes at 48 kHz/10 ms, 1920 bytes at
48 kHz/20 ms, and 640 bytes at 16 kHz/20 ms. The default RTP payload budget is
1200 bytes. Keeping TX at 16 kHz/20 ms reduces its packet rate from 100 to 50 packets
per second. It does not require changing the microphone, AFE or mixer cadence.

TX and RX packet times are independent. SDP `ptime` describes the duration the
sender of that SDP prefers to receive; it does not impose an identical duration
in both directions. A remote `ptime` guides ESP transmission among its declared
formats; a remote `maxptime` limits the ESP's transmit packet duration.
Advertised capabilities stay within the configured RTP payload budget, which
defaults to 1200 bytes. The advertised `maxptime` respects every receive mapping
on the audio m-line, including alternatives.
See [RFC 3264, section 6.1](https://www.rfc-editor.org/rfc/rfc3264.html#section-6.1).

One audio m-line has one shared `maxptime`. When it advertises both 48 kHz PCM
at 10 ms and another receive codec, that codec also needs a declared receive
duration within the shared limit. The G.722 example includes its 10 ms RX
alternative for this reason. TX can still use 20 ms when the peer accepts it.
An unrepresentable receive set is rejected with an SDP diagnostic.

## Add G.722 for a conventional PBX

PCM remains the default. For a PBX that permits G.722, this adds it alongside
the package's PCM formats and can be used with an optional
[registered account](ESP_REGISTRATION.md):

```yaml
voip_stack:
  audio:
    codecs:
      - codec: g722
        tx:
          - {sample_rate: 16000, frame_ms: 20}
          - {sample_rate: 16000, frame_ms: 10}
        rx:
          - {sample_rate: 16000, frame_ms: 20}
          - {sample_rate: 16000, frame_ms: 10}
```

Enable G.722 on the PBX account too. G.722 provides wideband mono audio from
16 kHz PCM. Its standard RTP clock is 8 kHz; the component handles that distinction.
The supported RTP profile uses 64 kbit/s. See
[RFC 3551, section 4.5.2](https://www.rfc-editor.org/rfc/rfc3551.html#section-4.5.2).

The ESP backend uses Espressif `esp_audio_codec` 2.5.0 through its direct G.722
APIs. This version supports early P4 profiles. Do not raise a board's minimum
chip revision merely to satisfy a newer codec package.

## Opus

Maintained S3 VoIP-only profiles already use Opus.

The [WS3 LCD BOX V2 telephone profile](../yamls/voip-only/single-bus/waveshare-s3-touch-lcd-1.85c-box-v2-voip.yaml)
supplies this configuration together with the phone interface, directory, BLF
and MWI. The headless S3 Audio board has its own
[VoIP-only profile](../yamls/voip-only/single-bus/waveshare-s3-voip.yaml).

The following describes an asymmetric local PCM boundary with 20 ms packets:

```yaml
voip_stack:
  audio:
    codecs:
      - codec: opus
        tx:
          - {sample_rate: 16000, frame_ms: 20}
        rx:
          - {sample_rate: 48000, frame_ms: 20}
```

Opus accepts PCM rates of 8, 12, 16, 24 or 48 kHz. Supported packet times are 10
and 20 ms. Its RTP announcement is always `opus/48000/2`, including mono use;
these values do not describe the microphone's actual rate and channels.
See [RFC 7587, section 7](https://www.rfc-editor.org/rfc/rfc7587.html#section-7).

Opus remains optional and requires PSRAM. This candidate's validator accepts it
only on supported S3 VoIP-only profiles and rejects FULL/P4 configurations.
Qualification for those profiles is pending. The commented FULL examples show
the proposed schema; they cannot currently be enabled. A VoIP-only success does
not establish safe coexistence with Assist, wake words, music and video.

## Configuration and familiar PBX names

Each codec has its own ordered `tx` and `rx` lists. `sample_rate` and `frame_ms`
are explicit. `pcm_format` defaults to `s16le` and `channels` to 1. They can be
written explicitly when the hardware and codec support other values. A
microphone-only or speaker-only profile can declare just its applicable direction.

| ESP declaration | PCM rate | RTP clock | Familiar PBX name |
|---|---:|---:|---|
| `pcm`, 16000 Hz, 20 ms, S16 | 16 kHz | 16 kHz | FreeSWITCH `L16@16000h@20i`, Asterisk `slin16` |
| `g722`, 16000 Hz, 20 ms | 16 kHz | 8 kHz | FreeSWITCH `G722@20i`, Asterisk `g722` |
| `opus`, 16000 Hz TX PCM, 20 ms | 16 kHz input | 48 kHz | `opus`; SDP `opus/48000/2` |

FreeSWITCH's `i` suffix is the packet interval in milliseconds. Its `h` suffix
is a codec frequency selector, not a universal definition of RTP clock rate.
These PBX strings are documentation equivalents; ESP YAML uses structured fields.

G.722 is a useful optional choice when the PBX permits it. A route that only
allows G.711 needs PBX-side transcoding to an ESP codec that is actually declared.
Registration success alone does not prove there is a common media codec.

## Migrate an existing custom YAML

Replace `audio.tx`, `audio.rx`, `tx_formats` and `rx_formats` with
`audio.codecs`. Move each primary format and its alternatives into the applicable
codec's ordered direction list and remove `codec` from individual format entries.
Declare each codec once. The old syntax is rejected with migration instructions.

No codec or alternate format is inserted implicitly. A packet duration is not
an additional codec payload mapping. Formats outside the configured payload or
resource budget are rejected rather than silently replaced with another codec.
