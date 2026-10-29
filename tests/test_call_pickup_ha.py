"""Directed pickup retains the caller while a different real phone answers."""

import asyncio
import math
import struct
import wave

import pytest
from custom_components.voip_stack.store import config_entry
from .test_sip_phone_transfers_ha import registered_phones as registered_phones
from .test_trunk_calls_ha import trunks as trunks, lab as lab

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
@pytest.mark.parametrize("target", ["pickup.transfer_b", "*8101", "service"])
@pytest.mark.parametrize("hangup_side", [0, 2])
async def test_directed_sip_pickup_has_duplex_audio_and_one_caller(
    trunks, registered_phones, tmp_path, target, hangup_side
):
    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(
        entry, data={**entry.data, "parking": {"enabled": True}}
    )
    runtime.runtime.endpoints.update("transfer_b", extension="101")
    caller, old_phone, picker = registered_phones

    async def wait_for(predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(0.01)

    await asyncio.to_thread(caller.command, "/dial transfer_b")
    await wait_for(
        lambda: any(
            s.state == "remote_ringing" for s in runtime.calls.sessions.values()
        )
    )
    original = next(iter(runtime.calls.sessions.values()))
    if target == "service":
        runtime.runtime.endpoints.update("transfer_c", device_id="pickup-device")
        result = await runtime.hass.services.async_call("voip_stack", "pickup", {
            "device_id": "pickup-device", "target_device_id": "transfer_b", "answer_timeout": 3,
        }, blocking=True, return_response=True)
        assert result["call_id"] == original.call_id
    else:
        await asyncio.to_thread(picker.dial, target)
    await wait_for(
        lambda: original.state == "in_call" and "Call established" in caller.read()
    )
    assert len(runtime.calls.sessions) == 1
    assert caller.read().count("Call established") == 1
    assert "Call established" not in old_phone.read()
    assert (
        runtime.runtime.endpoints.get("transfer_c").active_call_id == original.call_id
    )
    await wait_for(
        lambda: not runtime.runtime.endpoints.get("transfer_b").active_call_id
    )
    relay = runtime.calls.resource_for(original.call_id, "relay")
    await wait_for(lambda: relay.left_tx_packets >= 50 and relay.right_tx_packets >= 50)
    await asyncio.to_thread(registered_phones[hangup_side].hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool.get("used")
    for peer in registered_phones:
        await asyncio.to_thread(peer.close)
    for name, tone in (("transfer_a", 650), ("transfer_c", 450)):
        found = False
        for path in (tmp_path / name).glob("*-dec.wav"):
            with wave.open(str(path), "rb") as wav:
                rate = wav.getframerate()
                samples = struct.unpack(
                    f"<{wav.getnframes()}h", wav.readframes(wav.getnframes())
                )[-1600:]
            if len(samples) < 800:
                continue
            power = {
                f: abs(
                    sum(
                        value
                        * complex(
                            math.cos(2 * math.pi * f * i / rate),
                            math.sin(2 * math.pi * f * i / rate),
                        )
                        for i, value in enumerate(samples)
                    )
                )
                for f in (450, 650)
            }
            found |= power[tone] > power[1100 - tone] * 5 and power[tone] > 10000
        assert found, f"{name} did not receive the opposite microphone"


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
@pytest.mark.parametrize(
    "winner", ["original_answer", "caller_hangup", "picker_cancel"]
)
@pytest.mark.parametrize("group", [False, True])
async def test_pickup_preparation_cannot_steal_answered_or_cancelled_call(
    trunks, registered_phones, monkeypatch, winner, group
):
    from custom_components.voip_stack.sip_rtp_bridge import SipRtpRelay

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(
        entry, data={**entry.data, "parking": {"enabled": True}}
    )
    caller, original, picker = registered_phones
    prepared, continue_preparation = asyncio.Event(), asyncio.Event()
    hold_preparation = group and winner == "original_answer"
    if hold_preparation:
        from custom_components.voip_stack import ring_group_orchestrator
        prepare = ring_group_orchestrator.async_prepare_group_candidates

        async def prepared_before_dial(*args, **kwargs):
            await prepare(*args, **kwargs)
            prepared.set()
            await continue_preparation.wait()

        monkeypatch.setattr(ring_group_orchestrator, "async_prepare_group_candidates", prepared_before_dial)

    async def wait_for(predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(0.01)

    if group:
        from custom_components.voip_stack.roster import RosterEntry
        runtime.roster.append(RosterEntry("PickupGroup", metadata={"group_type": "ring", "members": ["transfer_b"]}))
    await asyncio.to_thread(caller.command, "/dial PickupGroup" if group else "/dial transfer_b")
    await wait_for(
        lambda: any(
            s.state == ("ringing" if group else "remote_ringing") for s in runtime.calls.sessions.values()
        )
    )
    session = next(iter(runtime.calls.sessions.values()))
    if group:
        # A group claims phones before its SIP legs start. The BLF projection
        # can already be early while the actual destination is still calling.
        checked = asyncio.Event()

        def destination_is_ringing():
            checked.set()
            return any(
                leg.endpoint_id == "transfer_b" and leg.state == "remote_ringing"
                for leg in session.legs.values()
            ) and "Incoming call" in original.read()

        ready = asyncio.create_task(wait_for(destination_is_ringing))
        if hold_preparation:
            try:
                await asyncio.wait_for(prepared.wait(), 3)
                from custom_components.voip_stack.dialog_info import snapshot
                assert any(item.state == "early" for item in snapshot(runtime.runtime, "transfer_b"))
                await asyncio.wait_for(checked.wait(), 3)
                assert not ready.done(), "Pickup barrier admitted a claim without a ringing SIP leg"
            except BaseException:
                ready.cancel()
                await asyncio.gather(ready, return_exceptions=True)
                raise
            finally:
                continue_preparation.set()
        await ready
    entered, release = asyncio.Event(), asyncio.Event()
    start = SipRtpRelay.start
    first = True

    async def gated(relay):
        nonlocal first
        if first:
            first = False
            entered.set()
            await release.wait()
        return await start(relay)

    monkeypatch.setattr(SipRtpRelay, "start", gated)
    try:
        await asyncio.to_thread(picker.command, "/dial pickup.transfer_b")
        await asyncio.wait_for(entered.wait(), 3)
        from custom_components.voip_stack.runtime_data import sip_endpoint_manager

        endpoint = sip_endpoint_manager(runtime.hass).udp_server.endpoint
        picker_id = next(key for key in endpoint.pending_invites if key != session.call_id)
        picker_transaction = endpoint.pending_invites[picker_id]
        if winner == "original_answer":
            await asyncio.to_thread(original.command, "/accept")
            await wait_for(lambda: session.state == "in_call")
        elif winner == "caller_hangup":
            await asyncio.to_thread(caller.hangup)
            # Writing the peer's PTY does not mean CANCEL reached the PBX.
            # Hold media preparation until the authoritative owner accepted
            # termination; full cleanup can depend on releasing this gate.
            await wait_for(session.termination_started.is_set)
            assert not session.live
        else:
            await asyncio.to_thread(picker.hangup)
            await wait_for(lambda: picker_id not in endpoint.pending_invites)
        release.set()
        # The final-response cache is retired on ACK. Keep the transaction
        # identity; local cancellation and remote rejection close differently.
        if winner == "picker_cancel":
            await wait_for(lambda: picker_transaction.final_task is None)
        else:
            await wait_for(lambda: "session closed" in picker.read().lower())
        assert picker_transaction.status >= 300
        await wait_for(
            lambda: not runtime.runtime.endpoints.get("transfer_c").active_call_id
        )
        assert "Call established" not in picker.read()
        if winner == "picker_cancel":
            assert session.live and session.state == ("ringing" if group else "remote_ringing")
            await asyncio.to_thread(original.command, "/accept")
            await wait_for(lambda: session.state == "in_call")
        if winner != "caller_hangup":
            assert session.live
            await asyncio.to_thread(caller.hangup)
        await wait_for(lambda: not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool.get("used")
    finally:
        release.set()


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_browser_picks_up_local_phone_using_original_session(trunks, monkeypatch):
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import softphone_originate
    from custom_components.voip_stack.local_softphone_runtime import (
        async_setup_local_softphone_bridge,
        start_local_softphone_call,
    )
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.roster import RosterEntry, dump_roster_json

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(
        entry, data={**entry.data, "parking": {"enabled": True}}
    )
    for name in ("local-a", "local-b", "local-c"):
        runtime.runtime.endpoints.register(
            PhoneEndpoint(
                endpoint_id=name,
                name=name,
                device_id="device-" + name,
                kind=EndpointKind.BROWSER,
                availability="available",
                capabilities=frozenset({"audio", "dtmf"}),
            )
        )
        runtime.roster.append(
            RosterEntry(name, metadata={"local_ha": True, "endpoint_id": name})
        )
    runtime.hass.states.async_set(
        "sensor.voip_phonebook",
        "ready",
        {"roster_json": dump_roster_json(runtime.roster)},
    )
    monkeypatch.setattr(
        softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1")
    )
    monkeypatch.setattr(
        softphone_originate, "_resolve_target_device", AsyncMock(return_value=None)
    )
    bridge = async_setup_local_softphone_bridge(runtime.hass)
    call = start_local_softphone_call(
        runtime.hass, "local-a", "local-b", caller_owner_id="owner-a"
    )
    session = runtime.calls.get_session(call.call_id)
    await runtime.hass.services.async_call(
        "voip_stack",
        "call",
        {
            "device_id": "device-local-c",
            "destination": "pickup.local-b",
            "media_client_id": "owner-c",
        },
        blocking=True,
    )
    assert runtime.calls.get_session(call.call_id) is session
    assert not runtime.runtime.endpoints.get("local-b").active_call_id
    assert runtime.runtime.endpoints.get("local-c").active_call_id == call.call_id
    caller = bridge.acquire_media(call.call_id, "local-a", "owner-a")
    picker = bridge.acquire_media(call.call_id, "local-c", "owner-c")
    for source, lease, dest, other, sample in [
        ("local-a", caller, "local-c", picker, 7000),
        ("local-c", picker, "local-a", caller, 9000),
    ]:
        pcm = struct.pack("<480h", *([sample] * 480))
        bridge.send_audio(call.call_id, source, lease.token, pcm)
        assert bridge.receive_audio_nowait(call.call_id, dest, other.token) == pcm
    await runtime.hass.services.async_call(
        "voip_stack", "hangup", {"device_id": "device-local-c"}, blocking=True
    )
    async with asyncio.timeout(3):
        while runtime.calls.sessions:
            await asyncio.sleep(0.01)
    assert not runtime.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_sip_phone_picks_up_local_browser_call(
    trunks, registered_phones, monkeypatch
):
    from aiohttp import WSMsgType
    from types import SimpleNamespace
    from custom_components.voip_stack import audio_ws, audio_ws_view
    from custom_components.voip_stack.local_softphone_runtime import (
        async_setup_local_softphone_bridge,
        start_local_softphone_call,
    )
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(
        entry, data={**entry.data, "parking": {"enabled": True}}
    )
    for name in ("local-a", "local-b"):
        runtime.runtime.endpoints.register(
            PhoneEndpoint(
                endpoint_id=name,
                name=name,
                device_id="device-" + name,
                kind=EndpointKind.BROWSER,
                availability="available",
                capabilities=frozenset({"audio", "dtmf"}),
            )
        )
    async_setup_local_softphone_bridge(runtime.hass)
    call = start_local_softphone_call(
        runtime.hass, "local-a", "local-b", caller_owner_id="owner-a"
    )
    await asyncio.to_thread(registered_phones[2].dial, "pickup.local-b")
    session = runtime.calls.get_session(call.call_id)
    assert session.state == "in_call"
    assert not runtime.runtime.endpoints.get("local-b").active_call_id
    media = audio_ws_view._active_softphone_media_session(runtime.hass, "local-a")
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
    worker = asyncio.create_task(
        audio_ws_view._run_audio_session(
            runtime.hass, browser, media, endpoint_id="local-a"
        )
    )
    try:
        await asyncio.wait_for(browser.ready.wait(), 2)
        frame = (
            struct.pack("<h", 7000)
            * media.send_format.audio_format.nominal_frame_samples
        )
        for _ in range(20):
            await browser.messages.put(
                SimpleNamespace(
                    type=WSMsgType.BINARY, data=audio_ws.encode_audio_frame(frame)
                )
            )
            await asyncio.sleep(0.02)
        pcm = audio_ws.decode_audio_frame(
            await asyncio.wait_for(browser.audio.get(), 3)
        )
        assert any(pcm)
        relay = runtime.calls.resource_for(call.call_id, "relay")
        assert relay.right_tx_packets > 0 and relay.left_tx_packets > 0
        await asyncio.to_thread(registered_phones[2].hangup)
        async with asyncio.timeout(3):
            while runtime.calls.sessions:
                await asyncio.sleep(0.01)
        assert not runtime.runtime.rtp_port_pool.get("used")
    finally:
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
@pytest.mark.parametrize("pickup_path", ["sip_request", "sip_service", "browser_service"])
async def test_sip_pickup_after_forward_to_browser_retains_original_caller(
    trunks, registered_phones, pickup_path
):
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    from custom_components.voip_stack.roster import RosterEntry, dump_roster_json

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(
        entry, data={**entry.data, "parking": {"enabled": True}}
    )
    runtime.runtime.endpoints.register(
        PhoneEndpoint(
            endpoint_id="browser-target",
            name="browser-target",
            device_id="browser-device",
            kind=EndpointKind.BROWSER,
            availability="available",
            capabilities=frozenset({"audio", "dtmf"}),
        )
    )
    runtime.roster.append(
        RosterEntry(
            "browser-target",
            metadata={"local_ha": True, "endpoint_id": "browser-target"},
        )
    )
    runtime.hass.states.async_set(
        "sensor.voip_phonebook",
        "ready",
        {"roster_json": dump_roster_json(runtime.roster)},
    )
    caller, original, picker = registered_phones

    async def wait_for(predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(0.01)

    await asyncio.to_thread(caller.command, "/dial transfer_b")
    await wait_for(
        lambda: any(
            s.state == "remote_ringing" for s in runtime.calls.sessions.values()
        )
    )
    session = next(iter(runtime.calls.sessions.values()))
    await runtime.calls.forward_call(
        call_id=session.call_id, destination="browser-target", on_failure="terminate"
    )
    await wait_for(
        lambda: session.owner == "ha_softphone" and session.state == "ringing"
    )
    # A physical ESP caller publishes its own outgoing projection while the
    # receiving browser session retains its internal ringing state.
    from custom_components.voip_stack.websocket_api import _fire_call_event
    _fire_call_event(runtime.hass, {
        "call_id": session.call_id, "state": "remote_ringing",
        "direction": "outgoing", "endpoint_id": "transfer_a",
    }, "esp")
    assert session.state == "ringing"
    if pickup_path == "sip_request":
        await asyncio.to_thread(picker.dial, "pickup.browser-target")
    else:
        if pickup_path == "browser_service":
            runtime.runtime.endpoints.register(PhoneEndpoint(
                endpoint_id="browser-picker", name="browser-picker", device_id="picker-device",
                kind=EndpointKind.BROWSER, availability="available",
                capabilities=frozenset({"audio", "dtmf"}),
            ))
            runtime.roster.append(RosterEntry(
                "browser-picker", metadata={"local_ha": True, "endpoint_id": "browser-picker"},
            ))
        else:
            runtime.runtime.endpoints.update("transfer_c", device_id="picker-device")
        await runtime.hass.services.async_call("voip_stack", "pickup", {
            "device_id": "picker-device", "target_device_id": "browser-device",
            "media_client_id": "test-browser-picker",
        }, blocking=True, return_response=True)
    assert (
        runtime.calls.get_session(session.call_id) is session
        and session.state == "in_call"
    )
    if pickup_path != "browser_service":
        relay = runtime.calls.resource_for(session.call_id, "relay")
        await wait_for(lambda: relay.left_tx_packets > 20 and relay.right_tx_packets > 20)
    assert caller.read().count("Call established") == 1
    await asyncio.to_thread(caller.hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
@pytest.mark.parametrize("picker_kind", ["sip", "browser"])
async def test_directed_pickup_of_parking_return_keeps_answered_caller(
    trunks, registered_phones, monkeypatch, picker_kind
):
    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "parking": {
                "enabled": True,
                "timeout": 1,
                "return_timeout": 10,
                "return_destination": "transfer_b",
            },
        },
    )
    caller, original, picker = registered_phones

    async def wait_for(predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(0.01)

    await asyncio.to_thread(caller.dial, "700")
    session = next(iter(runtime.calls.sessions.values()))
    source_transport = runtime.calls.resource_for(session.call_id, "relay").transport
    await wait_for(
        lambda: session.owner == "router"
        and any(
            leg.endpoint_id == "transfer_b" and leg.state == "remote_ringing"
            for leg in session.legs.values()
        )
    )
    assert runtime.runtime.endpoints.get("transfer_b").active_call_id == session.call_id
    if picker_kind == "browser":
        from unittest.mock import AsyncMock
        from custom_components.voip_stack import softphone_originate
        from custom_components.voip_stack.phone_endpoint import (
            PhoneEndpoint,
            EndpointKind,
        )
        from custom_components.voip_stack.roster import RosterEntry, dump_roster_json

        runtime.runtime.endpoints.register(
            PhoneEndpoint(
                endpoint_id="browser-picker",
                name="browser-picker",
                device_id="browser-picker-device",
                kind=EndpointKind.BROWSER,
                availability="available",
                capabilities=frozenset({"audio", "dtmf"}),
            )
        )
        runtime.roster.append(
            RosterEntry(
                "browser-picker",
                metadata={"local_ha": True, "endpoint_id": "browser-picker"},
            )
        )
        runtime.hass.states.async_set(
            "sensor.voip_phonebook",
            "ready",
            {"roster_json": dump_roster_json(runtime.roster)},
        )
        monkeypatch.setattr(
            softphone_originate,
            "_ha_advertise_host",
            AsyncMock(return_value="127.0.0.1"),
        )
        monkeypatch.setattr(
            softphone_originate, "_resolve_target_device", AsyncMock(return_value=None)
        )
        await runtime.hass.services.async_call(
            "voip_stack",
            "call",
            {
                "device_id": "browser-picker-device",
                "destination": "pickup.transfer_b",
                "media_client_id": "picker-owner",
            },
            blocking=True,
        )
        assert (
            runtime.runtime.endpoints.get("browser-picker").active_call_id
            == session.call_id
        )
    else:
        await asyncio.to_thread(picker.dial, "pickup.transfer_b")
    await wait_for(lambda: session.state == "in_call")
    if picker_kind == "sip":
        relay = runtime.calls.resource_for(session.call_id, "relay")
        assert relay.left_transport is source_transport
        await wait_for(
            lambda: relay.left_tx_packets > 20 and relay.right_tx_packets > 20
        )
    assert caller.read().count("Call established") == 1
    assert "Call established" not in original.read()
    await asyncio.to_thread(caller.hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b", "video": ("transfer_a", "transfer_c")}], indirect=True)
async def test_sip_pickup_negotiates_video_and_forwards_decodable_frames(registered_phones, trunks, monkeypatch, tmp_path):
    from collections import defaultdict
    from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay
    from .test_sip_phone_transfers_ha import assert_h264_color_bars

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {"enabled": True}})
    captured = defaultdict(list)
    original_start = SipVideoRtpRelay.start
    class Capture:
        def __init__(self, transport, key):
            self.transport, self.key = transport, key
        def sendto(self, raw, address):
            if len(captured[self.key]) < 512:
                captured[self.key].append(bytes(raw))
            self.transport.sendto(raw, address)
        def __getattr__(self, name):
            return getattr(self.transport, name)
    async def start(relay):
        await original_start(relay)
        for key, transport in tuple(relay._transports.items()):
            if not key[1] and not isinstance(transport, Capture):
                relay._transports[key] = Capture(transport, (id(relay), key[0]))
    monkeypatch.setattr(SipVideoRtpRelay, "start", start)
    caller, _ringing, picker = registered_phones
    async def wait_for(predicate):
        async with asyncio.timeout(7):
            while not predicate():
                await asyncio.sleep(.01)
    await asyncio.to_thread(caller.command, "/dial transfer_b")
    await wait_for(lambda: any(s.state == "remote_ringing" for s in runtime.calls.sessions.values()))
    session = next(iter(runtime.calls.sessions.values()))
    await asyncio.to_thread(picker.dial, "pickup.transfer_b")
    await wait_for(lambda: session.state == "in_call")
    relay = runtime.calls.resource_for(session.call_id, "relay")
    assert relay.video_relay is not None
    video = relay.video_relay
    await wait_for(lambda: all(len(captured[(id(video), side)]) >= 35 for side in ("left", "right")))
    for side in ("left", "right"):
        assert_h264_color_bars(captured[(id(video), side)], tmp_path / f"pickup-{side}.h264")
    assert relay.left_tx_packets > 0 and relay.right_tx_packets > 0
    await asyncio.to_thread(picker.hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool["used"]


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
@pytest.mark.parametrize("mixed", [False, True, "browser_only"])
@pytest.mark.parametrize("via_service", [False, True])
async def test_sip_pickup_wins_a_ringing_group_without_redialing_caller(registered_phones, trunks, mixed, via_service):
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {"enabled": True}})
    members = [] if mixed == "browser_only" else ["transfer_b"]
    if mixed:
        runtime.runtime.endpoints.register(PhoneEndpoint(endpoint_id="group-browser", name="Group browser",
            device_id="group-browser-device", kind=EndpointKind.BROWSER, availability="available"))
        runtime.roster.append(RosterEntry("group-browser", metadata={"local_ha": True, "endpoint_id": "group-browser"}))
        members.append("group-browser")
    runtime.roster.append(RosterEntry("PickupGroup", metadata={"group_type": "ring", "members": members}))
    caller, ringing, picker = registered_phones
    async def wait_for(predicate):
        async with asyncio.timeout(6):
            while not predicate():
                await asyncio.sleep(.01)
    await asyncio.to_thread(caller.command, "/dial PickupGroup")
    await wait_for(lambda: any(runtime.calls.resource_for(s.call_id, "fork_answer") is not None for s in runtime.calls.sessions.values()))
    session = next(iter(runtime.calls.sessions.values()))
    from custom_components.voip_stack.dialog_info import snapshot
    await wait_for(lambda: any(item.state == "early" for item in snapshot(runtime.runtime, "group-browser" if mixed == "browser_only" else "transfer_b")))
    pickup_target = "group-browser" if mixed == "browser_only" else "transfer_b"
    if mixed == "browser_only":
        assert runtime.calls.resource_for(session.call_id, "fork_answer").attempts == []
    if via_service:
        runtime.runtime.endpoints.update("transfer_c", device_id="pickup-device")
        result = await runtime.hass.services.async_call("voip_stack", "pickup", {
            "device_id": "pickup-device", "target_device_id": pickup_target, "answer_timeout": 3,
        }, blocking=True, return_response=True)
        assert result["call_id"] == session.call_id
    else:
        await asyncio.to_thread(picker.dial, "pickup." + pickup_target)
    await wait_for(lambda: session.state == "in_call")
    assert caller.read().count("Call established") == 1
    assert "Call established" not in ringing.read()
    relay = runtime.calls.resource_for(session.call_id, "relay")
    await wait_for(lambda: relay.left_tx_packets >= 50 and relay.right_tx_packets >= 50)
    assert runtime.runtime.endpoints.get("transfer_b").active_call_id == ""
    await asyncio.to_thread(picker.hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool["used"]


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
async def test_early_replaces_matches_all_tags_before_picking_up(registered_phones, trunks):
    import time
    from .test_refer_correlation import Peer
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.sip_registrar import SipAccount, SipRegistration
    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {"enabled": True}})
    caller, _old, _unused = registered_phones
    await asyncio.to_thread(caller.command, "/dial transfer_b")
    async with asyncio.timeout(4):
        while not any(s.state == "remote_ringing" for s in runtime.calls.sessions.values()):
            await asyncio.sleep(.01)
    session = next(iter(runtime.calls.sessions.values()))
    client = runtime.calls.sip_client_for(runtime.calls.bridge_link_for(session.call_id))
    assert len(client.early_dialogs) == 1
    remote_tag = next(iter(client.early_dialogs))
    transport, peer = await runtime.hass.loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]
    registrar = runtime.calls.component("registrar")
    registrar.update_accounts([*registrar.accounts.values(), SipAccount("pickup-test", "Pickup test", "test-only")])
    registrar.registrations["pickup-test"] = SipRegistration(username="pickup-test", contact_uri=f"sip:pickup-test@127.0.0.1:{port}",
        source_host="127.0.0.1", source_port=port, transport="UDP", expires_at=time.time() + 60)
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    runtime.runtime.endpoints.register(PhoneEndpoint(endpoint_id="pickup-test", name="Pickup test", username="pickup-test", kind="sip_account"))
    from custom_components.voip_stack.roster import RosterEntry
    runtime.roster.append(RosterEntry("pickup-test", sip_uri=f"sip:pickup-test@127.0.0.1:{port}",
        metadata={"endpoint_id": "pickup-test", "registered": True, "endpoint_kind": "sip_account"}))
    target = ("127.0.0.1", runtime.runtime.transport_config["sip_port"])
    try:
        for index, tag in enumerate(("wrong-tag", client.dialog_ids.local_tag)):
            identity = f"early-pickup-{index}"
            headers = [("Via", f"SIP/2.0/UDP 127.0.0.1:{port};branch=z9hG4bKpick{index}"),
                ("From", "<sip:pickup-test@127.0.0.1>;tag=picker"), ("To", "<sip:transfer_b@127.0.0.1>"),
                ("Call-ID", identity), ("CSeq", "1 INVITE"), ("Contact", f"<sip:pickup-test@127.0.0.1:{port}>"),
                ("Content-Type", "application/sdp"), ("Require", "replaces"),
                ("Replaces", f"{client.dialog_ids.call_id};to-tag={tag};from-tag={remote_tag};early-only")]
            body = b"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=test\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 45000 RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=sendrecv\r\n"
            transport.sendto(sip.build_request("INVITE", "sip:transfer_b@127.0.0.1", headers, body), target)
            while True:
                response, address = await peer.receive()
                if response.header("Call-ID") == identity and response.is_response and response.status_code >= 200:
                    break
            assert response.status_code == (481 if index == 0 else 200)
            ack = [(key, "1 ACK" if key == "CSeq" else response.header("To") if key == "To" else value)
                   for key, value in headers if key not in {"Content-Type", "Require", "Replaces"}]
            transport.sendto(sip.build_request("ACK", "sip:HA@127.0.0.1", ack), address)
            if index == 0:
                assert session.live and client.dialog is None
        async with asyncio.timeout(4):
            while session.state != "in_call":
                assert session.live
                await asyncio.sleep(.01)
        assert runtime.calls.resolve_session_id("early-pickup-1") == session.call_id
        bye = [(key, "2 BYE" if key == "CSeq" else value) for key, value in ack]
        transport.sendto(sip.build_request("BYE", "sip:HA@127.0.0.1", bye), target)
        async with asyncio.timeout(4):
            while runtime.calls.sessions:
                await asyncio.sleep(.01)
        assert not runtime.runtime.rtp_port_pool["used"]
    finally:
        transport.close()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": ("transfer_b", "transfer_c")}], indirect=True)
@pytest.mark.parametrize("ending", ["cancel", "timeout"])
async def test_group_callback_failure_keeps_original_phone_ringing(trunks, registered_phones, ending):
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.dialog_info import snapshot
    runtime = trunks
    caller, original, _picker = registered_phones
    runtime.roster.append(RosterEntry("PickupGroup", metadata={"group_type": "ring", "members": ["transfer_b"]}))
    runtime.runtime.endpoints.update("transfer_c", device_id="pickup-device")
    async def wait_for(predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(.01)
    await asyncio.to_thread(caller.command, "/dial PickupGroup")
    await wait_for(lambda: any(item.state == "early" for item in snapshot(runtime.runtime, "transfer_b")))
    session = next(iter(runtime.calls.sessions.values()))
    task = asyncio.create_task(runtime.hass.services.async_call("voip_stack", "pickup", {
        "device_id": "pickup-device", "target_device_id": "transfer_b", "answer_timeout": 1,
    }, blocking=True, return_response=True))
    try:
        await wait_for(lambda: any(item.state == "early" for item in snapshot(runtime.runtime, "transfer_c")))
        if ending == "cancel":
            task.cancel()
        result = (await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3))[0]
        assert isinstance(result, (asyncio.CancelledError, TimeoutError))
        assert session.live and session.state == "ringing"
        assert not runtime.runtime.endpoints.get("transfer_c").active_call_id
        assert any(item.state == "early" for item in snapshot(runtime.runtime, "transfer_b"))
        await asyncio.to_thread(original.command, "/accept")
        await wait_for(lambda: session.state == "in_call")
        relay = runtime.calls.resource_for(session.call_id, "relay")
        await wait_for(lambda: relay.left_tx_packets > 5 and relay.right_tx_packets > 5)
        await asyncio.to_thread(caller.hangup)
        await wait_for(lambda: not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool["used"]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": ("transfer_b", "transfer_c")}], indirect=True)
@pytest.mark.parametrize("via_service", [False, True])
async def test_pickup_of_forwarded_group_uses_the_same_fork_owner(trunks, registered_phones, via_service):
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.dialog_info import snapshot
    runtime = trunks
    caller, picker, ringing = registered_phones
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {"enabled": True}})
    runtime.roster.append(RosterEntry("PickupGroup", metadata={"group_type": "ring", "members": ["transfer_c"]}))
    from custom_components.voip_stack.store import store_manual_roster_entries
    store_manual_roster_entries(runtime.hass, [runtime.roster[-1]])
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)
    await asyncio.to_thread(caller.command, "/dial transfer_b")
    await wait_for(lambda: any(item.state == "early" for item in snapshot(runtime.runtime, "transfer_b")))
    session = next(iter(runtime.calls.sessions.values()))
    await runtime.calls.forward_call(call_id=session.call_id, destination="PickupGroup", on_failure="resume")
    await wait_for(lambda: any(item.state == "early" for item in snapshot(runtime.runtime, "transfer_c")))
    await wait_for(lambda: not runtime.runtime.endpoints.get("transfer_b").active_call_id)
    if via_service:
        runtime.runtime.endpoints.update("transfer_b", device_id="pickup-device")
        pickup_task = asyncio.create_task(runtime.hass.services.async_call("voip_stack", "pickup", {
            "device_id": "pickup-device", "target_device_id": "transfer_c", "answer_timeout": 3,
        }, blocking=True, return_response=True))
        try:
            await wait_for(lambda: any(item.state == "early" for item in snapshot(runtime.runtime, "transfer_b")))
            await asyncio.to_thread(picker.command, "/accept")
            result = await pickup_task
        finally:
            pickup_task.cancel()
            await asyncio.gather(pickup_task, return_exceptions=True)
        assert result["call_id"] == session.call_id
    else:
        await asyncio.to_thread(picker.dial, "pickup.transfer_c")
    await wait_for(lambda: session.state == "in_call")
    assert caller.read().count("Call established") == 1
    assert "Call established" not in ringing.read()
    relay = runtime.calls.resource_for(session.call_id, "relay")
    await wait_for(lambda: relay.left_tx_packets >= 20 and relay.right_tx_packets >= 20)
    await asyncio.to_thread(picker.hangup)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool["used"]


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": ("transfer_b", "transfer_c")}], indirect=True)
@pytest.mark.parametrize("forwarded", [False, True])
async def test_browser_service_can_join_an_existing_sip_group(trunks, registered_phones, forwarded):
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    from custom_components.voip_stack.dialog_info import snapshot
    from custom_components.voip_stack.store import store_manual_roster_entries
    runtime = trunks
    caller, _old, ringing = registered_phones
    browser = PhoneEndpoint(endpoint_id="pickup-browser", device_id="pickup-browser-device", name="Pickup browser",
                            kind="browser", availability="available", capabilities=frozenset({"audio", "dtmf"}))
    runtime.runtime.endpoints.register(browser)
    group = RosterEntry("PickupGroup", metadata={"group_type": "ring", "members": ["transfer_c"]})
    runtime.roster.append(group)
    store_manual_roster_entries(runtime.hass, [group])
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)
    await asyncio.to_thread(caller.command, "/dial transfer_b" if forwarded else "/dial PickupGroup")
    await wait_for(lambda: bool(runtime.calls.sessions))
    session = next(iter(runtime.calls.sessions.values()))
    if forwarded:
        await wait_for(lambda: any(item.state == "early" for item in snapshot(runtime.runtime, "transfer_b")))
        await runtime.calls.forward_call(call_id=session.call_id, destination="PickupGroup", on_failure="resume")
    await wait_for(lambda: any(item.state == "early" for item in snapshot(runtime.runtime, "transfer_c")))
    result = await runtime.hass.services.async_call("voip_stack", "pickup", {
        "device_id": browser.device_id, "target_device_id": "transfer_c", "answer_timeout": 3,
        "media_client_id": "browser-pickup-owner",
    }, blocking=True, return_response=True)
    assert result["call_id"] == session.call_id
    await wait_for(lambda: "Call established" in caller.read())
    assert session.state == "in_call" and runtime.runtime.endpoints.get(browser.endpoint_id).active_call_id == session.call_id
    assert "Call established" not in ringing.read()
    await runtime.hass.services.async_call("voip_stack", "hangup", {"device_id": browser.device_id}, blocking=True)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool["used"]


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
async def test_cancelling_callback_waiter_cannot_close_the_selected_fork_leg(trunks, registered_phones, monkeypatch):
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.dialog_info import snapshot
    from custom_components.voip_stack.dial_fork import ForkAnswerGate
    runtime = trunks
    from custom_components.voip_stack.websocket_api import CALL_EVENT
    events = []
    detach = runtime.hass.bus.async_listen(CALL_EVENT, lambda event: events.append(event.data))
    caller, _original, picker = registered_phones
    runtime.roster.append(RosterEntry("PickupGroup", metadata={"group_type": "ring", "members": ["transfer_b"]}))
    runtime.runtime.endpoints.update("transfer_c", device_id="pickup-device")
    async def wait_for(predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(.01)
    original_confirm = ForkAnswerGate.confirm
    async def confirm(gate):
        result = await original_confirm(gate)
        task.cancel()
        return result
    monkeypatch.setattr(ForkAnswerGate, "confirm", confirm)
    await asyncio.to_thread(caller.command, "/dial PickupGroup")
    await wait_for(lambda: any(item.state == "early" for item in snapshot(runtime.runtime, "transfer_b")))
    session = next(iter(runtime.calls.sessions.values()))
    task = asyncio.create_task(runtime.hass.services.async_call("voip_stack", "pickup", {
        "device_id": "pickup-device", "target_device_id": "transfer_b", "answer_timeout": 3,
    }, blocking=True, return_response=True))
    try:
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        assert isinstance(result, asyncio.CancelledError)
        await wait_for(lambda: session.state == "in_call")
        relay = runtime.calls.resource_for(session.call_id, "relay")
        await wait_for(lambda: relay.left_tx_packets > 20 and relay.right_tx_packets > 20)
        assert caller.read().count("Call established") == 1
        assert sum(event.get("event_type") == "picked_up" for event in events) == 1
        await asyncio.to_thread(picker.hangup)
        await wait_for(lambda: not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool["used"]
    finally:
        detach()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
async def test_pickup_rollback_survives_repeated_cancellation(trunks, registered_phones, monkeypatch):
    from custom_components.voip_stack.sip_rtp_bridge import SipRtpRelay
    runtime = trunks
    caller, original, picker = registered_phones
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {"enabled": True}})
    async def wait_for(predicate):
        async with asyncio.timeout(4):
            while not predicate():
                await asyncio.sleep(.01)
    await asyncio.to_thread(caller.command, "/dial transfer_b")
    await wait_for(lambda: any(s.state == "remote_ringing" for s in runtime.calls.sessions.values()))
    session = next(iter(runtime.calls.sessions.values()))
    ports = set(runtime.runtime.rtp_port_pool['used'])
    entered, stopping, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    preparation = relay_owner = None
    original_start, original_stop = SipRtpRelay.start, SipRtpRelay.stop
    async def start(relay):
        nonlocal preparation, relay_owner
        await original_start(relay)
        if relay_owner is None:
            relay_owner = relay
            preparation = asyncio.current_task()
            entered.set()
            await asyncio.Event().wait()
    async def stop(relay):
        if relay is relay_owner:
            stopping.set()
            await release.wait()
        await original_stop(relay)
    monkeypatch.setattr(SipRtpRelay, "start", start)
    monkeypatch.setattr(SipRtpRelay, "stop", stop)
    try:
        await asyncio.to_thread(picker.command, "/dial pickup.transfer_b")
        await asyncio.wait_for(entered.wait(), 3)
        preparation.cancel()
        await asyncio.wait_for(stopping.wait(), 3)
        preparation.cancel()
        release.set()
        await asyncio.wait_for(asyncio.gather(preparation, return_exceptions=True), 3)
        assert runtime.runtime.rtp_port_pool['used'] == ports
        assert runtime.calls.resource_for(session.call_id, "pickup") is None
        assert not runtime.runtime.endpoints.get('transfer_c').active_call_id
        assert session.live
        await asyncio.to_thread(original.command, '/accept')
        await wait_for(lambda: session.state == 'in_call')
        relay = runtime.calls.resource_for(session.call_id, 'relay')
        await wait_for(lambda: relay.left_tx_packets > 10 and relay.right_tx_packets > 10)
        await asyncio.to_thread(caller.hangup)
        await wait_for(lambda: not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool['used']
    finally:
        release.set()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
async def test_declined_browser_pickup_preserves_real_group_and_next_call(trunks, registered_phones):
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    from custom_components.voip_stack.phone_state import phone_states
    from custom_components.voip_stack.roster import RosterEntry
    from homeassistant.exceptions import ServiceValidationError

    runtime = trunks
    caller = registered_phones[0]
    browsers = []
    for name in ("group-a", "group-b"):
        endpoint = PhoneEndpoint(endpoint_id=name, name=name, device_id=name + "-device",
                                 kind="browser", availability="available", capabilities=frozenset({"audio", "dtmf"}))
        runtime.runtime.endpoints.register(endpoint)
        runtime.roster.append(RosterEntry(name, metadata={"local_ha": True, "endpoint_id": name}))
        browsers.append(endpoint)
    runtime.roster.append(RosterEntry("BrowserGroup", metadata={"group_type": "ring", "members": [e.name for e in browsers]}))
    a, b = browsers

    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.01)

    await asyncio.to_thread(caller.command, "/dial BrowserGroup")
    await wait_for(lambda: all(phone_states(runtime.hass)[e.endpoint_id]["state"] == "ringing" for e in browsers))
    session = next(iter(runtime.calls.sessions.values()))
    route = session.artifacts.pending_route
    await runtime.hass.services.async_call("voip_stack", "decline", {
        "device_id": a.device_id, "call_id": session.call_id,
    }, blocking=True)
    await wait_for(lambda: phone_states(runtime.hass)[a.endpoint_id]["state"] == "idle")
    members = route["ring_group_endpoint_ids"]
    for _ in range(3):
        with pytest.raises(ServiceValidationError):
            await runtime.hass.services.async_call("voip_stack", "pickup", {
                "device_id": a.device_id, "target_device_id": b.device_id,
                "call_id": session.call_id, "media_client_id": "test-a", "answer_timeout": 1,
            }, blocking=True, return_response=True)
        assert phone_states(runtime.hass)[a.endpoint_id]["state"] == "idle"
        assert phone_states(runtime.hass)[b.endpoint_id]["state"] == "ringing"
        assert route["ring_group_endpoint_ids"] == members
        assert a.endpoint_id not in session.endpoint_claims
        assert not route["future"].done()
    await runtime.hass.services.async_call("voip_stack", "answer", {
        "device_id": b.device_id, "call_id": session.call_id, "media_client_id": "test-b",
    }, blocking=True)
    await wait_for(lambda: session.state == "in_call" and "Call established" in caller.read())
    await runtime.hass.services.async_call("voip_stack", "hangup", {"device_id": b.device_id}, blocking=True)
    await wait_for(lambda: not runtime.calls.sessions)
    await asyncio.to_thread(caller.command, "/dial " + a.name)
    await wait_for(lambda: phone_states(runtime.hass)[a.endpoint_id]["state"] == "ringing")
    await runtime.hass.services.async_call("voip_stack", "answer", {
        "device_id": a.device_id, "media_client_id": "test-a",
    }, blocking=True)
    await wait_for(lambda: caller.read().count("Call established") == 2)
    await runtime.hass.services.async_call("voip_stack", "hangup", {"device_id": a.device_id}, blocking=True)
    await wait_for(lambda: not runtime.calls.sessions)
    assert not runtime.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
