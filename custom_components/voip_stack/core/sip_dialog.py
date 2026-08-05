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
