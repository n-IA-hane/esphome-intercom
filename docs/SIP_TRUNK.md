# Optional SIP trunk

Home Assistant can connect one SIP trunk to a provider or PBX, either with
registration or as a static trunk authorized by source IP. This does not change the local contract: ESP devices remain direct SIP
phones and do not register to the provider.

When the trunk is disabled, no trunk registration, external outbound routing or
inbound DTMF collector is started.

## Setup flow

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

Enable the trunk, then choose **Register the trunk**. It is enabled by default
for existing provider accounts. The following step asks for trunk details:

- transport: `udp`, `tcp` or `tls`
- server, port and optional domain
- username, optional auth username and password
- REGISTER expiration when registration is enabled
- allowed incoming IP addresses when registration is disabled
- optional outbound proxy
- inbound default target
- incoming routing mode: Direct or DTMF extension selection
- optional experimental automation routing override
- DTMF timeout and optional terminator

### Static trunk without REGISTER

Disable **Register the trunk** when the other PBX or provider routes calls to a
fixed SIP address instead of accepting account registrations. In this mode:

1. Set **Trunk server** and **Trunk SIP port** to the destination for outbound calls.
2. Add each **Allowed incoming IP address**, or CIDR network, supplied by the provider.
   These are the source addresses of its SIP signaling, which may differ from the
   outbound server. Use literal IPv4/IPv6 addresses or restricted networks; `/0`
   entries are rejected. This list is required and is shown only in static mode.
3. Configure the other system to send inbound calls to HA's reachable SIP address
   and listening port. Without REGISTER, HA does not publish this destination to it.
4. Select the incoming destination or routing automation as usual.

For example, a local gateway at `192.168.1.50:5060` can be both the outbound server
and the allowed incoming address `192.168.1.50`. A provider with several signaling
servers needs each permitted source address or network listed separately.

Username and password are optional in static mode. Keep credentials if the peer
challenges outbound INVITEs; disabling REGISTER does not disable SIP digest support.
The username also supplies the outbound SIP identity when configured.

HA sends no REGISTER, registration refresh or unregister in this mode. OPTIONS
is not used to simulate registration. Availability means the static route is
configured, not that the remote peer has been probed successfully. Calls still
report connection failures normally. The allowed IP list applies to SIP signaling,
not the RTP media addresses negotiated in SDP.

Calls from a matching source use the existing trunk routing rules. Other sources
do not acquire trunk privileges; registered phones and ESP endpoints keep their
existing call paths. Local phonebook destinations, DTMF and automation routing
continue to use the shared call implementation. This option does not add multiple
trunks or change how the inbound fallback destination is selected.

### Registered trunk

The REGISTER Request-URI identifies the registrar domain, for example
`sip:example.invalid`. The account address of record remains in `To` and
`From`, for example `sip:alice@example.invalid`. Keeping the username out of
the Request-URI is required by FRITZBox and remains valid for ordinary SIP
registrars. Digest authentication still uses the configured auth username and
realm.

REGISTER runs as a background SIP client transaction. Home Assistant setup
does not block while an unreachable registrar consumes the RFC non-INVITE
deadline. UDP retransmissions retain the same transaction identity, while an
authenticated retry after `401` or `407` uses a new CSeq and Via branch.

## Outbound routing

Local targets still resolve through the phonebook first.

- `sip:name@host:port` routes direct.
- A known phonebook name routes direct or via HA according to the roster.
- A logical name can be bridged by HA.
- A contact `number` or unresolved external-looking number can route through
  the available trunk. Local/internal digits should be modeled as
  `extension`.

In registration mode, the trunk must be registered before routing unresolved
outbound targets. In static mode, the configured route is available without
registration. There is no proprietary intercom compatibility route.

## Inbound routing

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

## Experimental automation override

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

RTP packet duration is treated as a negotiated target, not an assumption about
every received datagram. When the codec, sample rate, channel count and PCM
layout already match, shorter aligned packets are accumulated and emitted at
the destination frame size. This supports peers that advertise 16 or 20 ms but
send 10 ms PCMA/PCMU packets without hiding malformed or unaligned payloads.

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
