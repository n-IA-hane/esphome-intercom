"""One isolated HA service witness for a caller included in local paging."""
import asyncio
import socket
import struct
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import callback

from custom_components.voip_stack import endpoint_runtime, invite_router, peer_snapshot, softphone_originate
from custom_components.voip_stack import audio_ws_view
from custom_components.voip_stack.browser_paging import async_originate_browser_paging
from custom_components.voip_stack.core import rtp
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.ha_bindings import HaCardBindings
from custom_components.voip_stack.paging import async_setup_paging
from custom_components.voip_stack.paging_config import normalize_paging_group, paging_group_entries
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.roster import RosterEntry, dump_roster_json
from custom_components.voip_stack.websocket_api import HA_SOFTPHONE_STATE_EVENT, _ha_softphone_state
from tests.test_automation_originate_ha import lab as lab, assert_call_resources_released
from tests.test_browser_paging_ha import Browser, _answer, _ringing, browser_lab as browser_lab

pytestmark = pytest.mark.ha


async def test_local_paging_group_does_not_offer_the_call_back_to_its_browser_caller(lab, monkeypatch, unused_tcp_port_factory):
    await lab.calls.shutdown()
    lab.runtime.sip = lab.calls = SipEndpointRuntime(allow_dark_sessions=True)
    port = unused_tcp_port_factory()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", port))
    lab.runtime.transport_config.update(sip_port=port, sip_registrar_enabled=True)
    entry = lab.hass.config_entries.async_entries("voip_stack")[0]
    accounts = []
    for role in ("caller", "receiver"):
        endpoint = PhoneEndpoint(endpoint_id="sip:paging-"+role, name="Paging "+role,
            username="paging-"+role, device_id="paging-"+role+"-device", kind=EndpointKind.SIP_ACCOUNT,
            availability="available", capabilities=frozenset({"audio", "dtmf"}))
        accounts.append(endpoint)
        lab.runtime.endpoints.register(endpoint)
        lab.runtime.softphone_presence[endpoint.endpoint_id] = 1
        lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type="phone",
            title=endpoint.name, unique_id="phone:"+endpoint.endpoint_id, data={
                "name": endpoint.name, "kind": endpoint.kind.value, "username": endpoint.username,
                "ha_bindings": [{"source": "migration", "reference": "legacy_ha_phone:"+role}]}))
        lab.roster.append(RosterEntry(endpoint.username, name=endpoint.name,
            metadata={"endpoint_id": endpoint.endpoint_id, "endpoint_kind": endpoint.kind.value, "local_ha": True}))
    bindings = lab.runtime.ha_bindings = HaCardBindings(lab.hass, entry, lab.runtime, lambda _changed: None)
    await bindings.async_start()
    group = normalize_paging_group({"name": "Local paging", "extension": "402",
        "members": [endpoint.endpoint_id for endpoint in accounts], "answer_timeout": 2, "timeout": 5}, identity="local-paging")
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type="paging", title=group["name"],
        unique_id="paging:local-paging", data=group))
    lab.roster.extend(paging_group_entries(entry))
    lab.runtime.directory.replace(lab.roster)
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(peer_snapshot, "async_build_peer_snapshot", AsyncMock(return_value=[]))
    monkeypatch.setattr("custom_components.voip_stack.endpoint_group_calls.roster_from_peers", lambda *_args: lab.roster)
    monkeypatch.setattr(invite_router, "_roster_from_peers", lambda *_args: lab.roster)
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    assert await endpoint_runtime.async_start_sip_endpoint(lab.hass)
    manager = await async_setup_paging(lab.hass, entry)
    manager.browser_originate = async_originate_browser_paging
    events = []
    receiver_offered = asyncio.Event()
    caller_connected = asyncio.Event()

    @callback
    def observe(event):
        data = dict(event.data)
        events.append(data)
        if data.get("endpoint_id") == accounts[1].endpoint_id and data.get("state") == "ringing":
            receiver_offered.set()
        if data.get("endpoint_id") == accounts[0].endpoint_id and data.get("state") == "in_call":
            caller_connected.set()

    remove = lab.hass.bus.async_listen(HA_SOFTPHONE_STATE_EVENT, observe)
    workers, browsers = [], []
    seen_ids = set()
    try:
        for cycle in range(2):
            receiver_offered.clear()
            caller_connected.clear()
            events.clear()
            await lab.hass.services.async_call("voip_stack", "call", {
                "device_id": accounts[0].device_id, "destination": group["name"],
                "media_client_id": "paging-caller-browser",
            }, blocking=True)
            await asyncio.wait_for(receiver_offered.wait(), 2)
            state = _ha_softphone_state(lab.hass, accounts[0].endpoint_id)
            offered = _ha_softphone_state(lab.hass, accounts[1].endpoint_id)
            assert state["call_id"] not in seen_ids and offered["call_id"] not in seen_ids
            seen_ids.update((state["call_id"], offered["call_id"]))
            assert state["direction"] == "outgoing" and not state["can_answer"]
            assert offered["direction"] == "incoming" and offered["can_answer"]
            assert offered["audio_direction"] == "recvonly" and offered["answer_mode"] == "auto"
            caller_session = lab.calls.get_session(state["call_id"])
            receiver_session = lab.calls.get_session(offered["call_id"])
            assert caller_session.metadata["source_endpoint_id"] == accounts[0].endpoint_id
            client = lab.calls.sip_client_for(state["call_id"])
            assert client.signaling_transport == "TCP"
            await lab.hass.services.async_call("voip_stack", "answer", {
                "device_id": accounts[1].device_id, "call_id": offered["call_id"],
                "media_client_id": "paging-receiver-browser",
            }, blocking=True)
            application = lab.calls.resource_for(offered["call_id"], "browser_paging")
            browser = Browser()
            browsers.append(browser)
            media = audio_ws_view._active_softphone_media_session(lab.hass, accounts[1].endpoint_id)
            application.playback.attach(browser)
            worker = asyncio.create_task(audio_ws_view._run_audio_session(lab.hass, browser, media,
                endpoint_id=accounts[1].endpoint_id, playback=application.playback))
            workers.append(worker)
            browser.ready()
            await asyncio.wait_for(caller_connected.wait(), 2)
            async with asyncio.timeout(2):
                while client.dialog is None:
                    await asyncio.sleep(0)
            fmt = client.dialog.send_format
            count = fmt.audio_format.nominal_frame_samples
            sample = 6000 + cycle * 1000
            # Actual source RTP traverses the local paging application and
            # existing receive-only WebSocket adapter, without host audio.
            for index in range(8):
                payload = struct.pack(f"!{count}h", *([sample] * count))
                raw = rtp.build_packet(rtp.RtpPacket(fmt.payload_type, index,
                    index * fmt.rtp_timestamp_step, 0x12345000 + cycle, payload))
                await lab.hass.loop.sock_sendto(client.audio_rtp_socket, raw,
                    (client.dialog.remote_rtp_host, client.dialog.remote_rtp_port))
                await asyncio.sleep(fmt.frame_ms / 1000)
            async with asyncio.timeout(2):
                while not any(any(frame) for frame in browser.frames):
                    await asyncio.sleep(0)
            levels = [struct.unpack(f"<{len(frame)//2}h", frame) for frame in browser.frames if any(frame)]
            # The actual48k->16k resampler has an initial filter transient
            # and quantizes this constant signal by one signed16bit unit.
            stable = [values for values in levels if all(abs(value - sample) <= 2 for value in values)]
            assert len(stable) >= 2, (sample, [(min(v), max(v)) for v in levels])
            assert not any(event.get("endpoint_id") == accounts[0].endpoint_id
                and event.get("direction") == "incoming" and event.get("state") == "ringing" for event in events)
            job = caller_session and next(iter(manager.jobs.values()))
            assert job.results[accounts[0].endpoint_id]["status"] == "skipped"
            assert accounts[0].endpoint_id not in job.calls
            await lab.hass.services.async_call("voip_stack", "hangup", {
                "device_id": accounts[0].device_id, "call_id": state["call_id"],
            }, blocking=True)
            await asyncio.wait_for(caller_session.terminated.wait(), 2)
            await asyncio.wait_for(receiver_session.terminated.wait(), 2)
            await asyncio.wait_for(worker, 2)
            assert not manager.jobs
            assert_call_resources_released(lab)
        assert len(seen_ids) == 4
    finally:
        remove()
        for session in tuple(lab.calls.sessions.values()):
            barrier = lab.calls.request_termination(session.call_id, TerminationIntent("test_done"))
            if barrier is not None:
                await barrier
        await manager.close()
        for browser in browsers:
            browser.force_close()
        await asyncio.gather(*workers, return_exceptions=True)
        await bindings.async_close()
        assert_call_resources_released(lab)
        await endpoint_runtime.async_stop_sip_endpoint(lab.hass)


