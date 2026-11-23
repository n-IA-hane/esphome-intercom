"""Real SIP calls preserve ownership through card startup and adoption failures."""

import asyncio
from contextlib import asynccontextmanager
import struct

import pytest

from custom_components.voip_stack import audio_ws_view, browser_application_handoff, call_projection
from custom_components.voip_stack.account_runtime import recompute_account_availability
from custom_components.voip_stack.core import rtp
from custom_components.voip_stack.core.rtp_payload import RtpPayloadEncoder
from custom_components.voip_stack.directory_runtime import compose_directory_entries, routing_snapshot
from custom_components.voip_stack.endpoint_device import async_ensure_endpoint_device
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.ha_bindings import HaCardBindings
from custom_components.voip_stack.local_call_media import LocalCallMedia
from custom_components.voip_stack.owned_sip_source import OwnedSipSource
from custom_components.voip_stack.phone_config import endpoint_from_subentry
from custom_components.voip_stack.phone_endpoint import EndpointKind
from custom_components.voip_stack.store import config_entry, sip_accounts
from .test_account_consolidation_ha import add_phone as add_native_account
from .test_browser_pcm_edge_ha import WebSocketBoundary
from .test_queue_call_ha import queue_lab as queue_lab, wait_until
from .test_trunk_calls_ha import trunks as trunks, lab as lab

pytestmark = pytest.mark.ha


async def add_card(runtime, identity, bindings, user):
    entry = config_entry(runtime.hass)
    subentry = add_native_account(runtime.hass, entry, {
        "endpoint_id": identity, "username": identity, "name": identity,
        "kind": "sip_account", "username_source": "user", "enabled": True,
        "password": "audit-only-account-password", "call_policy": {"ringing_timeout": None},
    })
    endpoint = endpoint_from_subentry(entry, subentry)
    runtime.runtime.endpoints.register(endpoint)
    async_ensure_endpoint_device(runtime.hass, entry, endpoint, runtime.runtime.endpoints)
    registrar = runtime.calls.component("registrar")
    previous = dict(registrar.accounts)
    contacts = {name: tuple(registrar.registered_contacts(name)) for name in previous}
    account = next(account for account in sip_accounts(runtime.hass) if account.username == identity)
    registrar.update_accounts([*previous.values(), account])
    assert all(registrar.accounts[name] == value for name, value in previous.items())
    assert all(tuple(registrar.registered_contacts(name)) == value for name, value in contacts.items())
    await bindings.async_set_popup_binding(user.id, identity + "-binding", endpoint.endpoint_id, True)
    recompute_account_availability(runtime.hass, endpoint.endpoint_id)
    # Add only this account's real composed row. The lab's caller, contacts,
    # queues and trunks retain their original fixture ownership and routing.
    composed = compose_directory_entries(runtime.hass, [], registrar.registered_roster_entries())
    row = next(row for row in composed if row.metadata.get("endpoint_id") == endpoint.endpoint_id)
    previous_rows = [row for row in runtime.roster if row.metadata.get("endpoint_id") != endpoint.endpoint_id]
    runtime.runtime.directory.replace([*previous_rows, row])
    bound_row = next(row for row in routing_snapshot(runtime.runtime)
                     if row.metadata.get("endpoint_id") == endpoint.endpoint_id)
    runtime.roster[:] = [*previous_rows, bound_row]
    runtime.runtime.directory.replace(runtime.roster)
    current = runtime.runtime.endpoints.require(endpoint.endpoint_id)
    assert current.kind is EndpointKind.SIP_ACCOUNT and current.device_id
    assert bindings.has_binding(current.endpoint_id)
    assert current.availability.value == "available"
    assert bound_row.metadata["endpoint_kind"] == "sip_account" and bound_row.metadata["ha_binding"]
    return current


@pytest.fixture
async def card_accounts(queue_lab, hass_admin_user):
    runtime = queue_lab
    owner = runtime.runtime.ha_bindings
    created = owner is None
    if created:
        owner = HaCardBindings(runtime.hass, config_entry(runtime.hass), runtime.runtime,
            lambda identities: recompute_account_availability(runtime.hass, identities))
        runtime.runtime.ha_bindings = owner
        await owner.async_start()
    assert isinstance(owner, HaCardBindings)
    identities = []
    async def create(identity):
        endpoint = await add_card(runtime, identity, owner, hass_admin_user)
        identities.append((endpoint.endpoint_id, identity + "-binding"))
        return endpoint
    try:
        yield create
    finally:
        for identity, binding_id in identities:
            await owner.async_set_popup_binding(hass_admin_user.id, binding_id, identity, False)
        if created:
            await owner.async_close()
            runtime.runtime.ha_bindings = None


