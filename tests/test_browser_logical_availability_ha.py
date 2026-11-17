"""Configured browser phones retain policy and signaling without media cards.

These in-process HA and loopback SIP tests do not qualify browser audio or ESPs.
"""

import asyncio
from dataclasses import replace
import re

from homeassistant.config_entries import ConfigSubentry
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.trigger import TriggerConfig
import pytest

from custom_components.voip_stack.automation_context import current_execution
from custom_components.voip_stack.endpoint_routing import roster_from_peers
from custom_components.voip_stack.peer_snapshot import async_build_peer_snapshot
from custom_components.voip_stack.phone_config import (
    endpoint_from_data, endpoint_from_subentry, sync_registry_from_entry,
)
from custom_components.voip_stack.phone_policy_runtime import setup_phone_policy_events
from custom_components.voip_stack.runtime_diagnostics import runtime_resource_snapshot
from custom_components.voip_stack.store import config_entry
from custom_components.voip_stack.trigger import PhoneStateTrigger
from custom_components.voip_stack.websocket_api import async_register_websocket_api

from .test_sip_phone_transfers_ha import registered_phones as registered_phones
from .test_trunk_calls_ha import trunks as trunks, lab as lab

pytestmark = pytest.mark.ha


@pytest.fixture
async def logical_browser(trunks, registered_phones, request, monkeypatch):
    from custom_components.voip_stack import endpoint_runtime, invite_router, peer_snapshot

    entry = config_entry(trunks.hass)
    timeout = getattr(request, "param", {}).get("ringing_timeout", 1)
    subentry = ConfigSubentry(
        subentry_type="phone", title="Casa", unique_id="phone:casa",
        data={
            "endpoint_id": "casa", "name": "Casa", "extension": "221",
            "kind": "browser", "enabled": True,
            "call_policy": {
                "unavailable": {"kind": "contact", "target": "transfer_b"},
                "ringing_timeout": timeout,
                "no_answer": {"kind": "contact", "target": "transfer_c"},
            },
        },
    )
    trunks.hass.config_entries.async_add_subentry(entry, subentry)
    entity = er.async_get(trunks.hass).async_get_or_create(
        "event", "voip_stack", "browser-casa-call", config_entry=entry,
        suggested_object_id="browser_casa_call",
    )
    endpoint = replace(
        endpoint_from_subentry(entry, subentry), device_id="browser-casa",
        entity_ids=(entity.entity_id,),
    )
    trunks.runtime.endpoints.register(endpoint)
    # The reused transfer fixture freezes its directory. Exercise real configured
    # phone/contact discovery instead, including changes made after startup.
    peer_snapshot.async_build_peer_snapshot.side_effect = async_build_peer_snapshot
    monkeypatch.setattr(endpoint_runtime, "_roster_from_peers", roster_from_peers)
    monkeypatch.setattr(invite_router, "_roster_from_peers", roster_from_peers)
    setup_phone_policy_events(trunks.hass, entry)
    trunks.browser_subentry = subentry
    assert not trunks.runtime.softphone_presence
    # The independent peers have completed real REGISTER before this fixture.
    assert all(trunks.runtime.endpoints.require(name).availability.value == "available"
               for name in ("transfer_a", "transfer_b", "transfer_c"))
    return trunks


async def public_phone(lab):
    return await lab.hass.services.async_call(
        "voip_stack", "get_phone_state", {"device_id": "browser-casa"},
        blocking=True, return_response=True,
    )


def resources(lab):
    return runtime_resource_snapshot(
        lab.calls, rtp_port_pool=lab.runtime.rtp_port_pool,
        call_artifacts=lab.calls, browser_media=lab.runtime.media,
        runtime_tasks=lab.runtime.tasks,
    )


def assert_no_browser_media_owner(lab):
    counts = resources(lab)["resource_counts"]
    for key in ("active_audio_sessions", "active_video_sessions", "audio_ws_owners",
                "video_ws_owners", "media_identity_locks"):
        assert counts[key] == 0, (key, counts)


async def end_call(lab, caller):
    await asyncio.to_thread(caller.hangup)
    async with asyncio.timeout(3):
        while lab.calls.sessions:
            await asyncio.sleep(.01)
    assert resources(lab)["call_scoped_quiescent"], resources(lab)


