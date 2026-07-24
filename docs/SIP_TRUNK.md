# Optional SIP Trunk

Home Assistant can optionally register one SIP trunk account with a provider or
PBX. This does not change the local contract: ESP devices remain direct SIP
phones and do not register to the provider.

When the trunk is disabled, no trunk registration, external outbound routing or
inbound DTMF collector is started.

## Setup Flow

The first VoIP Stack setup step configures HA's local SIP
endpoint identity and media ports:

- SIP port
- RTP base port
- advertised host/IP
- optional Assist intents and callable Assist endpoint
- optional SIP/RTP diagnostics
- optional browser SIP video
- optional local SIP registrar
- optional trunk enable switch

Only when the trunk switch is enabled does the second setup step ask for trunk
details:

- transport: `udp` or `tcp`
- server, port and optional domain
- username, optional auth username and password
- REGISTER expiration
- optional outbound proxy
- inbound default target
- incoming routing mode: Direct or DTMF extension selection
- optional experimental automation routing override
- DTMF timeout and optional terminator

## Outbound Routing

Local targets still resolve through the phonebook first.

- `sip:name@host:port` routes direct.
- A known phonebook name routes direct or via HA according to the roster.
- A logical name can be bridged by HA.
- A contact `number` or unresolved external-looking number can route through
  the registered trunk. Local/internal digits should be modeled as
  `extension`.

If the trunk is configured but not registered, outbound unresolved targets fail
as routing errors. There is no proprietary intercom compatibility route.

## Inbound Routing

Provider inbound calls arrive at HA's SIP endpoint and use the shared phonebook
as their default dial plan. **Inbound default target** accepts HA, a phonebook
name, extension, group, registered SIP phone, Assist extension, SIP URI or a
routable number.

Choose one routing mode in the config flow:

- **Direct to default destination** skips DTMF collection and immediately
  resolves the configured target through the phonebook.
- **DTMF extension selection** answers the trunk leg with SDP, collects
  negotiated telephone-event or SIP INFO digits, and resolves explicit digits
  as phonebook extensions.

DTMF digits resolve against the central phonebook `extension` field:

```yaml
service: voip_stack.add_contact
data:
  name: Kitchen
  extension: "100"
```

Example user flow:

1. A caller dials the public provider number.
2. HA answers the trunk leg.
3. The caller sends post-dial digits such as `100`.
4. HA routes the call to the phonebook entry whose `extension` is `100`.
5. If no digits/route hint arrive before timeout, HA resolves the configured
   default target (`HA` is only the default value).
6. If explicit digits arrive but do not resolve, HA terminates the answered leg
   as `route_not_found`.

No final `#` is required. `trunk_dtmf_terminator` can be set if a deployment
wants one, but the normal path is a short timeout. Ambiguous prefixes are
resolved against the live phonebook extensions: HA collects within the timeout,
tries the final digit buffer, and fails loudly if that explicit buffer cannot
be resolved.

The route collector prefers negotiated RTP `telephone-event` and also accepts
the widely deployed legacy SIP INFO DTMF representation. Acoustic in-band
tones are not decoded.

## Experimental Automation Override

**Allow experimental automation routing overrides** is a separate switch and
is disabled by default. Enabling it adds one bounded `route_requested` decision
point:

- Direct mode exposes it before the default target.
- DTMF mode exposes it only after the digit window produced no digits.
- Explicit DTMF digits always retain priority and never enter the automation
  path.

If no matching automation acts within 1.5 seconds, the original phonebook route
continues. This makes time, presence and other HA state useful for contextual
routing without replacing the normal dial plan. See
[Automation Dial Plan](AUTOMATION_DIALPLAN.md) for native UI-compatible
examples.

Version 1 entries migrate without changing their route. Existing DTMF-enabled
entries with a non-zero timeout become DTMF mode; other entries become Direct
mode. Automation routing stays off until explicitly enabled.

## Media

The provider leg and local leg are separate SIP dialogs. HA bridges RTP between
them with the same relay/resampler used for local HA bridge calls. ESP devices
remain PCM-only and reject unsupported media with standard SIP errors. HA trunk
and softphone legs may accept common SIP codecs such as Opus, G.722, PCMA or PCMU,
then convert toward ESP PCM when the route requires it.

## Observability

The HA softphone snapshot exposes:

- `sip_trunk.trunk_enabled`
- `sip_trunk.trunk_registered`
- `sip_trunk.trunk_status_code`
- `sip_trunk.trunk_status_reason`
- `sip_trunk.trunk_last_sip_event`
- `sip_trunk.trunk_transport`
- `sip_trunk.trunk_server`

INFO logs describe normal call progress. DEBUG logs should be used when tracing
REGISTER, INVITE, DTMF routing and RTP relay behavior.
