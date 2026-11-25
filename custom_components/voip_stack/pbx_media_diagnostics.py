"""Read committed media processing for native account diagnostics.

No delivery cache or ESP API command is needed: the existing phone projection
notifies native entities when the current relay's semantic processing changes.
"""

from .core.sdp import remote_can_receive, remote_can_send
from .endpoint_session import LegKind, LegPhase

PROCESSING_STATES = ["idle", "unknown", "inactive", "forwarded", "processed", "mixed"]
_MEDIA_FIELDS = ("audio_tx", "audio_rx", "video_tx", "video_rx")


def _video_operations(relay):
    video = relay.video_relay
    if video is None:
        return "inactive", "inactive"
    result = []
    for side, source, target in (
        ("left", video.left, video.right),
        ("right", video.right, video.left),
    ):
        if not remote_can_send(source.video_format) or not remote_can_receive(
            target.video_format, connection_held=target.connection_held
        ):
            result.append("inactive")
        elif not video.processing_known_from(side):
            result.append("unknown")
        else:
            result.append("converted" if video.transcodes_from(side) else "forwarded")
    return tuple(result)


def call_media_processing(runtime, endpoint_id, call):
    """Return TX/RX relative to this account's exact committed SIP leg.

    A browser/local PCM adapter has additional processing boundaries. Until its
    owner exposes those operations, report unknown rather than equating a coarse
    direct route with unchanged media. Never reuse a previous call generation.
    """
    unknown = dict.fromkeys(_MEDIA_FIELDS, "unknown")
    owner = runtime.sip
    session = owner.get_session(call["call_id"], generation=call["generation"]) if owner is not None else None
    if session is None or not session.live or call["state"] != "confirmed":
        return unknown
    leg = session.legs.get(call["leg_id"])
    if (leg is None or leg.endpoint_id != endpoint_id or leg.kind != LegKind.SIP
            or leg.phase not in {LegPhase.ANSWERED, LegPhase.HELD}
            or (call["sip_call_id"] and leg.sip_call_id != call["sip_call_id"])):
        return unknown
    sides = [index for index, key in enumerate(("source_endpoint_id", "dest_endpoint_id"))
             if session.metadata.get(key) == endpoint_id]
    resources = [item.value for item in session.resources if item.name.startswith("relay:")]
    from .sip_rtp_bridge import SipRtpRelay

    if len(sides) != 1 or len(resources) != 1 or not isinstance(resources[0], SipRtpRelay):
        return unknown
    audio, video = resources[0].audio_operations, _video_operations(resources[0])
    side, reverse = sides[0], 1 - sides[0]
    return dict(zip(_MEDIA_FIELDS, (audio[side], audio[reverse], video[side], video[reverse]), strict=True))


def account_media_processing(runtime, endpoint_id, calls):
    """Aggregate a detached call projection while preserving every occurrence."""
    details = [{**{key: call[key] for key in ("call_id", "generation", "leg_id", "sip_call_id")},
                **call_media_processing(runtime, endpoint_id, call)} for call in calls]
    operations = {item[field] for item in details for field in _MEDIA_FIELDS}
    active = operations - {"inactive"}
    state = (
        "idle" if not details else
        "unknown" if "unknown" in operations else
        "inactive" if not active else
        "forwarded" if active == {"forwarded"} else
        "mixed" if "forwarded" in active else
        "processed"
    )
    return state, {"call_count": len(details), "calls": details}


def publish_media_processing(_hass, data, session):
    """Refresh the shared projection; unchanged values produce no HA update."""
    observer = data.sip.component("phone_state")
    if observer is not None:
        observer.media_changed(session.token)
