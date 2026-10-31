"""Slots share the call session's cleanup and arbitrate pickup atomically."""

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.voip_stack.parking_lot import ParkingLot, ParkingError
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.endpoint_session import TerminationIntent
from .test_sip_phone_transfers_ha import registered_phones as registered_phones
from .test_trunk_calls_ha import trunks as trunks, lab as lab
from .test_voicemail_ha import custom_integrations as custom_integrations

pytestmark = pytest.mark.ha


@pytest.mark.fault
@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize(
    "boundary,via_service",
    [("leg", True), ("leg", False), ("relay", True)],
    ids=["leg-service", "leg-refer", "relay-service"],
)
async def test_caller_bye_owns_resources_after_parking_close(
    registered_phones, trunks, monkeypatch, boundary, via_service
):
    from custom_components.voip_stack.endpoint_session import CallLeg, EndpointCallSession
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.sip_rtp_bridge import SipRtpRelay
    from custom_components.voip_stack.store import config_entry

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(
        entry, data={**entry.data, "parking": {"enabled": True}}
    )
    caller, operator, _picker = registered_phones

    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)

    await asyncio.to_thread(caller.dial, "transfer_b")
    await wait_for(
        lambda: any(s.state == "in_call" for s in runtime.calls.sessions.values())
    )
    session = next(iter(runtime.calls.sessions.values()))
    leg = session.legs[runtime.calls.bridge_link_for(session.call_id)]
    relay = runtime.calls.resource_for(session.call_id, "relay")
    retained_transport = relay.left_transport
    closed, resume, terminating, cleanup = (asyncio.Event() for _ in range(4))
    native_close = CallLeg.close
    native_stop = SipRtpRelay.stop
    native_cleanup = EndpointCallSession._run_termination
    paused_task = None
    stop_claimed = False
    service_task = None

    async def pause_after_close():
        nonlocal paused_task
        paused_task = asyncio.current_task()
        closed.set()
        await resume.wait()

    async def close_leg(current, reason):
        await native_close(current, reason)
        if (
            boundary == "leg"
            and current is leg
            and asyncio.current_task().get_name().startswith("parking-retire-")
        ):
            await pause_after_close()

    async def stop_relay(current):
        nonlocal stop_claimed
        pause = boundary == "relay" and current is relay and not stop_claimed
        stop_claimed |= pause
        await native_stop(current)
        if pause:
            await pause_after_close()

    async def terminate(current, intent):
        if current is session:
            terminating.set()
            await cleanup.wait()
        return await native_cleanup(current, intent)

    monkeypatch.setattr(CallLeg, "close", close_leg)
    monkeypatch.setattr(SipRtpRelay, "stop", stop_relay)
    monkeypatch.setattr(EndpointCallSession, "_run_termination", terminate)
    try:
        if via_service:
            runtime.runtime.endpoints.update("transfer_b", device_id="operator-device")
            service_task = asyncio.create_task(
                runtime.hass.services.async_call(
                    "voip_stack", "park", {"device_id": "operator-device"},
                    blocking=True, return_response=True,
                )
            )
        else:
            await asyncio.to_thread(
                operator.command,
                f'/transfer sip:700@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}',
            )
        await asyncio.wait_for(closed.wait(), 5)
        lot = get_parking_lot(runtime.hass)
        assert lot.calls["701"].session is session
        assert runtime.calls.resource_for(session.call_id, "relay").transport is retained_transport
        assert caller.read().count("Call established") == 1

        # BYE claims termination before its cleanup task can drain resources.
        # Resume the already-completed close in that exact scheduling window.
        await asyncio.to_thread(caller.hangup)
        await asyncio.wait_for(terminating.wait(), 5)
        assert not session.live
        with pytest.raises(RuntimeError, match="cleanup has started"):
            session.ensure_transferable()
        resume.set()
        result = (await asyncio.gather(paused_task, return_exceptions=True))[0]
        assert not isinstance(result, BaseException), repr(result)
        retired = leg if boundary == "leg" else relay
        assert any(resource.value is retired for resource in session.resources)
        if service_task is not None:
            response = await service_task
            assert response == {
                "call_id": session.call_id, "slot": "701", "generation": session.generation,
            }
    finally:
        resume.set()
        cleanup.set()
        if session._termination_task is not None:
            result = await asyncio.wait_for(asyncio.shield(session._termination_task), 5)
            assert not result.errors
        if paused_task is not None:
            await asyncio.gather(paused_task, return_exceptions=True)
        if service_task is not None:
            await asyncio.gather(service_task, return_exceptions=True)
        assert not runtime.calls.sessions
        assert not runtime.runtime.rtp_port_pool["used"]
        assert not get_parking_lot(runtime.hass).calls
        assert not session.endpoint_claims


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("hangup_side", [0, 1])
async def test_real_sip_parking_pickup_keeps_audio_and_releases_resources(registered_phones, trunks, tmp_path, hangup_side):
    import math
    import struct
    import wave
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {"enabled": True}})
    caller, picker, _other = registered_phones
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)
    await asyncio.to_thread(caller.dial, "700")
    lot = get_parking_lot(runtime.hass)
    assert list(lot.calls) == ["701"]
    parked = lot.calls["701"]
    source_id = parked.session.call_id
    media = runtime.calls.resource_for(source_id, "relay")
    source_transport = media.transport
    await asyncio.to_thread(picker.dial, "701")
    await wait_for(lambda: not lot.calls)
    relay = runtime.calls.resource_for(source_id, "relay")
    assert relay.left_transport is source_transport
    assert len(runtime.calls.sessions) == 1
    assert runtime.runtime.rtp_port_pool['used'] == {relay.left_port, relay.right_port}
    await wait_for(lambda: relay.left_tx_packets >= 50 and relay.right_tx_packets >= 50)
    if hangup_side == 1:
        left_contract = relay.left
        await asyncio.to_thread(picker.command, "/hold")
        await wait_for(lambda: not relay.right.can_receive)
        assert relay.left is left_contract
        await asyncio.to_thread(picker.command, "/resume")
        await wait_for(lambda: relay.right.can_receive)
        left_count, right_count = relay.left_tx_packets, relay.right_tx_packets
        await wait_for(lambda: relay.left_tx_packets >= left_count + 50 and relay.right_tx_packets >= right_count + 50)
    await asyncio.to_thread(registered_phones[hangup_side].hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool['used']
    for peer in registered_phones:
        await asyncio.to_thread(peer.close)
    for name, expected in (("transfer_a", 550), ("transfer_b", 450)):
        found = False
        for path in (tmp_path/name).glob('*-dec.wav'):
            with wave.open(str(path),'rb') as recording:
                rate=recording.getframerate()
                samples=struct.unpack(f'<{recording.getnframes()}h',recording.readframes(recording.getnframes()))[-1600:]
            if len(samples)<800:
                continue
            energies={frequency: abs(sum(value*complex(math.cos(2*math.pi*frequency*index/rate),math.sin(2*math.pi*frequency*index/rate)) for index,value in enumerate(samples))) for frequency in (450,550)}
            found |= energies[expected] > energies[1000-expected]*5 and energies[expected]>10000
        assert found, f'{name} did not receive the opposite phone audio after pickup'


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{}, {"manual": "transfer_c"}], indirect=True)
async def test_parking_timeout_uses_existing_return_route(registered_phones, trunks, request):
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {
        "enabled": True, "timeout": 1, "return_timeout": 1,
        "return_destination": "transfer_c",
    }})
    caller, _picker, destination = registered_phones
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)
    await asyncio.to_thread(caller.dial, "700")
    lot = get_parking_lot(runtime.hass)
    await wait_for(lambda: "Incoming call" in destination.read() or "Call established" in destination.read())
    await wait_for(lambda: not lot.calls)
    if "manual" in request.node.callspec.params["registered_phones"]:
        await wait_for(lambda: not runtime.calls.sessions)
        assert "Call established" not in destination.read()
    else:
        await asyncio.to_thread(destination.wait_for, "Call established", 3)
        await wait_for(lambda: any(s.owner == "bridge" and s.state == "in_call" for s in runtime.calls.sessions.values()))
        session = next(iter(runtime.calls.sessions.values()))
        relay = runtime.calls.resource_for(session.call_id, "relay")
        await wait_for(lambda: relay.left_tx_packets >= 20 and relay.right_tx_packets >= 20)
        assert runtime.calls.resource_for(session.call_id, "parking") is None
        # Returning a call must retire the old application so another park
        # acquires a fresh slot and controller on the same live dialog.
        await asyncio.to_thread(destination.command,
            f'/transfer sip:700@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}')
        await wait_for(lambda: "701" in lot.calls)
        assert lot.calls["701"].session is session
        assert runtime.calls.resource_for(session.call_id, "parking") is not None
        await asyncio.to_thread(caller.hangup)
        await wait_for(lambda: not runtime.calls.sessions)
        assert not lot.calls
    assert not runtime.runtime.rtp_port_pool['used']


