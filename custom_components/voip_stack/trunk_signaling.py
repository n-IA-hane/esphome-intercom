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
    if trunk is not None and getattr(trunk, "config", None) is not None:
        from .core.codec_policy import CodecPolicy
        client.set_codec_policy(CodecPolicy(trunk.config.audio_codecs, trunk.config.video_codecs,
            getattr(trunk.config, "audio_ordered", True), getattr(trunk.config, "video_ordered", True),
            getattr(trunk.config, "audio_configured", None)))
        client.media_ip = trunk.config.outbound_media_address or client.local_ip
        if getattr(trunk.config, "server", ""):
            client.initial_next_hop = (trunk.config.server, int(trunk.config.port))
        from .trunk_media import discover_pending_media, prepare_trunk_media
        client.prepare_media = lambda host: prepare_trunk_media(
            client, trunk.config, host, getattr(trunk, "media_stun_urls", None)
        )
        client.discover_media_sockets = lambda sockets: discover_pending_media(
            sockets, getattr(trunk, "media_stun_urls", None)
        )
        public_gruu = getattr(trunk, "public_gruu", "") if getattr(trunk, "registered", False) else ""
        if public_gruu:
            client.local_contact_uri = public_gruu
    open_dialog = getattr(trunk, "open_outbound_dialog", None)
    if not callable(open_dialog):
        return False
    call_id = client.dialog_ids.call_id
    opened = open_dialog(call_id)
    if opened is None:
        return False
    send, responses = opened
    # The existing registration owner has already selected this UDP next hop.
    # Do not pin subsequent dialog requests to it after Contact/Record-Route.
    client.initial_next_hop = trunk.active_registrar_target
    client.use_reused_signaling_flow(
        send=send, responses=responses,
        close=lambda: trunk.close_outbound_dialog(call_id),
        udp_send_to=lambda raw, target: trunk.send_outbound_dialog_to(call_id, raw, target),
    )
    bind_usage = getattr(trunk, "bind_outbound_usage", None)
    if bind_usage is not None:
        client.bind_signaling_usage = lambda usage_id: bind_usage(call_id, usage_id)
    udp_port = getattr(trunk, "_udp_local_port", None)
    if udp_port:
        client.local_sip_port = int(udp_port)
    return True
