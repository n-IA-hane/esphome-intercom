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
