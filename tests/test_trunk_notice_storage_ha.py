"""Retirement notices survive independent real HA storage writes and restarts."""

from copy import deepcopy
import json
from pathlib import Path
import shutil

import pytest
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant.common import MockConfigEntry, async_test_home_assistant, flush_store

from .test_trunk_migration_recovery_ha import add_trunk

pytestmark = pytest.mark.ha


@pytest.fixture
def hass_storage():
    """Use the real Store file IO, not the plugin's dictionary-backed store."""
    return {}


@pytest.fixture
def load_registries():
    """Load the real IssueRegistry explicitly, without no-write registry stores."""
    return False


@pytest.fixture
def hass_config_dir(tmp_path):
    directory = tmp_path / "first-start"
    directory.mkdir()
    return str(directory)


async def test_config_saved_before_issue_registry_restores_notice_and_respects_ignore(hass, tmp_path):
    from custom_components.voip_stack import async_ensure_duration_policies, async_migrate_entry
    from custom_components.voip_stack.trunk_config import LEGACY_TRUNK_MENU_KEYS, PENDING_TRUNK_MENU_NOTICES

    await ir.async_load(hass)
    registry = ir.async_get(hass)
    assert type(registry._store) is ir.IssueRegistryStore
    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=5, data={})
    entry.add_to_hass(hass)
    row = add_trunk(hass, entry, "provider", trunk_inbound_mode="dtmf", trunk_dtmf_timeout_ms=3000)
    issue_id = "trunk_dtmf_retired_" + row.subentry_id
    assert await async_migrate_entry(hass, entry)
    assert registry.async_get_issue("voip_stack", issue_id)
    assert not LEGACY_TRUNK_MENU_KEYS.intersection(row.data)
    assert entry.minor_version == 7

    # Complete only the actual config-entry Store write. The IssueRegistry
    # still has a delayed save: no issue file exists on disk at the crash point.
    await flush_store(hass.config_entries._store)
    source = Path(hass.config.path(".storage"))
    persisted = json.loads((source / "core.config_entries").read_text())
    saved_entry, = persisted["data"]["entries"]
    assert saved_entry["minor_version"] == 7
    assert not LEGACY_TRUNK_MENU_KEYS.intersection(saved_entry["subentries"][0]["data"])
    assert registry._store._data is not None
    assert not (source / ir.STORAGE_KEY).exists()
    restart_path = tmp_path / "after-crash"
    shutil.copytree(source, restart_path / ".storage")

    # Fresh HA owners load only the files present at the crash point. A normal
    # shutdown of the original fixture cannot flush into this isolated copy.
    async with async_test_home_assistant(load_registries=False, config_dir=str(restart_path)) as restarted:
        try:
            await restarted.config_entries.async_initialize()
            await ir.async_load(restarted)
            restored = restarted.config_entries.async_get_known_entry(entry.entry_id)
            after_crash = ir.async_get(restarted)
            assert after_crash.async_get_issue("voip_stack", issue_id) is None
            assert async_ensure_duration_policies(restarted, restored)
            recovered = after_crash.async_get_issue("voip_stack", issue_id)
            assert recovered is not None
            assert recovered.is_persistent
            assert restored.data[PENDING_TRUNK_MENU_NOTICES] == [row.subentry_id]
            assert not LEGACY_TRUNK_MENU_KEYS.intersection(restored.subentries[row.subentry_id].data)
            assert restored.subentries[row.subentry_id].data["trunk_inbound_default_target"] == "Desk"

            # The owner's native Ignore operation remains authoritative after
            # both stores persist and another process loads the retained journal.
            ignored = after_crash.async_ignore("voip_stack", issue_id, True)
            assert ignored.dismissed_version is not None
            await flush_store(restarted.config_entries._store)
            await flush_store(after_crash._store)
            ignored_store = json.loads((restart_path / ".storage" / ir.STORAGE_KEY).read_text())
            saved_issue, = ignored_store["data"]["issues"]
            assert saved_issue["dismissed_version"] == ignored.dismissed_version
            after_ignore_path = tmp_path / "after-ignore"
            shutil.copytree(restart_path / ".storage", after_ignore_path / ".storage")
        finally:
            await restarted.async_stop(force=True)

    async with async_test_home_assistant(load_registries=False, config_dir=str(after_ignore_path)) as final:
        try:
            await final.config_entries.async_initialize()
            await ir.async_load(final)
            final_entry = final.config_entries.async_get_known_entry(entry.entry_id)
            final_registry = ir.async_get(final)
            before_issue = final_registry.async_get_issue("voip_stack", issue_id)
            assert before_issue.dismissed_version == ignored.dismissed_version
            before_entry = deepcopy(final_entry.as_dict())
            events = []

            @callback
            def observe(event):
                if event.data["domain"] == "voip_stack" and event.data["issue_id"] == issue_id:
                    events.append(event.data["action"])

            unsubscribe = final.bus.async_listen(ir.EVENT_REPAIRS_ISSUE_REGISTRY_UPDATED, observe)
            try:
                assert async_ensure_duration_policies(final, final_entry)
                assert async_ensure_duration_policies(final, final_entry)
                await final.async_block_till_done()
            finally:
                unsubscribe()
            assert final_registry.async_get_issue("voip_stack", issue_id) == before_issue
            assert final_entry.as_dict() == before_entry
            assert not events
            assert final_entry.data[PENDING_TRUNK_MENU_NOTICES] == [row.subentry_id]
        finally:
            await final.async_stop(force=True)


async def test_notice_for_deleted_trunk_does_not_block_current_setup(hass):
    from custom_components.voip_stack import async_ensure_duration_policies
    from custom_components.voip_stack.duration_config import initialize_duration_config
    from custom_components.voip_stack.trunk_config import PENDING_TRUNK_MENU_NOTICES

    await ir.async_load(hass)
    entry = MockConfigEntry(domain="voip_stack", version=7, minor_version=7,
        data=initialize_duration_config({PENDING_TRUNK_MENU_NOTICES: ["deleted-trunk-subentry"]}))
    entry.add_to_hass(hass)
    assert async_ensure_duration_policies(hass, entry)
    assert not entry.subentries
    assert ir.async_get(hass).async_get_issue("voip_stack", "trunk_dtmf_retired_deleted-trunk-subentry") is None
    assert ir.async_get(hass).async_get_issue("voip_stack", "duration_configuration_invalid_" + entry.entry_id) is None