async def test_slot_contention_rollback_cleanup_and_reuse():
    owner = SipEndpointRuntime()
    owner.activate()
    lot = ParkingLot(("701",))
    first = owner.upsert("first", state="in_call", owner="parking")
    second = owner.upsert("second", state="in_call", owner="parking")
    slot = lot.reserve(first)
    assert lot.reserve(first) is slot
    with pytest.raises(ParkingError, match="full"):
        lot.reserve(second)
    claim = lot.claim("701", "pickup-a")
    with pytest.raises(ParkingError, match="already"):
        lot.claim("701", "pickup-b")
    claim.rollback()
    successor = lot.claim("701", "pickup-b")
    claim.rollback()
    assert successor.current
    successor.commit()
    replacement = lot.reserve(second)
    await first.terminate(TerminationIntent("remote_hangup"))
    assert lot.calls["701"] is replacement
    with pytest.raises(ParkingError):
        successor.commit()
    await lot.close()
    assert not lot.calls
    await owner.shutdown()


async def test_timeout_and_pickup_cannot_both_control_the_call():
    owner = SipEndpointRuntime()
    owner.activate()
    session = owner.upsert("call", state="in_call", owner="parking")
    lot = ParkingLot(("701",))
    parked = lot.reserve(session)
    expired = asyncio.Event()
    release = asyncio.Event()
    async def on_return(claim):
        expired.set()
        await release.wait()
        claim.commit()
    lot.arm_return(parked, 0.01, on_return)
    await asyncio.wait_for(expired.wait(), 1)
    with pytest.raises(ParkingError, match="already"):
        lot.claim("701", "late-pickup")
    release.set()
    await parked.timer
    assert not lot.calls
    assert session.live
    await owner.shutdown()


async def test_remote_hangup_revokes_pending_pickup_and_owned_timer():
    owner = SipEndpointRuntime()
    owner.activate()
    session = owner.upsert("call", state="in_call", owner="parking")
    lot = ParkingLot(("701",))
    parked = lot.reserve(session)
    async def unexpected(_claim):
        pytest.fail("A terminated call returned from parking")
    lot.arm_return(parked, 60, unexpected)
    claim = lot.claim("701", "pickup")
    await session.terminate(TerminationIntent("remote_hangup"))
    assert not lot.calls and parked.timer.done()
    with pytest.raises(ParkingError):
        claim.commit()
    claim.rollback()
    await owner.shutdown()


async def test_failed_pickup_after_deadline_still_returns_the_parked_call():
    owner = SipEndpointRuntime()
    owner.activate()
    session = owner.upsert("call", state="in_call", owner="parking")
    lot = ParkingLot(("701",))
    parked = lot.reserve(session)
    returned = asyncio.Event()
    async def on_return(claim):
        claim.commit()
        returned.set()
    lot.arm_return(parked, 0.01, on_return)
    claim = lot.claim("701", "pickup")
    await asyncio.sleep(0.02)
    assert not returned.is_set() and claim.current
    claim.rollback()
    await asyncio.wait_for(returned.wait(), 1)
    await parked.timer
    assert not lot.calls
    await owner.shutdown()