async def ws_request(client, command, **kwargs):
    message = {"type": command, **kwargs}
    await client.send_json_auto_id(message)
    async with asyncio.timeout(3):
        while True:
            reply = await client.receive_json()
            if reply["type"] == "result" and reply["id"] == message["id"]:
                assert reply["success"], reply
                return reply.get("result"), message["id"]


async def browser_snapshot(client):
    result, _ = await ws_request(
        client, "voip_stack/ha_softphone_state", endpoint_id="casa",
    )
    return result


async def open_browser_ws(lab, hass_ws_client):
    client = await hass_ws_client(lab.hass)
    async_register_websocket_api(lab.hass)
    return client


async def test_configured_unregistered_sip_phone_stays_offline(lab):
    endpoint = replace(endpoint_from_data(
        config_entry(lab.hass),
        {"endpoint_id": "unregistered", "name": "Unregistered", "kind": "sip_account",
         "username": "unregistered", "enabled": True},
    ), device_id="unregistered-device")
    lab.runtime.endpoints.register(endpoint)
    state = await lab.hass.services.async_call(
        "voip_stack", "get_phone_state", {"device_id": endpoint.device_id},
        blocking=True, return_response=True,
    )
    assert state["availability"] == "offline" and state["state"] == "unavailable"
    assert_no_browser_media_owner(lab)
    assert resources(lab)["call_scoped_quiescent"]


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("transport", ["udp", "tcp"])
async def test_no_card_uses_noanswer_instead_of_unavailable_policy(
    logical_browser, registered_phones, transport,
):
    lab = logical_browser
    caller, unavailable, noanswer = registered_phones
    ringing, actions = [], []
    subject = PhoneStateTrigger(
        lab.hass, TriggerConfig("voip_stack.phone_state_changed",
            options={"phone": "browser-casa", "to": "ringing"}),
    )

    def record(payload, _description, _context):
        async def action():
            execution = current_execution.get()
            ringing.append((payload, execution.token if execution is not None else None))
        task = asyncio.create_task(action())
        actions.append(task)
        return task

    detach = await subject.async_attach_runner(record)
    try:
        before = await public_phone(lab)
        assert before["state"] == "idle" and before["availability"] == "available"
        assert_no_browser_media_owner(lab)
        target = f'sip:221@127.0.0.1:{lab.runtime.transport_config["sip_port"]};transport={transport}'
        await asyncio.to_thread(caller.command, f"/dial {target}")
        async with asyncio.timeout(4):
            while not ("Call established" in caller.read() and any(
                "Call established" in peer.read() for peer in (unavailable, noanswer)
            )):
                await asyncio.sleep(.01)
        assert "Call established" not in unavailable.read(), "No card incorrectly selected unavailable"
        assert "Call established" in noanswer.read()
        await asyncio.gather(*actions)
        assert len(ringing) == 1
        phone, token = ringing[0]
        assert phone["phone"]["state"] == "ringing"
        assert token is not None
        assert phone["call"]["call_id"] == token.call_id
        assert not lab.runtime.softphone_presence
        assert_no_browser_media_owner(lab)
        assert caller.read().count("Call established") == 1
    finally:
        detach()
        await end_call(lab, caller)
    assert (await public_phone(lab))["state"] == "idle"


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_public_snapshot_and_card_lease_do_not_change_enabled_state(
    logical_browser, hass_ws_client,
):
    lab = logical_browser
    client = await open_browser_ws(lab, hass_ws_client)
    try:
        baseline = resources(lab)
        state = await browser_snapshot(client)
        assert state["available"] and state["enabled"]
        assert state["connected_cards"] == 0
        assert state["sip_udp_ready"] and state["sip_tcp_ready"]
        _, subscription = await ws_request(
            client, "voip_stack/subscribe_ha_softphone_state", endpoint_id="casa",
        )
        assert (await browser_snapshot(client))["connected_cards"] == 1
        await ws_request(client, "unsubscribe_events", subscription=subscription)
        state = await browser_snapshot(client)
        assert state["available"] and state["enabled"] and state["connected_cards"] == 0
        assert resources(lab) == baseline
        entry = config_entry(lab.hass)
        lab.hass.config_entries.async_update_subentry(
            entry, lab.browser_subentry, data={**lab.browser_subentry.data, "enabled": False},
        )
        sync_registry_from_entry(lab.hass, entry)
        _, subscription = await ws_request(
            client, "voip_stack/subscribe_ha_softphone_state", endpoint_id="casa",
        )
        state = await browser_snapshot(client)
        assert not state["available"] and not state["enabled"]
        assert state["connected_cards"] == 1
        assert (await public_phone(lab))["state"] == "unavailable"
        await ws_request(client, "unsubscribe_events", subscription=subscription)
        assert not (await browser_snapshot(client))["enabled"]
        assert_no_browser_media_owner(lab)
        assert resources(lab)["call_scoped_quiescent"]
    finally:
        await client.close()
        await lab.hass.async_block_till_done()
    assert not lab.runtime.softphone_presence


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("transport", ["udp", "tcp"])
async def test_disabled_browser_rejects_without_using_phone_policy(
    logical_browser, registered_phones, transport, record_property,
):
    lab = logical_browser
    caller, unavailable, noanswer = registered_phones
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_subentry(
        entry, lab.browser_subentry, data={**lab.browser_subentry.data, "enabled": False},
    )
    sync_registry_from_entry(lab.hass, entry)
    assert (await public_phone(lab))["availability"] == "unavailable"
    output_start = len(caller.read())
    target = f'sip:221@127.0.0.1:{lab.runtime.transport_config["sip_port"]};transport={transport}'
    await asyncio.to_thread(caller.command, f"/dial {target}")
    try:
        async with asyncio.timeout(3):
            while not (final := re.search(r"\b(4\d\d)\b", caller.read()[output_start:])):
                await asyncio.sleep(.01)
        record_property("sip_final_status", int(final[1]))
        assert "Call established" not in unavailable.read()
        assert "Call established" not in noanswer.read()
        assert (await public_phone(lab))["state"] == "unavailable"
    finally:
        await end_call(lab, caller)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("logical_browser", [{"ringing_timeout": 3}], indirect=True)
