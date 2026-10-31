"""Safety and provenance boundaries of the maintained native gallery workflow."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import hashlib
import json
import asyncio
import time

import pytest

from tools.ha_voip_lab import display_gallery as gallery
from tests.test_phone_services_hil_tool import native_audio_dump
from tests.test_esp_screenshot_witness import witness_module


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


def test_navigation_wait_uses_the_matching_compiled_source_contract(tmp_path):
    source = tmp_path / "src/main.cpp"
    source.parent.mkdir()
    source.write_text("nav_anim_ms->value() = 0;\nnav_anim_ms->value() = 300;\n// nav_anim_ms->value() = 9000;\n")
    receipt = {"private_debug_source": str(tmp_path),
               "main_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}
    assert gallery.compiled_transition_seconds(receipt) == .3
    source.write_text("nav_anim_ms->value() = 9000;\n")
    with pytest.raises(RuntimeError, match="compiled receipt"):
        gallery.compiled_transition_seconds(receipt)


@pytest.mark.parametrize("username", ["GalleryHome", "galleryhome"])
async def test_existing_gallery_account_prevents_every_account_mutation(username):
    fixture = SimpleNamespace(owned={}, run_id="fresh", action=AsyncMock(return_value={
        "accounts": [{"username": username}]}))
    with pytest.raises(RuntimeError, match="existing account"):
        await gallery.create_accounts(fixture)
    assert fixture.action.await_args_list[0].args == ("list_accounts",)
    assert fixture.action.await_count == 1 and fixture.owned == {}


async def test_gallery_journals_ownership_before_a_create_request_can_fail(tmp_path):
    async def action(name, data=None, **_kwargs):
        if name == "list_accounts":
            return {"accounts": []}
        record = json.loads((tmp_path / "fixture-accounts.json").read_text())
        assert record["baseline"] == [] and record["run_id"] == "fresh"
        assert record["owned"] == [data]
        assert (tmp_path / "fixture-accounts.json").stat().st_mode & 0o777 == 0o600
        raise RuntimeError("interrupted response")
    fixture = SimpleNamespace(output=tmp_path, owned={}, run_id="fresh", action=action)
    with pytest.raises(RuntimeError, match="interrupted response"):
        await gallery.create_accounts(fixture)
    assert set(fixture.owned) == {"GalleryGarage"}


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


def deferred_diagnostic_witness():
    module = witness_module()
    client = SimpleNamespace()
    witness = gallery.gallery_witness(module)(client, SimpleNamespace(), "example.invalid")
    witness.services = {"phone_runtime_snapshot": "snapshot", "registration_runtime_diagnostics": "ui"}
    requests = []

    async def execute(service, _data):
        requests.append(time.monotonic())
        loop = asyncio.get_running_loop()
        if service == "snapshot":
            loop.call_soon(witness.on_log, SimpleNamespace(message=b"[phone.hil] contacts=8"))
        for index, message in enumerate(native_audio_dump(1000 * len(requests))):
            loop.call_later(index * .001, witness.on_log, SimpleNamespace(message=message.encode()))
    client.execute_service = execute
    return witness, requests


async def test_gallery_diagnostic_service_waits_for_the_real_deferred_dump_boundary():
    witness, _requests = deferred_diagnostic_witness()
    await witness.service("phone_runtime_snapshot")
    assert gallery.hil._completed_audio_dump(witness.log) is not None


async def test_gallery_serializes_both_diagnostic_actions_through_one_second_admission():
    witness, requests = deferred_diagnostic_witness()
    await asyncio.gather(witness.service("phone_runtime_snapshot"),
                         witness.service("registration_runtime_diagnostics"))
    assert len(requests) == 2 and requests[1] - requests[0] >= 1.0


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

        async def service(self, name, data=None):
            assert name == "registration_runtime_diagnostics"
            self.log.append({"message": f"[ui.hil] page={page} avatar_visible={avatar}"})
            self.log.extend({"message": value} for value in native_audio_dump())

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
