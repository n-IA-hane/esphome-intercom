"""Retirement journals close only after the native issue is durable."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
from unittest.mock import patch

import pytest
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry, async_test_home_assistant, flush_store

from .test_trunk_migration_recovery_ha import add_trunk

pytestmark = pytest.mark.ha


@pytest.fixture
def hass_storage():
    """Exercise native file IO, not the test plugin's in-memory store."""
    return {}


@pytest.fixture
def load_registries():
    """The tests explicitly load a writable native issue registry."""
    return False


@pytest.fixture
def hass_config_dir(tmp_path):
    directory = tmp_path / "initial"
    directory.mkdir()
    return str(directory)


async def pending_notice(hass):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.trunk_config import PENDING_TRUNK_MENU_NOTICES

    await ir.async_load(hass)
    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=5, data={})
    entry.add_to_hass(hass)
    row = add_trunk(hass, entry, "provider", trunk_inbound_mode="dtmf", trunk_dtmf_timeout_ms=3000)
    assert await async_migrate_entry(hass, entry)
    assert entry.data[PENDING_TRUNK_MENU_NOTICES] == [row.subentry_id]
    return entry, row, "trunk_dtmf_retired_" + row.subentry_id


async def test_durable_notice_closes_journal_and_stays_ignored_after_reload(hass, tmp_path):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack import duration_config
    from custom_components.voip_stack.trunk_config import PENDING_TRUNK_MENU_NOTICES

    entry, row, issue_id = await pending_notice(hass)
    registry = ir.async_get(hass)
    ignored = registry.async_ignore("voip_stack", issue_id, True)
    await flush_store(registry._store)
    saved = json.loads(Path(hass.config.path(".storage", ir.STORAGE_KEY)).read_text())
    assert saved["data"]["issues"][0]["dismissed_version"] == ignored.dismissed_version
    before = deepcopy(entry.as_dict())
    assert await async_migrate_entry(hass, entry)
    assert PENDING_TRUNK_MENU_NOTICES not in entry.data
    assert entry.data == {key: value for key, value in before["data"].items()
                          if key != PENDING_TRUNK_MENU_NOTICES}
    assert row.data == before["subentries"][0]["data"]
    await flush_store(hass.config_entries._store)
    restart_path = tmp_path / "completed-restart"
    shutil.copytree(Path(hass.config.path(".storage")), restart_path / ".storage")
    async with async_test_home_assistant(load_registries=False, config_dir=str(restart_path)) as restarted:
        try:
            await restarted.config_entries.async_initialize()
            await ir.async_load(restarted)
            restored = restarted.config_entries.async_get_known_entry(entry.entry_id)
            restored_registry = ir.async_get(restarted)
            with patch.object(duration_config, "migrate_duration_policies",
                              wraps=duration_config.migrate_duration_policies) as migration:
                assert await async_migrate_entry(restarted, restored)
                assert await async_migrate_entry(restarted, restored)
            assert migration.call_count == 0
            assert PENDING_TRUNK_MENU_NOTICES not in restored.data
            assert restored_registry.async_get_issue("voip_stack", issue_id).dismissed_version == ignored.dismissed_version
        finally:
            await restarted.async_stop(force=True)


async def test_pending_notice_does_not_reenter_completed_duration_migration(hass):
    from custom_components.voip_stack import async_ensure_duration_policies, duration_config

    entry, _, _ = await pending_notice(hass)
    with patch.object(duration_config, "migrate_duration_policies",
                      wraps=duration_config.migrate_duration_policies) as migration:
        assert async_ensure_duration_policies(hass, entry)
        assert async_ensure_duration_policies(hass, entry)
    assert migration.call_count == 0


async def test_issue_present_only_in_memory_keeps_crash_recovery_journal(hass):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.trunk_config import PENDING_TRUNK_MENU_NOTICES

    entry, row, issue_id = await pending_notice(hass)
    await flush_store(hass.config_entries._store)
    assert ir.async_get(hass).async_get_issue("voip_stack", issue_id) is not None
    assert not Path(hass.config.path(".storage", ir.STORAGE_KEY)).exists()
    assert await async_migrate_entry(hass, entry)
    assert entry.data[PENDING_TRUNK_MENU_NOTICES] == [row.subentry_id]


