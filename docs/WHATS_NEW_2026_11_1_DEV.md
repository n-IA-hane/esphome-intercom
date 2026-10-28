# VoIP Stack 2026.11.1-dev

Changes since stable **2026.11.0**.

## Your phone system, on one dashboard

The new **VoIP Dashboard** brings your extensions and saved contacts into a
single operator view. See who is available, who is calling and which
conversations are on hold, then call or pick up directly from the same card.

![VoIP Dashboard with ringing, active and held calls](images/voip-dashboard-desktop.png)

Illustrative snapshot with voicemail, Do not disturb and an offline phone.

<details>
<summary>Mobile portrait layout</summary>

![VoIP Dashboard in mobile portrait layout](images/voip-dashboard-mobile.png)

</details>

Glass surfaces follow your Home Assistant theme. The responsive grid adapts
from a narrow phone screen to a full-width operator console, with separate
sections for internal phones and external contacts. Give the card more space
in the dashboard layout editor to display more columns and rows.

- **Live extension tiles:** browser phones, ESP phones and registered SIP
  accounts share one view of availability and call state.
- **Recognizable destinations:** icons distinguish phone types, saved
  contacts and automation destinations, with separate availability indicators.
- **Call details:** ringing tiles show the caller; busy tiles expose active
  and held conversations as separate entries.
- **Directed pickup:** recover an eligible ringing call on your selected
  browser or ESP phone.
- **Phone controls:** answer, decline and hang up from the selected phone's
  header. The calling-phone selector stays in the visual editor.
- **The same phone options:** auto-answer, ringtone, Do not disturb and media
  device preferences use the familiar softphone controls.
- **Video calls:** choose video or audio-only when answering a video offer.
  The first received video frame opens the full-card call view, with the
  familiar red call bar and phone controls. The tiles return after hangup.
- **DND and voicemail:** see Do not disturb, mailbox waiting status and the
  count of new messages alongside each phone.
- **Saved contacts:** call external destinations from their own section.

The header belongs to the phone selected for this card. It keeps that phone's
identity, status and call controls together while the grid shows the rest of
the system.

Leave the optional title empty to use the selected phone's name as the heading,
with its extension underneath when available. A custom title keeps the phone's
identity below it.

Add **VoIP Dashboard** from Home Assistant's card picker and choose its calling
phone in the visual editor. The card loads through the existing VoIP Stack
resource. See the [dashboard guide](USER_GUIDE.md#use-the-voip-dashboard) for
layout, controls and supported phone types.

## The same calls and media engine

The dashboard and the familiar softphone card share the page-level media
engine. A call continues when you leave its card or dashboard view, with its
browser presence and keepalive maintained until the call ends. Opening both
cards on one view uses that same call owner.

Selecting an ESP phone keeps audio on the ESP. Selecting a browser phone uses
the browser microphone and speaker, with microphone permission requested when
placing or answering a call. Browser video uses the same media engine and
controls as the existing phone card. A video offer alone keeps the tiles
visible until a remote frame arrives.

<details>
<summary>Video call layouts on desktop and mobile</summary>

Browser call screenshots with a synthetic camera source.

![A video call fills the dashboard with native call controls](images/voip-dashboard-video-desktop.png)

![The same video call and controls on mobile](images/voip-dashboard-video-mobile.png)

</details>

Registered SIP phones appear as monitored and callable destinations. Choose a
browser or ESP phone as the dashboard's calling phone. Multiple and held calls
are displayed from the backend's actual call records, preserving their
individual identities.

## Live updates from the PBX

The dashboard shares the authoritative phone-state observer used by SIP busy
lamps. It receives an initial WebSocket snapshot followed by changed or
removed endpoints, with a fresh snapshot after reconnecting.

Endpoint permissions apply to the subscription and are reconciled when access
changes. Monitoring a phone is passive: its presence, microphone and media
ownership stay with their existing owners. Closing a view releases its
display listeners, and connection cleanup follows Home Assistant's
subscription lifecycle.

## Interoperability and regression coverage

The accompanying qualification tools exercise SIP BLF subscriptions through
PJSIP over UDP and TCP. The regression suite covers snapshot and delta
delivery, endpoint removal, permission changes, held calls, multiple calls and
WebSocket cleanup. Browser scenarios check the dashboard controls against real
call states and verify that call resources are released after hangup.

The [testing tools guide](../tools/README.md) describes the reproducible lab
workflow and its evidence. Software and browser checks cover their respective
layers; physical audio quality remains a separate device test.

## Updating from 2026.11.0

This prerelease updates the Home Assistant integration and frontend. Existing
phone-card configurations remain usable; add the dashboard as a separate card
where you want the operator view.

ESP firmware and device YAMLs retain their **2026.11.0** Audio Stack, ESP VoIP
Stack and Runtime Controller components from `main`. Keep your existing ESP
firmware for this dashboard update. Home Assistant **2026.7.0 or newer** remains
the integration requirement.
