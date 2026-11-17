"""The explicit browser-TX witness cannot pass on counters or SDP alone."""

from copy import deepcopy
import sys

import pytest

from tools import sip_video_browser_probe as probe


def send_only_snapshot():
    return {
        "card_state": "in_call", "video_active": True, "engine_video_active": True,
        "video_direction": "sendonly", "engine_video_visible": False,
        "engine_stats": {"sent": 81, "received": 64,
            "video": {"sent": 7, "received": 0, "rendered": 0}},
        "backend_state": {"state": "in_call", "video_active": True,
            "video_negotiated": True, "video_direction": "sendonly",
            "video_rtp_tx_packets": 23, "video_access_units_tx": 6},
        "layout": {"canvas": None},
    }


def test_send_only_accepts_real_transmission_without_a_receive_canvas():
    probe._require_send_only_video(send_only_snapshot())


@pytest.mark.parametrize("path,value", [
    (("card_state",), "idle"),
    (("video_direction",), "recvonly"),
    (("video_direction",), "sendrecv"),
    (("video_active",), False),
    (("engine_video_active",), False),
    (("backend_state", "state"), "idle"),
    (("backend_state", "video_direction"), "inactive"),
    (("backend_state", "video_negotiated"), False),
    (("backend_state", "video_active"), False),
    (("engine_stats", "sent"), 0),
    (("engine_stats", "received"), 0),
    (("engine_stats", "video", "sent"), 0),
    (("backend_state", "video_rtp_tx_packets"), 0),
    (("backend_state", "video_access_units_tx"), 0),
])
def test_send_only_rejects_inactive_wrong_direction_or_missing_media(path, value):
    state = deepcopy(send_only_snapshot())
    target = state
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(RuntimeError):
        probe._require_send_only_video(state)


@pytest.mark.parametrize("other", ["--expect-audio-only", "--deny-camera", "--camera-send=off"])
def test_send_only_conflicts_fail_before_browser_or_auth(monkeypatch, other):
    monkeypatch.setattr(sys, "argv", ["probe", "--expect-video-send-only", other])
    with pytest.raises(SystemExit) as error:
        probe.main()
    assert error.value.code == 2
