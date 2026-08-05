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
            self.right = peer

        return commit


class _VideoRelay:
    def __init__(self, left: object, right: object) -> None:
        self.left = left
        self.right = right
        self.staged: list[tuple[str, object]] = []

    def prepare_peer_reconfiguration(self, side: str, peer: object):
        self.staged.append((side, peer))

        def commit() -> None:
            self.right = peer

        return commit


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

    dependencies = {
        "endpoint_lifecycle": {
            "call_registry": lambda hass: hass.registry,
        },
        "core.sdp": {
            "video_formats_passthrough_compatible": lambda left, right: left == right,
        },
        "sip_bridge": {
            "dialog_rtp_peer": lambda updated: updated.audio_peer,
            "dialog_video_rtp_peer": lambda updated: updated.video_peer,
        },
        "sip_client": {
            "SipCallClient": type("SipCallClient", (), {}),
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
    asyncio.run(commit())
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
        asyncio.run(stale_commit())
    assert relay.right is old_peer


def test_audio_and_video_commit_share_one_owner_check(
    bridge_media_updates,
) -> None:
    registry = _Registry()
    hass = SimpleNamespace(registry=registry)
    old_audio = object()
    new_audio = object()
    old_video = object()
    video_format = SimpleNamespace(direction="sendrecv")
    new_video = SimpleNamespace(
        send_format="peer-recv",
        recv_format="peer-send",
    )
    relay = _Relay(old_audio)
    relay.video_relay = _VideoRelay(
        SimpleNamespace(
            recv_format="peer-recv",
            send_format="peer-send",
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
    assert relay.right is old_audio
    assert relay.video_relay.right is old_video
    asyncio.run(commit())
    assert relay.right is new_audio
    assert relay.video_relay.right is new_video
