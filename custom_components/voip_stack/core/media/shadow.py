"""Shadow comparison: run the new leg negotiator beside the engine in production.

The shadow receives the same immutable inputs and only reports. It never allocates a port,
answers a request, starts media or stores state, and it can never make a call fail: every error
inside it is caught and logged. Production acts on the result of the engine in use.
"""

from __future__ import annotations

import logging
import os

from ..sdp_audio import first_offered_dtmf_format
from ..sdp_types import RtpPcmDirection
from . import leg

_LOGGER = logging.getLogger(__name__)

# Which engine an entry point uses. "legacy": the engine in production, nothing else runs.
# "shadow": production decides, the new engine is computed beside it and differences are logged.
# The default costs nothing. The environment variable lets one installation collect real calls.
ENGINE_MODES = ("legacy", "shadow")
_engine_mode = os.environ.get("VOIP_STACK_MEDIA_ENGINE", "legacy")


def set_engine_mode(mode: str) -> None:
    global _engine_mode
    if mode not in ENGINE_MODES:
        raise ValueError(f"unknown media engine mode {mode!r}")
    _engine_mode = mode


def engine_mode() -> str:
    return _engine_mode if _engine_mode in ENGINE_MODES else "legacy"


def _field(fmt, name: str):
    if fmt is None:
        return None
    return {
        "codec": f"{fmt.encoding}/{fmt.sample_rate}/{fmt.channels}",
        "ptime": fmt.frame_ms,
        "payload": fmt.payload_type,
    }[name]


def inbound_offer_differences(
    remote_sdp,
    selected: RtpPcmDirection | None,
    *,
    dahua: bool = False,
    encodings: frozenset[str] | None = None,
) -> tuple[list[str], tuple[str, ...]]:
    """Return the fields where the new engine decides differently, and its trace.

    Field names are those of the differential ledger, so a line in a log can be checked
    against the declared contract changes.
    """
    policy = leg.PHONE_POLICY if encodings is None else leg.LegPolicy(
        send_rank=leg.PHONE_POLICY.send_rank, receive_rank=leg.PHONE_POLICY.receive_rank,
        encodings=encodings)
    result = leg.answer_offer(remote_sdp, policy, dahua=dahua)
    refused = isinstance(result, leg.Refusal)
    if selected is None or refused:
        old, new = ("reject" if selected is None else "accept"), ("reject" if refused else "accept")
        return ([f"outcome: {old} -> {new}"] if old != new else []), result.trace
    differences = []
    for side, old_fmt, new_fmt in (("send", selected.send, result.send), ("recv", selected.recv, result.receive)):
        for name in ("codec", "ptime", "payload"):
            old, new = _field(old_fmt, name), _field(new_fmt, name)
            if old != new:
                differences.append(f"{side}.{name}: {old} -> {new}")
    old_dtmf = first_offered_dtmf_format(remote_sdp)
    for name, old, new in (
        ("clock", getattr(old_dtmf, "sample_rate", None), getattr(result.dtmf, "sample_rate", None)),
        ("payload", getattr(old_dtmf, "payload_type", None), getattr(result.dtmf, "payload_type", None)),
    ):
        if old != new:
            differences.append(f"dtmf.{name}: {old} -> {new}")
    return differences, result.trace


def shadow_inbound_offer(call_id: str, remote_sdp, selected, *, dahua: bool = False, codec_policy=None) -> None:
    """Log what the new engine would have decided for an inbound offer. Never raises."""
    if engine_mode() != "shadow":
        return
    try:
        audio = getattr(codec_policy, "audio", None)
        differences, trace = inbound_offer_differences(
            remote_sdp, selected, dahua=dahua,
            encodings=frozenset(audio) if audio is not None else None)
    except Exception:  # noqa: BLE001 - the shadow must never affect a call
        _LOGGER.debug("media shadow failed call_id=%s", call_id, exc_info=True)
        return
    if differences:
        _LOGGER.info("media shadow differs call_id=%s entry=inbound-offer %s trace=[%s]",
                     call_id, "; ".join(differences), ", ".join(trace))
    else:
        _LOGGER.debug("media shadow agrees call_id=%s entry=inbound-offer", call_id)
