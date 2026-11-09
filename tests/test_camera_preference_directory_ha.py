"""Camera-send intent must not rebuild the unchanged public phone directory."""

import asyncio
import socket
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp import WSMsgType
from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import Unauthorized
import pytest

from custom_components import voip_stack
from custom_components.voip_stack import config_entry_runtime as updates, phone_config
from custom_components.voip_stack import softphone_video_intent, websocket_api
from tests.test_phone_settings_ha import phones as phones

pytestmark = pytest.mark.ha


@pytest.fixture
async def camera(hass, phones, monkeypatch):
    from custom_components.voip_stack.contact_config import contact_dicts

    runtime = phones.entry.runtime_data
    runtime.transport_config.update(
        sip_port=5099, rtp_port=40000, advertise_host="127.0.0.1"
    )
    store = websocket_api._ha_softphone_store(hass, "kitchen")
    store.update(
        config_entry_id=phones.entry.entry_id, state="in_call", call_id="camera-call"
    )
    phones.registry.claim_call("kitchen", "camera-call")
    # The minimal shared fixture omits persisted preference defaults. Populate
    # them before taking the baseline; adding defaults is a mixed change.
    await websocket_api.async_set_ha_softphone_settings(
        hass, endpoint_id="kitchen", send_video=False
    )
    runtime.entry_runtime_signature = updates.entry_runtime_signature(phones.entry)
    runtime.entry_phone_signature = updates.entry_phone_signature(phones.entry)
    runtime.entry_contacts_signature = tuple(contact_dicts(phones.entry))
    completed = asyncio.Event()

    async def updated(_hass, entry):
        await updates.async_config_entry_updated(_hass, entry)
        completed.set()

    remove = phones.entry.add_update_listener(updated)
    refresh = AsyncMock(wraps=updates.async_refresh_and_push_phonebook)
    publish = Mock(wraps=updates._publish_ha_softphone_state)
    intent = AsyncMock(return_value=True)
    monkeypatch.setattr(updates, "async_refresh_and_push_phonebook", refresh)
    monkeypatch.setattr(updates, "_publish_ha_softphone_state", publish)
    monkeypatch.setattr(softphone_video_intent, "async_apply_send_video_intent", intent)

    async def send(enabled, context=None):
        completed.clear()
        await voip_stack._handle_set_send_video_service(
            ServiceCall(
                hass,
                "voip_stack",
                "set_send_video",
                {"device_id": "device-kitchen", "send_video": enabled},
                context=context or Context(),
            )
        )
        await asyncio.wait_for(completed.wait(), 1)

    yield SimpleNamespace(
        **vars(phones),
        runtime=runtime,
        store=store,
        completed=completed,
        refresh=refresh,
        publish=publish,
        intent=intent,
        send=send,
    )
    remove()


async def test_camera_service_persists_publishes_and_keeps_live_intent_without_directory_scan(
    hass, camera
):
    for enabled in (True, False):
        await camera.send(enabled)
        saved = phone_config.phone_subentry_by_endpoint_id(camera.entry, "kitchen")
        assert saved.data["send_video"] is enabled
        assert camera.registry.require("kitchen").send_video is enabled
        assert camera.registry.require("kitchen").active_call_id == "camera-call"
        assert camera.store["send_video"] is enabled
        assert camera.store["call_id"] == "camera-call"
        assert camera.store["state"] == "in_call"
        assert camera.runtime.entry_phone_records["kitchen"]["send_video"] is enabled
        assert camera.runtime.entry_phone_signature == updates.entry_phone_signature(
            camera.entry
        )
        camera.publish.assert_any_call(hass, endpoint_id="kitchen")
        camera.intent.assert_awaited_with(hass, "kitchen", enabled)
    camera.refresh.assert_not_awaited()


