"""Transport-independent SIP dialog identity helpers."""

from __future__ import annotations

from dataclasses import dataclass

from . import sip


@dataclass(frozen=True, slots=True)
class DialogKey:
    """RFC 3261 dialog identity from the local endpoint's perspective.

    A TCP connection, UDP source address, Via branch, and Contact target are
    deliberately not part of this key. They belong to transport bindings or
    transactions and may legitimately change while a dialog remains alive.
    """

    call_id: str
    local_tag: str
    remote_tag: str

    @classmethod
    def for_uas_dialog(
        cls,
        initial_request: sip.SipMessage,
        *,
        local_tag: str,
    ) -> DialogKey:
        """Build the key for a dialog created by an inbound request."""

        return cls(
            call_id=initial_request.header("Call-ID"),
            local_tag=str(local_tag or ""),
            remote_tag=sip.extract_tag(initial_request.header("From")),
        )

    @classmethod
    def from_uas_request(cls, request: sip.SipMessage) -> DialogKey:
        """Build the key carried by a subsequent inbound UAS request."""

        return cls(
            call_id=request.header("Call-ID"),
            local_tag=sip.extract_tag(request.header("To")),
            remote_tag=sip.extract_tag(request.header("From")),
        )

    @property
    def complete(self) -> bool:
        """Return whether all mandatory dialog identifiers are present."""

        return bool(self.call_id and self.local_tag and self.remote_tag)


@dataclass(frozen=True, slots=True)
class DialogRequest:
    """Serialized in-dialog request with its transaction and route identity."""

    raw: bytes
    ids: sip.SipDialogIds
    routing: sip.SipDialogRoute


def build_dialog_request(
    method: str,
    *,
    call_id: str,
    local_tag: str,
    remote_tag: str,
    cseq: int,
    local_uri: str,
    remote_uri: str,
    remote_target_uri: str,
    route_set: tuple[str, ...] = (),
    contact_uri: str = "",
    transport: str = "UDP",
    local_display_name: str = "",
    remote_display_name: str = "",
    extra_headers: tuple[tuple[str, str], ...] = (),
    content_type: str = "",
    body: bytes = b"",
) -> DialogRequest:
    """Build one standards-shaped request shared by every dialog owner."""

    routing = sip.dialog_request_routing(remote_target_uri or remote_uri, route_set)
    ids = sip.SipDialogIds(
        call_id=call_id,
        local_tag=local_tag,
        remote_tag=remote_tag,
        cseq=cseq,
        branch=sip.make_branch(),
    )
    headers = sip.dialog_headers(
        request_uri=routing.request_uri,
        local_uri=local_uri,
        remote_uri=remote_uri,
        dialog=ids,
        method=method,
        contact_uri=contact_uri or local_uri,
        content_type=content_type,
        transport=transport,
        local_display_name=local_display_name,
        remote_display_name=remote_display_name,
    )
    headers.extend(("Route", value) for value in routing.route_headers)
    headers.extend(extra_headers)
    return DialogRequest(
        sip.build_request(method, routing.request_uri, headers, body),
        ids,
        routing,
    )


def uas_request_matches_dialog(
    request: sip.SipMessage,
    initial_request: sip.SipMessage,
    *,
    local_tag: str,
) -> bool:
    """Match an inbound in-dialog request independently of its transport."""

    expected = DialogKey.for_uas_dialog(initial_request, local_tag=local_tag)
    received = DialogKey.from_uas_request(request)
    return expected.complete and received.complete and received == expected
