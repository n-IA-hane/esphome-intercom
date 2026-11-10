# LDAP phone directory

VoIP Stack includes an LDAPv3 directory listener inside the integration. It
serves the same canonical phonebook used by HA routing, including phones,
contacts and callable services. No separate operating-system LDAP server is
required, and LDAP clients cannot edit the directory.

## Enable the listener

Open the VoIP Stack integration's **Reconfigure** menu and choose the phone
services/LDAP section. The current defaults are:

| Setting | Default |
| --- | --- |
| Enabled | Yes |
| Listen host | `0.0.0.0` |
| LDAP TCP port | `1389` |
| Base DN | `ou=phonebook,dc=voip` |
| Anonymous directory searches | No |
| Implicit TLS | No |

Enter the reachable HA address in the phone's LDAP configuration. The listen
host selects HA's local binding; it is not the server address advertised to a
remote phone. In a container or VM, make the configured TCP port reachable
through the existing network setup. A listener failure creates a Home Assistant
Repair instead of claiming that the directory is available.

For encrypted connections, configure a certificate and its private key on HA.
With **TLS** enabled, clients connect using implicit TLS on the configured port.
With **TLS** disabled but both certificate files supplied, the LDAP listener
offers the standard StartTLS operation on that port. The phone must trust the
certificate and use the matching connection mode. Certificate paths refer to
files accessible by the Home Assistant process.

LDAP simple Bind sends its password as part of the LDAP exchange; use TLS or
an appropriately protected network for that connection. StartTLS and Bind are
defined in [RFC 4513](https://www.rfc-editor.org/rfc/rfc4513.html). LDAP encryption
does not enable or change SIP transport encryption.

## Configure a phone or LDAP client

Use an enabled SIP account already configured in this VoIP Stack integration.
The LDAP listener checks the account's current password through the existing
account owner. It does not maintain another password database. Home Assistant
users, browser phone identities and remote trunk credentials are not LDAP
accounts.

For a SIP account named `desk01`, the client settings are:

| Client field | Value |
| --- | --- |
| Server | HA's reachable hostname or address |
| Port | The configured LDAP TCP port, normally `1389` |
| Protocol | LDAP version 3 |
| Authentication | Simple Bind |
| Bind identity | `uid=desk01,ou=accounts,ou=phonebook,dc=voip` |
| Password | That account's current VoIP Stack SIP password |
| Search base | `ou=phonebook,dc=voip` |
| Search scope | Subtree |
| Display/name attribute | `cn` |
| Number attribute | `telephoneNumber` |
| URI attribute, if supported | `labeledURI` |

The simple account name `desk01` is also accepted as Bind identity when a phone
cannot enter a full distinguished name. Adapt both DNs if you change the Base
DN. An account can authenticate without a current SIP registration, provided
that it remains configured, enabled and owned by a supported phone.

Use the phone manufacturer's documented placeholder for entered search text.
The LDAP filter structure is standard; the placeholder syntax belongs to the
phone, not this server. For example, a literal search for reception can use:

```text
(cn=*Reception*)
```

A number search can use:

```text
(telephoneNumber=*201*)
```

An OR filter for either field is:

```text
(|(cn=*Reception*)(telephoneNumber=*201*))
```

These are complete example filters, not templates to paste with an invented
phone-specific substitution token. LDAP filter escaping is specified by
[RFC 4515](https://www.rfc-editor.org/rfc/rfc4515.html).

## Directory content and account lifecycle

The base contains category branches for `esphome`, `sip_account`, `browser`,
`contact`, `automation`, `group` and `service`. Search the whole base to find
all categories, or a branch such as `ou=contact,ou=phonebook,dc=voip` to narrow
the result. The directory advertises its naming context in the standard root
DSE.

Each published entry includes `uid`, `cn` and `ou`. `telephoneNumber` contains
the configured extension and/or external number when present. `labeledURI`
provides the canonical SIP address through HA. A callable entry without a
numeric extension may therefore require the phone's URI-dialing support;
assign an extension if that client can dial only numeric LDAP results.

The directory is derived from existing phonebook owners. A phone going offline
does not by itself delete its configured identity. Renaming, changing an
extension or editing the canonical phonebook updates the directory projection.
Passwords and private authentication metadata are excluded from search results.

Disabling or removing an account, changing its password or replacing its owner
invalidates the corresponding authorization generation. Active LDAP access is
revoked instead of retaining the old password as a session-long permission.

Maintained ESP firmware with the native `configure_directory` and
`refresh_ldap_directory` actions receives its directory settings through the
existing ESPHome API provisioning path. Availability of those actions must be
checked in the running firmware; configuring the HA listener alone does not
prove that an ESP has received a new directory snapshot.

## Compatibility and troubleshooting

The server implements read-only LDAPv3 Bind and Search, standard filter
combinations, subtree/one-level/base searches, attribute selection, abandon,
paged results, and StartTLS when configured. Unsupported writes return an LDAP
error. SASL authentication is not implemented. Searches and connections have
bounded limits; the default result limit is 256, with standard paged results
available to clients that support them. The protocol comes from
[RFC 4511](https://www.rfc-editor.org/rfc/rfc4511.html); the actual supported
behavior is implemented in [the listener](../custom_components/voip_stack/ldap_server.py)
and [its configuration owner](../custom_components/voip_stack/phone_services.py).

Check these independently:

1. Confirm that the HA listener started and its configured port is reachable.
2. Confirm LDAPv3 and the correct implicit TLS or StartTLS mode.
3. Verify Bind with the configured SIP account and its current password.
4. Check the Base DN, subtree scope and a literal `(cn=*)` search.
5. Inspect returned `cn`, `telephoneNumber` and `labeledURI` attributes before
   debugging a phone's search-text substitution or dialing behavior.

The automated tests exercise the listener with independent LDAP clients,
including authentication, TLS and account revocation. They do not establish
compatibility for every desktop or mobile softphone, or the UI of a particular
firmware. Check the exact client's LDAP feature and attribute mapping in its
own documentation; SIP account support alone does not imply LDAP support.