@pytest.mark.parametrize("unconfirmed", ["wrong-owner", "not-persistent", "future-schema", "future-minor", "malformed"])
async def test_unconfirmed_disk_record_does_not_acknowledge_notice(hass, unconfirmed):
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.trunk_config import PENDING_TRUNK_MENU_NOTICES

    entry, row, issue_id = await pending_notice(hass)
    issue = ir.async_get(hass).async_get_issue("voip_stack", issue_id).to_json()
    payload = {"version": ir.STORAGE_VERSION_MAJOR, "minor_version": ir.STORAGE_VERSION_MINOR,
               "key": ir.STORAGE_KEY, "data": {"issues": [issue]}}
    if unconfirmed == "wrong-owner":
        issue["config_entry_id"] = "another-entry"
    elif unconfirmed == "not-persistent":
        issue["is_persistent"] = False
    elif unconfirmed == "future-schema":
        payload["version"] += 1
    elif unconfirmed == "future-minor":
        payload["minor_version"] += 1
    path = Path(hass.config.path(".storage", ir.STORAGE_KEY))
    path.parent.mkdir(exist_ok=True)
    path.write_text("broken-json" if unconfirmed == "malformed" else json.dumps(payload))
    assert await async_migrate_entry(hass, entry)
    assert entry.data[PENDING_TRUNK_MENU_NOTICES] == [row.subentry_id]


async def test_unreadable_issue_storage_keeps_recovery_without_blocking_migration(hass, monkeypatch):
    from homeassistant.exceptions import HomeAssistantError
    from homeassistant.util import json as json_util
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.trunk_config import PENDING_TRUNK_MENU_NOTICES

    entry, row, _ = await pending_notice(hass)

    def unreadable(_path):
        raise HomeAssistantError("Storage temporarily unavailable")

    monkeypatch.setattr(json_util, "load_json", unreadable)
    assert await async_migrate_entry(hass, entry)
    assert entry.data[PENDING_TRUNK_MENU_NOTICES] == [row.subentry_id]


async def test_new_journal_entry_is_preserved_when_configuration_changes_during_read(hass, monkeypatch):
    from homeassistant.util.json import load_json
    from custom_components.voip_stack import async_migrate_entry
    from custom_components.voip_stack.trunk_config import PENDING_TRUNK_MENU_NOTICES

    entry, _, _ = await pending_notice(hass)
    await flush_store(ir.async_get(hass)._store)
    original_executor = hass.async_add_executor_job
    later = None

    async def read_then_update(target, *args, **kwargs):
        nonlocal later
        result = await original_executor(target, *args, **kwargs)
        if target is load_json and later is None:
            later = add_trunk(hass, entry, "another-provider")
            hass.config_entries.async_update_entry(entry, data={
                **entry.data, "concurrent_setting": "preserved",
                PENDING_TRUNK_MENU_NOTICES: [*entry.data[PENDING_TRUNK_MENU_NOTICES], later.subentry_id],
            })
        return result

    monkeypatch.setattr(hass, "async_add_executor_job", read_then_update)
    assert await async_migrate_entry(hass, entry)
    assert later is not None
    assert entry.data[PENDING_TRUNK_MENU_NOTICES] == [later.subentry_id]
    assert entry.data["concurrent_setting"] == "preserved"


async def test_issue_changed_during_storage_read_keeps_recovery_journal(hass, monkeypatch):
    from homeassistant.util.json import load_json
    from custom_components.voip_stack.trunk_config import async_complete_trunk_menu_notices, PENDING_TRUNK_MENU_NOTICES

    entry, row, issue_id = await pending_notice(hass)
    registry = ir.async_get(hass)
    await flush_store(registry._store)
    original_executor = hass.async_add_executor_job

    async def read_then_ignore(target, *args, **kwargs):
        result = await original_executor(target, *args, **kwargs)
        if target is load_json:
            registry.async_ignore("voip_stack", issue_id, True)
        return result

    monkeypatch.setattr(hass, "async_add_executor_job", read_then_ignore)
    await async_complete_trunk_menu_notices(hass, entry)
    assert entry.data[PENDING_TRUNK_MENU_NOTICES] == [row.subentry_id]
    assert registry.async_get_issue("voip_stack", issue_id).dismissed_version is not None


async def test_replaced_entry_during_storage_read_cannot_acknowledge_old_owner(hass, monkeypatch):
    from homeassistant.util.json import load_json
    from custom_components.voip_stack.trunk_config import async_complete_trunk_menu_notices, PENDING_TRUNK_MENU_NOTICES

    entry, row, _ = await pending_notice(hass)
    await flush_store(ir.async_get(hass)._store)
    original_executor = hass.async_add_executor_job
    original_lookup = hass.config_entries.async_get_entry
    replacement = MockConfigEntry(domain="voip_stack", entry_id=entry.entry_id, version=7, minor_version=7,
                                  data={"unrelated": "replacement"})

    async def read_then_replace(target, *args, **kwargs):
        result = await original_executor(target, *args, **kwargs)
        if target is load_json:
            monkeypatch.setattr(hass.config_entries, "async_get_entry",
                lambda identity: replacement if identity == entry.entry_id else original_lookup(identity))
        return result

    monkeypatch.setattr(hass, "async_add_executor_job", read_then_replace)
    await async_complete_trunk_menu_notices(hass, entry)
    assert entry.data[PENDING_TRUNK_MENU_NOTICES] == [row.subentry_id]
    assert replacement.data == {"unrelated": "replacement"}