@pytest.mark.parametrize("hangup_side", ["caller", "pickup"])
async def test_joined_inbound_dialog_uses_parent_cleanup(monkeypatch, hangup_side):
    from custom_components.voip_stack import endpoint_termination
    from custom_components.voip_stack.endpoint_session import SipTerminationDisposition

    owner = SipEndpointRuntime()
    owner.activate()
    dialogs = {"caller", "pickup"}
    signaled = []
    async def bye(identity):
        if identity not in dialogs:
            return False
        dialogs.remove(identity)
        signaled.append(identity)
        return True
    server = SimpleNamespace(dialog_key=lambda identity: identity if identity in dialogs else None,
                             async_send_bye=bye)
    async def signal(current, intent):
        if intent.sip_disposition != SipTerminationDisposition.NONE:
            await bye(current)
    owner.bind_termination_signaler(signal)
    session = owner.upsert("caller", state="in_call", owner="bridge")
    leg = owner.attach_inbound_dialog("caller", "pickup", server, endpoint_id="desk",
                                     expected_generation=session.generation)
    assert owner.resolve_session_id("pickup") == "caller"
    assert not session.named_tasks and leg.dialog is None
    assert len(owner.sessions) == 1
    monkeypatch.setattr(endpoint_termination, "call_registry", lambda _h: owner)
    monkeypatch.setattr(endpoint_termination, "call_runtime_artifacts", lambda _h: owner)
    dialogs.remove(hangup_side)  # The native listener has accepted the remote BYE.
    await endpoint_termination.EndpointTerminationHandler(None).handle(hangup_side)
    assert not dialogs and not owner.sessions and not owner.leg_index
    assert signaled == ["pickup" if hangup_side == "caller" else "caller"]
    await owner.shutdown()


async def test_retired_inbound_leg_cannot_terminate_surviving_call(monkeypatch):
    from custom_components.voip_stack import endpoint_termination
    from custom_components.voip_stack.endpoint_session import LegPhase

    owner = SipEndpointRuntime()
    owner.activate()
    session = owner.upsert("caller", state="in_call", owner="bridge")
    async def bye(_identity):
        return True
    server = SimpleNamespace(dialog_key=lambda identity: identity, async_send_bye=bye)
    leg = owner.attach_inbound_dialog("caller", "retired", server, endpoint_id="desk",
                                     expected_generation=session.generation)
    leg.phase = LegPhase.CLOSING
    monkeypatch.setattr(endpoint_termination, "call_registry", lambda _h: owner)
    monkeypatch.setattr(endpoint_termination, "call_runtime_artifacts", lambda _h: owner)
    await endpoint_termination.EndpointTerminationHandler(None).handle("retired")
    assert session.live and owner.sessions["caller"] is session
    await owner.shutdown()


async def test_inbound_dialog_rejects_unknown_stale_and_already_owned_identity():
    owner = SipEndpointRuntime()
    owner.activate()
    session = owner.upsert("caller", state="in_call", owner="bridge")
    async def bye(_identity):
        return True
    server = SimpleNamespace(dialog_key=lambda identity: identity if identity == "pickup" else None,
                             async_send_bye=bye)
    with pytest.raises(RuntimeError):
        owner.attach_inbound_dialog("caller", "pickup", server, endpoint_id="desk",
                                    expected_generation=session.generation + 1)
    with pytest.raises(ValueError):
        owner.attach_inbound_dialog("caller", "unknown", server, endpoint_id="desk",
                                    expected_generation=session.generation)
    assert not session.legs and not owner.leg_index
    owner.attach_inbound_dialog("caller", "pickup", server, endpoint_id="desk",
                                expected_generation=session.generation)
    other = owner.upsert("other", state="in_call", owner="bridge")
    with pytest.raises(ValueError):
        owner.attach_inbound_dialog("other", "pickup", server, endpoint_id="other-desk",
                                    expected_generation=other.generation)
    assert not other.legs and owner.resolve_session_id("pickup") == "caller"
    await owner.shutdown()


@pytest.mark.parametrize('conflict',[False,True])
async def test_parking_configuration_validates_public_routes(lab, custom_integrations, conflict):
    from homeassistant.config_entries import ConfigSubentry
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.phonebook_services import _validate_contact_namespace
    from custom_components.voip_stack.roster import RosterEntry

    entry=config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry,data={})
    if conflict:
        lab.hass.config_entries.async_add_subentry(entry,ConfigSubentry(subentry_type='contact',title='701',unique_id='existing-slot',data={'id':'existing-slot','name':'701','number':'12345'}))
    lab.hass.config.components.update({'assist_pipeline','ffmpeg','http','lovelace','media_source','network'})
    form=await lab.hass.config_entries.flow.async_init('voip_stack',context={'source':'reconfigure','entry_id':entry.entry_id})
    form=await lab.hass.config_entries.flow.async_configure(form['flow_id'],{'next_step_id':'parking'})
    assert form['step_id']=='parking'
    data=form['data_schema']({})
    result=await lab.hass.config_entries.flow.async_configure(form['flow_id'],data)
    if conflict:
        assert result['errors']=={'base':'parking_route_conflict'}
        assert not entry.data.get('parking')
    else:
        assert result['type']=='abort'
        assert 'enabled' not in entry.data['parking'] and entry.data['parking']['slots'][0]=='701'
        with pytest.raises(ValueError,match='conflicts'):
            _validate_contact_namespace(lab.hass,[RosterEntry('future-conflict',name='701')])


