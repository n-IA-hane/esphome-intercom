# Registered ESP telephone accounts

Firmware can compile optional SIP registration alongside the direct HA path.
Each account belongs to the existing SIP worker. There is no additional media
engine, and registering a line does not disable Assist or the direct HA route.
Manual SIP registration is independent of HA provisioning. Compatible managed
firmware can also receive a named HA account while retaining additional PBX
accounts. Endpoint, codec and network compatibility still apply to each line.

```yaml
voip_stack:
  registration:
    max_accounts: 2
    accept_direct_calls: true
    default_line: direct
    accounts:
      - account_id: office
        registrar: pbx.example.org
        username: '100'
        password: !secret office_sip_password
        transport: udp
```

With neither `registration` nor HA auto-registration enabled, the account pool
is compiled out. Capacity is 1 to 4,
default 2. An empty `accounts: []` permits runtime provisioning. Manual account
registration supports UDP or TCP, Digest MD5/SHA-256, renewal and registrar
expiry negotiation. The account's successful binding is required to originate
on that line. Failed OPTIONS discovery does not prevent an otherwise valid call.

Use `voip_stack.start_call_on_line` with `id`, `target` and `line` to choose an
account explicitly. `voip_stack.select_line` changes subsequent calls only.
`direct` selects the existing phonebook and HA routing path. The native API's
`start_call_on_line` uses `dest` and `line`; the YAML action uses `target`.
An active call retains its own line, authentication and signaling flow.

`voip_stack.set_account` and `remove_account` are volatile unless `persist: true`
is requested. Persisted overrides take precedence over YAML after restart.
`reset_account` restores that account's YAML configuration. Unregister a bound
account before replacing credentials. Runtime line selection is not persisted.

`on_registration_state` supplies `account_id`, `state` and `status_code` to
automations in the component main context. It reports registration state,
independently of call state or media quality. Registered accounts do not become
HA trunk entries: HA's provider connections keep their own settings.

The complete action fields, resource budgets and persistence failure contracts
are documented in `esphome/components/voip_stack/REGISTRATION.md` in the selected
VoIP component source. Keep that source pinned together with the firmware.

The maintained P4 and WS3 LCD profiles preserve their preferred private
48 kHz receive / 16 kHz transmit modes and also admit mono 16 kHz PCM at 20 ms
for conventional PBXs. The 48 kHz mono 16-bit payload alone occupies 1920 bytes
at 20 ms and 960 bytes at 10 ms; the smaller quantum avoids fragmentation on
ordinary Ethernet paths. This does not impose a global packet time on other
codecs. L16 uses network byte order, separately from the local PCM buffer format
([RFC 3551, section 4.5.11](https://www.rfc-editor.org/rfc/rfc3551#section-4.5.11)).
