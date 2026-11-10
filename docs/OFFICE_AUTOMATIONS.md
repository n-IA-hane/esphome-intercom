# Office call flows with HA automations

Use phone and group defaults for fixed routing. Use native VoIP call triggers
when a destination depends on office hours, a rota, presence or a keypad choice.
The actions operate on the triggering call; an automation does not need to
copy a Call-ID or maintain a second SIP dialog.

## On-call rota

Create an HA helper containing the current on-call phone or group. Change that
helper with your schedule automation. An incoming-call automation selects its
value using `voip_stack.select_inbound_destination`. The helper persists the
selection independently from a browser card or phone registration.

Keep separate rules for ordinary public calls and door calls, with an explicit
fallback when the on-call phone is unavailable. See
[incoming numbers](INBOUND_NUMBERS.md) and [automations](AUTOMATION_DIALPLAN.md).

## Door commands as telephone destinations

Create an Automation contact such as **Open gate** or **Pedestrian gate** and
give it a free extension. A call to that contact runs an automation using the
existing device integration or HA action that actually controls the gate.

Validate the authenticated call source. A display name or supplied caller number
is not proof of permission. For an external caller, use a restricted entry
point and a PIN flow where required; do not expose a trusted internal gate
action as an unrestricted public destination.

For an actuator requiring two pulses eight seconds apart, let the HA automation
own that sequence. A caller hanging up must not cancel an already accepted
second pulse. Keep the command sequence separate from call playback/control so
ending the conversation does not stop the actuator's task.

Do not automatically retry a toggle after an uncertain timeout: the first
request may already have reached the gate. A deliberate second user command is
a separate operation. A successful HTTP response confirms command delivery,
not physical gate position. Publish physical open/closed status only from a
sensor that measures it.

## Announcements

Use [paging](PAGING.md) for scheduled TTS to one or more desk phones. Use queue
login and pause actions for staffing changes. These are different operations:
leaving a queue does not unregister a phone, and an announcement does not need
to change its global autoanswer preference.