@pytest.mark.parametrize('trunks',[True],indirect=True)
@pytest.mark.parametrize('retained_outgoing',[False,True])
async def test_browser_pickup_uses_existing_call_and_actual_rtp(registered_phones,trunks,monkeypatch,tmp_path,retained_outgoing):
    import math
    import struct
    import wave
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from aiohttp import WSMsgType
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint,EndpointKind
    from custom_components.voip_stack import softphone_originate,audio_ws_view,audio_ws
    from custom_components.voip_stack.roster import RosterEntry,dump_roster_json

    runtime=trunks
    entry=config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry,data={**entry.data,'parking':{'enabled':True}})
    endpoint=PhoneEndpoint(endpoint_id='browser-pickup',name='Pickup Browser',device_id='pickup-device',kind=EndpointKind.BROWSER,
                           availability='available',capabilities=frozenset({'audio','dtmf'}))
    runtime.runtime.endpoints.register(endpoint)
    runtime.roster.append(RosterEntry('Pickup Browser',metadata={'local_ha':True,'endpoint_id':endpoint.endpoint_id}))
    runtime.hass.states.async_set('sensor.voip_phonebook','ready',{'roster_json':dump_roster_json(runtime.roster)})
    monkeypatch.setattr(softphone_originate,'_ha_advertise_host',AsyncMock(return_value='127.0.0.1'))
    monkeypatch.setattr(softphone_originate,'_resolve_target_device',AsyncMock(return_value=None))
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)
    if retained_outgoing:
        await asyncio.to_thread(registered_phones[0].dial,'transfer_b')
        await wait_for(lambda:any(s.state=='in_call' for s in runtime.calls.sessions.values()))
        await asyncio.to_thread(registered_phones[0].command,f'/transfer sip:700@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}')
        await wait_for(lambda:'701' in get_parking_lot(runtime.hass).calls)
        await wait_for(lambda:not runtime.runtime.endpoints.get('transfer_a').active_call_id)
    else:
        await asyncio.to_thread(registered_phones[0].dial,'700')
    lot=get_parking_lot(runtime.hass)
    source_id=lot.calls['701'].session.call_id
    clients_before=set(runtime.calls.sip_clients_snapshot())
    await runtime.hass.services.async_call('voip_stack','call',{
        'device_id':endpoint.device_id,'destination':'701','media_client_id':'parking-browser-media-owner',
    },blocking=True)
    assert not lot.calls and len(runtime.calls.sessions)==1
    assert runtime.runtime.endpoints.get(endpoint.endpoint_id).active_call_id==source_id
    assert set(runtime.calls.sip_clients_snapshot())==clients_before
    session=audio_ws_view._active_softphone_media_session(runtime.hass,endpoint.endpoint_id)
    assert session.call_id==source_id
    class Browser:
        def __init__(self):
            self.audio=asyncio.Queue()
            self.messages=asyncio.Queue()
            self.ready=asyncio.Event()
            self.closed=False
        def force_close(self):self.closed=True
        def __aiter__(self):return self
        async def __anext__(self):return await self.messages.get()
        async def send_json(self,_value):self.ready.set()
        async def send_bytes(self,value):await self.audio.put(value)
    browser=Browser()
    task=asyncio.create_task(audio_ws_view._run_audio_session(runtime.hass,browser,session,endpoint_id=endpoint.endpoint_id))
    try:
        await asyncio.wait_for(browser.ready.wait(),2)
        pcm=audio_ws.decode_audio_frame(await asyncio.wait_for(browser.audio.get(),2))
        samples=struct.unpack(f'<{len(pcm)//2}h',pcm)
        assert max(abs(value) for value in samples)>500
        if retained_outgoing:
            await asyncio.to_thread(registered_phones[1].command,'/hold')
            await wait_for(lambda:session.local_audio_direction=='recvonly')
            await asyncio.to_thread(registered_phones[1].command,'/resume')
            await wait_for(lambda:session.local_audio_direction=='sendrecv')
        fmt=session.send_format.audio_format
        frame=struct.pack(f'<{fmt.nominal_frame_samples}h',*(int(8000*math.sin(2*math.pi*650*i/fmt.sample_rate)) for i in range(fmt.nominal_frame_samples)))
        for _ in range(30):
            await browser.messages.put(SimpleNamespace(type=WSMsgType.BINARY,data=audio_ws.encode_audio_frame(frame)))
            await asyncio.sleep(fmt.frame_ms/1000)
        from homeassistant.exceptions import ServiceValidationError
        original_owner = runtime.calls.get_session(source_id)
        with pytest.raises(ServiceValidationError, match="generation"):
            await runtime.hass.services.async_call('voip_stack', 'park',
                {'device_id': endpoint.device_id,
                 'expected_generation': original_owner.generation + 1}, blocking=True)
        assert not lot.calls and session.handoff_transport is not None
        response = await runtime.hass.services.async_call('voip_stack', 'park',
            {'device_id': endpoint.device_id}, blocking=True, return_response=True)
        assert response['call_id'] == source_id and response['slot'] == '701'
        application = runtime.calls.resource_for(source_id, 'parking')
        await asyncio.wait_for(task,2)
        assert browser.closed
        assert lot.calls['701'].session.call_id == source_id
        assert endpoint.endpoint_id not in lot.calls['701'].session.endpoint_claims
        assert application.media.transport is not None
        # A real SIP phone retrieves the same caller after browser parking.
        await asyncio.to_thread(registered_phones[2].dial, '701')
        await wait_for(lambda:not lot.calls)
        relay=runtime.calls.resource_for(source_id,'relay')
        await wait_for(lambda:relay.left_tx_packets >= 30 and relay.right_tx_packets >= 30)
        await asyncio.to_thread(registered_phones[2].hangup)
        await wait_for(lambda:not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool['used']
        for peer in registered_phones:
            await asyncio.to_thread(peer.close)
        heard=False
        for path in (tmp_path/('transfer_b' if retained_outgoing else 'transfer_a')).glob('*-dec.wav'):
            with wave.open(str(path),'rb') as recording:
                rate=recording.getframerate()
                values=struct.unpack(f'<{recording.getnframes()}h',recording.readframes(recording.getnframes()))
            if len(values)<800:
                continue
            power={frequency:max(abs(sum(value*complex(math.cos(2*math.pi*frequency*i/rate),math.sin(2*math.pi*frequency*i/rate)) for i,value in enumerate(values[start:start+800]))) for start in range(0,len(values)-799,400)) for frequency in (450,650)}
            heard |= power[650]>power[450]*5 and power[650]>10000
        assert heard, 'The SIP caller did not receive browser PCM after pickup'
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task,return_exceptions=True)


