# Ringing time and no-answer routing

Set the installation's ringing time in **Settings > Devices & services > VoIP
Stack > Reconfigure > General settings**. The first form is filled with **120
seconds**. Saving the form stores an ordinary configuration value; the runtime
has no hidden positive ringing default. Changing it updates future attempts
without restarting SIP transports or changing an active call's deadline.

## Inheritance and explicit values

| Configuration | Meaning |
| --- | --- |
| Account or group inherits | Use its parent PBX setting. |
| Explicit `0` | No ringing deadline at that policy level. |
| Positive seconds | End ringing at that configured deadline. |
| Missing global setting in a custom runtime | No application ringing deadline. |

An explicit routing-action `ringing_timeout` takes precedence over the
account/group setting, which takes precedence over the PBX value. The account
editor and card distinguish inheritance from an explicit zero. A saved
no-answer destination is independent of whether that account has its own
ringing number: it can act when the inherited PBX deadline expires.

When a deadline expires, the existing call owner cancels the pending SIP leg
and applies the account's `no_answer` policy. Without a fallback, it ends with
`no_answer` and SIP `480 Temporarily Unavailable` where a final inbound answer
is still possible. A final `200 OK` already accepted ends ringing, even while
media preparation is still completing. Caller hangup always remains available.
A matching native unanswered-call automation retains the existing decision
window before the default fallback runs.

The SIP transaction timers remain protocol timers. They still bound a request
that receives no response, CANCEL completion, BYE and transaction cleanup. They
are not replaced with the configured ringing duration. In particular, unlimited
ringing can remain pending after a provisional response if the peer disappears;
the caller must hang up in that case.

## Accounts, groups, queues and parking

- **Account:** inherit the PBX duration, or disable inheritance and enter a
  positive value or zero. Its no-answer action can be unset, hang up, forward,
  or use an enabled mailbox. Unset ends the attempt when its deadline expires.
- **Ring group:** inherit the PBX duration or retain an explicit overall value.
  The selected strategy, step timing and fallback remain group settings. A
  shorter inherited/per-action overall budget also caps its current step;
  it does not rewrite the stored step configuration.
- **Queue:** inherit for each offer or configure its own offer duration. The
  queue's maximum total waiting time still caps the final offer. A zero offer
  duration therefore does not make queue waiting unlimited. Group offers use
  queue policy, not each member's personal voicemail rule.
- **Parking return:** its explicit return duration overrides the destination
  account/PBX setting. Zero is unlimited even when the PBX value is positive;
  a missing value inherits the destination account, then the PBX. The return
  watchdog remains a backstop and does not race the forward or no-answer owner.
  Parking residence time is a separate setting. This follows the owner's latest
  zero/inheritance decision, superseding the earlier proposal to cap all returns
  by the account/PBX value.

## Actions

`voip_stack.route`, `voip_stack.select_inbound_destination` and
`voip_stack.forward` accept an optional `ringing_timeout` for the selected
attempt. Omitting it inherits; zero explicitly removes that level's ringing
limit. A new forward destination resolves a new attempt budget. A queue's
maximum wait still applies.

```yaml
action: voip_stack.forward
data:
  destination: "202"
  trunk_id: "configured_trunk_device_id"
  ringing_timeout: 45
  on_failure: terminate
```

For an Automation contact's outgoing call, the existing `answer_timeout` field
selects that initial attempt's ringing budget. Omit it to inherit the account
and PBX value, or set zero for unlimited. The session's existing policy owner
handles its ringing and no-answer routing. A later forward resolves its own
budget; the original value is not a second timer spanning the whole automation.
Synthesis, playback and the Automation contact's inactivity limit are separate.

Pickup uses the same optional `answer_timeout`: omit it to inherit from the
recovering account and PBX, or use `0` for unlimited. A normal or parked pickup
passes that override to its existing forward owner; it has no second wait
deadline. Group pickup applies one resolved limit to its callback.

`voip_stack.announce` also uses `answer_timeout` for each recipient's admission.
Omission inherits and zero is unlimited on SIP and browser recipients. Its
existing `timeout` bounds the complete announcement job, including playback,
and remains independent. Live calls to saved paging groups preserve their
explicit answer windows; new groups can inherit. Paging admission stays with
the existing application owner and does not arm another account-policy timer.

```yaml
action: voip_stack.call
data:
  source_automation: Wakeup-Caller
  destination: "101"
  answer_timeout: 0
```

`voip_stack.set_phone_settings` keeps omitted fields unchanged. To restore an
account's inheritance through YAML, supply `ringing_timeout: null`. Supply
`ringing_timeout: 0` to store an explicit unlimited value. `no_answer_action:
none` clears a saved fallback independently.

## Migration and release behavior

Migration 7.5 runs only after the SIP-account identity migration 7.4 succeeds.
A configuration stuck at 7.3 remains at 7.3 when that migration fails. Retrying
is safe; the failed migration never acquires a success version or a partial
ringing default. Existing identifiers, devices and entity references stay intact.

Existing installations without a saved global duration receive **120 seconds**
as stored configuration. Existing explicit global values, including zero,
remain unchanged. Existing account values and explicit group **30 s**, queue
**20 s** and parking return **30 s** values also remain unchanged. Only inherited
values use the PBX setting. The BLF lease option is independently initialized
at its previous **60 s** behavior.

Maintained ESP packages separately declare `voip_calling_timeout: 120s` in YAML.
The ESP component itself treats omitted or explicit zero as unlimited. On the
P4 phone, the previous incoming `ringing_timeout: 30s` stays an incoming limit;
the outgoing budget now comes from the separate package substitution, initially
120 seconds. REFER attempts use that same outgoing configuration. See the
firmware README for incoming/outgoing compatibility warnings and SIP cleanup.