@pytest.mark.parametrize("during_termination", [False, True], ids=["live-error", "terminal-close"])
async def test_paging_audio_readiness_close_uses_its_call_owner(browser_lab, monkeypatch, during_termination):
    """A terminal playback close is normal, but a live output error still fails."""
    lab, phone, job = browser_lab
    originate, owner = await _ringing(lab, phone, job)
    application = await _answer(lab, phone, owner)
    browser = Browser()
    application.playback.attach(browser)
    media_session = audio_ws_view._active_softphone_media_session(lab.hass, phone.endpoint_id)
    token = owner.token
    worker = asyncio.create_task(audio_ws_view._run_audio_session(
        lab.hass, browser, media_session, endpoint_id=phone.endpoint_id, playback=application.playback))
    barrier = None
    release_stop = asyncio.Event()
    try:
        browser.ready()
        assert await asyncio.wait_for(originate, 2) == {"call_id": owner.call_id, "generation": token.generation}

        async def frame():
            yield b"\x12\x34" * 320

        await application.media.play_pcm_stream(frame())
        async with asyncio.timeout(2):
            while not browser.frames:
                await asyncio.sleep(0)
        assert browser.frames[0] == b"\x12\x34" * 320
        # Reset readiness through the real connection lifecycle and let the
        # output reach its next real readiness wait, without replacing results.
        application.playback.detach(browser)
        waiting = asyncio.Event()
        real_wait = application.playback.wait_ready

        async def observed_wait(*args, **kwargs):
            waiting.set()
            return await real_wait(*args, **kwargs)

        monkeypatch.setattr(application.playback, "wait_ready", observed_wait)
        await application.media.play_pcm_stream(frame())
        await asyncio.wait_for(waiting.wait(), 2)
        assert owner.live and owner.token == token

        if during_termination:
            stopping = asyncio.Event()
            worker_finalizing = asyncio.Event()
            real_stop = application.media.stop
            stop_count = 0

            async def paused_stop(reason="ended"):
                nonlocal stop_count
                stop_count += 1
                (stopping if stop_count == 1 else worker_finalizing).set()
                await release_stop.wait()
                await real_stop(reason)

            monkeypatch.setattr(application.media, "stop", paused_stop)
            barrier = lab.calls.request_termination(owner.call_id, TerminationIntent("test_hangup"))
            await asyncio.wait_for(stopping.wait(), 2)
            await asyncio.wait_for(worker_finalizing.wait(), 2)
            assert owner.termination_started.is_set() and not owner.live
            assert owner.token == token and not application.media.closed.is_set()
            release_stop.set()
            await asyncio.wait_for(worker, 2)
            await asyncio.wait_for(barrier, 2)
        else:
            await application.playback.close("live_output_failure")
            with pytest.raises(ConnectionError, match="The announcement call has ended"):
                await asyncio.wait_for(worker, 2)
            assert owner.live and not owner.termination_started.is_set()
        assert browser.closed
    finally:
        release_stop.set()
        if owner.live:
            barrier = lab.calls.request_termination(owner.call_id, TerminationIntent("test_done"))
        if barrier is not None:
            await barrier
        browser.force_close()
        await asyncio.gather(originate, worker, return_exceptions=True)
        assert_call_resources_released(lab)