@pytest.mark.parametrize('trunks',[True],indirect=True)
@pytest.mark.parametrize('via_service', [False, True])
async def test_sip_refer_parks_established_caller_without_redial(registered_phones,trunks,via_service):
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot
    runtime=trunks
    entry=config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry,data={**entry.data,'parking':{'enabled':True}})
    caller,operator,picker=registered_phones
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)
    await asyncio.to_thread(caller.dial,'transfer_b')
    await wait_for(lambda: any(s.state=='in_call' for s in runtime.calls.sessions.values()))
    original=next(iter(runtime.calls.sessions.values()))
    source_id=original.call_id
    relay=runtime.calls.resource_for(source_id,'relay')
    transport=relay.left_transport
    if via_service:
        runtime.runtime.endpoints.update('transfer_b', device_id='operator-device')
        response = await runtime.hass.services.async_call('voip_stack', 'park',
            {'device_id': 'operator-device'}, blocking=True, return_response=True)
        assert response['slot'] == '701' and response['call_id'] == source_id
    else:
        await asyncio.to_thread(operator.command,f'/transfer sip:700@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}')
    lot=get_parking_lot(runtime.hass)
    await wait_for(lambda: '701' in lot.calls and not runtime.runtime.endpoints.get('transfer_b').active_call_id)
    assert lot.calls['701'].session is original
    assert runtime.calls.resource_for(source_id,'relay').transport is transport
    assert caller.read().count('Call established')==1
    await asyncio.to_thread(picker.dial,'701')
    await wait_for(lambda: not lot.calls)
    relay=runtime.calls.resource_for(source_id,'relay')
    await wait_for(lambda: relay.left_tx_packets>30 and relay.right_tx_packets>30)
    # The pickup leg is now a native incoming dialog. It can park the same
    # caller again without creating a new source call or leaking another pair.
    await asyncio.to_thread(picker.command,f'/transfer sip:700@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}')
    await wait_for(lambda: '701' in lot.calls and not runtime.runtime.endpoints.get('transfer_c').active_call_id)
    assert lot.calls['701'].session is original
    await asyncio.to_thread(operator.dial,'701')
    await wait_for(lambda: not lot.calls)
    assert len(runtime.runtime.rtp_port_pool['used'])==2
    await asyncio.to_thread(operator.hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool['used']


@pytest.mark.parametrize('trunks',[True],indirect=True)
@pytest.mark.parametrize("via_service", [False, True])
async def test_caller_can_park_answered_outgoing_peer_and_keep_its_dialog(registered_phones,trunks,via_service):
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot
    runtime=trunks
    entry=config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry,data={**entry.data,'parking':{'enabled':True}})
    operator,remaining,picker=registered_phones
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)
    await asyncio.to_thread(operator.dial,'transfer_b')
    await wait_for(lambda: any(s.state=='in_call' for s in runtime.calls.sessions.values()))
    original=next(iter(runtime.calls.sessions.values()))
    client=runtime.calls.sip_client_for(runtime.calls.bridge_link_for(original.call_id))
    transport=runtime.calls.resource_for(original.call_id,'relay').right_transport
    source_address=transport.get_extra_info('sockname')
    await asyncio.to_thread(operator.command,f'/transfer sip:700@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}')
    lot=get_parking_lot(runtime.hass)
    await wait_for(lambda: '701' in lot.calls and not runtime.runtime.endpoints.get('transfer_a').active_call_id)
    current=lot.calls['701'].session
    assert current is not original and current.state=='in_call'
    assert runtime.calls.sip_client_for(client.dialog_ids.call_id) is client
    media=runtime.calls.resource_for(current.call_id,'relay')
    assert media.transport is transport and remaining.read().count('Call established')==1
    await asyncio.to_thread(remaining.command,'/hold')
    await wait_for(lambda: not media.can_send)
    await asyncio.to_thread(remaining.command,'/resume')
    await wait_for(lambda: media.can_send)
    if via_service:
        runtime.runtime.endpoints.update("transfer_c", device_id="pickup-device")
        result = await runtime.hass.services.async_call("voip_stack", "pickup", {
            "device_id": "pickup-device", "slot": "701", "answer_timeout": 3,
        }, blocking=True, return_response=True)
        assert result["call_id"] == current.call_id
    else:
        await asyncio.to_thread(picker.dial,'701')
    await wait_for(lambda: not lot.calls)
    relay=runtime.calls.resource_for(current.call_id,'relay')
    if via_service:
        assert transport.is_closing()
        assert relay.left_transport.get_extra_info("sockname") == source_address
        assert runtime.calls.sip_client_for(client.dialog_ids.call_id) is client
    else:
        assert relay.left_transport is transport
    await wait_for(lambda: relay.left_tx_packets>30 and relay.right_tx_packets>30)
    await asyncio.to_thread(remaining.hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool['used']


@pytest.mark.parametrize('trunks',[True],indirect=True)
async def test_failed_pickup_handoff_keeps_original_call_and_allows_retry(registered_phones,trunks,monkeypatch):
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot
    runtime=trunks
    entry=config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry,data={**entry.data,'parking':{'enabled':True}})
    caller,first,second=registered_phones
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(0.01)
    await asyncio.to_thread(caller.dial,'700')
    lot=get_parking_lot(runtime.hass)
    parked=lot.calls['701']
    media=runtime.calls.resource_for(parked.session.call_id,'relay')
    transport=media.transport
    protocol=transport.get_protocol()
    original_set=transport.set_protocol
    failures=[]
    def fail_once(value):
        if not failures:
            failures.append(True)
            raise RuntimeError('injected RTP ownership failure')
        original_set(value)
    monkeypatch.setattr(transport,'set_protocol',fail_once)
    await asyncio.to_thread(first.command,'/dial 701')
    await wait_for(lambda:failures and parked.claim is None and not runtime.runtime.endpoints.get('transfer_b').active_call_id)
    assert parked.session.live and media.transport is transport and transport.get_protocol() is protocol
    assert runtime.runtime.rtp_port_pool['used']==set(media.reservation.ports)
    assert caller.read().count('Call established')==1
    await asyncio.to_thread(second.dial,'701')
    await wait_for(lambda:not lot.calls)
    await asyncio.to_thread(second.hangup)
    await wait_for(lambda:not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool['used']


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('parker', ['local-a', 'local-b'])
@pytest.mark.parametrize('pickup_kind', ['sip', 'browser', 'return-browser'])
async def test_local_browser_parking_keeps_other_phone_for_sip_pickup(registered_phones, trunks, parker, pickup_kind, monkeypatch):
    import struct
    from aiohttp import WSMsgType
    from custom_components.voip_stack import audio_ws, audio_ws_view
    from custom_components.voip_stack.local_softphone_runtime import (
        async_setup_local_softphone_bridge, async_shutdown_local_softphone_bridge,
        start_local_softphone_call,
    )
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.store import config_entry

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, 'parking': {'enabled': True}})
    for name in ('local-a', 'local-b', 'local-c'):
        runtime.runtime.endpoints.register(PhoneEndpoint(endpoint_id=name, name=name,
            device_id='device-' + name, kind=EndpointKind.BROWSER,
            availability='available', capabilities=frozenset({'audio', 'dtmf'})))
    bridge = async_setup_local_softphone_bridge(runtime.hass)
    call = start_local_softphone_call(runtime.hass, 'local-a', 'local-b', caller_owner_id='owner-a')
    bridge.answer(call.call_id, 'local-b', 'owner-b')
    retained = 'local-b' if parker == 'local-a' else 'local-a'
    picker = parker if pickup_kind == 'return-browser' else 'local-c'
    from custom_components.voip_stack.local_call_media import LocalCallMedia
    from homeassistant.exceptions import ServiceValidationError
    original_start = LocalCallMedia.start
    async def fail_after_bind(media):
        await original_start(media)
        raise OSError("injected application startup failure")
    with monkeypatch.context() as fault:
        fault.setattr(LocalCallMedia, 'start', fail_after_bind)
        with pytest.raises(OSError, match="injected"):
            await runtime.hass.services.async_call('voip_stack', 'park',
                {'device_id': 'device-' + parker}, blocking=True)
    assert bridge.get_call(call.call_id).callee_state.value == 'in_call'
    assert not get_parking_lot(runtime.hass).calls
    assert not runtime.runtime.rtp_port_pool['used']
    assert runtime.calls.resource_for(call.call_id, 'softphone_media')['local_bridge']
    lease = bridge.acquire_media(call.call_id, parker, 'owner-a' if parker == 'local-a' else 'owner-b')
    async def start_while_audio_arrives(media):
        await original_start(media)
        bridge.send_audio(call.call_id, parker, lease.token, struct.pack('<480h', *([6000] * 480)))
    with monkeypatch.context() as live_audio:
        live_audio.setattr(LocalCallMedia, 'start', start_while_audio_arrives)
        response = await runtime.hass.services.async_call('voip_stack', 'park',
            {'device_id': 'device-' + parker}, blocking=True, return_response=True)
    with pytest.raises(ServiceValidationError, match="does not own"):
        await runtime.hass.services.async_call('voip_stack', 'park',
            {'device_id': 'device-' + parker, 'call_id': call.call_id}, blocking=True)
    assert response['call_id'] == call.call_id
    assert bridge.get_call(call.call_id) is None
    assert not runtime.runtime.endpoints.get(parker).active_call_id
    assert runtime.runtime.endpoints.get(retained).active_call_id == call.call_id
    media = audio_ws_view._active_softphone_media_session(runtime.hass, retained)
    assert media is not None

    class Browser:
        def __init__(self):
            self.messages = asyncio.Queue()
            self.audio = asyncio.Queue()
            self.ready = asyncio.Event()
        def force_close(self):
            pass
        def __aiter__(self):
            return self
        async def __anext__(self):
            return await self.messages.get()
        async def send_json(self, value):
            self.ready.set()
        async def send_bytes(self, value):
            await self.audio.put(value)

    browser = Browser()
    worker = asyncio.create_task(audio_ws_view._run_audio_session(runtime.hass, browser, media, endpoint_id=retained))
    async def wait_for(predicate):
        async with asyncio.timeout(3):
            while not predicate():
                await asyncio.sleep(0.01)
    try:
        await asyncio.wait_for(browser.ready.wait(), 2)
        if pickup_kind != 'sip':
            from unittest.mock import AsyncMock
            from custom_components.voip_stack import softphone_originate
            from custom_components.voip_stack.roster import RosterEntry, dump_roster_json
            runtime.roster.append(RosterEntry(picker, metadata={'local_ha': True, 'endpoint_id': picker}))
            runtime.hass.states.async_set('sensor.voip_phonebook', 'ready', {'roster_json': dump_roster_json(runtime.roster)})
            monkeypatch.setattr(softphone_originate, '_ha_advertise_host', AsyncMock(return_value='127.0.0.1'))
            monkeypatch.setattr(softphone_originate, '_resolve_target_device', AsyncMock(return_value=None))
            await runtime.hass.services.async_call('voip_stack', 'call',
                {'device_id': 'device-' + picker, 'destination': '701', 'media_client_id': 'owner-c'}, blocking=True)
            resumed = bridge.get_call(call.call_id)
            assert resumed is not None and resumed.caller_endpoint_id == retained
            await asyncio.wait_for(worker, 2)
            assert not runtime.runtime.rtp_port_pool['used']
            source_lease = bridge.acquire_media(call.call_id, retained, 'owner-b' if retained == 'local-b' else 'owner-a')
            target_lease = bridge.acquire_media(call.call_id, picker, 'owner-c')
            left_pcm = struct.pack('<480h', *([7000] * 480))
            right_pcm = struct.pack('<480h', *([9000] * 480))
            bridge.send_audio(call.call_id, retained, source_lease.token, left_pcm)
            bridge.send_audio(call.call_id, picker, target_lease.token, right_pcm)
            assert bridge.receive_audio_nowait(call.call_id, picker, target_lease.token) == left_pcm
            assert bridge.receive_audio_nowait(call.call_id, retained, source_lease.token) == right_pcm
            # The restored ordinary browser call can be parked in the opposite direction.
            await runtime.hass.services.async_call('voip_stack', 'park',
                {'device_id': 'device-' + retained}, blocking=True)
            assert get_parking_lot(runtime.hass).calls['701'].session.call_id == call.call_id
            assert runtime.calls.get_session(call.call_id).metadata['source_endpoint_id'] == picker
            await runtime.hass.services.async_call('voip_stack', 'hangup',
                {'device_id': 'device-' + picker}, blocking=True)
            await wait_for(lambda: not runtime.calls.sessions)
            assert not runtime.runtime.rtp_port_pool['used']
            return
        else:
            await asyncio.to_thread(registered_phones[2].dial, '701')
        await wait_for(lambda: not get_parking_lot(runtime.hass).calls)
        frame = struct.pack('<h', 7000) * media.send_format.audio_format.nominal_frame_samples
        for _ in range(20):
            await browser.messages.put(SimpleNamespace(type=WSMsgType.BINARY, data=audio_ws.encode_audio_frame(frame)))
            await asyncio.sleep(media.send_format.audio_format.frame_ms / 1000)
        async with asyncio.timeout(3):
            while True:
                pcm = audio_ws.decode_audio_frame(await browser.audio.get())
                if max(abs(value) for value in struct.unpack(f'<{len(pcm)//2}h', pcm)) > 1000:
                    break
        relay = runtime.calls.resource_for(call.call_id, 'relay')
        assert relay.left_rx_packets > 0 and relay.right_rx_packets > 0
        await asyncio.to_thread(registered_phones[2].hangup)
        await wait_for(lambda: not runtime.calls.sessions)
        await asyncio.wait_for(worker, 2)
        assert not runtime.runtime.rtp_port_pool['used']
    finally:
        if not worker.done():
            worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        async_shutdown_local_softphone_bridge(runtime.hass)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_inbound_trunk_parking_can_return_to_same_browser(trunks, monkeypatch):
    """An external caller remains parked while its original browser rejoins."""
    import socket
    import struct
    from unittest.mock import AsyncMock
    from aiohttp import WSMsgType
    from custom_components.voip_stack import audio_ws, audio_ws_view, softphone_originate
    from custom_components.voip_stack.config import normalized_trunk_config
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.core import rtp
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.roster import RosterEntry, dump_roster_json
    from custom_components.voip_stack.sip_client import SipCallClient
    from custom_components.voip_stack.store import config_entry

    lab = trunks
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {"enabled": True}})
    phone = PhoneEndpoint(endpoint_id="desk", name="Desk", device_id="desk-device",
                          kind=EndpointKind.BROWSER, availability="available", capabilities=frozenset({"audio", "dtmf"}))
    lab.runtime.endpoints.register(phone)
    lab.runtime.preferred_phone_device_id = phone.device_id
    lab.roster.append(RosterEntry("Desk", metadata={"local_ha": True, "endpoint_id": phone.endpoint_id}))
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    lab.runtime.trunk_configs["home"] = normalized_trunk_config({
        "trunk_enabled": True, "trunk_register": False, "trunk_server": "127.0.0.3",
        "trunk_allowed_ips": ["127.0.0.3"], "trunk_inbound_default_target": "Desk",
    })
    from custom_components.voip_stack.trunk_runtime import async_start_sip_trunk, async_stop_sip_trunk
    await async_stop_sip_trunk(lab.hass)
    lab.runtime.trunk_configs = {"home": lab.runtime.trunk_configs["home"]}
    lab.runtime.trunk_config = lab.runtime.trunk_configs["home"]
    await async_start_sip_trunk(lab.hass, local_ip="127.0.0.1")
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)

    class Browser:
        def __init__(self):
            self.audio = asyncio.Queue()
            self.messages = asyncio.Queue()
            self.ready = asyncio.Event()
        def force_close(self):
            pass
        def __aiter__(self):
            return self
        async def __anext__(self):
            return await self.messages.get()
        async def send_json(self, value):
            self.ready.set()
        async def send_bytes(self, value):
            await self.audio.put(value)

    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.3", 0))
    audio.setblocking(False)
    client = SipCallClient(local_ip="127.0.0.3", local_name="External", local_sip_port=0,
                           local_rtp_port=audio.getsockname()[1], supported_formats=[AudioFormat(16000, "s16le", 1, 20)])
    from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
    client.transport, client.protocol = await lab.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(client.queue), local_addr=("127.0.0.3", 0))
    client.local_sip_port = client.transport.get_extra_info("sockname")[1]
    workers = []
    signaling = None
    try:
        port = lab.runtime.transport_config["sip_port"]
        assert await client.invite(target="Desk", remote_host="127.0.0.1", remote_sip_port=port,
                                   request_uri=f"sip:Desk@127.0.0.1:{port}", timeout=2) == "ringing"
        call_id = client.dialog_ids.call_id
        await lab.hass.services.async_call("voip_stack", "answer", {"device_id": phone.device_id, "call_id": call_id}, blocking=True)
        await wait_for(lambda: client.dialog is not None and lab.runtime.endpoints.get(phone.endpoint_id).active_call_id == call_id)
        signaling = asyncio.create_task(client.wait_for_dialog_termination(timeout=15))
        owner = lab.calls.get_session(call_id)
        assert owner.metadata["inbound_trunk_id"] == "home"
        lot = get_parking_lot(lab.hass)
        for cycle in range(2):
            media = audio_ws_view._active_softphone_media_session(lab.hass, phone.endpoint_id)
            browser = Browser()
            worker = asyncio.create_task(audio_ws_view._run_audio_session(lab.hass, browser, media, endpoint_id=phone.endpoint_id))
            workers.append(worker)
            await asyncio.wait_for(browser.ready.wait(), 2)
            # Keep the call's real RTP path observable before each park.
            frame = struct.pack("<h", 6000) * media.send_format.audio_format.nominal_frame_samples
            await browser.messages.put(SimpleNamespace(type=WSMsgType.BINARY, data=audio_ws.encode_audio_frame(frame)))
            async with asyncio.timeout(2):
                while True:
                    raw = await lab.hass.loop.sock_recv(audio, 4096)
                    payload = rtp.parse_packet(raw).payload
                    if any(payload):
                        samples = struct.unpack(f"!{len(payload)//2}h", payload)
                        assert 5940 <= max(samples) <= 6060
                        break
            assert client.dialog.send_format.encoding == "L16"
            for index in range(10):
                sequence = cycle * 10 + index
                packet = rtp.build_packet(rtp.RtpPacket(
                    client.dialog.send_format.payload_type, sequence, sequence * 320, 99,
                    struct.pack("!h", 7000) * 320))
                await lab.hass.loop.sock_sendto(audio, packet,
                    (client.dialog.remote_rtp_host, client.dialog.remote_rtp_port))
                await asyncio.sleep(.02)
            async with asyncio.timeout(2):
                while True:
                    decoded = audio_ws.decode_audio_frame(await browser.audio.get())
                    if any(decoded):
                        assert max(struct.unpack(f"<{len(decoded)//2}h", decoded)) == 7000
                        break
            response = await lab.hass.services.async_call("voip_stack", "park", {"device_id": phone.device_id},
                                                          blocking=True, return_response=True)
            await asyncio.wait_for(worker, 2)
            assert response["slot"] == "701"
            assert not lab.runtime.endpoints.get(phone.endpoint_id).active_call_id
            assert phone.endpoint_id not in owner.endpoint_claims
            assert lot.calls["701"].session is owner
            await lab.hass.services.async_call("voip_stack", "call", {"device_id": phone.device_id, "destination": "701"}, blocking=True)
            assert not lot.calls
            assert lab.calls.get_session(call_id) is owner
            assert lab.runtime.endpoints.get(phone.endpoint_id).active_call_id == call_id
        await lab.hass.services.async_call("voip_stack", "hangup", {"device_id": phone.device_id}, blocking=True)
        await wait_for(lambda: not lab.calls.sessions)
        assert not lot.calls
        assert await asyncio.wait_for(signaling, 2) == "remote_hangup"
        assert not lab.runtime.rtp_port_pool["used"]
    finally:
        await client.close()
        if signaling is not None:
            workers.append(signaling)
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        audio.close()


