from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from scripts.run_p4_h264_reinvite_hil import (
    SerialCapture,
    _wait_ha_endpoint_ready,
    cleanup_evidence,
    parse_ffprobe,
    parse_serial_metrics,
    quiet_p4,
    validate_cycle,
)


SERIAL = """
[I][p4_video_renderer]: H.264 first AU: bytes=7900 key=YES
[I][esp_h264_video_source]: H.264 TX stats: raw=20 converted=18 encoded=18 convert_avg_us=2
[I][p4_video_renderer]: H.264 RX stats: admitted=19 rendered=17 rate_drop=0 busy_drop=0 dependency_drop=0 decode_drop=2 geometry_drop=0 presented=16 refresh_done=16
[I][voip_stack.video]: Video session totals: tx=50 rx=70 completed_au=18 dropped_au=0 backpressure=0 send_fail=0 slow_send=0
"""

COMPACT_SERIAL = """
[I][p4_video_renderer]: H.264 first AU: bytes=7900 key=YES
[I][esp_h264_video_source]: H.264 TX evidence: raw=20 encoded=18
[I][p4_video_renderer]: H.264 RX evidence: admitted=19 rendered=17 presented=16 refresh_done=16
[I][voip_stack.video]: Video session evidence: tx=50 rx=70 completed_au=18 dropped_au=0
"""


def _peer(*, remote_bye: bool = False) -> dict[str, object]:
    return {
        "ok": True,
        "initial_codec": "audio",
        "sip_statuses": [100, 180, 200],
        "answer_sdp": "v=0\r\nm=audio 40000 RTP/AVP 8\r\n",
        "reinvite_status": 200,
        "reinvite_negotiated_video": "pt=102:H264/90000;direction=sendrecv",
        "reinvite_video_direction": "sendrecv",
        "video_transitions": [
            {
                "name": "reinvite",
                "active": True,
                "audio_tx_packets": 50,
                "audio_rx_packets": 49,
            }
        ],
        "audio_tx_packets": 200,
        "audio_rx_packets": 198,
        "video_tx_packets": 100,
        "video_rx_packets": 90,
        "video_rx_marker_packets": 20,
        "bye_response_status": None if remote_bye else 200,
        "remote_bye": remote_bye,
    }


def test_serial_parser_extracts_presentation_and_transport_counters() -> None:
    metrics = parse_serial_metrics(SERIAL)

    assert metrics["first_keyframe"] is True
    assert metrics["rx"]["rendered"] == 17
    assert metrics["rx"]["presented"] == 16
    assert metrics["tx"]["encoded"] == 18
    assert metrics["session"]["completed_au"] == 18
    assert metrics["fatal_errors"] == []


def test_serial_parser_accepts_release_evidence_without_debug_logging() -> None:
    metrics = parse_serial_metrics(COMPACT_SERIAL)

    assert metrics["first_keyframe"] is True
    assert metrics["rx"]["presented"] == 16
    assert metrics["tx"]["encoded"] == 18
    assert metrics["session"]["completed_au"] == 18


def test_serial_capture_recognizes_compact_release_evidence(tmp_path) -> None:
    capture = SerialCapture("unused", tmp_path / "serial.log")
    capture._lines.extend(COMPACT_SERIAL.splitlines(keepends=True))

    assert capture.wait_cycle(0, timeout=0.01) == COMPACT_SERIAL


def test_serial_parser_rejects_missing_owner_evidence() -> None:
    with pytest.raises(AssertionError, match="missing H.264 RX stats"):
        parse_serial_metrics("H.264 first AU: key=YES\n")


def test_serial_parser_reports_fatal_runtime_events() -> None:
    metrics = parse_serial_metrics(SERIAL + "Guru Meditation Error\n")

    assert metrics["fatal_errors"] == ["Guru Meditation"]


def test_ffprobe_requires_decodable_h264_frames() -> None:
    result = parse_ffprobe(
        {
            "streams": [
                {
                    "codec_name": "h264",
                    "width": 352,
                    "height": 288,
                    "nb_read_frames": "12",
                }
            ]
        }
    )

    assert result == {"codec": "h264", "width": 352, "height": 288, "frames": 12}


@pytest.mark.parametrize("owner,remote", [("peer", False), ("p4", True)])
def test_cycle_validation_proves_both_hangup_owners(owner: str, remote: bool) -> None:
    result = validate_cycle(
        _peer(remote_bye=remote),
        parse_serial_metrics(SERIAL),
        {"codec": "h264", "width": 352, "height": 288, "frames": 12},
        termination_owner=owner,
    )

    assert all(result["checks"].values())