@asynccontextmanager
async def incoming_card_call(runtime, card):
    caller, audio = await runtime.caller()
    watcher = None
    call_id = ""
    try:
        assert await caller.invite(target=card.username, remote_host="127.0.0.1",
            remote_sip_port=runtime.runtime.transport_config["sip_port"], timeout=3) == "ringing"
        call_id = caller.dialog_ids.call_id
        await runtime.hass.services.async_call("voip_stack", "answer", {
            "device_id": card.device_id, "call_id": call_id, "media_client_id": card.endpoint_id + "-owner"},
            blocking=True)
        assert await caller.wait_for_final(timeout=3) == "in_call"
        watcher = asyncio.create_task(caller.wait_for_dialog_termination())
        session = runtime.calls.get_session(call_id)
        assert session is not None and session.live
        yield caller, audio, session
    finally:
        if call_id and (current := runtime.calls.get_session(call_id)) is not None and current.live:
            await runtime.calls.request_termination(call_id, TerminationIntent("audit_test_cleanup"))
        if call_id:
            await wait_until(lambda: runtime.calls.get_session(call_id) is None)
        if watcher is not None:
            if not watcher.done():
                watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_unlimited_ring_card_answer_without_websocket_resumes_original_call(queue_lab, card_accounts, monkeypatch):
    runtime = queue_lab
    original = await card_accounts("audit-source-card")
    target = await card_accounts("audit-stalled-card")
    assert target.policy.ringing_timeout is None
    # This changes only the technical owner deadline, never the ring policy.
    monkeypatch.setattr(browser_application_handoff, "BROWSER_MEDIA_START_TIMEOUT", 0.05)
    transfer = None
    async with incoming_card_call(runtime, original) as (caller, _audio, session):
        identity = (caller.dialog_ids.call_id, caller.dialog_ids.local_tag, caller.dialog_ids.remote_tag)
        descriptor = runtime.calls.resource_for(session.call_id, "softphone_media")
        try:
            transfer = asyncio.create_task(runtime.hass.services.async_call("voip_stack", "transfer", {
                "device_id": original.device_id, "call_id": session.call_id,
                "destination": target.username}, blocking=True, return_response=True))
            await wait_until(lambda: runtime.calls.artifact_for(session.call_id, "pending_route") is not None)
            await runtime.hass.services.async_call("voip_stack", "answer", {
                "device_id": target.device_id, "call_id": session.call_id,
                "media_client_id": target.endpoint_id + "-owner"}, blocking=True)
            result = await asyncio.wait_for(transfer, 5)
            assert not result["success"]
            assert session.live and session.owner == "ha_softphone"
            assert original.endpoint_id in session.endpoint_claims
            assert target.endpoint_id not in session.endpoint_claims
            assert runtime.calls.resource_for(session.call_id, "softphone_media") is descriptor
            assert (caller.dialog_ids.call_id, caller.dialog_ids.local_tag, caller.dialog_ids.remote_tag) == identity
            assert caller.dialog is not None
        finally:
            if transfer is not None and not transfer.done():
                transfer.cancel()
                await asyncio.gather(transfer, return_exceptions=True)