@pytest.mark.parametrize("active_video", [False, True])
async def test_browser_video_parking_reports_media_limit_before_audio_readiness(monkeypatch, active_video):
    from unittest.mock import Mock
    from custom_components.voip_stack import parking
    owner = SimpleNamespace(call_id="video-call", live=True, state="in_call", endpoint_claims={"browser"})
    registry = Mock()
    registry.resolve_session_id.return_value = "video-call"
    registry.get_session.return_value = owner
    registry.resource_for.return_value = None if active_video else {"video_direction": "sendrecv", "video_format": object()}
    runtime = SimpleNamespace(sip=registry, media=SimpleNamespace(sessions_for=lambda kind:
        {("video-call", "browser"): object()} if active_video and kind == "video" else {}))
    monkeypatch.setattr(parking, "require_runtime_data", lambda _: runtime)
    with pytest.raises(ParkingError, match="audio-only"):
        await parking.park_browser_sip_call(object(), "video-call", "browser")
    registry.take_media.assert_not_called()
    registry.request_termination.assert_not_called()


@pytest.mark.parametrize('trunks', [True], indirect=True)
@pytest.mark.parametrize('offer_video', [False, True])
async def test_originated_browser_call_parks_without_inbound_media_descriptor(
    registered_phones, trunks, monkeypatch, offer_video, tmp_path,
):
    import struct
    import wave
    from aiohttp import WSMsgType
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import audio_ws_view, audio_ws, softphone_originate
    from custom_components.voip_stack.const import CONF_SIP_VIDEO
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.store import config_entry

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, 'parking': {'enabled': True}})
    runtime.runtime.transport_config[CONF_SIP_VIDEO] = offer_video
    if offer_video:
        runtime.runtime.endpoints.update('transfer_a', capabilities=frozenset({'audio', 'dtmf', 'video'}))
    endpoint = PhoneEndpoint(endpoint_id='direct-browser', name='Direct browser', device_id='direct-browser-device',
        kind=EndpointKind.BROWSER, availability='available', capabilities=frozenset({'audio', 'dtmf', 'video'}))
    runtime.runtime.endpoints.register(endpoint)
    from custom_components.voip_stack.roster import RosterEntry, dump_roster_json
    runtime.roster.append(RosterEntry(endpoint.name, metadata={'local_ha': True, 'endpoint_id': endpoint.endpoint_id}))
    runtime.hass.states.async_set('sensor.voip_phonebook', 'ready', {'roster_json': dump_roster_json(runtime.roster)})
    monkeypatch.setattr(softphone_originate, '_ha_advertise_host', AsyncMock(return_value='127.0.0.1'))
    monkeypatch.setattr(softphone_originate, '_resolve_target_device', AsyncMock(return_value=None))

    class Browser:
        def __init__(self):
            self.ready = asyncio.Event()
            self.audio = asyncio.Queue()
            self.messages = asyncio.Queue()
            self.closed = False
        def force_close(self): self.closed = True
        def __aiter__(self): return self
        async def __anext__(self): return await self.messages.get()
        async def send_json(self, _): self.ready.set()
        async def send_bytes(self, value): self.audio.put_nowait(value)

    tasks = []
    async def connect():
        media = audio_ws_view._active_softphone_media_session(runtime.hass, endpoint.endpoint_id)
        client = runtime.calls.sip_client_for(media.call_id)
        if client is not None and client.audio_rtp_socket is media.rtp_socket:
            client.audio_rtp_socket = None  # The HTTP handler transfers this socket before running media.
        browser = Browser()
        task = asyncio.create_task(audio_ws_view._run_audio_session(runtime.hass, browser, media, endpoint_id=endpoint.endpoint_id))
        tasks.append(task)
        await asyncio.wait_for(browser.ready.wait(), 2)
        pcm = audio_ws.decode_audio_frame(await asyncio.wait_for(browser.audio.get(), 2))
        samples = struct.unpack(f'<{len(pcm)//2}h', pcm)
        assert max(abs(value) for value in samples) > 500
        return browser, media

    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)

    try:
        await runtime.hass.services.async_call('voip_stack', 'call', {
            'device_id': endpoint.device_id, 'destination': 'transfer_a', 'media_client_id': 'direct-browser-media',
        }, blocking=True)
        call_id = runtime.runtime.endpoints.get(endpoint.endpoint_id).active_call_id
        client = runtime.calls.sip_client_for(call_id)
        assert client.dialog is not None and client.dialog.video_format is None
        assert runtime.calls.resource_for(call_id, 'softphone_media') is None
        if offer_video:
            assert client.media_reservation is not None
        browser, media = await connect()
        source = media.rtp_source
        response = await runtime.hass.services.async_call('voip_stack', 'park', {
            'device_id': endpoint.device_id,
        }, blocking=True, return_response=True)
        assert response['slot'] == '701' and response['call_id'] == call_id
        await asyncio.wait_for(tasks[0], 2)
        assert browser.closed
        application = runtime.calls.resource_for(call_id, 'parking')
        assert application.media.rtp_source is source
        assert runtime.calls.sip_client_for(call_id) is client
        assert not client._closed
        await runtime.hass.services.async_call('voip_stack', 'call', {
            'device_id': endpoint.device_id, 'destination': '701', 'media_client_id': 'direct-browser-media',
        }, blocking=True)
        assert not get_parking_lot(runtime.hass).calls
        recovered_browser, recovered_media = await connect()
        fmt = recovered_media.send_format.audio_format
        pcm = struct.pack(f'<{fmt.nominal_frame_samples}h', *([1200] * fmt.nominal_frame_samples))
        for _ in range(30):
            recovered_browser.messages.put_nowait(SimpleNamespace(type=WSMsgType.BINARY, data=audio_ws.encode_audio_frame(pcm)))
            await asyncio.sleep(fmt.frame_ms / 1000)
        await runtime.hass.services.async_call('voip_stack', 'hangup', {'device_id': endpoint.device_id}, blocking=True)
        await wait_for(lambda: not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool['used']
        await asyncio.to_thread(registered_phones[0].close)
        heard = 0
        for path in (tmp_path / 'transfer_a').glob('*-dec.wav'):
            with wave.open(str(path), 'rb') as recording:
                samples = struct.unpack(f'<{recording.getnframes()}h', recording.readframes(recording.getnframes()))
                heard += sum(abs(value - 1200) < 80 for value in samples)
        assert heard > 1000, 'The retained SIP peer did not receive browser audio after pickup'
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
