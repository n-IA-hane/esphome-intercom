# Your PBX should be nervous.

2026.12.0-dev development preview.

After updating VoIP Stack at the company where I work, I started looking at our
FreeSWITCH installation a little differently. More and more of what I needed
was already sitting in Home Assistant.

Eventually, one question stopped leaving me alone: could VoIP Stack replace our
office PBX? We already run Home Assistant. What is actually missing?

Apparently, asking that question was enough to ruin another perfectly good
development schedule.

With SIP BLF and the VoIP Dashboard card, the operator console is already taking
shape. This preview adds call queues with operator login, logout and pause,
alongside paging and more explicit incoming-number routing.

Looking back, exposing dialplan operations through Home Assistant automations
keeps proving its worth. Office hours, on-call rotations, presence, door controls
and custom call flows can use the same tools you already use to run everything
else.

Phone provisioning is another thing I want to tackle. Managed ESP devices
already have it. Now imagine entering the MAC address of your desk phone,
choosing its model and letting VoIP Stack configure its account, keys and
directory.

That part is still ahead of us. The ideas, unfortunately, show no sign of
stopping.

The intercom wants the whole damn office.

## Group devices and queues

Ring groups and queues are now configurable VoIP Stack service devices. Create
one manually, or type a new name in a phone's group settings. Another phone
declaring the same name joins the existing group. The central member selection
and phone settings modify the same membership.

Automatic groups disappear after their last configured membership is removed.
Manual groups can remain empty. Renaming a group preserves its stable identity.
Offline ESP changes are persisted and synchronized without registering the SIP
account again; reconnect snapshots cannot undo a pending HA edit.

Queues use FIFO callers and ring-all available operators, with bounded waiting,
answer windows, retries, post-call wrap-up, optional waiting audio and a fallback
destination. Login, logout and pause persist separately from membership. SIP
keys use the existing authenticated dialog-info BLF owner.

See [group and queue setup](GROUPS_AND_QUEUES.md).

## Paging, including ordinary desk phones

The new `announce` action sends a TTS message or recording to one or several
phones, optionally through a saved paging group. TTS is generated once, then
distributed to the recipients with bounded queues. Busy phones are skipped and
results identify each target. `stop_announcement` stops only its own job.

SIP recipients receive a per-call `Answer-Mode` request. Updated ESP firmware
authorizes paging for the intended account without toggling global autoanswer.
A browser recipient receives audio without opening its microphone and must
confirm playback readiness. Calling a paging group's extension supports a live
one-way announcement.

Your phones can be your announcement speakers. A separate smart speaker is
optional. See [paging](PAGING.md) for setup and browser limitations.

## Incoming numbers belong to the call

Named trunks can select the called number from Request-URI, To,
P-Called-Party-ID, an explicit custom header or a fixed value. Ordered fallback
sources and prefix replacements are configurable rather than provider-specific.

Native automation triggers expose the original and normalized called number,
its provenance, caller identity and stable incoming line. That initial identity
survives forwarding and termination. A native line-device selector makes it
possible to distinguish trunks without depending on their display names.

The qualification bank exercises 100 mapped public numbers, exceptions and
unknown-number fallback through HA automations and real SIP recipients. It does
not register with your production carrier or certify every provider's format.
See [DID and number blocks](INBOUND_NUMBERS.md).

## Assist and desk-phone directory

The callable Assist endpoint gains tools scoped to its current telephone call:
inspect, transfer to permitted internal destinations, resume a pending transfer
and hang up. A failed transfer retains the conversation. Tools cannot select an
arbitrary Call-ID, external number or unrelated user's call.

The new [LDAP phone guide](LDAP_PHONES.md) covers the actual server settings,
Bind identity, filters, returned attributes and TLS. It distinguishes protocol
qualification from a physical phone's implementation. The
[office automation examples](OFFICE_AUTOMATIONS.md) cover on-call routing and
door-command ownership without creating another dialplan engine.

## Upgrade and preview limits

Update both the HA integration and ESP VoIP component for the new firmware
features. Existing ring-group declarations are retained. Queue declarations and
their optional native text control are additive. Changing memberships does not
require a transport reload or another REGISTER.

This remains a development prerelease. The new paths have software and real
SIP/RTP laboratory coverage, including media content and teardown. That is
distinct from physical qualification on every desk phone, ESP board and carrier.
The Fanvil hardware check and office cutover remain separate steps.

This release does not include another MWW or acoustic AFE tuning pass. The
current stable release and existing release tags remain available.
