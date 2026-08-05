"""Bounded snapshots of call-scoped VoIP runtime resources."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .runtime_data import BrowserMediaRuntime


def _active_task_count(value: object) -> int:
    """Count unfinished tasks in a runtime collection."""

    items = value.values() if isinstance(value, dict) else value
    if not isinstance(items, (list, tuple, set, frozenset)) and not hasattr(
        items, "__iter__"
    ):
        return 0
    count = 0
    for task in items:
        done = getattr(task, "done", None)
        if not callable(done) or not done():
            count += 1
    return count


def _mapping_keys(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    return sorted(str(key) for key in value)


def runtime_resource_snapshot(
    bucket: dict[str, Any],
    registry: Any | None,
    *,
    detailed: bool = False,
    rtp_port_pool: dict[str, Any] | None = None,
    call_artifacts: Any | None = None,
    browser_media: BrowserMediaRuntime | None = None,
) -> dict[str, Any]:
    """Return stable counts used to prove that a call cleaned up completely.

    Long-lived listener and trunk tasks remain visible so a lab can compare
    the result with its idle baseline. Call-scoped resources are listed
    separately and must return to zero after every terminal call path.
    """

    registry_snapshot: dict[str, Any] = {}
    snapshot = getattr(registry, "snapshot", None)
    if callable(snapshot):
        registry_snapshot = dict(snapshot())
    counts = {
        str(key): int(value)
        for key, value in dict(registry_snapshot.get("resource_counts") or {}).items()
    }
    active_audio = browser_media.sessions_for("audio") if browser_media else {}
    active_video = browser_media.sessions_for("video") if browser_media else {}
    audio_owners = browser_media.owners_for("audio") if browser_media else {}
    video_owners = browser_media.owners_for("video") if browser_media else {}
    identity_locks = browser_media.identity_locks if browser_media else {}
    port_pool = rtp_port_pool or bucket.get("sip_rtp_port_pool")
    used_ports = (
        set(port_pool.get("used") or ()) if isinstance(port_pool, dict) else set()
    )
    counts.update(
        {
            "active_audio_sessions": len(active_audio)
            if isinstance(active_audio, dict)
            else 0,
            "active_video_sessions": len(active_video)
            if isinstance(active_video, dict)
            else 0,
            "audio_ws_owners": len(audio_owners)
            if isinstance(audio_owners, dict)
            else 0,
            "video_ws_owners": len(video_owners)
            if isinstance(video_owners, dict)
            else 0,
            "media_identity_locks": len(identity_locks)
            if isinstance(identity_locks, dict)
            else 0,
            "allocated_rtp_ports": len(used_ports),
            "forward_tasks": _active_task_count(
                getattr(call_artifacts, "forward_tasks", {})
            ),
            "call_deadlines": _active_task_count(
                getattr(call_artifacts, "deadlines", {})
            ),
            "runtime_tasks": _active_task_count(bucket.get("runtime_tasks", set())),
            "video_transcoders": int(bucket.get("video_transcoder_active") is not None),
        }
    )
    call_scoped_keys = (
        "sessions",
        "legs",
        "pending_routes",
        "pending_invites",
        "preanswered",
        "softphone_media",
        "sip_clients",
        "client_watchers",
        "relays",
        "bridges",
        "endpoint_claims",
        "active_audio_sessions",
        "active_video_sessions",
        "audio_ws_owners",
        "video_ws_owners",
        "media_identity_locks",
        "allocated_rtp_ports",
        "forward_tasks",
        "call_deadlines",
        "video_transcoders",
    )
    result: dict[str, Any] = {
        "resource_counts": counts,
        "call_scoped_quiescent": all(
            int(counts.get(key, 0)) == 0 for key in call_scoped_keys
        ),
    }
    if detailed:
        result["call_ids"] = {
            "registry": list(registry_snapshot.get("call_ids") or ()),
            "pending": list(registry_snapshot.get("pending_call_ids") or ()),
            "media": list(registry_snapshot.get("media_call_ids") or ()),
            "audio_sessions": _mapping_keys(active_audio),
            "video_sessions": _mapping_keys(active_video),
            "audio_owners": _mapping_keys(audio_owners),
            "video_owners": _mapping_keys(video_owners),
        }
        result["allocated_rtp_ports"] = sorted(int(port) for port in used_ports)
    return result
