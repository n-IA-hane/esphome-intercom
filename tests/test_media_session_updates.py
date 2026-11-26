"""Behavioral tests for committed browser media-session updates."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace


MODULE = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "voip_stack"
    / "media_session_updates.py"
)
SPEC = importlib.util.spec_from_file_location("voip_stack_media_session_updates_test", MODULE)
assert SPEC is not None and SPEC.loader is not None
MEDIA_SESSION_UPDATES = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MEDIA_SESSION_UPDATES)
commit_audio_session_update = MEDIA_SESSION_UPDATES.commit_audio_session_update
commit_video_session_update = MEDIA_SESSION_UPDATES.commit_video_session_update


def test_audio_update_commits_full_contract_before_waking_owner() -> None:
    update_event = asyncio.Event()
    session = SimpleNamespace(media_generation=4, update_event=update_event)
    negotiated = SimpleNamespace(
        send_format="send",
        recv_format="receive",
        remote_rtp_host="192.0.2.10",
        remote_rtp_port="41000",
        audio_rtcp_allowed=False,
        local_audio_direction="recvonly",
        remote_audio_connection_held=True,
        send_dtmf_payload_type=126,
        send_dtmf_clock_rate=48000,
        send_dtmf_events=frozenset({6}),
    )

    commit_audio_session_update(
        session,
        negotiated,
        dtmf_payload_type=101,
        dtmf_events={0, 1, 2},
    )

    assert session.send_format == "send"
    assert session.recv_format == "receive"
    assert (session.remote_rtp_host, session.remote_rtp_port) == (
        "192.0.2.10",
        41000,
    )
    assert session.audio_rtcp_allowed is False
    assert session.local_audio_direction == "recvonly"
    assert session.remote_audio_connection_held is True
    assert session.dtmf_payload_type == 101
    assert session.dtmf_events == frozenset({0, 1, 2})
    assert session.send_dtmf_payload_type == 126
    assert session.send_dtmf_clock_rate == 48000
    assert session.send_dtmf_events == frozenset({6})
    assert session.media_generation == 5
    assert update_event.is_set()


def test_video_update_commits_rtp_rtcp_and_direction_before_waking_owner() -> None:
    update_event = asyncio.Event()
    session = SimpleNamespace(media_generation=7, update_event=update_event)
    negotiated = SimpleNamespace(
        video_format="send-video",
        recv_video_format="receive-video",
        remote_video_rtp_host="198.51.100.20",
        remote_video_rtp_port=42000,
        remote_video_rtcp_host="",
        remote_video_rtcp_port=0,
        remote_video_rtcp_mux=False,
        remote_video_payload_types=[96, 97],
        remote_video_connection_held=False,
    )

    commit_video_session_update(
        session,
        negotiated,
        local_direction="sendrecv",
    )

    assert (session.remote_rtp_host, session.remote_rtp_port) == (
        "198.51.100.20",
        42000,
    )
    assert (session.remote_rtcp_host, session.remote_rtcp_port) == (
        "198.51.100.20",
        42001,
    )
    assert session.remote_rtcp_mux is False
    assert session.remote_video_payload_types == (96, 97)
    assert session.video_format == "send-video"
    assert session.local_video_format == "receive-video"
    assert session.local_direction == "sendrecv"
    assert session.remote_connection_held is False
    assert session.media_generation == 8
    assert update_event.is_set()


def test_video_update_rejects_missing_video_contract_without_waking_owner() -> None:
    update_event = asyncio.Event()
    session = SimpleNamespace(media_generation=2, update_event=update_event)
    negotiated = SimpleNamespace(video_format=None)

    try:
        commit_video_session_update(
            session,
            negotiated,
            local_direction="inactive",
        )
    except ValueError as err:
        assert str(err) == "committed video update has no video format"
    else:
        raise AssertionError("missing video contract was accepted")

    assert session.media_generation == 2
    assert not update_event.is_set()
