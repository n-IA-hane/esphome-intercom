# Native Companion calling: historical prototype

This branch is a museum of an idea demonstrated before Home Assistant had an
agreed public calling contract. It preserves the design, source and experiments
so future work can start with context. It is not a supported installation,
a current upstream proposal, or an APK distribution channel.

The maintained integration retired this code in commit `9e67c004`. Normal
browser cards, ESPHome phones, SIP accounts, trunks and automation routing
continue on the maintained branch. No Android calling or dashboard-menu
extension is required by that version.

## What we wanted

A compatible Companion registration could be an independently addressable
phone, alongside browser, SIP and ESPHome phones. Its stable identity followed
the app registration and authenticated user; its display name followed the
Companion device tracker. Renaming the phone should not create another endpoint.

Routing, groups, conferences, forwarding and call lifecycle remained in VoIP
Stack. Companion supplied operating-system calling controls and native audio.
The long-term aim was a generic Core/Companion contract reusable by other
integrations, not a private SIP stack embedded in Companion.

## Incoming call

```text
Caller -> VoIP Stack existing routing -> Companion phone endpoint
                                          |
                             mobile_app command_call notification
                             (ring, call_id, authenticated call_path)
                                          |
                                          v
                               Companion Android
                               fetch current call descriptor
                               present Android Core-Telecom call
                               answer / decline / hangup
                                          |
                                          v
                           Existing authenticated HA PCM WebSocket
```

The prototype's `app_data.native_calls: 1`, notification payload, HTTP call
contract and External Bus messages were proposals. They were not standardized
Home Assistant APIs. `mobile_app.call` was discussed as a possible public entry
point, not implemented as an official Core service.

A calling action and a destination phone are different objects. A ring group
contains destination identities. The provider must cancel the other invitations
when one member answers, and retain authoritative session/generation checks.
A future generic calling action alone does not replace those endpoint semantics.

## Outgoing calls and dashboard composer

The separate frontend proposal exposed a generic dashboard-menu extension API.
The integration registered a VoIP Stack menu item that opened a composer.
Desktop defaulted to the preferred browser phone; a compatible app defaulted
to its native phone. The user could select another caller and a phonebook target.

The composer reused the existing card and backend call services. Browser calls
used the shared browser media engine, with one controller owning microphone and
speaker access. Other cards could display the call without capturing a second
microphone. Native calls used Android audio; closing the composer did not end
the operating-system call or remove its ongoing hangup notification.

External Bus messages included `call/context`, `call/prepare`, `call/start` and
`call/control`. The Android code did not implement SIP routing. The maintained
integration has removed these native paths and the composer presentation mode.

## What was tried, and what was not established

Historical owner tests reported incoming ringing, answer/decline, ongoing
notifications and hangup on Android, plus outgoing local and trunk calls.
Emulator work exercised authenticated audio and call control. Those observations
apply to experimental builds, not to the official Companion app or every patch
captured here.

The broader local Android experiment also accumulated lifecycle and cancellation
changes. The Android patch includes previously uncommitted source and tests;
archival inclusion is not a new test qualification. Earlier notes mention an
AndroidX/Core-Telecom memory-leak investigation and a local dependency correction.
Do not infer that this archive fixes AndroidX upstream or that an override should
be carried into a future PR. Reassess current official dependencies first.

There is no iOS implementation or iOS hardware qualification here. Remote push
wake-up, Bluetooth routes, multiple registrations of official/experimental apps
and long-running lifecycle behavior need their own evidence. Changelog screenshot
PNG baselines are omitted from the Android source patch and can be regenerated.

## Upstream proposals

- [Android PR #7536](https://github.com/home-assistant/android/pull/7536), closed.
  Submitted incoming-call revision: `cc4bf46511ee27c90197cdf5a9cc591980b4a6de`.
  Its scope was smaller than the local outgoing-call experiment archived here.
- [Frontend PR #54435](https://github.com/home-assistant/frontend/pull/54435), closed.
  Submitted revision: `c468671402e0597c0c109197a269ccb172a31514`.
- [Original user request #84](https://github.com/n-IA-hane/esphome-intercom/issues/84).

The Android maintainers requested agreement with Core and both mobile platforms
on a generic architecture before this feature. Frontend maintainers were
redesigning navigation and did not want the proposed menu contract to constrain
that work. These closures are not evidence that native calling is scheduled.

## Contents and future use

`source-revisions.json` records the exact upstream bases and local source heads.

- `intercom-prototype.patch`: restores the historical integration prototype onto
  the retirement commit. `git apply --check` passed on that exact base.
- `android-prototype.patch`: source delta against the recorded Android upstream
  base, including local tracked edits and two untracked test files.
- `frontend-prototype.patch`: source delta against the recorded frontend base.
- The rest of this branch retains the integration snapshot and its original
  documentation/tests, including `docs/COMPANION_CALLS.md`. That document is
  historical and may contain claims superseded by this note.

Before reviving the idea:

1. Read current Core, Companion and frontend APIs and maintainer guidance.
2. Map endpoint identity, permissions, invitation cancellation, call generations
   and audio ownership onto those supported APIs.
3. Reuse only the parts still needed. Do not replay the patches onto production
   or downgrade configuration schema versions. Retirement removed native-phone
   subentries; code restoration alone cannot restore a user's deleted settings.
4. Submit small independent changes, with tests and explicit platform boundaries.
5. Validate Android and iOS separately. Do not treat prior APK behavior as proof
   for new upstream dependencies.

No credentials, private device configuration, APKs or runtime captures belong in
this archive.