@asynccontextmanager
async def outgoing_reserved_card_call(runtime, card):
    from custom_components.voip_stack.const import CONF_SIP_VIDEO
    from .test_video_originate_unknown_ha import set_configured_video_policy

    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry,
        data={**entry.data, CONF_SIP_VIDEO: True})
    for username in (card.username, "bedroom0"):
        set_configured_video_policy(runtime, username, True)
    runtime.runtime.transport_config[CONF_SIP_VIDEO] = True
    await runtime.hass.async_block_till_done()
    call_id = ""
    try:
        # Requesting video allocates a real combined reservation. This peer
        # declines video, leaving its live audio port owned by the SIP client.
        await runtime.hass.services.async_call("voip_stack", "call", {
            "device_id": card.device_id, "destination": "bedroom0",
            "media_client_id": card.endpoint_id + "-owner",
            "send_video": False, "receive_video": True}, blocking=True)
        call_id = runtime.runtime.endpoints.require(card.endpoint_id).active_call_id
        assert call_id
        await wait_until(lambda: runtime.calls.get_session(call_id).state == "in_call")
        session = runtime.calls.get_session(call_id)
        client = runtime.calls.sip_client_for(call_id)
        assert client is not None and client.dialog is not None
        assert client.media_reservation is not None and client.audio_rtp_socket is not None
        assert client.dialog.video_format is None
        yield client, runtime.phones[0].rtp_socket, session
    finally:
        if call_id and (current := runtime.calls.get_session(call_id)) is not None and current.live:
            await runtime.calls.request_termination(call_id, TerminationIntent("audit_test_cleanup"))
        if call_id:
            await wait_until(lambda: runtime.calls.get_session(call_id) is None)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_failed_application_adoption_keeps_original_reservation_and_udp_usable(queue_lab, card_accounts, monkeypatch):
    runtime = queue_lab
    card = await card_accounts("audit-adoption-card")
    worker = None
    async with outgoing_reserved_card_call(runtime, card) as (_caller, audio, session):
        source = OwnedSipSource.capture(runtime.hass, session, card.endpoint_id)
        assert source is not None and source.active is None and source.reservation is not None
        reservation = source.reservation
        descriptor = runtime.calls.resource_for(session.call_id, "softphone_media")
        assert source.client is not None and reservation is source.client.media_reservation
        assert source.local_port in reservation.ports
        original = source.client.audio_rtp_socket
        assert original is not None and original.fileno() >= 0
        original_fd = original.fileno()
        async def installation_failure(_self):
            raise OSError("deterministic endpoint installation failure")
        async def complete(reason):
            await runtime.calls.request_termination(session.call_id, TerminationIntent(reason))
        with monkeypatch.context() as context:
            context.setattr(LocalCallMedia, "start", installation_failure)
            with pytest.raises(OSError, match="endpoint installation"):
                await source.adopt_application(complete)
        assert not source.adopted and not reservation.released
        assert original.fileno() == original_fd
        assert runtime.calls.resource_for(session.call_id, "softphone_media") is descriptor
        assert card.endpoint_id in session.endpoint_claims
        fmt = source.invite.recv_format
        encoder = RtpPayloadEncoder(fmt)
        payload = encoder.encode(struct.pack(f"<{fmt.audio_format.nominal_frame_bytes // 2}h",
                              *([1700] * (fmt.audio_format.nominal_frame_bytes // 2))))
        encoder.close()
        wire = rtp.build_packet(rtp.RtpPacket(fmt.payload_type, 100, 0, 0x552299, payload))
        await runtime.hass.loop.sock_sendto(audio, wire, ("127.0.0.1", source.local_port))
        connection = audio_ws_view._active_softphone_media_session(runtime.hass, card.endpoint_id)
        browser = WebSocketBoundary()
        try:
            worker = asyncio.create_task(audio_ws_view._run_audio_session(runtime.hass, browser,
                connection, endpoint_id=card.endpoint_id))
            received = await asyncio.wait_for(browser.audio.get(), 2)
            assert received and session.live
            assert not reservation.released
        finally:
            if worker is not None:
                worker.cancel()
                await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_parking_commits_canonical_application_party_to_original_card(queue_lab, card_accounts, monkeypatch):
    from custom_components.voip_stack.parking import get_parking_lot, park_browser_sip_call
    from custom_components.voip_stack.store import config_entry

    runtime = queue_lab
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {"enabled": True}})
    card = await card_accounts("audit-parking-card")
    projections = []
    original = call_projection.observe_phone_leg_projection
    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        projections.append(kwargs)
        return result
    monkeypatch.setattr(call_projection, "observe_phone_leg_projection", observe)
    async with incoming_card_call(runtime, card) as (_caller, _audio, session):
        await park_browser_sip_call(runtime.hass, session.call_id, card.endpoint_id, slot="701")
        assert get_parking_lot(runtime.hass).calls["701"].session is session
        assert any(p.get("dialed_target") == "Parking 701" and p.get("reason") == "forwarded"
                   for p in projections)
        assert session.live and session.owner == "parking"
