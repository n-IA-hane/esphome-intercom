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
    if "." in name:
        parent = name.rsplit(".", 1)[0]
        parent_name = f"{PACKAGE}.{parent}"
        if parent_name not in sys.modules:
            package = types.ModuleType(parent_name)
            package.__path__ = []
            sys.modules[parent_name] = package
    module = types.ModuleType(f"{PACKAGE}.{name}")
    for key, value in values.items():
        setattr(module, key, value)
    sys.modules[module.__name__] = module
    return module


def _load_module(registry, answer_calls: list[dict]):
    registry.resource_for = lambda call_id, kind: (
        getattr(registry, f"{kind}s", {}).get(call_id)
        if kind == "relay"
        else getattr(registry, kind, {}).get(call_id)
    )
    registry.sip_client_for = lambda call_id: registry.sip_clients.get(call_id)
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
    _module(
        "runtime_data",
        endpoint_directory=lambda _hass: types.SimpleNamespace(
            get=lambda _endpoint_id: None,
        ),
        require_runtime_data=lambda hass: hass.runtime,
    )

    def build_answer_directional(*_args, **kwargs):
        answer_calls.append(dict(kwargs))
        return "m=audio 41000 RTP/AVP 111\r\nm=video 0 RTP/AVP 104\r\n"

    _module(
        "core.sdp",
        build_answer_directional=build_answer_directional,
        constrained_media_direction=lambda *_args, **_kwargs: "sendrecv",
        constrained_video_direction=lambda *_args, **_kwargs: "inactive",
        first_offered_dtmf_format=lambda _sdp: None,
        remote_can_receive=lambda *_args, **_kwargs: False,
        remote_can_send=lambda *_args, **_kwargs: False,
    )
    def invite_rtp_peer(invite, *, established=None):
        if not hasattr(invite, "outbound_rtp_format"):
            invite.outbound_rtp_format = invite.send_format
            invite.inbound_rtp_format = invite.recv_format
        return invite

    _module(
        "sip_bridge",
        dialog_video_rtp_peer=lambda dialog: dialog,
        invite_rtp_peer=invite_rtp_peer,
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
    _module("sip_video_relay")
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


def test_bridged_audio_refresh_preserves_owned_video_transcoding() -> None:
    """An unchanged transcoded video m-line must survive an audio refresh."""

    answer_calls: list[dict] = []
    registry = types.SimpleNamespace(
        preanswered={},
        softphone_media={},
        relays={},
        sip_clients={},
        sessions={"call": types.SimpleNamespace(generation=7)},
        resolve_session_id=lambda call_id: call_id,
        is_generation_current=lambda *_args: True,
    )
    module, _assist = _load_module(registry, answer_calls)
    validation: dict[str, object] = {}

    def validate(*_args, **kwargs):
        validation.update(kwargs)
        return types.SimpleNamespace(accepted=True, reason="")

    module.validate_bridged_video_reoffer = validate
    module.remote_can_send = lambda *_args: True
    module.remote_can_receive = lambda *_args, **_kwargs: True
    module.constrained_video_direction = lambda *_args, **_kwargs: "recvonly"
    module.invite_rtp_peer = lambda invite: invite.audio_peer
    module.invite_video_rtp_peer = lambda invite: invite.video_peer

    class PeerOwner:
        def __init__(self, left, right) -> None:
            self.left = left
            self.right = right

        def prepare_peer_reconfiguration(self, side, peer):
            return lambda: setattr(self, side, peer)

    video = types.SimpleNamespace(direction="recvonly")
    video_relay = PeerOwner(
        object(),
        types.SimpleNamespace(
            send_format="h264-send",
            recv_format="h264-recv",
            video_format=types.SimpleNamespace(direction="sendonly"),
            connection_held=False,
        ),
    )
    video_relay.left_port = 43000
    video_relay.transcodes_from = lambda side: side in {"left", "right"}
    relay = PeerOwner(
        object(),
        types.SimpleNamespace(can_send=True, can_receive=True),
    )
    relay.left_port = 41000
    relay.video_relay = video_relay
    registry.relays["call"] = relay
    previous = types.SimpleNamespace(
        call_id="call",
        video_format=video,
        remote_video_connection_held=False,
    )
    updated = types.SimpleNamespace(
        call_id="call",
        send_format="opus-send",
        recv_format="opus-recv",
        audio_peer=object(),
        video_peer=object(),
        remote_sdp=b"audio-refresh-with-unchanged-video",
        remote_audio_direction="sendrecv",
        remote_audio_connection_held=False,
        video_format=video,
        recv_video_format=video,
        answer_video_format=video,
        remote_video_connection_held=False,
    )

    result = asyncio.run(
        module.async_prepare_media_update(
            types.SimpleNamespace(), "192.0.2.10", previous, updated, "INVITE"
        )
    )

    assert result.status == 200
    assert validation["caller_to_peer_transcoding"] is True
    assert validation["peer_to_caller_transcoding"] is True


def test_bridge_video_addition_cancellation_releases_staged_media() -> None:
    answer_calls: list[dict] = []
    session = types.SimpleNamespace(generation=9)
    registry = types.SimpleNamespace(
        preanswered={},
        softphone_media={},
        relays={},
        sessions={"source": session},
        sip_clients={},
        bridge_for=lambda _call_id: ("source", "destination"),
        resolve_session_id=lambda call_id: call_id,
        is_generation_current=lambda *_args: True,
    )
    module, _assist = _load_module(registry, answer_calls)

    const = sys.modules[f"{PACKAGE}.const"]
    const.CONF_SIP_VIDEO = "sip_video"
    const.CONF_VIDEO_TRANSCODING = "video_transcoding"
    _module(
        "config",
        transport_config=lambda _hass: {
            "sip_video": True,
            "video_transcoding": True,
        },
    )

    class Reservation:
        ports = (43000, 43002)
        detached = False

        def detach(self) -> None:
            self.detached = True

    class VideoRelay:
        left_port = 43000
        right_port = 43002

        def __init__(self) -> None:
            self.start_entered = asyncio.Event()
            self.stopped = False

        async def start(self) -> None:
            self.start_entered.set()
            await asyncio.Event().wait()

        async def stop(self) -> None:
            self.stopped = True

    reservation = Reservation()
    video_relay = VideoRelay()
    media_ports = sys.modules[f"{PACKAGE}.media_ports"]
    media_ports.release_sip_rtp_port_pair = lambda *_args: None
    media_ports.reserve_sip_video_relay_media = lambda _hass: (
        reservation,
        (None, None, None, None),
    )

    candidate = types.SimpleNamespace()

    class Client:
        def __init__(self) -> None:
            self.dialog = types.SimpleNamespace(remote_host="192.0.2.20")
            self.aborted = False

        async def async_prepare_video_reinvite(self, **_kwargs):
            return candidate

        def abort_prepared_reinvite(self, previous, staged) -> None:
            assert previous is self.dialog
            assert staged is candidate
            self.aborted = True

    client = Client()
    registry.sip_clients["destination"] = client
    registry.relays["source"] = types.SimpleNamespace()
    sip_bridge = sys.modules[f"{PACKAGE}.sip_bridge"]
    sip_bridge.build_pending_invite_video_relay = lambda *_args, **_kwargs: video_relay
    sip_bridge.configure_answered_invite_video_relay = lambda *_args, **_kwargs: (
        types.SimpleNamespace(
            video_format="jpeg",
            direction="sendrecv",
        )
    )
    sip_bridge.dialog_rtp_peer = lambda item: item
    sip_bridge.video_bridge_offer_formats = lambda *_args, **_kwargs: ("jpeg",)

    video = types.SimpleNamespace(direction="sendrecv")
    previous = types.SimpleNamespace(call_id="source", video_format=None)
    updated = types.SimpleNamespace(call_id="source", video_format=video)

    async def run() -> None:
        task = asyncio.create_task(
            module._prepare_bridge_video_contract_change(
                types.SimpleNamespace(),
                "192.0.2.10",
                previous,
                updated,
                registry.relays["source"],
            )
        )
        await asyncio.wait_for(video_relay.start_entered.wait(), 1.0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("cancelled media preparation returned normally")

    asyncio.run(run())

    assert reservation.detached is True
    assert client.aborted is True
    assert video_relay.stopped is True


def test_bridge_activates_video_when_initial_offer_was_recvonly() -> None:
    """A dormant source m-line must not masquerade as an active bridge leg."""

    answer_calls: list[dict] = []
    session = types.SimpleNamespace(generation=10)
    registry = types.SimpleNamespace(
        preanswered={},
        softphone_media={},
        relays={},
        sessions={"source": session},
        sip_clients={},
        bridge_for=lambda _call_id: ("source", "destination"),
        resolve_session_id=lambda call_id: call_id,
        is_generation_current=lambda call_id, generation: (
            call_id == "source" and generation == 10
        ),
    )
    module, _assist = _load_module(registry, answer_calls)

    const = sys.modules[f"{PACKAGE}.const"]
    const.CONF_SIP_VIDEO = "sip_video"
    const.CONF_VIDEO_TRANSCODING = "video_transcoding"
    _module(
        "config",
        transport_config=lambda _hass: {
            "sip_video": True,
            "video_transcoding": True,
        },
    )

    class Reservation:
        ports = (43000, 43002)

        def detach(self) -> None:
            return None

    old_audio_left = types.SimpleNamespace(name="old-audio-left")
    old_audio_right = types.SimpleNamespace(
        name="old-audio-right", can_send=True, can_receive=True
    )
    new_audio_left = types.SimpleNamespace(
        name="new-audio-left",
        outbound_rtp_format="audio-send",
        inbound_rtp_format="audio-receive",
    )
    new_audio_right = types.SimpleNamespace(
        name="new-audio-right", can_send=True, can_receive=True
    )
    new_video_left = types.SimpleNamespace(name="new-video-left")

    class VideoRelay:
        left_port = 43000
        right_port = 43002

        def __init__(self) -> None:
            self.started = False
            self.stopped = False

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

    staged_video_relay = VideoRelay()
    media_ports = sys.modules[f"{PACKAGE}.media_ports"]
    media_ports.release_sip_rtp_port_pair = lambda *_args: None
    media_ports.reserve_sip_video_relay_media = lambda _hass: (
        Reservation(),
        (None, None, None, None),
    )

    class Relay:
        left_port = 41000

        def __init__(self) -> None:
            self.left = old_audio_left
            self.right = old_audio_right
            self.video_relay = None

        def prepare_peer_reconfiguration(self, side, peer):
            previous_peer = getattr(self, side)

            def commit() -> None:
                assert getattr(self, side) is previous_peer
                setattr(self, side, peer)

            return commit

        def attach_video_relay(self, video_relay) -> None:
            assert self.video_relay is None
            self.video_relay = video_relay

    relay = Relay()
    registry.relays["source"] = relay
    destination_dialog = types.SimpleNamespace(remote_host="192.0.2.20")
    candidate = types.SimpleNamespace(video_format="jpeg")

    class Client:
        def __init__(self) -> None:
            self.dialog = destination_dialog
            self.prepared = None
            self.committed = False

        async def async_prepare_video_reinvite(self, **kwargs):
            self.prepared = kwargs
            return candidate

        def commit_prepared_reinvite(self, previous, staged) -> bool:
            assert previous is destination_dialog
            assert staged is candidate
            self.committed = True
            return True

        def abort_prepared_reinvite(self, *_args) -> None:
            raise AssertionError("committed video activation was rolled back")

    client = Client()
    registry.sip_clients["destination"] = client
    sip_bridge = sys.modules[f"{PACKAGE}.sip_bridge"]
    sip_bridge.build_pending_invite_video_relay = lambda *_args, **_kwargs: (
        staged_video_relay
    )
    sip_bridge.configure_answered_invite_video_relay = lambda *_args, **_kwargs: (
        types.SimpleNamespace(
            video_format="vp8",
            direction="sendrecv",
        )
    )
    sip_bridge.dialog_rtp_peer = lambda _dialog: new_audio_right
    sip_bridge.invite_rtp_peer = lambda _invite, **_kwargs: new_audio_left
    sip_bridge.video_bridge_offer_formats = lambda *_args, **_kwargs: ("jpeg",)
    module.invite_rtp_peer = lambda _invite: new_audio_left
    module.invite_video_rtp_peer = lambda _invite: new_video_left

    previous_video = types.SimpleNamespace(direction="recvonly")
    updated_video = types.SimpleNamespace(direction="sendrecv")
    previous = types.SimpleNamespace(
        call_id="source",
        video_format=previous_video,
        remote_video_connection_held=False,
    )
    updated = types.SimpleNamespace(
        call_id="source",
        video_format=updated_video,
        recv_video_format=updated_video,
        answer_video_format=updated_video,
        remote_video_connection_held=False,
        send_format="audio-send",
        recv_format="audio-receive",
        remote_sdp=b"audio-video-sendrecv",
        remote_audio_direction="sendrecv",
        remote_audio_connection_held=False,
    )

    result = asyncio.run(
        module.async_prepare_media_update(
            types.SimpleNamespace(), "192.0.2.10", previous, updated, "INVITE"
        )
    )

    assert result.status == 200
    assert client.prepared == {
        "local_video_rtp_port": 43002,
        "video_formats": ("jpeg",),
        "video_direction": "sendrecv",
    }
    assert staged_video_relay.started is True
    assert relay.video_relay is None
    asyncio.run(result.commit())
    assert client.committed is True
    assert relay.video_relay is staged_video_relay
    assert answer_calls[-1]["video_port"] == 43000
    assert answer_calls[-1]["video_direction"] == "sendrecv"


def test_bridge_video_removal_updates_both_dialogs_and_stops_media() -> None:
    answer_calls: list[dict] = []
    session = types.SimpleNamespace(generation=11)
    registry = types.SimpleNamespace(
        preanswered={},
        softphone_media={},
        relays={},
        sessions={"source": session},
        sip_clients={},
        bridge_for=lambda _call_id: ("source", "destination"),
        resolve_session_id=lambda call_id: call_id,
        is_generation_current=lambda call_id, generation: (
            call_id == "source" and generation == 11
        ),
    )
    module, _assist = _load_module(registry, answer_calls)

    const = sys.modules[f"{PACKAGE}.const"]
    const.CONF_SIP_VIDEO = "sip_video"
    const.CONF_VIDEO_TRANSCODING = "video_transcoding"
    _module(
        "config",
        transport_config=lambda _hass: {
            "sip_video": True,
            "video_transcoding": True,
        },
    )
    media_ports = sys.modules[f"{PACKAGE}.media_ports"]
    media_ports.release_sip_rtp_port_pair = lambda *_args: None
    media_ports.reserve_sip_video_relay_media = lambda _hass: None

    class VideoRelay:
        def __init__(self) -> None:
            self.stopped = False

        async def stop(self) -> None:
            self.stopped = True

    video_relay = VideoRelay()

    class Relay:
        left_port = 41000

        def __init__(self) -> None:
            self.left = types.SimpleNamespace(name="old-left")
            self.right = types.SimpleNamespace(name="old-right")
            self.video_relay = video_relay

        def prepare_peer_reconfiguration(self, side, peer):
            previous = getattr(self, side)

            def commit() -> None:
                assert getattr(self, side) is previous
                setattr(self, side, peer)

            return commit

    relay = Relay()
    registry.relays["source"] = relay
    candidate = types.SimpleNamespace(can_send=True, can_receive=True)

    class Client:
        def __init__(self) -> None:
            self.dialog = types.SimpleNamespace(remote_host="192.0.2.20")
            self.video_formats = ("jpeg",)
            self.prepared: dict = {}
            self.committed = False

        async def async_prepare_video_reinvite(self, **kwargs):
            self.prepared = kwargs
            return candidate

        def commit_prepared_reinvite(self, previous, staged) -> bool:
            assert previous is self.dialog
            assert staged is candidate
            self.committed = True
            return True

        def abort_prepared_reinvite(self, _previous, _staged) -> None:
            raise AssertionError("committed removal was rolled back")

    client = Client()
    registry.sip_clients["destination"] = client
    sip_bridge = sys.modules[f"{PACKAGE}.sip_bridge"]
    sip_bridge.dialog_rtp_peer = lambda item: item
    sip_bridge.build_pending_invite_video_relay = lambda *_args, **_kwargs: None
    sip_bridge.configure_answered_invite_video_relay = lambda *_args, **_kwargs: None
    sip_bridge.video_bridge_offer_formats = lambda *_args, **_kwargs: ()

    previous = types.SimpleNamespace(
        call_id="source",
        video_format=types.SimpleNamespace(direction="sendrecv"),
    )
    updated = types.SimpleNamespace(
        call_id="source",
        video_format=None,
        answer_video_format=None,
        send_format="audio-send",
        recv_format="audio-receive",
        remote_sdp=b"audio-with-rejected-video",
        remote_audio_direction="sendrecv",
        remote_audio_connection_held=False,
    )

    result = asyncio.run(
        module._prepare_bridge_video_contract_change(
            types.SimpleNamespace(),
            "192.0.2.10",
            previous,
            updated,
            relay,
        )
    )

    assert result.status == 200
    assert client.prepared == {
        "local_video_rtp_port": 0,
        "video_formats": ("jpeg",),
        "video_direction": "inactive",
    }
    assert relay.video_relay is video_relay
    assert video_relay.stopped is False
    asyncio.run(result.commit())
    assert client.committed is True
    assert relay.video_relay is None
    assert video_relay.stopped is True
    assert relay.left is updated
    assert relay.right is candidate


def test_bridge_video_inactive_updates_both_video_legs_atomically() -> None:
    answer_calls: list[dict] = []
    session = types.SimpleNamespace(generation=13)
    registry = types.SimpleNamespace(
        preanswered={},
        softphone_media={},
        relays={},
        sessions={"source": session},
        sip_clients={},
        bridge_for=lambda _call_id: ("source", "destination"),
        resolve_session_id=lambda call_id: call_id,
        is_generation_current=lambda call_id, generation: (
            call_id == "source" and generation == 13
        ),
    )
    module, _assist = _load_module(registry, answer_calls)

    const = sys.modules[f"{PACKAGE}.const"]
    const.CONF_SIP_VIDEO = "sip_video"
    const.CONF_VIDEO_TRANSCODING = "video_transcoding"
    _module(
        "config",
        transport_config=lambda _hass: {
            "sip_video": True,
            "video_transcoding": True,
        },
    )
    media_ports = sys.modules[f"{PACKAGE}.media_ports"]
    media_ports.release_sip_rtp_port_pair = lambda *_args: None
    media_ports.reserve_sip_video_relay_media = lambda _hass: None

    old_audio_left = types.SimpleNamespace(name="old-audio-left")
    old_audio_right = types.SimpleNamespace(
        name="old-audio-right", can_send=True, can_receive=True
    )
    new_audio_left = types.SimpleNamespace(
        name="new-audio-left",
        outbound_rtp_format="audio-send",
        inbound_rtp_format="audio-receive",
    )
    new_audio_right = types.SimpleNamespace(
        name="new-audio-right", can_send=True, can_receive=True
    )
    old_video_left = types.SimpleNamespace(name="old-video-left")
    old_video_right = types.SimpleNamespace(name="old-video-right")
    new_video_left = types.SimpleNamespace(name="new-video-left")
    new_video_right = types.SimpleNamespace(
        name="new-video-right",
        send_format="jpeg-send",
        recv_format="jpeg-recv",
        video_format=types.SimpleNamespace(direction="inactive"),
        connection_held=False,
    )

    class VideoRelay:
        left_port = 43000
        right_port = 43002

        def __init__(self) -> None:
            self.left = old_video_left
            self.right = old_video_right

        def transcodes_from(self, _side: str) -> bool:
            return True

        def prepare_peer_reconfiguration(self, side, peer):
            previous_peer = getattr(self, side)

            def commit() -> None:
                assert getattr(self, side) is previous_peer
                setattr(self, side, peer)

            return commit

    video_relay = VideoRelay()

    class Relay:
        left_port = 41000

        def __init__(self) -> None:
            self.left = old_audio_left
            self.right = old_audio_right
            self.video_relay = video_relay

        def prepare_peer_reconfiguration(self, side, peer):
            previous_peer = getattr(self, side)

            def commit() -> None:
                assert getattr(self, side) is previous_peer
                setattr(self, side, peer)

            return commit

    relay = Relay()
    registry.relays["source"] = relay
    destination_dialog = types.SimpleNamespace(
        remote_host="192.0.2.20",
        recv_video_format="jpeg-recv",
        video_format="jpeg-send",
    )
    candidate = types.SimpleNamespace(
        video_format=types.SimpleNamespace(direction="inactive"),
        remote_video_connection_held=False,
    )

    class Client:
        def __init__(self) -> None:
            self.dialog = destination_dialog
            self.video_formats = ("jpeg",)
            self.prepared = None
            self.committed = False

        async def async_prepare_video_reinvite(self, **kwargs):
            self.prepared = kwargs
            return candidate

        def commit_prepared_reinvite(self, previous, staged) -> bool:
            assert previous is destination_dialog
            assert staged is candidate
            self.committed = True
            return True

        def abort_prepared_reinvite(self, *_args) -> None:
            raise AssertionError("committed direction update was rolled back")

    client = Client()
    registry.sip_clients["destination"] = client
    sip_bridge = sys.modules[f"{PACKAGE}.sip_bridge"]
    sip_bridge.build_pending_invite_video_relay = lambda *_args, **_kwargs: None
    sip_bridge.configure_answered_invite_video_relay = lambda *_args, **_kwargs: None
    sip_bridge.dialog_rtp_peer = lambda _dialog: new_audio_right
    sip_bridge.invite_rtp_peer = lambda _invite, **_kwargs: new_audio_left
    sip_bridge.dialog_video_rtp_peer = lambda _dialog: new_video_right
    sip_bridge.video_bridge_offer_formats = lambda *_args, **_kwargs: ()
    module.invite_rtp_peer = lambda _invite: new_audio_left
    module.invite_video_rtp_peer = lambda _invite: new_video_left
    module.remote_can_send = lambda fmt: fmt.direction in {"sendonly", "sendrecv"}
    module.remote_can_receive = lambda fmt, **_kwargs: (
        fmt.direction
        in {
            "recvonly",
            "sendrecv",
        }
    )
    module.constrained_video_direction = lambda *_args, **_kwargs: "inactive"

    previous_video = types.SimpleNamespace(direction="sendrecv")
    updated_video = types.SimpleNamespace(direction="inactive")
    previous = types.SimpleNamespace(
        call_id="source",
        video_format=previous_video,
        remote_video_connection_held=False,
    )
    updated = types.SimpleNamespace(
        call_id="source",
        video_format=updated_video,
        recv_video_format=updated_video,
        answer_video_format=updated_video,
        remote_video_connection_held=False,
        send_format="audio-send",
        recv_format="audio-receive",
        remote_sdp=b"audio-video-inactive",
        remote_audio_direction="sendrecv",
        remote_audio_connection_held=False,
    )

    result = asyncio.run(
        module.async_prepare_media_update(
            types.SimpleNamespace(), "192.0.2.10", previous, updated, "INVITE"
        )
    )

    assert result.status == 200
    assert client.prepared == {
        "local_video_rtp_port": 43002,
        "video_formats": ("jpeg",),
        "video_direction": "inactive",
    }
    assert client.committed is False
    assert video_relay.left is old_video_left
    assert video_relay.right is old_video_right
    asyncio.run(result.commit())
    assert client.committed is True
    assert relay.left is new_audio_left
    assert relay.right is new_audio_right
    assert video_relay.left is new_video_left
    assert video_relay.right is new_video_right
    assert answer_calls[-1]["video_port"] == 43000
    assert answer_calls[-1]["video_direction"] == "inactive"
