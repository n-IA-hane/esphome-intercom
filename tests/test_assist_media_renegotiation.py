"""Regression for an in-dialog Wildix offer while Assist owns RTP."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "custom_components" / "voip_stack" / "media_renegotiation.py"
PACKAGE = "voip_assist_reinvite_test"


def _module(name: str, **values):
    module = types.ModuleType(f"{PACKAGE}.{name}")
    for key, value in values.items():
        setattr(module, key, value)
    sys.modules[module.__name__] = module
    return module


def _load_module(registry, answer_calls: list[dict]):
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(SOURCE.parent)]
    sys.modules[PACKAGE] = package

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    sys.modules.setdefault("homeassistant", homeassistant)
    sys.modules.setdefault("homeassistant.core", core)

    class AssistMediaSession:
        def __init__(self) -> None:
            self.local_rtp_port = 41000
            self.committed = False

        def prepare_media_update(self, _updated):
            def commit() -> None:
                self.committed = True

            return commit

    _module("assist_runtime", AssistMediaSession=AssistMediaSession)
    _module("const", DOMAIN="voip_stack", HA_SOFTPHONE_DEVICE_ID="device")
    _module("endpoint_lifecycle", call_registry=lambda _hass: registry)

    decision = types.SimpleNamespace(accepted=True, reason="")
    _module(
        "media_offer_answer",
        validate_bridged_video_reoffer=lambda *_args, **_kwargs: decision,
        validate_direct_video_reoffer=lambda *_args, **_kwargs: decision,
    )
    _module(
        "media_ports",
        release_video_media_reservation=lambda _item: None,
        reserve_sip_video_media=lambda _hass: (),
    )
    _module(
        "media_session_updates",
        commit_audio_session_update=lambda *_args, **_kwargs: None,
        commit_video_session_update=lambda *_args, **_kwargs: None,
    )
    _module("phone_endpoint", DEFAULT_ENDPOINT_ID="default")

    def build_answer_directional(*_args, **kwargs):
        answer_calls.append(dict(kwargs))
        return "m=audio 41000 RTP/AVP 111\r\nm=video 0 RTP/AVP 104\r\n"

    _module(
        "sdp",
        build_answer_directional=build_answer_directional,
        constrained_media_direction=lambda *_args, **_kwargs: "sendrecv",
        constrained_video_direction=lambda *_args, **_kwargs: "inactive",
        first_offered_dtmf_format=lambda _sdp: None,
    )
    _module(
        "sip_bridge",
        invite_rtp_peer=lambda invite: invite,
        invite_video_rtp_peer=lambda invite: invite,
    )

    @dataclass(frozen=True)
    class SipInviteResult:
        status: int
        reason: str
        answer_sdp: str = ""
        to_tag: str = ""
        defer_final: bool = False
        decline_reason: str = ""
        commit: object | None = None
        rollback: object | None = None

    _module("sip_listener", SipInvite=object, SipInviteResult=SipInviteResult)
    _module(
        "sip_video_relay",
        remote_can_receive=lambda *_args, **_kwargs: False,
        remote_can_send=lambda *_args, **_kwargs: False,
    )
    _module(
        "websocket_api",
        _fire_call_event=lambda *_args, **_kwargs: None,
        _ha_softphone_store=lambda *_args, **_kwargs: {},
    )

    spec = importlib.util.spec_from_file_location(
        f"{PACKAGE}.media_renegotiation", SOURCE
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, AssistMediaSession


def test_assist_reinvite_returns_audio_answer_and_declines_video() -> None:
    answer_calls: list[dict] = []
    registry = types.SimpleNamespace(
        preanswered={},
        softphone_media={},
        relays={},
        sessions={"call": types.SimpleNamespace(generation=7)},
        resolve_session_id=lambda call_id: call_id,
        is_generation_current=lambda call_id, generation: (
            call_id == "call" and generation == 7
        ),
    )
    module, AssistMediaSession = _load_module(registry, answer_calls)
    owner = AssistMediaSession()
    registry.relays["call"] = owner
    video = types.SimpleNamespace(direction="recvonly")
    invite = types.SimpleNamespace(
        call_id="call",
        send_format="opus-send",
        recv_format="opus-receive",
        remote_sdp=b"offer-with-audio-and-video",
        remote_rtp_host="198.51.100.20",
        remote_rtp_port=42000,
        remote_audio_direction="sendrecv",
        local_audio_direction="sendrecv",
        remote_audio_connection_held=False,
        video_format=video,
        recv_video_format=video,
        answer_video_format=video,
    )

    result = asyncio.run(
        module.async_prepare_media_update(
            types.SimpleNamespace(), "192.0.2.10", invite, invite, "INVITE"
        )
    )

    assert result.status == 200
    assert "m=video 0" in result.answer_sdp
    assert answer_calls == [
        {
            "dtmf": None,
            "remote_sdp": b"offer-with-audio-and-video",
            "audio_direction": "sendrecv",
            "video_port": 0,
            "video_format": video,
            "video_direction": "inactive",
        }
    ]
    assert owner.committed is False
    asyncio.run(result.commit())
    assert owner.committed is True