@pytest.mark.parametrize(
    "change",
    [
        {"name": "New kitchen"},
        {"extension": "567"},
        {"ring_group": "family"},
        {"conference_group": "family"},
        {"conference_ring": True},
        {"enabled": False},
        {"dnd": True},
        {"auto_answer": True},
        {"call_policy": {"always": {"target": "bedroom"}}},
        {"video_enabled": False},
        {"kind": "sip_account", "username": "desk", "password": "fixture-password"},
    ],
)
@pytest.mark.parametrize("with_camera", [False, True])
async def test_routing_and_mixed_phone_changes_still_refresh(
    hass, camera, change, with_camera
):
    values = {**change, **({"send_video": True} if with_camera else {})}
    phone_config.update_phone_subentry(hass, camera.entry, "kitchen", values)
    await asyncio.wait_for(camera.completed.wait(), 1)
    camera.refresh.assert_awaited_once_with(hass)


async def test_phone_removal_with_camera_change_still_refreshes_and_retires_state(
    hass, camera, monkeypatch
):
    from custom_components.voip_stack import mailbox

    remove_mailbox = AsyncMock()
    monkeypatch.setattr(mailbox, "async_remove_mailbox", remove_mailbox)
    # Coalesce changes before the callback, as HA can do for concurrent edits.
    bedroom = phone_config.phone_subentry_by_endpoint_id(camera.entry, "bedroom")
    camera.runtime.softphone_presence["bedroom"] = 1
    hass.config_entries.async_remove_subentry(camera.entry, bedroom.subentry_id)
    phone_config.update_phone_subentry(
        hass, camera.entry, "kitchen", {"send_video": True}
    )
    await asyncio.wait_for(camera.completed.wait(), 1)
    await hass.async_block_till_done()
    assert camera.refresh.await_count >= 1
    assert camera.registry.get("bedroom") is None
    assert "bedroom" not in camera.runtime.softphone_presence
    remove_mailbox.assert_awaited_once_with(hass, "bedroom")


@pytest.mark.parametrize("mutation", ["title", "add_phone", "add_contact"])
async def test_directory_structure_changes_with_camera_still_refresh(
    hass, camera, mutation
):
    from homeassistant.config_entries import ConfigSubentry

    if mutation == "title":
        child = phone_config.phone_subentry_by_endpoint_id(camera.entry, "kitchen")
        hass.config_entries.async_update_subentry(
            camera.entry, child, title="New title"
        )
    elif mutation == "add_phone":
        phone_config._add_phone_subentry(
            hass,
            camera.entry,
            data={
                "endpoint_id": "new-phone",
                "kind": "browser",
                "name": "New phone",
            },
            title="New phone",
        )
    else:
        hass.config_entries.async_add_subentry(
            camera.entry,
            ConfigSubentry(
                subentry_type="contact",
                unique_id="contact:new-peer",
                title="New peer",
                data={"id": "new-peer", "number": "42", "name": "New peer"},
            ),
        )
    await camera.send(True)
    await hass.async_block_till_done()
    assert camera.refresh.await_count >= 1


@pytest.mark.parametrize(
    "change",
    [
        {"username": "renamed-desk"},
        {"password": "updated-fixture-password"},
        {"send_video": True},
    ],
)
async def test_sip_account_changes_never_take_browser_exemption(hass, camera, change):
    phone_config._add_phone_subentry(
        hass,
        camera.entry,
        data={
            "endpoint_id": "sip:desk",
            "kind": "sip_account",
            "name": "Desk",
            "username": "desk",
            "password": "fixture-password",
        },
        title="Desk",
    )
    await asyncio.wait_for(camera.completed.wait(), 1)
    camera.completed.clear()
    camera.refresh.reset_mock()
    phone_config.update_phone_subentry(hass, camera.entry, "sip:desk", change)
    await asyncio.wait_for(camera.completed.wait(), 1)
    camera.refresh.assert_awaited_once_with(hass)


