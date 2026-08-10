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
        self._transcode_from = transcode_from
        self.staged: list[tuple[str, object]] = []

    def transcodes_from(self, side: str) -> bool:
        return side in self._transcode_from

    def prepare_peer_reconfiguration(self, side: str, peer: object):
        self.staged.append((side, peer))

        def commit() -> None:
            setattr(self, side, peer)

        return commit


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
        ) -> None:
            self.commit = commit
            self.rollback = rollback
            self.answer_video_format = answer_video_format

    dependencies = {
        "endpoint_lifecycle": {
            "call_registry": lambda hass: hass.registry,
        },
        "media_offer_answer": {
            "validate_bridged_video_reoffer": validate_bridged_video_reoffer,
        },
        "runtime_data": {
            "sip_endpoint_manager": lambda hass: getattr(hass, "endpoint", None),
        },
        "core.sdp": {
            "video_answer_contract": lambda offered, answered: answered,
        },
        "sip_bridge": {
            "dialog_rtp_peer": lambda updated: updated.audio_peer,
            "dialog_video_rtp_peer": lambda updated: updated.video_peer,
            "invite_video_rtp_peer": lambda updated: updated.video_peer,
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
        video_format=SimpleNamespace(direction="sendrecv"),
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
            video_format=video_format,
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
    assert relay.video_relay.right is old_video
    asyncio.run(commit.commit())
    assert relay.video_relay.right is new_video
