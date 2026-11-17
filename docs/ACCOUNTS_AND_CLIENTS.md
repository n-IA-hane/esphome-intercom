# One account, SIP phones and Home Assistant cards

A SIP account is the callable identity: extension, name, DND, routing rules,
mailbox, queue memberships and current calls. Physical phones and Home Assistant
cards are clients of that account. A second card or another registered handset
does not create another extension or queue agent.

The account device and the physical phone device have different purposes:

| Device | Configuration |
| --- | --- |
| SIP account | Extension, credentials, DND, routing, queues and mailbox |
| Physical phone | MAC/model, language, provisioning, lines and programmable keys |

Choose **Associated physical phones** in the account editor to assign the account
to a handset and an explicit line slot. The physical phone's **Lines** editor
shows the same association. Both edit the physical phone's canonical line list;
there is no second list of associations to reconcile.

## Upgrade existing HA phones

Version 7 of the integration configuration converts existing HA/browser phone
records to SIP accounts in place. Endpoint IDs, subentry IDs, device IDs, entity
identities, extensions, names, routing rules and mailbox references are retained.
An identifier beginning with `browser:` remains a valid opaque account ID.
Missing SIP credentials are generated once and never sent to the card.

Existing card configurations, including `mode: ha_softphone`, `card_mode` and
`device_id`, keep their meaning. ESP mirror and directory cards retain their own
roles. The integration and frontend update together; reload an already open HA
page to load the new frontend. Recreating accounts or rewriting dashboards is
not part of the upgrade.

The migration records the existing HA reception declaration as an association
on that same account. It remains visible in configuration and can be removed.
This preserves legacy reception through a global popup or a dynamically generated
dashboard that the backend cannot inspect while its browser is closed.

## Availability and closed browsers

An enabled account is online when it has at least one valid SIP registration or
at least one configured HA association. Closing every browser, hiding a card or
switching views does not remove a saved association. An enabled account with
neither is offline. DND, busy state, queue login, pause and wrap-up are separate
policies and continue to apply.

Saved audio card/dashboard declarations and explicit popup preferences are
tracked by the integration. Previewing an unsaved card does not permanently
associate it. Multiple saved references are counted separately: removing one
does not remove the others. Storage dashboard changes are reconciled through HA's
configuration events, without periodic per-card checks.

After changing a dashboard YAML file, use **VoIP Stack: Reload card associations**
to load the updated saved configuration. Arbitrary frontend strategies cannot
be evaluated by the backend; use an explicit saved popup association for that
case. A failed dashboard read retains the last known configuration instead of
marking every account offline.

## Receive through a notification

1. Associate the card with the account and save the dashboard.
2. Use the existing incoming call/phone-state trigger to send a Companion
   notification pointing to that dashboard view.
3. The PBX keeps the call ringing under the existing account timeout and routing
   rules even with no browser connected.
4. Open HA. The subscription supplies the current snapshot and then updates, so
   the card can display and answer the same pending call.

If the caller already hung up, a handset answered or the configured timeout
expired, the card displays that current result. Opening a dashboard never
answers a later, unrelated call through a stale call ID.

The card uses HA authentication and the account's existing HA access controls.
It does not perform SIP REGISTER or expose the SIP password. Browser microphone
permission and media setup happen when that client answers or originates.

## Shared calls and media ownership

A registered SIP phone and an associated card can receive the same account
offer. The existing fork arbitrates their answers; only one terminal acquires
the media. Losing branches are cancelled and release their own resources.
Several cards still contribute one logical HA account offer and one queue
admission, not several agents or statistical answers.

Opening a card while a handset is talking displays the account's conversation
without opening browser audio or a camera. Closing that observing card does not
hang up the handset. The account remains the unit for DND, routing, queue login,
pause, voicemail and call activity.

To move audio explicitly between handset and card, park the conversation in an
existing PBX slot and retrieve it from the other client. This uses the same
parking, ownership and cleanup mechanism as other PBX calls.

For example, with the default parking configuration, transfer the handset's
current call to `700` or use **P** on the card that owns the media. HA reports a
slot such as `701`. Open the other client of that same account and call that
reported slot. Parking releases the old client while retaining the remote
party's call; retrieval attaches the new client. Merely opening a card is still
observation, not a request to move audio.

Programmable **Monitor/retrieve parking** (`park`) and **Park current call**
(`park_call`) are separate functions. The former watches a configured slot;
the latter requests the model's Call Park action for the current conversation.
Existing `park` keys retain their monitor behavior and target numbers. See
[the key examples](PHONE_PROVISIONING.md#parking-monitor-and-action).

The account device's activity records logical calling, answered and terminal
phases. When supplied by that same call occurrence, the result includes its
reason, SIP status and answering party. Several Contacts do not create several
copies of an account phase; unrelated trunk diagnostics are not its call result.

## DND and phone capabilities

DND belongs to the account. Compatible provisioned phones can synchronize their
native per-line DND with HA through authenticated feature-event subscriptions.
The server publishes the current state at subscription and after changes;
explicit ON/OFF operations are idempotent.

Support is declared by the selected model profile. A phone's local DND setting
remains local when its model/firmware does not implement the qualified feature
protocol. A rejected call (`486` or `603`) is never interpreted as a persistent
DND command. Software protocol qualification and a physical handset test are
separate evidence.

Browser **Auto Answer** and **Send Video** remain HA client preferences. They do
not enable hardware auto-answer on every handset sharing the account. For a
backend TTS or media announcement, use the existing
[`voip_stack.announce` action](USER_GUIDE.md#make-an-announcement-without-a-browser).

## Adopt a phone before assigning an account

With optional provisioning/discovery enabled, HA proposes supported discovered
phones for adoption. Account assignment is optional during adoption: an
unassigned handset remains available for later association. DHCP addresses are
not the phone's stable identity. Its MAC/model and private provisioning
credential identify it; an optional network hint does not determine online
state.

Removing the last managed line invalidates old credential-bearing output and
generates the vendor revocation for the line that the integration owned. The
private provisioning URL remains usable for delivering that revocation. Unmanaged
slots and unrelated settings are preserved.

The unassigned profile contains no SIP or LDAP account passwords. Account
deletion, account password rotation and physical dissociation are different
operations: dissociating one handset does not disable a shared account on its
other clients. Delivery waits for calls on the old managed line to finish, and
download alone does not prove the handset applied a removal.

See [managed phones](PHONE_PROVISIONING.md), [routing](PHONE_ROUTING.md) and
[groups and queues](GROUPS_AND_QUEUES.md) for the respective controls.
