# What is new in 2026.9.0

`2026.9.0` is the stable release of the call-lifecycle, SIP interoperability and
ESP32-P4 videophone work developed after `2026.8.0`.

## One Home Assistant call model

Dashboard phones, ESPHome devices, registered SIP clients, trunks, forwarding,
ring groups and conferences now use one generation-owned call session with
explicit legs. Answer, bridge activation, media updates, termination and final
cleanup pass through shared primitives instead of route-specific call engines.

Public idle is reported only after call tasks, sockets, relays, transcoders and
RTP reservations are reusable. This prevents a completed call from leaving an
endpoint busy or racing an immediate redial.

## Standards-based SIP trunks and media

Provider identity, logical SIP domain, local Contact and outbound proxy are
kept as separate roles. Registered UDP trunks reuse the signaling socket and
source port that completed REGISTER for INVITE, authentication, ACK, in-dialog
requests and BYE.

Each bridge leg negotiates its own codec, payload, packet time and direction.
Compatible media remains direct. A bounded converter is used only when active
legs require different supported video formats. Audio remains established when
video is added, removed or rejected through re-INVITE.

## Home Assistant phone experience

Phonebook presence follows real SIP registrations and ESPHome availability,
while static contacts and browser phones retain their own availability rules.
DTMF follows the established bridge through RFC 4733 or SIP INFO. Browser
phones remember their preferred microphone, speaker and camera and can change
them without replacing the SIP call.

The card, editor, configuration, entities, repairs and service errors include
Brazilian Portuguese and German translations.

The card Options view now lets each browser or Companion app select its
preferred microphone, speaker and camera. It can also request media permission
and switch between the available cameras without changing the active SIP call.
Future releases will continue consolidating media-device discovery and
selection across browsers and mobile platforms.

![Home Assistant VoIP card media-device options](images/voip-card-media-device-options-2026-9-0.jpg)

## ESPHome P4 videophone

The ESP32-P4 is not only a network camera. It is a complete SIP videophone that
can send its camera and display the remote party on its own MIPI DSI panel while
bidirectional audio remains active.

The video path works in both directions:

- For outgoing video, `esp_video` owns the MIPI-CSI and V4L2 camera pipeline.
  The JPEG profile sends the camera's already encoded JPEG frame instead of
  capturing or encoding the same image twice. The H.264 profile uses the P4
  hardware encoder.
- For incoming video, the VoIP stack receives and reassembles RTP/JPEG or H.264
  frames, then passes complete frames to the codec-specific P4 renderer.
- JPEG is decoded by the P4 hardware JPEG engine. H.264 uses Espressif's
  software decoder. PPA performs the required scaling, rotation and
  pixel-format conversion without moving that work into the ESPHome main loop.
- The decoded image is presented on the physical MIPI DSI display through the
  maintained display adapter. Video presentation is paced by the media clock
  and uses bounded queues, persistent workers and reusable buffers, so an old
  or slow frame cannot build an unbounded backlog behind the live call.
- The video view and the normal LVGL interface have an explicit lifecycle. The
  call can enter video, return to audio-only and hang up without leaving a stale
  frame, damaged navigation controls or a hidden lower bar. The idle interface
  is restored after the call and is ready for an immediate redial.

Both video modes support a call that starts with audio and adds video later by
re-INVITE, as well as a call that offers audio and video from the initial
INVITE. Adding, removing or rejecting video does not restart the established
audio path. JPEG and H.264 remain separate compile-time choices, so the JPEG
firmware does not silently include the H.264 pipeline and vice versa.

The full JPEG profile combines the videophone with AFE, Micro Wake Word, Voice
Assistant, LVGL, TTS, HTTP media and Sendspin. H.264 remains the experimental
codec profile, while JPEG is the stable full-device baseline. The full profile
therefore remains a voice-assistant and media panel when idle, but becomes a
bidirectional audio and video phone during a SIP call.

Qualification covered the three independent layers required for a real P4
videophone: SIP/SDP negotiation, RTP and codec processing, and presentation on
the physical panel. The tested scenarios included initial video, audio-first
video activation, video removal, hangup, immediate redial and repeated calls.
The H.264 profile presented approximately 10 frames per second in its final
three-cycle hardware run. JPEG is the maintained full-profile baseline.

Camera work is maintained in the project fork of @Psix-anp's component and is
being proposed upstream as focused pull requests. Once the required changes
are accepted there, maintained YAMLs can point directly to that upstream
repository.

## Coordinated ESP components

Audio Stack, ESP VoIP Stack, Runtime Controller and the P4 camera component are
released together as `v2026.9.0`. Maintained YAMLs point to their stable `main`
branches. Project-owned ESPHome compatibility forks are documented in
[Espressif components and licenses](ESPRESSIF_COMPONENTS.md).

## Known issues

On some browsers and mobile devices, the operating system initially exposes
only a generic camera or a partial device list. Open the card Options and tap
`Allow media access` to grant permission and display all available
microphones, speakers and cameras.

## Upgrade

Read [Breaking changes](BREAKING_CHANGES.md) before upgrading from `2026.8.0`.
Restart Home Assistant after installing the HACS archive, clear the dashboard
or Companion app frontend cache and rebuild maintained ESPHome profiles from
the current YAML files.
