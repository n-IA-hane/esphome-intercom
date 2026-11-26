# NOTIFY transport and subscription recovery

Application requests use the dialog Contact and route set. The transport of
the SUBSCRIBE request does not override an ordinary Contact. A named UDP
Contact or a UDP registration is not proof that the peer supports only UDP.

The complete serialized request, including authentication and Route headers,
is checked against 1300 bytes. A larger ordinary UDP request opens TCP. Only
the connection-opening failures named by RFC 3261 permit the UDP compatibility
retry; timeout and uncertain delivery never do.

A subscription can instead retain an exact authenticated SIP Outbound binding.
The registrar remains its owner. The notification path checks the binding's
generation, flow key and current account credentials before using its flow.
A live UDP Outbound binding keeps that flow for a large request, and a TCP
Outbound binding keeps the client-initiated connection. Binding replacement or
loss fails that subscription; another Contact is not borrowed as a fallback.

TCP recovery distinguishes two observations. If the writer rejects the first
send before accepting bytes, one reconnect to the same resolved peer is
allowed within the original deadline. If the request was queued or written,
EOF does not prove non-delivery. That NOTIFY is not replayed. A transport
failure or missing transaction response terminates the usage and releases its timer,
pending update and notifier. The subscriber's bounded lease/renewal then
creates a fresh SUBSCRIBE and receives a complete current snapshot. No idle
state is inferred from missing packets, and subscriptions do not become
immortal after transport loss.

Dialog subscriptions keep a 60-second maximum by default. Reconfigure exposes
**Maximum BLF subscription lease**, accepting 60-3600 seconds. A user may choose
600 or 3600 to reduce periodic renewal traffic. Shorter phone requests are still
honored. Every state change still sends NOTIFY immediately; a longer lease
lengthens the interval between periodic full snapshots after a missed update.
An ESP renewing at 80 percent requests a new snapshot around 48 seconds for a
60-second grant, or 480 seconds for a 600-second grant. These are client renewal
choices, not registration/offline deadlines. SIP REGISTER and account
availability remain independent; never infer that a phone is offline simply
because its BLF lease is longer or there were no state changes.

One monotonic TimerHandle owns each lease. Refresh cancels its old handle;
late old callbacks cannot expire a renewed subscription. Terminal NOTIFY uses
the existing notifier task owner. Expiry, unsubscribe, failed NOTIFY and runtime
shutdown release handles and notifier resources. A renewal does not rebuild the
installation-wide capacity map because it cannot consume a new usage.

The same selected-flow distinction applies to complete INVITE requests.
`SipCallClient._prepare_invite_request_transport` retains an explicit UDP
Outbound next hop instead of releasing its borrowed call lease to open TCP.
Ordinary Contacts and trunk sockets keep the standard size upgrade and
connection-opening fallback. A pinned request above the 65507-byte UDP limit
returns an explicit transport error. Normal call cleanup releases that usage,
leaving another call's lease and the authenticated listener in place. Native
wire tests cover one authenticated INVITE above 1300 bytes, preparation
cancellation, stale binding rejection, hard-limit refusal and final lease counts.

The preserved negative witness is a peer that reads a complete NOTIFY and
closes before its response. The previous sender delivered the same request
twice; the corrected sender delivers it once and a new subscription recovers
the current full state. A separate pre-send close witness proves one delivery
after reconnect. Native HA tests cover large UDP notifications, Digest 401 and
407, ordinary TCP Contact routing, pinned Outbound UDP/TCP, generation fencing,
TCP closure between updates, real lease expiry, full renewal and final cleanup.

The large-document fixture sets the peer endpoint's actual display name. This
follows the current identity owner; an arbitrary session fallback is not an
authoritative endpoint name.

References: [RFC 3261 section 18.1.1](https://www.rfc-editor.org/rfc/rfc3261.html#section-18.1.1),
[RFC 5626 section 7](https://www.rfc-editor.org/rfc/rfc5626.html#section-7), and
[RFC 6665 sections 4.1.2 and 4.2.2](https://www.rfc-editor.org/rfc/rfc6665.html#section-4.1.2).
