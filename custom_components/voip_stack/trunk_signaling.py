"""Attach outbound calls to the signaling flow owned by their trunk."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .sip_client import SipCallClient
    from .sip_trunk import SipTrunkClient


def reuse_registered_trunk_flow(
    trunk: SipTrunkClient | None, client: SipCallClient,
) -> bool:
    """Borrow a registered flow; closing the call releases only its dialog."""
    open_dialog = getattr(trunk, "open_outbound_dialog", None)
    if not callable(open_dialog):
        return False
    call_id = client.dialog_ids.call_id
    opened = open_dialog(call_id)
    if opened is None:
        return False
    send, responses = opened
    client.use_reused_signaling_flow(
        send=send, responses=responses,
        close=lambda: trunk.close_outbound_dialog(call_id),
    )
    udp_port = getattr(trunk, "_udp_local_port", None)
    if udp_port:
        client.local_sip_port = int(udp_port)
    return True
