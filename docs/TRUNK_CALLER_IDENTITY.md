# Caller identity and calls between PBX systems

VoIP Stack keeps the SIP caller, the credentials used to authenticate a trunk,
the dialog's Contact and the called destination separate. A trunk can therefore
authenticate as `link-a` while carrying a call from extension `101` to `202`.
No private SIP header or separate Home Assistant signaling channel is involved.

## Choose the caller presented by a trunk

Edit the named SIP trunk and select **Outgoing caller identity**. The available
policies are:

| Policy | Local SIP account | Caller already admitted through an incoming trunk |
| --- | --- | --- |
| `trunk` | Use the configured trunk caller number and name. If the number is empty, use the trunk username. | Use the same trunk identity. |
| `caller` | Use the account's optional outgoing caller number and name; otherwise use its SIP username and calling name. | Preserve the original From URI and calling name. |
| `account` | Use the account's SIP username, even when the account has a public outgoing number. | Preserve the original From URI and calling name. |

Existing trunks keep `trunk` until an administrator explicitly changes the
policy. This preserves the identity they presented before this option existed.
New trunk forms also show that choice explicitly. Use `caller` for an outside
line that accepts the account's direct number, or `account` for a private
extension plan shared by two PBXs.

A legacy static trunk with neither a caller number nor a trunk username keeps
the supplied caller as its last compatibility fallback. Configure a trunk
caller number when this otherwise anonymous static line must present a fixed
generic identity.

For example, account `101` can have public outgoing number `+39061234101`, while
the outside trunk has generic caller number `+3906123400`:

| Selection on the outgoing trunk | From user |
| --- | --- |
| Caller identity (`caller`) | `+39061234101` |
| SIP account (`account`) | `101` |
| Trunk identity (`trunk`) | `+3906123400` |

The account's username, device identity, entity IDs and saved automation targets
do not change when its public caller number changes. Digest still uses the
trunk's configured authentication username and password. Contact remains a
reachable address of the owning HA dialog. Request-URI still identifies the
called destination. An incoming peer account authenticates the other PBX; it
does not replace that PBX's individual callers in the phone registry.

The common caller resolver is used by SIP phone calls, the dashboard phone,
Automation contacts, forwarding and explicit trunk selection. Media continues
through the existing SIP/SDP/RTP owners and negotiation engine.

A forced identity also stays with an established outgoing leg if a transfer
retains that leg. Connected-party updates cannot replace its From with a local
extension; its subsequent BYE uses the same forced presentation. The other
policies retain ordinary negotiated connected-identity updates.

## What this option does not imply

A network operator decides which caller numbers it accepts and may reject or
replace a number outside the customer's assigned range. Setting an account's
public caller number does not grant the operator's permission to use it.

This feature controls the SIP **From** identity. It does not add or trust
`P-Asserted-Identity`, `P-Preferred-Identity` or `Remote-Party-ID`. A provider
that requires a fixed From account with the original caller carried separately
in P-Asserted-Identity needs that separate interoperability feature; do not
interpret `trunk` as enabling it. Incoming caller identity is admitted through
the configured SIP trust policy, not arbitrary asserted headers.

PBXs use different names for these choices. FreePBX's **Force Trunk CID**
corresponds to forcing the generic identity, while FreeSWITCH's
`caller-id-in-from=true` selects the opposite behavior. Yeastar's DOD maps a
public outgoing number to an extension. Asterisk separates `outbound_auth`,
`from_user`, `callerid` and `send_pai`. These names are not interchangeable.
In particular, 3CX's `EnforcedOutboundCallerId` is not simply a synonym for
forcing a generic trunk number: its documented purpose includes bypassing
anonymous-call suppression when resolving the outbound caller ID.

## Connect two Home Assistant instances directly

Example extension plan:

- Main home: local extensions `100` to `199`, including `101`.
- Sea home: local extensions `200` to `299`, including `202`.

Create ordinary SIP accounts on the receiving instances and bind them to the
existing named trunks. The crossed layout is:

| Setting | Main home | Sea home |
| --- | --- | --- |
| Local SIP account for the remote PBX | `link-b` | `link-a` |
| Outgoing trunk server | Sea home's reachable SIP address | Main home's reachable SIP address |
| Outgoing trunk username / auth username | `link-a` | `link-b` |
| Outgoing trunk password | Password of Sea home's `link-a` | Password of Main home's `link-b` |
| Authenticated incoming account bound to this trunk | Main home's `link-b` | Sea home's `link-a` |
| Outgoing caller identity | `account` | `account` |
| Unknown incoming destination | `reject` | `reject` |

Each enabled incoming binding belongs to one enabled trunk. A disabled
replacement can retain the setting without taking over the active binding.
The selected local account must exist and be enabled. Ordinary phones keep
their normal rule that the authenticated account and From user agree; only
the explicitly bound peer can authenticate on behalf of different callers.