async def test_route_forward_invalidates_real_sip_pickup_before_ordinary_answer_commit(
    trunks, registered_phones, monkeypatch
):
    from custom_components.voip_stack.dial_fork import ForkAnswerGate
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    from custom_components.voip_stack.roster import RosterEntry

    runtime = trunks
    entry = config_entry(runtime.hass)
    runtime.hass.config_entries.async_update_entry(entry, data={**entry.data, "parking": {"enabled": True}})
    caller, _, picker = registered_phones
    ordinary, alternate = runtime.phones
    ordinary.auto_answer = False
    for index, phone in enumerate(runtime.phones):
        name = f"bedroom{index}"
        runtime.runtime.endpoints.update("sip:" + name, availability="available")
        runtime.roster.append(RosterEntry(name,
            sip_uri=f"sip:{name}@127.0.0.1:{phone.transport.get_extra_info('sockname')[1]}",
            metadata={"endpoint_id": "sip:" + name, "endpoint_kind": "sip_account", "registered": True}))
    browser = PhoneEndpoint(endpoint_id="route-browser", name="Route browser", device_id="route-browser-device",
                            kind="browser", availability="available", capabilities=frozenset({"audio", "dtmf"}))
    runtime.runtime.endpoints.register(browser)
    runtime.roster.append(RosterEntry(browser.name, metadata={"local_ha": True, "endpoint_id": browser.endpoint_id}))
    runtime.roster.append(RosterEntry("RouteGroup", metadata={"group_type": "ring", "members": ["bedroom0", browser.name]}))
    from custom_components.voip_stack.store import store_manual_roster_entries
    store_manual_roster_entries(runtime.hass, runtime.roster)
    events = []
    commands = []
    tickets = []
    original_submit = ForkAnswerGate.submit
    route_requested = runtime.hass.loop.create_future()
    session = None
    route = None

    def submit(gate, endpoint_id):
        ticket = original_submit(gate, endpoint_id)
        tickets.append(ticket)
        def selected(done):
            if done.cancelled() or not done.result():
                return
            # HA starts jobs eagerly by default. This callback only issues the
            # public command and the peer's real UDP answer; it does not delay
            # the controller or replace prepare_sip_pickup/validate/rollback.
            commands.append(runtime.hass.async_create_task(runtime.hass.services.async_call(
                "voip_stack", "forward", {"device_id": browser.device_id,
                    "call_id": session.call_id, "destination": "bedroom1"}, blocking=True)))
            route_requested.set_result(route["future"].done())
            ordinary.answer()
        ticket.selected.add_done_callback(selected)
        return ticket

    monkeypatch.setattr(ForkAnswerGate, "submit", submit)
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.001)
    try:
        await asyncio.to_thread(caller.command, "/dial RouteGroup")
        await asyncio.wait_for(ordinary.invited.wait(), 5)
        session = next(iter(runtime.calls.sessions.values()))
        route = session.artifacts.pending_route
        await wait_for(lambda: runtime.calls.resource_for(session.call_id, "fork_answer") is not None)
        # The peer's INVITE event only confirms that it sent 180. Wait until
        # the PBX has consumed that progress and published this leg as ringing;
        # otherwise the pickup can legitimately fail before testing arbitration.
        await wait_for(lambda: any(
            leg.endpoint_id == "sip:bedroom0" and leg.state == "remote_ringing"
            for leg in session.legs.values()
        ))
        controls = [t for t in asyncio.all_tasks() if t.get_name() == "voip-dial-fork-control-browser:route-control"]
        branches = [t for t in asyncio.all_tasks() if t.get_name() == "voip-dial-fork-branches"]
        assert len(controls) == len(branches) == 1
        controls[0].add_done_callback(lambda _: events.append("route_read"))
        branches[0].add_done_callback(lambda t: events.append("ordinary_answered" if not t.cancelled() and t.result().winner else "ordinary_closed"))
        await asyncio.to_thread(picker.command, "/dial pickup.bedroom0")
        assert await asyncio.wait_for(route_requested, 5), "real forward must be accepted while confirmation is pending"
        await wait_for(lambda: commands[0].done() or session.state == "in_call")
        assert session.metadata.get("dest_endpoint_id") != "sip:bedroom0", (
            "the old SIP answer must not commit after an accepted forward"
        )
        await asyncio.wait_for(commands[0], 6)
        await wait_for(lambda: alternate.invited.is_set() and session.state == "in_call")
        assert session.metadata["dest_endpoint_id"] == "sip:bedroom1"
        assert not tickets[0].accepted
        assert runtime.calls.resource_for(session.call_id, "pickup") is None
        assert runtime.calls.resource_for(session.call_id, "fork_answer") is None
        assert events.index("route_read") < events.index("ordinary_answered")
        assert ordinary.bye.is_set() and len(alternate.invites) == 1
        assert caller.read().count("Call established") == 1
        await asyncio.to_thread(caller.hangup)
        await wait_for(lambda: not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool.get("used")
        # Immediate next call uses the same caller and destination normally.
        await asyncio.to_thread(caller.command, "/dial bedroom1")
        await wait_for(lambda: len(alternate.invites) == 2 and caller.read().count("Call established") == 2)
        await asyncio.to_thread(caller.hangup)
        await wait_for(lambda: not runtime.calls.sessions)
        assert not runtime.runtime.rtp_port_pool.get("used")
        assert not any(
            task.get_name().startswith(("voip-dial-fork", "voip-fork-confirm"))
            for task in asyncio.all_tasks() if not task.done()
        )
    finally:
        for command in commands:
            if not command.done():
                command.cancel()
        await asyncio.gather(*commands, return_exceptions=True)
        for peer in registered_phones:
            await asyncio.to_thread(peer.command, "/hangupall")