async def test_camera_change_keeps_parent_reconciliations(hass, camera, monkeypatch):
    from custom_components.voip_stack import mailbox, phone_services, trunk_runtime

    trunks = AsyncMock()
    services = AsyncMock()
    mailboxes = AsyncMock()
    monkeypatch.setattr(trunk_runtime, "async_reconcile_trunks", trunks)
    monkeypatch.setattr(phone_services, "async_reconcile_phone_services", services)
    monkeypatch.setattr(mailbox, "async_refresh_mailbox_state", mailboxes)
    hass.config_entries.async_update_entry(
        camera.entry,
        data={
            "parking": {"slots": ["701", "702"], "timeout": 120},
            "default_trunk_id": "new-default",
            "phone_services": {},
            "voicemail": {"enabled": False},
        },
    )
    await camera.send(True)
    await hass.async_block_till_done()
    trunks.assert_awaited_with(hass, camera.entry)
    services.assert_awaited_with(hass, camera.entry)
    mailboxes.assert_awaited_with(hass)
    from custom_components.voip_stack.parking_config import parking_settings

    assert parking_settings(hass)["slots"] == ["701", "702"]
    assert camera.runtime.entry_phone_records["kitchen"]["send_video"] is True
    camera.intent.assert_awaited_once_with(hass, "kitchen", True)


async def test_camera_service_keeps_authorization(hass, camera, hass_read_only_user):
    with pytest.raises(Unauthorized):
        await camera.send(True, Context(user_id=hass_read_only_user.id))
    camera.intent.assert_not_awaited()
    camera.refresh.assert_not_awaited()
    assert not camera.registry.require("kitchen").send_video


async def test_camera_preference_keeps_real_pcm_owner_and_packets(
    hass, camera, socket_enabled
):
    from custom_components.voip_stack import audio_ws_view as view, audio_ws
    from custom_components.voip_stack.core import rtp, sdp

    class Browser:
        def __init__(self):
            self.messages = asyncio.Queue()
            self.ready = asyncio.Event()

        async def send_json(self, _value):
            self.ready.set()

        async def send_bytes(self, _value):
            pass

        def force_close(self):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            value = await self.messages.get()
            if value is None:
                raise StopAsyncIteration
            return value

    remote = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    remote.bind(("127.0.0.1", 0))
    remote.setblocking(False)
    local = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    local.bind(("127.0.0.1", 0))
    local.setblocking(False)
    session = view._SoftphoneMediaSession(
        call_id="camera-call",
        local_rtp_port=local.getsockname()[1],
        remote_rtp_host="127.0.0.1",
        remote_rtp_port=remote.getsockname()[1],
        send_format=sdp.RtpPcmFormat(100, "L16", 48000, 1, 10),
        recv_format=sdp.RtpPcmFormat(97, "L16", 16000, 1, 10),
        rtp_socket=local,
        signaling_host="127.0.0.1",
    )
    browser = Browser()
    wire = []

    async def source():
        for index in range(60):
            browser.messages.put_nowait(
                SimpleNamespace(
                    type=WSMsgType.BINARY,
                    data=audio_ws.encode_audio_frame(
                        struct.pack("<480h", *([1000 + index] * 480))
                    ),
                )
            )
            await asyncio.sleep(0.01)

    async def receive():
        while True:
            packet = await hass.loop.sock_recv(remote, 2048)
            wire.append(rtp.parse_packet(packet))

    worker = asyncio.create_task(
        view._run_audio_session(hass, browser, session, endpoint_id="kitchen")
    )
    reader = asyncio.create_task(receive())
    producer = asyncio.create_task(source())
    try:
        await asyncio.wait_for(browser.ready.wait(), 1)
        await asyncio.sleep(0.15)
        await camera.send(True)
        assert (
            camera.runtime.media.sessions_for("audio")[("camera-call", "kitchen")]
            is session
        )
        await producer
        await asyncio.sleep(0.12)
    finally:
        producer.cancel()
        browser.messages.put_nowait(None)
        await asyncio.wait_for(worker, 2)
        reader.cancel()
        await asyncio.gather(producer, reader, return_exceptions=True)
        remote.close()
    received = {struct.unpack("!480h", packet.payload)[-1] for packet in wire}
    assert set(range(1000, 1060)) <= received
    assert all((b.sequence - a.sequence) & 65535 == 1 for a, b in zip(wire, wire[1:]))
    assert all(
        (b.timestamp - a.timestamp) & 0xFFFFFFFF == 480 for a, b in zip(wire, wire[1:])
    )
    assert session.media_generation == 0
    camera.refresh.assert_not_awaited()