REGISTER is optional for a static, routed deployment. With a bound incoming
account, a static trunk authenticates the incoming INVITE with SIP Digest and
does not require an incoming source-IP allowlist. Turning off REGISTER does
not turn off outbound INVITE authentication. Both UDP and TCP are supported.
When REGISTER is enabled, the registered flow and the listener that actually
owns an incoming dialog keep their respective Contacts; an incoming peer call
on the main listener must not acquire a different trunk socket's GRUU.

Use a routed private network or VPN with reachable signaling and negotiated
media addresses. This does not add an Internet traversal service, SRTP or a
proprietary HA tunnel. Existing SIP/SDP advertised-address settings still apply.

### Route extension ranges with native HA automations

On Main home, use the native **VoIP Stack: Route requested** trigger for
extension destinations `200` through `299`, then select the Sea home trunk in
the **VoIP Stack: Route** action. The YAML equivalent is:

```yaml
alias: Route sea home extensions
triggers:
  - trigger: voip_stack.route_requested
    options:
      ingress: extension
      destination: "200"
      destination_match: range
      destination_range_end: "299"
actions:
  - action: voip_stack.route
    data:
      action: forward
      destination: "{{ trigger.call.dialed_target }}"
      trunk_id: REPLACE_WITH_SEA_HOME_TRUNK_DEVICE_ID
mode: parallel
```

Choose the real trunk with the action editor's device selector. The placeholder
above is not a new configuration key or a literal trunk name. On Sea home,
use range `100` through `199` and select the Main home trunk. The incoming trunk
uses direct routing and resolves the requested local account normally.

The route may optionally supply `ringing_timeout`; omitting it inherits the
account or integration setting, while explicit `0` means unlimited. The global
value is initially stored as `120` seconds by setup or migration. Existing
explicit group, queue and parking settings retain their values.

No second prefix routing table is introduced. These native automations are
the range-routing rules, and disabling one disables that rule. For an extension
plan that must never fall through to a provider, leave the default outgoing
trunk unset and select trunks explicitly in the relevant automations. A
trunk's native configuration exposes **Use as the default outgoing trunk**,
including for a sole trunk; uncheck the currently selected line. The existing
missing-default warning is still shown, but does not block explicit named
routes or select another line automatically. A
disabled automation does not reserve its former range. A bound incoming trunk
set to `reject` returns 404 for an unresolved destination instead of silently
using a fallback. If a matching active route decision is requested but never
answered, its bounded decision window ends with 504. Both policies reuse the
existing routing owner.

### Put another PBX between the two instances

The same identities and routing rules work through a conventional SIP PBX:
HA A sends range `2xx` to that PBX, which routes it to HA B; the reverse route
handles `1xx`. Configure each receiving HA's local peer account on the upstream
PBX's outgoing leg, and bind that account to the appropriate HA trunk.

On each intermediate leg, choose whether that PBX preserves the caller or
replaces it with a generic identity. Once an intermediate PBX replaces From,
VoIP Stack cannot reconstruct the original caller from the new From alone.
Preserve From on the intermediate PBX when extension identity must survive.
The PBX's outgoing Digest credentials may still differ from that From user.

The qualification suite includes two independent HA cores with production SIP
endpoints, direct crossed trunks with and without REGISTER, and two HA cores
connected through Asterisk. It checks both directions, UDP/TCP, preserved and
replaced callers, negotiated G.711, bidirectional RTP, BYE and owner cleanup.
It is not a claim that every provider's identity policy has been tested.

## Standards and vendor references

- [RFC 3261](https://www.rfc-editor.org/rfc/rfc3261.html), especially section 22.1: authentication and the identity a gateway represents are separate concerns.
- [RFC 3325](https://www.rfc-editor.org/rfc/rfc3325.html) and [RFC 5876](https://www.rfc-editor.org/rfc/rfc5876.html): asserted identity belongs to a defined trust model.
- [FreePBX trunk options](https://sangomakb.atlassian.net/wiki/spaces/PG/pages/25690439): Allow Any CID, Block Foreign CIDs and Force Trunk CID.
- [Yeastar SIP trunk settings](https://help.yeastar.com/en/p-series-appliance-edition/administrator-guide/sip-trunk-settings.html): From fields, authentication and outgoing number choices.
- [3CX outbound calls](https://www.3cx.com/docs/sip-trunk-outbound-calls/): outbound caller ID precedence and the EnforcedOutboundCallerId source.
- [FreeSWITCH gateways](https://developer.signalwire.com/freeswitch/users-and-endpoints/gateways/): caller-id-in-from and gateway credentials.
- [Asterisk PJSIP endpoint options](https://docs.asterisk.org/Asterisk_20_Documentation/API_Documentation/Module_Configuration/res_pjsip/): from_user, outbound_auth and identity headers.
