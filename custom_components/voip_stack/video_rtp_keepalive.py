"""RFC 6263 video mapping maintenance independent of HA and codec workers."""

from __future__ import annotations

import asyncio
import logging

from .core.video_rtp import unknown_dynamic_payload_type

_LOGGER = logging.getLogger(f"{__package__}.video_ws_view")


async def run_symmetric_video_keepalive(
    session, *, closed, keepalive_updated, counters, sender, rtp_send_lock,
    rtp_source, transport, destination, store_counters,
    probe_interval: float, probe_attempts: int, refresh_interval: float,
) -> None:
    """Open and refresh the RTP mapping for the whole video session.

    SIP media relays commonly use symmetric RTP when an endpoint is behind
    NAT. A recvonly answer, delayed camera permission, or a browser with
    Send Camera disabled can otherwise leave the advertised private RTP
    port unreachable. RFC 6263 section 4.6 defines a zero-payload RTP
    packet on an unnegotiated dynamic payload type for this purpose; it
    carries no video media but lets the relay learn the symmetric RTP
    source tuple.
    """

    loop = asyncio.get_running_loop()
    attempt = 0
    last_warning_at = 0.0
    observed_generation = -1
    keepalive_payload_type: int | None = None
    generation_rx_packets = 0
    generation_tx_packets = 0
    while not closed.is_set():
        keepalive_updated.clear()
        generation = int(session.media_generation)
        if generation != observed_generation:
            negotiated_payload_types = {
                *session.remote_video_payload_types,
                session.send_video_format.payload_type,
                session.recv_video_format.payload_type,
            }
            keepalive_payload_type = unknown_dynamic_payload_type(
                negotiated_payload_types
            )
            counters["video_symmetric_rtp_keepalive_payload_type"] = int(
                keepalive_payload_type or 0
            )
            observed_generation = generation
            attempt = 0
            generation_rx_packets = counters["video_rtp_rx_packets"]
            generation_tx_packets = counters["video_rtp_tx_packets"]
            if keepalive_payload_type is None:
                _LOGGER.warning(
                    "SIP video symmetric RTP keepalive unavailable call_id=%s: all dynamic payload types negotiated",
                    session.call_id,
                )
        if session.remote_connection_held:
            try:
                await asyncio.wait_for(
                    keepalive_updated.wait(), timeout=refresh_interval
                )
            except TimeoutError:
                pass
            continue
        if keepalive_payload_type is None:
            try:
                await asyncio.wait_for(
                    keepalive_updated.wait(), timeout=refresh_interval
                )
            except TimeoutError:
                pass
            continue
        probing = bool(
            attempt < probe_attempts
            and counters["video_rtp_rx_packets"] == generation_rx_packets
            and counters["video_rtp_tx_packets"] == generation_tx_packets
        )
        interval = (
            probe_interval
            if probing
            else refresh_interval
        )
        if not probing:
            try:
                await asyncio.wait_for(keepalive_updated.wait(), timeout=interval)
            except TimeoutError:
                pass
            if closed.is_set():
                return
            if keepalive_updated.is_set():
                continue
            if (
                sender.last_regular_rtp_tx_at
                and loop.time() - sender.last_regular_rtp_tx_at < interval
            ):
                continue
        try:
            async with rtp_send_lock:
                keepalive = rtp_source.build_keepalive(
                    keepalive_payload_type,
                    now=loop.time(),
                )
                # build_keepalive advances the shared source even if the
                # datagram send fails; keep the browser sender aligned.
                sender.sequence = int(rtp_source.sequence)
                transport.sendto(keepalive, destination())
        except (OSError, RuntimeError, ValueError) as err:
            counters["video_keepalive_task_errors"] += 1
            attempt += 1
            now = loop.time()
            if now - last_warning_at >= refresh_interval:
                _LOGGER.warning(
                    "HA softphone video RTP keepalive failed call_id=%s error=%s",
                    session.call_id,
                    err,
                )
                last_warning_at = now
            store_counters()
            retry_delay = min(
                refresh_interval,
                probe_interval * (2 ** min(attempt, 6)),
            )
            try:
                await asyncio.wait_for(keepalive_updated.wait(), timeout=retry_delay)
            except TimeoutError:
                pass
            continue
        counters["video_symmetric_rtp_keepalives"] += 1
        attempt += 1
        if probing:
            try:
                await asyncio.wait_for(
                    keepalive_updated.wait(), timeout=probe_interval
                )
            except TimeoutError:
                pass
