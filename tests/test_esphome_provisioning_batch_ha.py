"""Bound global reconciliation through the production native-phone coordinator."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from aioesphomeapi import DeviceInfo
from homeassistant.components.esphome.entry_data import RuntimeEntryData
from homeassistant.helpers import device_registry as dr, entity_registry as er
from pytest_homeassistant.common import MockConfigEntry
import pytest

from .test_managed_phones_ha import (
    custom_integration_path as custom_integration_path,
    managed as managed, native_services, phone_lab as phone_lab,
)

pytestmark = pytest.mark.ha


def add_phone(lab, index):
    mac = f"84:c7:bb:01:{index // 256:02x}:{index % 256:02x}"
    native = MockConfigEntry(domain="esphome", title=f"Native {index}",
        data={"host": f"127.0.1.{index}", "device_name": f"phone_{index}"})
    native.add_to_hass(lab.hass)
    device = dr.async_get(lab.hass).async_get_or_create(config_entry_id=native.entry_id,
        identifiers={("esphome", mac.replace(":", ""))},
        connections={(dr.CONNECTION_NETWORK_MAC, mac)}, name=f"Phone {index}")

    async def execute(service, data):
        if service.name == "set_account":
            # Credentials must be admitted by the real registrar before any RPC.
            account = lab.registrar.accounts[data["username"].casefold()]
            assert account.password == data["password"]
            assert lab.runtime.endpoints.by_username(data["username"]) is not None
            assert len(lab.registrar.accounts) == lab.expected_count
            assert len(lab.runtime.endpoints.endpoints) == lab.expected_count
        lab.sent.append((service.name, dict(data)))

    native.runtime_data = RuntimeEntryData(entry_id=native.entry_id, title=native.title,
        client=SimpleNamespace(is_connected=True, execute_service=execute), store=MagicMock(),
        device_info=DeviceInfo(name=f"phone_{index}", friendly_name=f"Phone {index}", mac_address=mac),
        services=native_services(), available=True)
    native.runtime_data.first_connect_done.set()
    entity = er.async_get(lab.hass).async_get_or_create("sensor", "esphome",
        mac.replace(":", "") + "-voip_state", config_entry=native, device_id=device.id,
        suggested_object_id=f"phone_{index}_voip_state")
    lab.hass.states.async_set(entity.entity_id, "idle")
    return native, device


@pytest.mark.parametrize("count", [1, 10, 50])
async def test_unchanged_inventory_has_no_global_replacement(managed, monkeypatch, count):
    from custom_components.voip_stack import esphome_provisioning as production

    lab = managed
    lab.expected_count = count
    for index in range(1, count):
        add_phone(lab, index)
    counters = {"projection": 0, "registrar": 0, "upsert": 0}
    project, refresh, upsert = (production.sync_registry_from_entry,
        lab.registrar.update_accounts, lab.runtime.endpoints.upsert)

    def project_counted(*args, **kwargs):
        counters["projection"] += 1
        return project(*args, **kwargs)

    def registrar_counted(*args, **kwargs):
        counters["registrar"] += 1
        return refresh(*args, **kwargs)

    def upsert_counted(*args, **kwargs):
        counters["upsert"] += 1
        return upsert(*args, **kwargs)

    monkeypatch.setattr(production, "sync_registry_from_entry", project_counted)
    monkeypatch.setattr(lab.registrar, "update_accounts", registrar_counted)
    monkeypatch.setattr(lab.runtime.endpoints, "upsert", upsert_counted)
    await lab.controller.async_sync()
    initial = dict(counters)
    assert len(lab.registrar.accounts) == len(lab.runtime.endpoints.endpoints) == count
    assert len([item for item in lab.sent if item[0] == "set_account"]) == count
    counters.update(projection=0, registrar=0, upsert=0)
    before = list(lab.sent)
    loop = asyncio.get_running_loop()
    tick, started = [], loop.time()
    loop.call_soon(lambda: tick.append(loop.time() - started))
    await lab.controller.async_sync()
    elapsed = loop.time() - started
    await asyncio.sleep(0)
    metrics = {"phones": count, "initial": initial, "unchanged": dict(counters),
        "elapsed_seconds": elapsed, "loop_delay_seconds": tick[0]}
    folder = Path("/tmp/voip-tests/night-a06")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    (folder / f"inventory-{count}.json").write_text(json.dumps(metrics, indent=2) + "\n")
    assert lab.sent == before
    assert counters == {"projection": 0, "registrar": 0, "upsert": 0}, metrics
    assert initial["projection"] == initial["registrar"] == 1, metrics
    assert initial["upsert"] == 2 * count, metrics

    # A native configuration burst changes one phone, while the coordinator
    # keeps the existing registrar credentials and projects the final state once.
    counters.update(projection=0, registrar=0, upsert=0)
    phone = next(row for row in lab.entry.subentries.values() if row.subentry_type == "phone")
    # Native modified_at is a wall clock, not a configuration generation.
    import homeassistant.config_entries as native_entries
    timestamp = lab.entry.modified_at
    monkeypatch.setattr(native_entries, "utcnow", lambda: timestamp)
    lab.hass.config_entries.async_update_subentry(lab.entry, phone,
        data={**phone.data, "dnd": True})
    for _ in range(50):
        lab.controller.schedule()
    await lab.hass.async_block_till_done(wait_background_tasks=True)
    assert lab.runtime.endpoints.by_username(phone.data["username"]).dnd
    assert counters["projection"] == 1
    assert counters["registrar"] == 0
    assert counters["upsert"] == count
    assert lab.sent == before
    metrics["one_changed_phone_burst"] = dict(counters)
    (folder / f"inventory-{count}.json").write_text(json.dumps(metrics, indent=2) + "\n")