def test_cycle_validation_rejects_video_without_prior_audio() -> None:
    peer = _peer()
    peer["video_transitions"][0]["audio_rx_packets"] = 0

    with pytest.raises(AssertionError, match="audio_before_video"):
        validate_cycle(
            peer,
            parse_serial_metrics(SERIAL),
            {"codec": "h264", "width": 352, "height": 288, "frames": 12},
            termination_owner="peer",
        )


def test_cycle_validation_requires_more_completed_than_dropped_access_units() -> None:
    serial = SERIAL.replace(
        "completed_au=18 dropped_au=0", "completed_au=18 dropped_au=17"
    )
    assert all(
        validate_cycle(
            _peer(),
            parse_serial_metrics(serial),
            {"codec": "h264", "width": 352, "height": 288, "frames": 12},
            termination_owner="peer",
        )["checks"].values()
    )

    with pytest.raises(AssertionError, match="p4_video_session"):
        validate_cycle(
            _peer(),
            parse_serial_metrics(
                serial.replace(
                    "completed_au=18 dropped_au=17", "completed_au=18 dropped_au=18"
                )
            ),
            {"codec": "h264", "width": 352, "height": 288, "frames": 12},
            termination_owner="peer",
        )


def test_cleanup_requires_ha_and_p4_quiescence() -> None:
    snapshot = {
        "state": "idle",
        "active_dialogs": 0,
        "runtime_resources": {
            "call_scoped_quiescent": True,
            "resource_counts": {"allocated_rtp_ports": 0},
        },
    }

    assert cleanup_evidence(snapshot, "idle")["call_scoped_quiescent"] is True
    with pytest.raises(AssertionError, match="did not quiesce"):
        cleanup_evidence(snapshot, "in_call")


def test_ha_endpoint_readiness_requires_same_esp_host_and_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeWs:
        calls = 0

        async def command(self, _message: dict[str, object]) -> dict[str, object]:
            self.calls += 1
            devices = [
                {
                    "endpoint_id": "browser:wrong",
                    "endpoint_type": "browser",
                    "extension": "668",
                    "host": "",
                },
                {
                    "endpoint_id": "esphome:p4",
                    "endpoint_type": "esphome",
                    "extension": "668",
                    "host": "192.0.2.4",
                },
            ]
            return {"result": {"devices": devices}}

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    ws = FakeWs()

    result = asyncio.run(_wait_ha_endpoint_ready(ws, "668", "192.0.2.4"))

    assert result == {
        "endpoint_type": "esphome",
        "extension": "668",
        "resolved": True,
    }
    assert ws.calls == 2


class _FakeEsp:
    def __init__(self) -> None:
        self.spec = SimpleNamespace(key="p4")
        self.values = {"master_volume": 37.0, "auto_answer": True}
        self.entities = {"master_volume": SimpleNamespace(key=1)}
        self.actions: list[tuple[str, object]] = []
        self.client = SimpleNamespace(number_command=self.number_command)

    async def number_command(self, _key: int, value: float) -> None:
        self.actions.append(("volume", value))
        self.values["master_volume"] = value

    async def wait(self, *_args, **_kwargs) -> None:
        return None

    async def switch(self, _name: str, value: bool) -> None:
        self.actions.append(("auto_answer", value))
        self.values["auto_answer"] = value


class _RestoreFailureEsp(_FakeEsp):
    async def switch(self, name: str, value: bool) -> None:
        await super().switch(name, value)
        if value:
            raise RuntimeError("auto-answer restore failed")


def test_quiet_p4_restores_settings_when_body_fails() -> None:
    esp = _FakeEsp()

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="scenario failed"):
            async with quiet_p4(esp):
                assert esp.values == {"master_volume": 1.0, "auto_answer": False}
                raise RuntimeError("scenario failed")

    asyncio.run(exercise())

    assert esp.values == {"master_volume": 37.0, "auto_answer": True}
    assert esp.actions == [
        ("volume", 1.0),
        ("auto_answer", False),
        ("auto_answer", True),
        ("volume", 37.0),
    ]


def test_quiet_p4_attempts_volume_restore_after_other_restore_failure() -> None:
    esp = _RestoreFailureEsp()

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="restoration failed"):
            async with quiet_p4(esp):
                pass

    asyncio.run(exercise())

    assert esp.values["master_volume"] == 37.0
    assert esp.actions[-1] == ("volume", 37.0)
