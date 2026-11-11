"""Native queue-member controls project the existing authorized queue owner."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from homeassistant.core import Context
from homeassistant.exceptions import Unauthorized

from .test_managed_phones_ha import phone_lab as phone_lab, custom_integration_path as custom_integration_path
from .test_group_devices_ha import add_phone

pytestmark = pytest.mark.ha


@pytest.fixture
async def select_lab(phone_lab):
    from custom_components.voip_stack.call_queues import QueueManager, async_set_queue_agent_state
    from custom_components.voip_stack import services
    from homeassistant.helpers import entity_registry as er
    lab = phone_lab
    shared = er.async_get(lab.hass).async_get_or_create("event", "voip_stack", "voip_stack_call_event", config_entry=lab.entry)
    lab.hass.states.async_set(shared.entity_id, "idle")
    for identity in ("first", "second"):
        add_phone(lab, identity, queues=["Support"])
        lab.runtime.endpoints.update(identity, availability="available", entity_ids=frozenset({f"sensor.{identity}_phone"}))
        lab.hass.states.async_set(f"sensor.{identity}_phone", "idle")
    manager = QueueManager(lab.hass, lab.entry)
    lab.runtime.queues = lab.manager = manager
    manager.refresh()
    lab.identity = manager.resolve("Support").queue_id
    manager._unsub = lab.runtime.endpoints.subscribe(manager.endpoint_changed)
    await services.async_register_services(lab.hass, {"set_queue_agent_state": async_set_queue_agent_state})
    yield lab
    await manager.close()


async def test_select_projects_each_pair_updates_without_polling_and_tracks_membership(select_lab):
    from custom_components.voip_stack import select
    lab = select_lab
    entry = lab.entry
    added = []

    def add(entities, **_kwargs):
        for entity in entities:
            entity.entity_id = f"select.agent_{len(added)}"
            entity.async_write_ha_state = Mock()
            entity.async_remove = AsyncMock()
            added.append(entity)

    await select.async_setup_entry(lab.hass, entry, add)
    assert len(added) == 2 and len({entity.unique_id for entity in added}) == 2
    first = next(entity for entity in added if entity.endpoint_id == "first")
    assert first.options == ["available", "paused", "logged_out"]
    assert not first.should_poll and first.current_option == "available"
    assert ("voip_stack", f"group:{lab.identity}") in first.device_info["identifiers"]
    lab.manager.set_agent_state(lab.identity, first.endpoint_id, "paused", reason="operator_break")
    assert first.current_option == "paused"
    assert first.extra_state_attributes["pause_reason"] == "operator_break"
    first.async_write_ha_state.assert_called()
    lab.runtime.endpoints.update(first.endpoint_id, availability="offline", dnd=True)
    assert first.available
    assert first.extra_state_attributes["online"] is False
    assert first.extra_state_attributes["dnd"] is True
    lab.manager.start_wrap_up(lab.identity, first.endpoint_id, duration=5)
    assert first.extra_state_attributes["wrap_up_remaining"] > 0
    identity = first.unique_id
    lab.manager.definitions[lab.identity] = replace(lab.manager.definitions[lab.identity], name="Renamed queue")
    lab.manager._changed()
    assert len(added) == 2 and first.unique_id == identity
    # Runtime endpoints can rename without changing the queue/member identity.
    lab.runtime.endpoints.update(first.endpoint_id, name="New phone name")
    assert first.translation_placeholders == {"phone": "New phone name"}
    lab.manager.definitions[lab.identity] = replace(lab.manager.definitions[lab.identity], members=("second",))
    lab.manager.agent_states[lab.identity].pop(first.endpoint_id)
    lab.manager._changed()
    await lab.hass.async_block_till_done()
    assert not first.available
    first.async_remove.assert_awaited_once_with(force_remove=True)


async def test_select_invokes_real_agent_state_service_with_user_context_and_denies_read_only(select_lab, hass_admin_user, hass_read_only_user):
    from custom_components.voip_stack import select
    lab = select_lab
    entry = lab.entry
    entities = []
    await select.async_setup_entry(lab.hass, entry, lambda rows, **_kwargs: entities.extend(rows))
    entity = entities[0]
    context = Context(user_id=hass_admin_user.id)
    entity.async_set_context(context)
    await entity.async_select_option("logged_out")
    assert lab.manager.agent_states[lab.identity][entity.endpoint_id] == "logged_out"
    entity.async_set_context(Context(user_id=hass_read_only_user.id))
    with pytest.raises(Unauthorized):
        await entity.async_select_option("available")
    assert lab.manager.agent_states[lab.identity][entity.endpoint_id] == "logged_out"


async def test_wrap_up_expiry_updates_select_through_one_owned_deadline_and_unload_cancels_it(select_lab):
    from custom_components.voip_stack import select
    lab = select_lab
    entities = []
    await select.async_setup_entry(lab.hass, lab.entry, lambda rows, **_kwargs: entities.extend(rows))
    first = entities[0]
    first.entity_id = "select.operator_wrap"
    first.async_write_ha_state = Mock()
    expired = asyncio.Event()
    remove = lab.manager.subscribe(lambda: expired.set() if not first.extra_state_attributes["wrap_up_remaining"] else None)
    try:
        lab.manager.start_wrap_up(lab.identity, first.endpoint_id, duration=0.02)
        assert first.extra_state_attributes["wrap_up_remaining"] > 0
        await asyncio.wait_for(expired.wait(), 1)
        assert first.extra_state_attributes["wrap_up_remaining"] == 0
        assert first.async_write_ha_state.call_count >= 2
        lab.manager.start_wrap_up(lab.identity, first.endpoint_id, duration=5)
        timer = lab.manager._wrap_timer
        await lab.manager.close()
        assert timer.cancelled() and lab.manager._wrap_timer is None
    finally:
        remove()
