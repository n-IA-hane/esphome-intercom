"""Behavioral tests for committed outbound bridge media updates."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "bridge_media_updates.py"


class _Registry:
    def __init__(self) -> None:
        self.sessions = {"call-1": SimpleNamespace(generation=7)}
        self.current = True

    @staticmethod
    def resolve_session_id(call_id: str) -> str:
        return call_id

    def is_generation_current(self, call_id: str, generation: int) -> bool:
        return call_id == "call-1" and generation == 7 and self.current


class _Relay:
    def __init__(self, right: object) -> None:
        self.right = right
        self.video_relay = None
        self.staged: list[tuple[str, object]] = []

    def prepare_peer_reconfiguration(self, side: str, peer: object):
        self.staged.append((side, peer))

        def commit() -> None:
            setattr(self, side, peer)

        return commit

    def attach_video_relay(self, relay: object) -> None:
        assert self.video_relay is None
        self.video_relay = relay

class _VideoRelay:
    def __init__(
        self,
        left: object,
        right: object,
        *,
        transcode_from: frozenset[str] = frozenset(),
    ) -> None:
        self.left = left
        self.right = right
        self.left_port = 43000
        self.right_port = 43002
        self._transcode_from = transcode_from
        self.staged: list[tuple[str, object]] = []
        self.commits: list[str] = []
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    def transcodes_from(self, side: str) -> bool:
        return side in self._transcode_from

    def transcode_directions_for(self, _left: object, _right: object) -> set[str]:
        return set(self._transcode_from)

    def prepare_peer_reconfiguration(self, side: str, peer: object):
        self.staged.append((side, peer))

        def commit() -> None:
            self.commits.append(side)
            setattr(self, side, peer)

        return commit

    def stage_peer_reconfiguration(self, side: str, peer: object):
        commit = self.prepare_peer_reconfiguration(side, peer)
        return commit, lambda: None

    async def async_prepare_peer_generation(
        self,
        *,
        left: object,
        right: object,
    ):
        previous_left = self.left
        previous_right = self.right
        settled = False

        async def commit() -> None:
            nonlocal settled
            if settled:
                raise RuntimeError("video peer generation already settled")
            settled = True
            self.commits.extend(("left", "right"))
            self.left = left
            self.right = right

        async def rollback() -> None:
            nonlocal settled
            if settled:
                return
            settled = True
            self.left = previous_left
            self.right = previous_right

        return commit, rollback


class _PreparedSource:
    def __init__(self, candidate: object) -> None:
        self.candidate = candidate
        self.committed = False
        self.aborted = False
        self.restored = False

    def commit(self) -> bool:
        self.committed = True
        return True

    def abort(self) -> None:
        self.aborted = True

    async def restore(self, **_kwargs) -> bool:
        self.restored = True
        return True


class _SourceEndpoint:
    def __init__(self, candidate: object) -> None:
        self.prepared = _PreparedSource(candidate)
        self.calls: list[tuple[str, dict]] = []

    async def async_prepare_video_reinvite(self, call_id: str, **kwargs):
        self.calls.append((call_id, kwargs))
        return self.prepared


@pytest.fixture
def bridge_media_updates(monkeypatch):
    package_name = "voip_stack_bridge_media_updates_test"
    package = ModuleType(package_name)
    package.__path__ = [str(MODULE.parent)]
    monkeypatch.setitem(sys.modules, package_name, package)

    homeassistant = ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)

    def validate_bridged_video_reoffer(
        _previous,
        updated_send,
        _updated_recv,
        **kwargs,
    ):
        accepted = bool(
            getattr(updated_send, "passthrough", False)
            or (
                kwargs["caller_to_peer_transcoding"]
                and kwargs["peer_to_caller_transcoding"]
            )
        )
        return SimpleNamespace(accepted=accepted, reason="incompatible")

    class PreparedDialogMediaUpdate:
        def __init__(
            self,
            commit,
            rollback=None,
            *,
            answer_video_format=None,
            answer_video_rtp_port=None,
        ) -> None:
            self.commit = commit
            self.rollback = rollback
            self.answer_video_format = answer_video_format
            self.answer_video_rtp_port = answer_video_rtp_port

    dependencies = {
        "endpoint_lifecycle": {
            "call_registry": lambda hass: hass.registry,
        },
        "media_offer_answer": {
            "validate_bridged_video_reoffer": validate_bridged_video_reoffer,
        },
        "media_ports": {
            "release_sip_rtp_port_pair": lambda *_args: None,
            "reserve_sip_video_relay_media": lambda _hass: None,
        },
        "runtime_data": {
            "sip_endpoint_manager": lambda hass: getattr(hass, "endpoint", None),
        },
        "core.sdp": {
            "video_answer_contract": lambda offered, answered: answered,
            "remote_can_send": lambda fmt: fmt.direction in {"sendrecv", "sendonly"},
            "remote_can_receive": lambda fmt, connection_held=False: (
                not connection_held and fmt.direction in {"sendrecv", "recvonly"}
            ),
        },
        "sip_bridge": {
            "build_pending_invite_video_relay": lambda *_args, **_kwargs: None,
            "configure_answered_invite_video_relay": lambda *_args, **_kwargs: None,
            "dialog_rtp_peer": lambda updated: updated.audio_peer,
            "dialog_video_rtp_peer": lambda updated: updated.video_peer,
            "invite_video_rtp_peer": lambda updated: updated.video_peer,
            "video_bridge_offer_formats": lambda video, source_receive=None, **_kwargs: (
                source_receive or video,
            ),
        },
        "sip_client": {
            "SipCallClient": type("SipCallClient", (), {}),
            "PreparedDialogMediaUpdate": PreparedDialogMediaUpdate,
        },
    }
    for name, values in dependencies.items():
        if "." in name:
            parent = name.rsplit(".", 1)[0]
            parent_name = f"{package_name}.{parent}"
            parent_module = ModuleType(parent_name)
            parent_module.__path__ = []
            monkeypatch.setitem(sys.modules, parent_name, parent_module)
        dependency = ModuleType(f"{package_name}.{name}")
        for key, value in values.items():
            setattr(dependency, key, value)
        monkeypatch.setitem(sys.modules, dependency.__name__, dependency)

    module_name = f"{package_name}.bridge_media_updates"
    spec = importlib.util.spec_from_file_location(module_name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def test_audio_update_stays_staged_until_generation_checked(
    bridge_media_updates,
) -> None:
    registry = _Registry()
    hass = SimpleNamespace(registry=registry)
    old_peer = object()
    new_peer = object()
    relay = _Relay(old_peer)
    client = SimpleNamespace(
        dialog_ids=SimpleNamespace(call_id="dest-1"),
        on_media_update=None,
    )
    updated = SimpleNamespace(
        audio_peer=new_peer,
        video_peer=None,
        video_format=None,
        remote_rtp_host="192.0.2.10",
        remote_rtp_port=41000,
        remote_audio_direction="sendrecv",
    )
    binder = bridge_media_updates.BridgeMediaUpdateBinder(hass)
    binder.attach(client, relay, source_call_id="call-1")

    commit = asyncio.run(
        client.on_media_update(
            SimpleNamespace(video_format=None),
            updated,
            "INVITE",
        )
    )

    assert commit is not None
    assert relay.right is old_peer
    asyncio.run(commit.commit())
    assert relay.right is new_peer

    relay.right = old_peer
    registry.current = False
    stale_commit = asyncio.run(
        client.on_media_update(
            SimpleNamespace(video_format=None),
            updated,
            "UPDATE",
        )
    )
    with pytest.raises(RuntimeError, match="terminated call"):
        asyncio.run(stale_commit.commit())
    assert relay.right is old_peer


def test_audio_and_video_commit_share_one_owner_check(
    bridge_media_updates,
) -> None:
    registry = _Registry()
    source_peer = SimpleNamespace(
        send_format="peer-send",
        recv_format="peer-recv",
        video_format=SimpleNamespace(direction="recvonly"),
        connection_held=False,
    )
    hass = SimpleNamespace(
        registry=registry,
        endpoint=_SourceEndpoint(SimpleNamespace(video_peer=source_peer)),
    )
    old_audio = object()
    new_audio = object()
    old_video = object()
    video_format = SimpleNamespace(direction="sendrecv", passthrough=True)
    new_video = SimpleNamespace(
        send_format="peer-recv",
        recv_format="peer-send",
    )
    relay = _Relay(old_audio)
    relay.video_relay = _VideoRelay(
        SimpleNamespace(
            recv_format="peer-recv",
            send_format="peer-send",
            video_format=SimpleNamespace(direction="recvonly"),
            connection_held=False,
        ),
        old_video,
    )
    client = SimpleNamespace(
        dialog_ids=SimpleNamespace(call_id="dest-2"),
        on_media_update=None,
    )
    updated = SimpleNamespace(
        audio_peer=new_audio,
        video_peer=new_video,
        video_format=video_format,
        recv_video_format="peer-send",
        remote_video_connection_held=False,
        remote_video_rtp_port=42000,
        remote_rtp_host="198.51.100.20",
        remote_rtp_port=41000,
        remote_audio_direction="sendrecv",
    )
    binder = bridge_media_updates.BridgeMediaUpdateBinder(hass)
    binder.attach(client, relay, source_call_id="call-1")

    commit = asyncio.run(
        client.on_media_update(
            SimpleNamespace(video_format=video_format),
            updated,
            "INVITE",
        )
    )

    assert commit is not None
    assert hass.endpoint.prepared.committed is True
    assert hass.endpoint.prepared.aborted is False
    assert relay.right is old_audio
    assert relay.video_relay.right is old_video
    asyncio.run(commit.commit())
    assert relay.right is new_audio
    assert relay.video_relay.right is new_video
    assert relay.video_relay.commits == ["left", "right"]


def test_cross_codec_video_direction_change_uses_owned_transcoders(
    bridge_media_updates,
    monkeypatch,
) -> None:
    registry = _Registry()
    source_peer = SimpleNamespace(
        send_format="h264-tx",
        recv_format="h264-rx",
        video_format=SimpleNamespace(direction="sendrecv"),
        connection_held=False,
    )
    hass = SimpleNamespace(
        registry=registry,
        endpoint=_SourceEndpoint(SimpleNamespace(video_peer=source_peer)),
    )
    video_format = SimpleNamespace(direction="sendrecv")
    old_video = object()
    new_video = SimpleNamespace(send_format="vp8-rx", recv_format="vp8-tx")
    relay = _Relay(object())
    relay.video_relay = _VideoRelay(
        SimpleNamespace(
            send_format="h264-tx",
            recv_format="h264-rx",
            video_format=video_format,
            connection_held=False,
        ),
        old_video,
        transcode_from=frozenset({"left", "right"}),
    )
    client = SimpleNamespace(
        dialog_ids=SimpleNamespace(call_id="dest-cross-codec"),
        on_media_update=None,
    )
    updated = SimpleNamespace(
        audio_peer=object(),
        video_peer=new_video,
        video_format=video_format,
        recv_video_format="vp8-tx",
        remote_video_connection_held=False,
        remote_video_rtp_port=42000,
        remote_rtp_host="198.51.100.20",
        remote_rtp_port=41000,
        remote_audio_direction="sendrecv",
    )
    validation: dict[str, object] = {}

    def validate(previous, updated_send, updated_recv, **kwargs):
        validation.update(
            previous=previous,
            updated_send=updated_send,
            updated_recv=updated_recv,
            **kwargs,
        )
        return SimpleNamespace(accepted=True, reason="")

    monkeypatch.setattr(
        bridge_media_updates,
        "validate_bridged_video_reoffer",
        validate,
    )
    bridge_media_updates.BridgeMediaUpdateBinder(hass).attach(
        client,
        relay,
        source_call_id="call-1",
    )

    commit = asyncio.run(
        client.on_media_update(
            SimpleNamespace(video_format=video_format),
            updated,
            "INVITE",
        )
    )

    assert commit is not None
    assert validation["caller_to_peer_transcoding"] is True
    assert validation["peer_to_caller_transcoding"] is True
    assert validation["peer_send"] == "h264-tx"
    assert validation["peer_recv"] == "h264-rx"
    assert validation["updated_recv"] == "vp8-tx"
    assert commit.answer_video_format is None
    assert hass.endpoint.calls == []
    assert hass.endpoint.prepared.committed is False
    assert relay.video_relay.right is old_video
    asyncio.run(commit.commit())
    assert relay.video_relay.right is new_video


def test_destination_reinvite_can_add_video_to_audio_only_bridge(
    bridge_media_updates,
    monkeypatch,
) -> None:
    registry = _Registry()
    source_video = SimpleNamespace(
        send_format="h264-tx",
        recv_format="h264-rx",
        video_format=SimpleNamespace(direction="sendrecv"),
        connection_held=False,
    )
    source_candidate = SimpleNamespace(video_peer=source_video)
    endpoint = _SourceEndpoint(source_candidate)
    hass = SimpleNamespace(registry=registry, endpoint=endpoint)
    relay = _Relay(object())
    destination_video = SimpleNamespace(
        send_format="h264-rx",
        recv_format="h264-tx",
    )
    staged = _VideoRelay(source_video, destination_video)

    class Reservation:
        ports = (43000, 43002)

        def __init__(self) -> None:
            self.detached = False

        def detach(self) -> None:
            self.detached = True

        def release(self) -> None:
            raise AssertionError("committed reservation must detach")

    reservation = Reservation()
    monkeypatch.setattr(
        bridge_media_updates,
        "reserve_sip_video_relay_media",
        lambda _hass: (reservation, (object(), object(), object(), object())),
    )
    monkeypatch.setattr(
        bridge_media_updates,
        "build_pending_invite_video_relay",
        lambda *_args, **_kwargs: staged,
    )
    monkeypatch.setattr(
        bridge_media_updates,
        "configure_answered_invite_video_relay",
        lambda *_args, **_kwargs: SimpleNamespace(
            video_format="h264-answer",
            direction="sendrecv",
        ),
    )
    package = bridge_media_updates.__package__
    config = ModuleType(f"{package}.config")
    config.transport_config = lambda _hass: {
        "sip_video": True,
        "video_transcoding": False,
    }
    const = ModuleType(f"{package}.const")
    const.CONF_SIP_VIDEO = "sip_video"
    const.CONF_VIDEO_TRANSCODING = "video_transcoding"
    monkeypatch.setitem(sys.modules, config.__name__, config)
    monkeypatch.setitem(sys.modules, const.__name__, const)

    client = SimpleNamespace(
        dialog_ids=SimpleNamespace(call_id="dest-add-video"),
        on_media_update=None,
    )
    updated_video = SimpleNamespace(direction="sendrecv", passthrough=True)
    updated = SimpleNamespace(
        audio_peer=object(),
        video_peer=destination_video,
        video_format=updated_video,
        recv_video_format="h264-tx",
        remote_video_connection_held=False,
        remote_video_rtp_port=42000,
        remote_host="198.51.100.20",
        remote_rtp_host="198.51.100.20",
        remote_rtp_port=41000,
        remote_audio_direction="sendrecv",
    )
    original_source_send = SimpleNamespace(
        direction="sendrecv",
        profile_level_id="42c00c",
    )
    original_source_receive = SimpleNamespace(
        direction="sendrecv",
        profile_level_id="42c01f",
    )
    bridge_media_updates.BridgeMediaUpdateBinder(hass).attach(
        client,
        relay,
        source_call_id="call-1",
        source_video_send_format=original_source_send,
        source_video_receive_format=original_source_receive,
    )

    prepared = asyncio.run(
        client.on_media_update(
            SimpleNamespace(video_format=None),
            updated,
            "INVITE",
        )
    )

    assert prepared is not None
    assert endpoint.prepared.committed is True
    assert endpoint.calls[0][1]["video_formats"] == (original_source_receive,)
    assert reservation.detached is True
    assert staged.started is True
    assert relay.video_relay is None
    assert prepared.answer_video_rtp_port == staged.right_port
    asyncio.run(prepared.commit())
    assert relay.video_relay is staged
    assert staged.stopped is False


def test_destination_reinvite_removes_video_from_both_bridge_legs(
    bridge_media_updates,
) -> None:
    registry = _Registry()
    endpoint = _SourceEndpoint(SimpleNamespace(video_peer=None))
    hass = SimpleNamespace(registry=registry, endpoint=endpoint)
    relay = _Relay(object())
    old_video = _VideoRelay(
        SimpleNamespace(
            send_format="h264-tx",
            recv_format="h264-rx",
            video_format=SimpleNamespace(direction="sendrecv"),
            connection_held=False,
        ),
        object(),
    )
    relay.video_relay = old_video
    client = SimpleNamespace(
        dialog_ids=SimpleNamespace(call_id="dest-remove-video"),
        on_media_update=None,
    )
    updated = SimpleNamespace(
        audio_peer=object(),
        video_peer=None,
        video_format=None,
        remote_rtp_host="198.51.100.20",
        remote_rtp_port=41000,
        remote_audio_direction="sendrecv",
    )
    bridge_media_updates.BridgeMediaUpdateBinder(hass).attach(
        client,
        relay,
        source_call_id="call-1",
    )

    prepared = asyncio.run(
        client.on_media_update(
            SimpleNamespace(video_format=SimpleNamespace(direction="sendrecv")),
            updated,
            "INVITE",
        )
    )

    assert prepared is not None
    assert endpoint.calls[0][1]["local_video_rtp_port"] == 0
    assert endpoint.prepared.committed is True
    assert relay.video_relay is old_video
    asyncio.run(prepared.commit())
    assert relay.video_relay is None
    assert old_video.stopped is True
