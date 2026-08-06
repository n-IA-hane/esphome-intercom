"""Bounded DTMF collection for pre-answered inbound trunk calls."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging

from .core import sdp
from .dtmf import DtmfCollector, collect_info_digits


_LOGGER = logging.getLogger(__name__)
_FIRST_DIGIT_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class TrunkDtmfSelection:
    """The first complete dial-string returned by either negotiated channel."""

    digits: str = ""
    destination: str = ""


async def collect_trunk_dtmf(
    invite,
    *,
    info_queue: asyncio.Queue,
    source_rtp_port: int,
    routes: dict[str, str],
    timeout: float,
    terminator: str = "",
) -> TrunkDtmfSelection:
    """Race SIP INFO and negotiated RFC 4733 without leaking the losing task."""

    collectors = {
        asyncio.create_task(
            collect_info_digits(
                info_queue,
                routes=routes,
                timeout=timeout,
                first_digit_timeout=max(_FIRST_DIGIT_TIMEOUT, timeout),
                terminator=terminator,
            ),
            name=f"voip-trunk-info-dtmf-{invite.call_id}",
        )
    }
    formats = sdp.offered_dtmf_formats(invite.remote_sdp)
    dtmf_format = formats[0] if formats else None
    if dtmf_format is not None:
        collectors.add(
            asyncio.create_task(
                DtmfCollector(
                    host="0.0.0.0",
                    port=source_rtp_port,
                    payload_type=dtmf_format.payload_type,
                    routes=routes,
                    timeout=timeout,
                    first_digit_timeout=max(_FIRST_DIGIT_TIMEOUT, timeout),
                    terminator=terminator,
                    remote_host=invite.remote_rtp_host,
                ).collect(),
                name=f"voip-trunk-rtp-dtmf-{invite.call_id}",
            )
        )
    else:
        _LOGGER.info(
            "SIP trunk inbound call has no telephone-event SDP offer; "
            "collecting SIP INFO with %.1fs inter-digit timeout",
            timeout,
        )

    pending = set(collectors)
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                try:
                    digits, destination = task.result()
                except Exception as err:  # noqa: BLE001 - one channel may fail.
                    _LOGGER.info("SIP trunk DTMF collector unavailable: %s", err)
                    continue
                if digits:
                    return TrunkDtmfSelection(digits, destination)
        return TrunkDtmfSelection()
    finally:
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
