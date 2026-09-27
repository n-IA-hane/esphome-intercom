# Native incoming calls in Companion (experimental)

This feature requires a Companion build that advertises native call support. It is not available simply by updating VoIP Stack while keeping an unmodified Companion app.

A compatible app can receive a call addressed to its own phone identity, answer or decline from Android's call notification, exchange audio through the communication channel and hang up. Home Assistant handles routing and codec conversion. The shared dashboard composer sends call commands to the native phone; it does not implement a second audio engine or a native dialer screen.

## Enable a phone

1. Register the compatible Companion app with Home Assistant normally.
2. Reconfigure VoIP Stack and enable **Enable Companion phones**.
3. Find the discovered phone in VoIP Stack. You do not need to add a browser phone or create a SIP account for the app.
4. Optionally configure an extension or the existing phone options. New phones do not join any group automatically.
5. Call the phone by its name or configured extension. Allow microphone access when answering for the first time.

The phone name follows the display name of its `mobile_app` device tracker. For example, `CPH2709` becomes `App Daniele` when that tracker is renamed. The registration remains the phone's identity, so changing the tracker entity ID does not create a second phone. Location updates are not required; a disabled tracker can still supply its registry name. If the tracker is absent, the app's registered device name is used.

Names must be unique in the VoIP phonebook. A collision produces an error and a persistent notification instead of silently inventing a different name. Correct the tracker name to restore availability. The last valid phone configuration is retained during a conflict.

Existing references by stable phone identity survive renaming. Automation text that explicitly contains the old name must be updated, or use a stable identifier or extension.

## Card and native phone are separate

Dashboard cards keep their own identities and browser audio behavior, including when displayed inside Companion. They do not automatically become the app's native phone. Native Companion phones are excluded from the card's owner selector.

Disabling Companion support makes its phones unavailable without deleting their saved configuration. Enabling it again reuses the same phone identities.

## Audio and limits

The app uses Android Core-Telecom and communication audio, so the call-volume control applies. PCM formats are negotiated with HA; 48 kHz has been measured in the emulator. Device and Bluetooth routes can impose their own limits.

## Dial from the dashboard menu

The experimental frontend adds **VoIP Stack** to the dashboard menu. This requires the proposed generic menu API in Home Assistant's frontend; updating the integration alone does not add that API to a released frontend.

The composer selects the default Home Assistant browser phone on a desktop. In a compatible Companion app it selects that app's phone identity. The **Calling phone** selector allows another supported source to be chosen before starting the call. Type a name, extension, group or SIP address in the destination field, or select a phonebook suggestion. The input uses the normal text keyboard. During a conversation, the keypad sends DTMF.

When a browser call starts from the popup, the popup controls the page's existing media engine. A card for the same phone displays the call state without opening another microphone. A call started from the card keeps the card as its controller. Opening the popup alone does not transfer audio.

For a native Companion call, Android owns the communication audio and ongoing call notification. Close the popup or leave the dashboard and continue the call; use **Hang up** in the system notification to end it. Minimizing a browser popup also keeps its call alive in that page, and the menu reopens its controls. Closing the browser page is different from minimizing the popup.

The test APK contains a separately qualified Core-Telecom source correction for a retained result receiver. This correction must be submitted and incorporated upstream before the standard dependency can be described as fixed. It is not silently applied to normal builds.

Local notification delivery has been tested in an Android emulator. Firebase remote wake, physical phone/tablet behavior and Bluetooth routes need separate qualification. iOS needs its own CallKit implementation and testing; Android results do not qualify iOS.
