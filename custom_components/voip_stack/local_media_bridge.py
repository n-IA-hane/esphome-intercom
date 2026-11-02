"""Transfer a local application's existing RTP socket to the shared relay."""

import asyncio
from dataclasses import dataclass

from .sip_bridge import invite_rtp_peer
from .sip_rtp_bridge import RtpPeer, SipRtpRelay, _RelayProtocol
from .local_call_media import ApplicationCallSource, LocalCallMedia
from .core.rtp import AudioRtpSenderState


def application_rtp_peer(source):
    if not isinstance(source, ApplicationCallSource):
        return invite_rtp_peer(source)
    return RtpPeer(source.remote_rtp_host, source.remote_rtp_port, source.recv_format.payload_type,
        source.recv_format.audio_format, rtp_format=source.recv_format,
        inbound_rtp_formats=source.recv_formats, outbound_rtp_formats=source.send_formats,
        match_received_codec=source.match_received_codec,
        send_payload_type=source.send_format.payload_type, send_audio_format=source.send_format.audio_format,
        send_rtp_format=source.send_format, dtmf_payload_type=source.dtmf_payload_type,
        dtmf_clock_rate=source.dtmf_clock_rate, dtmf_events=source.dtmf_events,
        send_dtmf_payload_type=source.send_dtmf_payload_type, send_dtmf_clock_rate=source.send_dtmf_clock_rate,
        send_dtmf_events=source.send_dtmf_events, can_send=source.local_audio_direction in {"recvonly", "sendrecv"},
        can_receive=source.local_audio_direction in {"sendonly", "sendrecv"} and not source.remote_audio_connection_held,
        connection_held=source.remote_audio_connection_held, signaling_host=source.source_host)


def adopt_relay_for_application(hass, relay, side, contract, reservation, on_complete):
    """Keep one established peer's socket and clock while retiring its bridge."""
    if side not in {"left", "right"} or relay._stop_requested or relay._start_task is not None or relay.video_relay is not None:
        raise ValueError("Application handoff requires an established audio relay")
    transport = getattr(relay, f"{side}_transport")
    if transport is None or transport.is_closing():
        raise RuntimeError("The retained media transport has ended")
    peer = getattr(relay, side)
    media = LocalCallMedia(hass, invite=contract, local_rtp_port=getattr(relay, f"{side}_port"),
        reservation=reservation, on_complete=on_complete,
        rtp_source=AudioRtpSenderState(peer.sequence, peer.timestamp, peer.ssrc, peer.clock_rate))
    media.remote_rtp_port, media.remote_ssrc = peer.port, peer.rx_ssrc
    encoder = getattr(relay, f"{side}_encoder")
    if encoder is not None:
        media.encoder.follow_received(encoder.fmt)
    media.adopt_transport(transport)
    setattr(relay, f"{side}_transport", None)
    relay._on_release = None
    relay._stop_requested = True
    return media


@dataclass(slots=True)
class LocalMediaBridge:
    media: object
    invite: object
    relay: SipRtpRelay
    transport: object
    committed: bool = False

    @classmethod
    async def prepare(cls, media, remote_invite):
        """Bind only the spare reserved port, leaving live application media alone."""
        if media.closed.is_set() or media.transport is None:
            raise RuntimeError("The local application has ended")
        invite, transport = media.invite, media.transport
        if getattr(invite, "video_format", None) or getattr(remote_invite, "video_format", None):
            raise ValueError("Local application handoff requires an audio-only call")
        relay = SipRtpRelay(
            left=application_rtp_peer(invite), right=invite_rtp_peer(remote_invite),
            left_port=media.local_rtp_port, right_port=media.reservation.ports[1],
        )
        result = cls(media, invite, relay, transport)
        sock = media.reservation.take_audio_socket(relay.right_port)
        if sock is None:
            sock = relay._rtp_socket(relay.right_port)
        try:
            relay.right_transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: _RelayProtocol(relay, "right", paused=True), sock=sock,
            )
            sock = None
            result.validate()
            return result
        except BaseException:
            if sock is not None:
                sock.close()
            await relay.stop()
            raise

    def validate(self):
        if (self.committed or self.media.closed.is_set()
                or self.media.invite is not self.invite
                or self.media.transport is not self.transport
                or self.transport.is_closing()
                or self.relay._stop_requested):
            raise RuntimeError("Application media changed during handoff")

    def commit(self):
        """Switch the protocol and ownership together, without an await window."""
        self.validate()
        old_protocol = self.transport.get_protocol()
        protocol = _RelayProtocol(self.relay, "left")
        protocol.connection_made(self.transport)
        try:
            self.transport.set_protocol(protocol)
            transport = self.media.detach_transport()
        except BaseException:
            self.transport.set_protocol(old_protocol)
            raise
        self.relay.left_transport = transport
        self.relay.right_transport.resume_reading()
        left, clock = self.relay.left, self.media.rtp_source
        left.sequence, left.timestamp, left.ssrc = clock.sequence, clock.timestamp, clock.ssrc
        left.clock_rate = clock.clock_rate
        left.port, left.rx_ssrc = self.media.remote_rtp_port, self.media.remote_ssrc
        if self.relay.left_encoder is not None:
            self.relay.left_encoder.follow_received(self.media.encoder.fmt)
        self.committed = True
        return self.relay

    async def rollback(self):
        if not self.committed:
            await self.relay.stop()