async def test_paging_audio_unexpected_write_error_survives_terminal_decision(browser_lab):
    """A call's terminal flag cannot hide an unrelated WebSocket write error."""
    lab, phone, job = browser_lab
    originate, owner = await _ringing(lab, phone, job)
    application = await _answer(lab, phone, owner)

    class BrokenBrowser(Browser):
        async def send_bytes(self, _value):
            raise RuntimeError("WebSocket write failed")

    browser = BrokenBrowser()
    application.playback.attach(browser)
    media_session = audio_ws_view._active_softphone_media_session(lab.hass, phone.endpoint_id)
    worker = asyncio.create_task(audio_ws_view._run_audio_session(
        lab.hass, browser, media_session, endpoint_id=phone.endpoint_id, playback=application.playback))
    try:
        browser.ready()
        await asyncio.wait_for(originate, 2)
        # This is the real synchronous terminal decision; resource cleanup has
        # not started yet, so the actual output task still observes the write.
        assert owner.claim_termination(TerminationIntent("test_hangup"))
        assert owner.termination_started.is_set() and not application.media.closed.is_set()

        async def frame():
            yield b"\x12\x34" * 320

        await application.media.play_pcm_stream(frame())
        with pytest.raises(RuntimeError, match="WebSocket write failed"):
            await asyncio.wait_for(worker, 2)
        assert application.media.counters["tx_error"] == 1
        assert browser.closed
    finally:
        await lab.calls.request_termination(owner.call_id, TerminationIntent("test_hangup"))
        browser.force_close()
        await asyncio.gather(originate, worker, return_exceptions=True)
        assert_call_resources_released(lab)
