# Firmware calling policy

The maintained phone packages set `voip_calling_timeout: 120s` explicitly in
all public YAML entrypoints. Each device binding passes that substitution to
`voip_stack.calling_timeout`, including full, phone-only, native-audio, S3,
P4, microphone-only and speaker-only profiles.

```yaml
substitutions:
  voip_calling_timeout: 120s
```

A positive duration limits an ordinary outgoing call and a REFER replacement
attempt. Use `0s` to ring until the caller hangs up. An omitted component option
in custom YAML also means unlimited; the firmware component and its schema do
not impose a default number. If a peer disappears after provisional progress,
an unlimited attempt stays pending until the user hangs up. SIP transaction
limits still apply when no provisional response arrives and during cancellation.

`ringing_timeout` remains the separate incoming-call option. It no longer acts
as an outgoing fallback. A custom configuration using only a positive incoming
limit gets an explicit migration message to configure the outgoing policy.
An intentional zero remains zero and is never rewritten to a finite value.

## P4 behavior change

The P4 phone binding keeps `ringing_timeout: 30s` for incoming calls. Its former
outgoing 30-second fallback becomes the explicit 120-second package policy.
Changing `voip_calling_timeout` changes outgoing calls and transfers only.

## SIP completion and REFER notifications

The configured deadline ends ringing and sends CANCEL. The existing transaction
owner retains the call until terminal signaling completes; a completely silent
peer can require up to another 32 seconds of cleanup. A crossed 200 receives
ACK and BYE. This does not declare the phone idle before the transport is ready.

REFER uses the same outgoing limit. Its SIP event subscription is a separate
finite, renewable lease. The peer can refresh it with standard in-dialog
SUBSCRIBE. Subscription expiry or unsubscribe stops notifications while the
transfer continues, so an unlimited transfer needs subscription refresh for
continued result reporting. No private header or ESPHome channel is introduced.

## Initial contact selection

An explicit `voip_initial_contact` remains dialable through its SIP line even
when absent from LDAP. With no explicit target, the first eligible contact is
selected once while idle, ordered by display name and then SIP username. Known
own-account destinations are skipped only for this automatic choice. Manual
selection and an explicitly configured own target remain possible. A later LDAP
refresh does not replace the user's selection or change stored identities.

The first alphabetical entry can be a group or service. Configure
`voip_initial_contact` when a fixed destination is desired.