@pytest.mark.parametrize("transport", ["udp", "tcp"])
async def test_late_card_can_publicly_answer_and_cancel_noanswer(
    logical_browser, registered_phones, hass_ws_client, transport,
):
    lab = logical_browser
    caller, unavailable, noanswer = registered_phones
    client = await open_browser_ws(lab, hass_ws_client)
    try:
        assert (await browser_snapshot(client))["connected_cards"] == 0
        target = f'sip:221@127.0.0.1:{lab.runtime.transport_config["sip_port"]};transport={transport}'
        await asyncio.to_thread(caller.command, f"/dial {target}")
        async with asyncio.timeout(3):
            while (phone := await public_phone(lab))["state"] != "ringing":
                await asyncio.sleep(.01)
        call_id = phone["calls"][0]["call_id"]
        generation = phone["calls"][0]["generation"]
        assert_no_browser_media_owner(lab)
        _, subscription = await ws_request(
            client, "voip_stack/subscribe_ha_softphone_state", endpoint_id="casa",
        )
        state = await browser_snapshot(client)
        assert state["available"] and state["connected_cards"] == 1
        assert_no_browser_media_owner(lab)
        await lab.hass.services.async_call(
            "voip_stack", "answer", {"device_id": "browser-casa", "call_id": call_id},
            blocking=True,
        )
        await asyncio.to_thread(caller.wait_for, "Call established", 3)
        answered = await public_phone(lab)
        assert answered["state"] == "in_call"
        assert answered["calls"][0]["call_id"] == call_id
        assert answered["calls"][0]["generation"] == generation
        await ws_request(client, "unsubscribe_events", subscription=subscription)
        assert (await browser_snapshot(client))["connected_cards"] == 0
        # Observe beyond the configured deadline to prove answer cancels fallback.
        await asyncio.sleep(3.1)
        assert (await public_phone(lab))["state"] == "in_call"
        assert "Call established" not in unavailable.read()
        assert "Call established" not in noanswer.read()
        await lab.hass.services.async_call(
            "voip_stack", "hangup", {"device_id": "browser-casa", "call_id": call_id},
            blocking=True,
        )
    finally:
        await client.close()
        await end_call(lab, caller)
    assert (await public_phone(lab))["state"] == "idle"
