"""Safety and provenance boundaries of the maintained native gallery workflow."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import hashlib
import json

import pytest

from tools.ha_voip_lab import display_gallery as gallery


def test_native_readiness_requires_all_eight_actual_getter_records():
    messages = [f"demo_blf id={name} available=YES state=3 presence=1 version=7 ringing=0 active=1 held=0"
                for name in gallery.DEMO_IDS]
    messages.append("demo_end waiting=YES contacts=8")
    snapshot = gallery.decode_native_status(messages)
    assert snapshot["waiting"] is True
    assert snapshot["blf"]["GalleryHome"] == {
        "available": True, "state": 3, "presence": 1, "version": 7,
        "ringing": 0, "active": 1, "held": 0,
    }
    with pytest.raises(RuntimeError, match="incomplete"):
        gallery.decode_native_status(messages[1:])


def test_gallery_requires_the_exact_preserved_profile_and_artifact(tmp_path):
    profile, artifact, receipt = (tmp_path / name for name in ("device.yaml", "firmware.bin", "receipt.json"))
    profile.write_text("private profile fixture")
    artifact.write_bytes(b"preserved artifact")
    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()
    receipt.write_text(json.dumps({"config_sha256": sha(profile), "files": {
        "firmware": {"path": str(artifact), "sha256": sha(artifact)}}}))
    assert gallery.checked_profile_receipt(profile, receipt)
    profile.write_text("a different profile")
    with pytest.raises(RuntimeError, match="profile"):
        gallery.checked_profile_receipt(profile, receipt)
    profile.write_text("private profile fixture")
    artifact.write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="artifact"):
        gallery.checked_profile_receipt(profile, receipt)


async def test_existing_gallery_account_prevents_every_account_mutation():
    fixture = SimpleNamespace(owned={}, run_id="fresh", action=AsyncMock(return_value={
        "accounts": [{"username": "GalleryHome"}]}))
    with pytest.raises(RuntimeError, match="existing account"):
        await gallery.create_accounts(fixture)
    assert fixture.action.await_args_list[0].args == ("list_accounts",)
    assert fixture.action.await_count == 1 and fixture.owned == {}


class OfflineWs:
    def __init__(self, *_args):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def command(self, request):
        if request == {"type": "config/device_registry/list"}:
            return {"result": [{"id": "fresh-device", "connections": [["mac", "AA:BB:CC:DD:EE:FF"]]}]}
        assert request == {"type": "config/entity_registry/list"}
        return {"result": [{"device_id": "fresh-device", "platform": "esphome", "disabled_by": None}]}


@pytest.mark.parametrize("enabled", [False, True])
async def test_mailbox_uses_fresh_mac_identity_and_preserves_existing_mailbox(tmp_path, monkeypatch, enabled):
    monkeypatch.setattr(gallery, "HaWs", OfflineWs)
    lookup = AsyncMock(return_value={"total": 0})
    monkeypatch.setattr(gallery.hil, "message_uid", lookup)
    fixture = SimpleNamespace(rest=SimpleNamespace(base_url="offline", token="fixture-only"),
        phone=AsyncMock(return_value={"mailbox": {"enabled": enabled, "new": 0, "old": 0}}))
    args = SimpleNamespace(output=tmp_path)
    if enabled:
        with pytest.raises(RuntimeError, match="existing enabled or nonempty"):
            await gallery.mailbox_args(args, fixture, "aa:bb:cc:dd:ee:ff")
    else:
        result = await gallery.mailbox_args(args, fixture, "aa:bb:cc:dd:ee:ff")
        assert result.mailbox_device == "fresh-device"
        assert result.output == tmp_path / "mailbox-scene.json"
    fixture.phone.assert_awaited_once_with("fresh-device")


@pytest.mark.parametrize("second_esphome_owner", [False, True])
async def test_shared_mac_selects_only_the_existing_esphome_entity_owner(tmp_path, monkeypatch, second_esphome_owner):
    class SharedMacWs(OfflineWs):
        async def command(self, request):
            if request["type"] == "config/device_registry/list":
                return {"result": [{"id": name, "connections": [["mac", "AA:BB:CC:DD:EE:FF"]]}
                                   for name in ("fresh-device", "unrelated-device")]}
            owners = ["fresh-device", *(["unrelated-device"] if second_esphome_owner else [])]
            return {"result": [{"device_id": name, "platform": "esphome", "disabled_by": None}
                               for name in owners]}
    monkeypatch.setattr(gallery, "HaWs", SharedMacWs)
    monkeypatch.setattr(gallery.hil, "message_uid", AsyncMock(return_value={"total": 0}))
    fixture = SimpleNamespace(rest=SimpleNamespace(base_url="offline", token="fixture-only"),
        phone=AsyncMock(return_value={"mailbox": {"enabled": False, "new": 0, "old": 0}}))
    if second_esphome_owner:
        with pytest.raises(RuntimeError, match="exactly one existing HA device"):
            await gallery.mailbox_args(SimpleNamespace(output=tmp_path), fixture, "AA:BB:CC:DD:EE:FF")
        fixture.phone.assert_not_awaited()
    else:
        result = await gallery.mailbox_args(SimpleNamespace(output=tmp_path), fixture, "AA:BB:CC:DD:EE:FF")
        assert result.mailbox_device == "fresh-device"


async def test_mailbox_target_adapter_restores_original_waiter_when_scenario_fails(monkeypatch):
    original = gallery.hil.MwiTrace.waiting
    monkeypatch.setattr(gallery, "HaWs", OfflineWs)
    monkeypatch.setattr(gallery.hil, "mailbox_menu_scenario", AsyncMock(side_effect=RuntimeError("owned failure")))
    fixture = SimpleNamespace(rest=SimpleNamespace(base_url="offline", token="fixture-only"))
    with pytest.raises(RuntimeError, match="owned failure"):
        await gallery.capture_mailbox(SimpleNamespace(), SimpleNamespace(), fixture, "example.invalid", "192.0.2.1")
    assert gallery.hil.MwiTrace.waiting is original


@pytest.mark.parametrize("page,avatar,expected,accepted", [
    ("call_idle", "NO", "call_idle", True),
    ("unknown", "NO", "settings", True),
    ("unknown", "NO", "call_idle", False),
    ("idle", "YES", "settings", False),
    ("call_dialer", "NO", "call_idle", False),
])
async def test_native_page_checkpoint_never_promotes_unknown_or_private_avatar(page, avatar, expected, accepted):
    class NativeBoundary:
        def __init__(self, _client):
            self.log = []

        async def service(self, name):
            assert name == "registration_runtime_diagnostics"
            self.log.append({"message": f"[ui.hil] page={page} avatar_visible={avatar}"})

        async def wait(self, predicate, timeout):
            assert predicate() and timeout == 5

    witness = gallery.gallery_witness(SimpleNamespace(EspWitness=NativeBoundary))(None, SimpleNamespace(), "example.invalid")
    witness.expected_page = expected
    if accepted:
        result = await witness.ui_checkpoint()
        assert result["page_identity_available"] is (page != "unknown")
        assert "semantic_review" not in result
    else:
        with pytest.raises(RuntimeError, match="safe phone page"):
            await witness.ui_checkpoint()
