#!/usr/bin/env python3
"""Conference mixer contract tests."""

from __future__ import annotations

import asyncio
from array import array
import importlib.util
import socket
import sys
import types
import unittest
from pathlib import Path

import voluptuous as vol


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


class _FakeUnauthorized(Exception):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args)
        self.details = kwargs


def _install_ha_fakes() -> None:
    ha = sys.modules.get("homeassistant")
    if ha is None:
        ha = types.ModuleType("homeassistant")
        sys.modules["homeassistant"] = ha
    if not hasattr(ha, "__path__"):
        ha.__path__ = []

    components = sys.modules.setdefault("homeassistant.components", types.ModuleType("homeassistant.components"))
    if not hasattr(components, "__path__"):
        components.__path__ = []
    helpers = sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
    if not hasattr(helpers, "__path__"):
        helpers.__path__ = []
    core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
    exceptions = sys.modules.setdefault(
        "homeassistant.exceptions", types.ModuleType("homeassistant.exceptions")
    )
    config_entries = sys.modules.setdefault("homeassistant.config_entries", types.ModuleType("homeassistant.config_entries"))
    device_registry = sys.modules.setdefault("homeassistant.helpers.device_registry", types.ModuleType("homeassistant.helpers.device_registry"))
    entity_registry = sys.modules.setdefault("homeassistant.helpers.entity_registry", types.ModuleType("homeassistant.helpers.entity_registry"))
    websocket_api = sys.modules.setdefault("homeassistant.components.websocket_api", types.ModuleType("homeassistant.components.websocket_api"))
    sys.modules.setdefault("voluptuous", vol)

    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    core.ServiceCall = getattr(core, "ServiceCall", type("ServiceCall", (), {}))
    core.callback = getattr(core, "callback", lambda fn: fn)
    exceptions.Unauthorized = getattr(
        exceptions, "Unauthorized", _FakeUnauthorized
    )
    exceptions.UnknownUser = getattr(exceptions, "UnknownUser", _FakeUnauthorized)
    config_entries.ConfigEntry = getattr(config_entries, "ConfigEntry", type("ConfigEntry", (), {}))
    config_entries.ConfigSubentry = getattr(
        config_entries, "ConfigSubentry", type("ConfigSubentry", (), {})
    )
    device_registry.async_get = getattr(device_registry, "async_get", lambda _hass: None)
    entity_registry.async_get = getattr(entity_registry, "async_get", lambda _hass: None)
    websocket_api.async_register_command = getattr(websocket_api, "async_register_command", lambda *args, **kwargs: None)
    websocket_api.websocket_command = getattr(websocket_api, "websocket_command", lambda _schema: (lambda fn: fn))
    websocket_api.async_response = getattr(websocket_api, "async_response", lambda fn: fn)
    vol.Required = getattr(vol, "Required", lambda key: key)
    vol.Optional = getattr(vol, "Optional", lambda key, default=None: key)


def _load_module(name: str):
    _install_ha_fakes()
    if "custom_components" not in sys.modules:
        root_pkg = types.ModuleType("custom_components")
        root_pkg.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root_pkg
    if PKG_NAME not in sys.modules:
        pkg = types.ModuleType(PKG_NAME)
        pkg.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = pkg
    full_name = f"{PKG_NAME}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(full_name, None)
        raise
    return module


conference = _load_module("conference")
endpoint_lifecycle = _load_module("endpoint_lifecycle")


def _test_runtime_component(hass, key):
    return hass.data.get("voip_stack", {}).get(key)


conference.conference_component = lambda hass: _test_runtime_component(
    hass, "conference_manager"
)
endpoint_lifecycle.conference_component = lambda hass: _test_runtime_component(
    hass, "conference_manager"
)
endpoint_lifecycle.sip_endpoint_manager = lambda hass: _test_runtime_component(
    hass, "sip_endpoint"
)
endpoint_registry_module = _load_module("endpoint_registry")
phone_endpoint = _load_module("phone_endpoint")
runtime_data_module = _load_module("runtime_data")
pbx_runtime_module = _load_module("pbx_runtime")
trunk_runtime = _load_module("trunk_runtime")
trunk_runtime.sip_trunk = lambda hass: _test_runtime_component(hass, "sip_trunk")
sip_listener = _load_module("sip_listener")
sip_client = _load_module("sip_client")
sdp = _load_module("sdp")
rtp = _load_module("rtp")
voip_sip = _load_module("sip")
const = _load_module("const")


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def async_fire(self, event_type: str, event: dict) -> None:
        self.events.append((event_type, dict(event)))


class _FakeHass:
    def __init__(self) -> None:
        endpoints = endpoint_registry_module.EndpointRegistry()
        endpoints.register(
            phone_endpoint.PhoneEndpoint(
                endpoint_id="default",
                name="HA",
                kind=phone_endpoint.EndpointKind.BROWSER,
                device_id="ha-softphone",
                availability=phone_endpoint.EndpointAvailability.AVAILABLE,
            )
        )
        self.data: dict = {const.DOMAIN: {"transport_config": {"sip_port": 5060, "rtp_port": _free_rtp_base()}}}
        self.config = types.SimpleNamespace(location_name="HA")
        self.bus = _FakeBus()
        self.runtime = types.SimpleNamespace(
            tasks=set(),
            calls=None,
            endpoints=endpoints,
            sip=None,
            shutdown_task=None,
            rtp_port_pool={},
            next_rtp_port=0,
            softphones={},
            softphone_presence={},
            media=types.SimpleNamespace(
                sessions_for=lambda _channel: {},
                owners_for=lambda _channel: {},
                identity_locks={},
                transcoder=None,
            ),
            call_artifacts=types.SimpleNamespace(
                forward_tasks={},
                forward_claims=set(),
                deadlines={},
                trunk_info_queues={},
                trunk_closed_calls=set(),
            ),
        )

    def async_create_task(self, coro):
        return asyncio.create_task(coro)


def _test_runtime(hass):
    return hass.runtime


runtime_data_module.require_runtime_data = _test_runtime
runtime_data_module.runtime_data = _test_runtime
endpoint_lifecycle.require_runtime_data = _test_runtime


def _free_rtp_base() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    finally:
        sock.close()
    base = max(10000, port - 2)
    return base if base % 2 == 0 else base - 1


def _frame(value: int) -> bytes:
    return array("h", [value] * (conference.CONFERENCE_FRAME_BYTES // 2)).tobytes()


def _first_sample(frame: bytes) -> int:
    pcm = array("h")
    pcm.frombytes(frame[:2])
    return pcm[0]


def _install_browser_endpoints(hass: _FakeHass, *endpoint_ids: str):
    registry = endpoint_registry_module.EndpointRegistry()
    for endpoint_id in endpoint_ids:
        registry.register(
            phone_endpoint.PhoneEndpoint(
                endpoint_id=endpoint_id,
                name=endpoint_id.title(),
                kind=phone_endpoint.EndpointKind.BROWSER,
                device_id=f"device-{endpoint_id}",
                availability=phone_endpoint.EndpointAvailability.AVAILABLE,
                capabilities={"audio", "video"},
            )
        )
    hass.runtime.endpoints = registry
    endpoint_lifecycle.call_registry(hass).bind_endpoint_registry(registry)
    return registry


async def _wait_for_sample(queue: asyncio.Queue[bytes], expected: int) -> bytes:
    async with asyncio.timeout(1.0):
        while True:
            frame = await queue.get()
            if _first_sample(frame) == expected:
                return frame


class ConferenceMixerTest(unittest.TestCase):
    def test_mix_frames_is_n_minus_one(self) -> None:
        out = conference.mix_frames([_frame(1000), _frame(2000), _frame(-500)])
        self.assertEqual([_first_sample(frame) for frame in out], [1500, 500, 3000])

    def test_mix_frames_scales_only_when_sum_exceeds_int16(self) -> None:
        out = conference.mix_frames([_frame(30000), _frame(30000), _frame(30000)])
        self.assertEqual([_first_sample(frame) for frame in out], [32767, 32767, 32767])

    def test_mix_frames_clips_two_participant_sum(self) -> None:
        out = conference.mix_frames([_frame(32767), _frame(-32768)])
        self.assertEqual([_first_sample(frame) for frame in out], [-32768, 32767])

    def test_mix_frames_single_talker_is_unity_gain(self) -> None:
        out = conference.mix_frames([_frame(30000), _frame(0), _frame(0)])
        self.assertEqual([_first_sample(frame) for frame in out], [0, 30000, 30000])

    def test_bad_length_is_silence(self) -> None:
        out = conference.mix_frames([_frame(1000), b""])
        self.assertEqual([_first_sample(frame) for frame in out], [0, 1000])


class ConferenceRuntimeTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _invite(*, call_id: str = "source-call"):
        fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
        return sip_listener.SipInvite(
            source_host="127.0.0.1",
            source_port=5060,
            request_uri=voip_sip.parse_sip_uri(
                "sip:Conference@127.0.0.1"
            ),
            caller_uri=voip_sip.parse_sip_uri("sip:Door@127.0.0.1"),
            target="Conference",
            caller="Door",
            call_id=call_id,
            cseq="1 INVITE",
            remote_sdp=b"",
            send_format=fmt,
            recv_format=fmt,
            remote_rtp_host="127.0.0.1",
            remote_rtp_port=45690,
        )

    async def test_conference_reservation_failure_rolls_back_prior_phone(self) -> None:
        hass = _FakeHass()
        endpoints = _install_browser_endpoints(hass, "kitchen", "hall")
        manager = conference.ConferenceManager(hass, local_ip="127.0.0.1")
        original_reserve = manager._reserve_ha_call
        reservations = 0

        def fail_second_reservation(*args, **kwargs):
            nonlocal reservations
            reservations += 1
            if reservations == 2:
                raise RuntimeError("simulated reservation failure")
            return original_reserve(*args, **kwargs)

        manager._reserve_ha_call = fail_second_reservation
        with self.assertRaisesRegex(RuntimeError, "reservation failure"):
            await manager.join(
                self._invite(),
                types.SimpleNamespace(name="Conference", id="Conference"),
                ring_endpoint_ids=("kitchen", "hall"),
            )

        registry = endpoint_lifecycle.call_registry(hass)
        self.assertFalse(manager.rooms)
        self.assertFalse(manager.ha_calls)
        self.assertFalse(registry.sessions)
        self.assertFalse(registry.endpoint_claims)
        self.assertFalse(endpoints.require("kitchen").active_call_id)
        self.assertFalse(endpoints.require("hall").active_call_id)

    async def test_conference_state_publication_failure_releases_all_claims(self) -> None:
        hass = _FakeHass()
        endpoints = _install_browser_endpoints(hass, "kitchen", "hall")
        manager = conference.ConferenceManager(hass, local_ip="127.0.0.1")
        room = conference.ConferenceRoom(
            hass,
            name="Conference",
            local_ip="127.0.0.1",
        )
        manager.rooms[room.name] = room
        original_publish = room._set_softphone_ringing
        publications = 0

        def fail_second_publication(*args, **kwargs):
            nonlocal publications
            publications += 1
            if publications == 2:
                raise RuntimeError("simulated state publication failure")
            return original_publish(*args, **kwargs)

        room._set_softphone_ringing = fail_second_publication
        with self.assertRaisesRegex(RuntimeError, "state publication failure"):
            manager.ring_ha_endpoints(
                "Conference",
                ("kitchen", "hall"),
                caller="Door",
            )

        registry = endpoint_lifecycle.call_registry(hass)
        self.assertFalse(manager.ha_calls)
        self.assertFalse(registry.sessions)
        self.assertFalse(registry.endpoint_claims)
        self.assertFalse(room._ha_softphone_announced)
        self.assertFalse(endpoints.require("kitchen").active_call_id)
        self.assertFalse(endpoints.require("hall").active_call_id)
        self.assertEqual(
            hass.runtime.softphones["kitchen"]["state"],
            "idle",
        )

    async def test_conference_leg_failure_does_not_leave_empty_room_or_claim(self) -> None:
        hass = _FakeHass()
        endpoints = _install_browser_endpoints(hass, "kitchen")
        manager = conference.ConferenceManager(hass, local_ip="127.0.0.1")
        room = conference.ConferenceRoom(
            hass,
            name="Conference",
            local_ip="127.0.0.1",
        )
        manager.rooms[room.name] = room

        def fail_add(*args, **kwargs):
            raise RuntimeError("simulated HA leg failure")

        room.add_ha_softphone_leg = fail_add
        with self.assertRaisesRegex(RuntimeError, "HA leg failure"):
            manager.start_ha_softphone("Conference", endpoint_id="kitchen")

        registry = endpoint_lifecycle.call_registry(hass)
        self.assertFalse(manager.rooms)
        self.assertFalse(manager.ha_calls)
        self.assertFalse(registry.sessions)
        self.assertFalse(registry.endpoint_claims)
        self.assertFalse(endpoints.require("kitchen").active_call_id)

    async def test_media_timeout_respects_direction_hold_and_ha_listener(self) -> None:
        hass = _FakeHass()
        room = conference.ConferenceRoom(hass, name="Conference", local_ip="127.0.0.1")
        now = 100.0

        def leg(**updates):
            values = {
                "call_id": "leg",
                "caller": "Desk",
                "role": "manual",
                "remote_host": "127.0.0.1",
                "remote_port": 40000,
                "in_converter": conference.PcmFrameConverter(
                    conference.CONFERENCE_FORMAT, conference.CONFERENCE_FORMAT
                ),
                "out_converter": conference.PcmFrameConverter(
                    conference.CONFERENCE_FORMAT, conference.CONFERENCE_FORMAT
                ),
                "last_rx": now - conference.CONFERENCE_INACTIVITY_S - 1,
            }
            values.update(updates)
            return conference._ConferenceLeg(**values)

        self.assertTrue(room._rx_inactivity_expired(leg(can_receive=True), now))
        self.assertFalse(room._rx_inactivity_expired(leg(can_receive=False), now))
        self.assertFalse(
            room._rx_inactivity_expired(
                leg(can_receive=True, connection_held=True), now
            )
        )
        self.assertFalse(
            room._rx_inactivity_expired(
                leg(can_receive=True, local_out=asyncio.Queue()), now
            )
        )

    async def test_legacy_connection_hold_suppresses_conference_tx_and_keeps_clock(self) -> None:
        hass = _FakeHass()
        room = conference.ConferenceRoom(hass, name="Conference", local_ip="127.0.0.1")
        fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
        sent: list[tuple[bytes, tuple[str, int]]] = []
        leg = conference._ConferenceLeg(
            call_id="held",
            caller="Desk",
            role="manual",
            remote_host="127.0.0.1",
            remote_port=40000,
            in_converter=conference.PcmFrameConverter(
                conference.CONFERENCE_FORMAT, conference.CONFERENCE_FORMAT
            ),
            out_converter=conference.PcmFrameConverter(
                conference.CONFERENCE_FORMAT, conference.CONFERENCE_FORMAT
            ),
            transport=types.SimpleNamespace(
                sendto=lambda packet, addr: sent.append((packet, addr))
            ),
            encoder=conference.RtpPayloadEncoder(fmt),
            can_receive=True,
            can_send=False,
            connection_held=True,
            timestamp=100,
        )
        room.legs[leg.call_id] = leg

        task = asyncio.create_task(room._mix_loop())
        await asyncio.sleep(0.03)
        room.legs.clear()
        await asyncio.wait_for(task, timeout=1)

        self.assertEqual(sent, [])
        self.assertEqual(leg.tx_packets, 0)
        self.assertGreater(leg.tx_suppressed, 0)
        self.assertGreater(leg.timestamp, 100)

    async def test_mixer_advances_negotiated_rtp_clock_when_one_send_fails(
        self,
    ) -> None:
        hass = _FakeHass()
        room = conference.ConferenceRoom(hass, name="Conference", local_ip="127.0.0.1")
        fmt = sdp.RtpPcmFormat(9, "G722", 8000, 1, 20)

        class FailingEncoder:
            def __init__(self) -> None:
                self.fmt = fmt

            def encode(self, _frame: bytes) -> bytes:
                # End the loop after this one mixer quantum.
                room.legs.clear()
                raise RuntimeError("simulated UDP send path failure")

        leg = conference._ConferenceLeg(
            call_id="drop",
            caller="Desk",
            role="manual",
            remote_host="127.0.0.1",
            remote_port=40000,
            in_converter=conference.PcmFrameConverter(conference.CONFERENCE_FORMAT, conference.CONFERENCE_FORMAT),
            out_converter=conference.PcmFrameConverter(conference.CONFERENCE_FORMAT, conference.CONFERENCE_FORMAT),
            transport=types.SimpleNamespace(sendto=lambda *_args: None),
            encoder=FailingEncoder(),
            timestamp=100,
        )
        room.legs[leg.call_id] = leg

        await room._mix_loop()

        self.assertEqual(leg.timestamp, 100 + 160)

    async def test_leg_disposal_closes_client_even_when_terminate_fails(self) -> None:
        hass = _FakeHass()
        room = conference.ConferenceRoom(hass, name="Conference", local_ip="127.0.0.1")
        calls: list[str] = []

        class Client:
            async def terminate(self) -> None:
                calls.append("terminate")
                raise RuntimeError("signaling path failed")

            async def close(self) -> None:
                calls.append("close")

        class Reservation:
            def release(self) -> None:
                calls.append("release")

        leg = conference._ConferenceLeg(
            call_id="outbound",
            caller="Desk",
            role="manual",
            remote_host="127.0.0.1",
            remote_port=40000,
            in_converter=conference.PcmFrameConverter(conference.CONFERENCE_FORMAT, conference.CONFERENCE_FORMAT),
            out_converter=conference.PcmFrameConverter(conference.CONFERENCE_FORMAT, conference.CONFERENCE_FORMAT),
            client=Client(),
            port_reservation=Reservation(),
        )

        await room._dispose_leg(leg, reason="local_hangup")

        self.assertEqual(calls, ["terminate", "close", "release"])

    async def test_inbound_media_timeout_signals_before_releasing_rtp_port(self) -> None:
        hass = _FakeHass()
        calls: list[str] = []

        async def on_timeout(call_id: str, reason: str) -> None:
            calls.append(f"signal:{call_id}:{reason}")

        room = conference.ConferenceRoom(
            hass,
            name="Conference",
            local_ip="127.0.0.1",
            on_inbound_timeout=on_timeout,
        )

        class Reservation:
            def release(self) -> None:
                calls.append("release")

        leg = conference._ConferenceLeg(
            call_id="inbound",
            caller="Desk",
            role="manual",
            remote_host="127.0.0.1",
            remote_port=40000,
            in_converter=conference.PcmFrameConverter(
                conference.CONFERENCE_FORMAT, conference.CONFERENCE_FORMAT
            ),
            out_converter=conference.PcmFrameConverter(
                conference.CONFERENCE_FORMAT, conference.CONFERENCE_FORMAT
            ),
            port_reservation=Reservation(),
        )

        await room._dispose_leg(leg, reason="media_timeout")

        self.assertEqual(calls, ["signal:inbound:media_timeout", "release"])

    async def test_cancelled_room_close_finishes_client_and_releases_port(self) -> None:
        hass = _FakeHass()
        room = conference.ConferenceRoom(hass, name="Conference", local_ip="127.0.0.1")
        terminate_entered = asyncio.Event()
        release_terminate = asyncio.Event()
        calls: list[str] = []

        class Client:
            async def terminate(self) -> None:
                calls.append("terminate")
                terminate_entered.set()
                await release_terminate.wait()

            async def close(self) -> None:
                calls.append("close")

        class Reservation:
            def release(self) -> None:
                calls.append("release")

        class Transport:
            def close(self) -> None:
                calls.append("transport")

        room.legs["outbound"] = conference._ConferenceLeg(
            call_id="outbound",
            caller="Desk",
            role="manual",
            remote_host="127.0.0.1",
            remote_port=40000,
            in_converter=conference.PcmFrameConverter(
                conference.CONFERENCE_FORMAT, conference.CONFERENCE_FORMAT
            ),
            out_converter=conference.PcmFrameConverter(
                conference.CONFERENCE_FORMAT, conference.CONFERENCE_FORMAT
            ),
            transport=Transport(),
            client=Client(),
            port_reservation=Reservation(),
        )

        close_task = asyncio.create_task(room.close(reason="local_hangup"))
        await asyncio.wait_for(terminate_entered.wait(), timeout=1)
        self.assertEqual(calls[:2], ["transport", "terminate"])
        self.assertNotIn("release", calls)
        close_task.cancel()
        await asyncio.sleep(0)
        close_task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(close_task.done())

        release_terminate.set()
        with self.assertRaises(asyncio.CancelledError):
            await close_task

        self.assertEqual(calls, ["transport", "terminate", "close", "release"])
        self.assertTrue(room._closed)
        self.assertFalse(room.legs)

    async def test_manager_rejects_new_rooms_while_close_is_in_progress(self) -> None:
        hass = _FakeHass()
        manager = conference.ConferenceManager(hass, local_ip="127.0.0.1")
        room = conference.ConferenceRoom(
            hass,
            name="Existing",
            local_ip="127.0.0.1",
        )
        manager.rooms[room.name] = room
        terminate_entered = asyncio.Event()
        release_terminate = asyncio.Event()

        class Client:
            async def terminate(self) -> None:
                terminate_entered.set()
                await release_terminate.wait()

            async def close(self) -> None:
                return

        room.legs["existing"] = conference._ConferenceLeg(
            call_id="existing",
            caller="Desk",
            role="manual",
            remote_host="127.0.0.1",
            remote_port=40000,
            in_converter=conference.PcmFrameConverter(
                conference.CONFERENCE_FORMAT,
                conference.CONFERENCE_FORMAT,
            ),
            out_converter=conference.PcmFrameConverter(
                conference.CONFERENCE_FORMAT,
                conference.CONFERENCE_FORMAT,
            ),
            client=Client(),
        )

        close_task = asyncio.create_task(manager.close())
        await asyncio.wait_for(terminate_entered.wait(), timeout=1)
        self.assertIsNone(manager.start_ha_softphone("New"))
        self.assertNotIn("New", manager.rooms)
        release_terminate.set()
        await asyncio.wait_for(close_task, timeout=1)

        self.assertTrue(manager._closed)
        self.assertFalse(manager.rooms)

    async def test_room_that_closes_itself_is_removed_from_manager(self) -> None:
        hass = _FakeHass()
        manager = conference.conference_manager(hass, local_ip="127.0.0.1")
        room = conference.ConferenceRoom(hass, name="Transient", local_ip="127.0.0.1")
        manager.rooms[room.name] = room

        await room.close(reason="idle")

        self.assertNotIn(room.name, manager.rooms)

    async def test_manager_close_waits_for_mixer_and_terminates_client_legs(self) -> None:
        hass = _FakeHass()
        manager = conference.ConferenceManager(hass, local_ip="127.0.0.1")
        room = conference.ConferenceRoom(hass, name="Conference", local_ip="127.0.0.1")
        manager.rooms[room.name] = room
        calls: list[str] = []

        class Client:
            async def terminate(self) -> None:
                calls.append("terminate")

            async def close(self) -> None:
                calls.append("close")

        class Reservation:
            def release(self) -> None:
                calls.append("release")

        room.legs["outbound"] = conference._ConferenceLeg(
            call_id="outbound",
            caller="Desk",
            role="manual",
            remote_host="127.0.0.1",
            remote_port=40000,
            in_converter=conference.PcmFrameConverter(conference.CONFERENCE_FORMAT, conference.CONFERENCE_FORMAT),
            out_converter=conference.PcmFrameConverter(conference.CONFERENCE_FORMAT, conference.CONFERENCE_FORMAT),
            client=Client(),
            port_reservation=Reservation(),
        )
        mixer = asyncio.create_task(asyncio.Event().wait())
        room._task = mixer

        await manager.close()

        self.assertEqual(calls, ["terminate", "close", "release"])
        self.assertTrue(mixer.done())
        self.assertFalse(manager.rooms)

    async def test_endpoint_shutdown_closes_resources_before_clearing_registry(self) -> None:
        hass = _FakeHass()
        registry = endpoint_lifecycle.call_registry(hass)
        calls: list[str] = []

        class Relay:
            async def stop(self) -> None:
                calls.append("relay_stop")

        class Client:
            async def terminate(self) -> None:
                calls.append("client_terminate")

            async def close(self) -> None:
                calls.append("client_close")

        class Manager:
            async def close(self, *, reason: str = "local_hangup") -> None:
                calls.append(f"conference_{reason}")

        class Reservation:
            def __init__(self, name: str) -> None:
                self.name = name

            def release(self) -> None:
                calls.append(f"release_{self.name}")

        class VideoSocket:
            def close(self) -> None:
                calls.append("video_socket_close")

        class Endpoint:
            def snapshot(self):
                return types.SimpleNamespace(pending_call_ids=("pending",), active_call_ids=("active",))

            def send_final_response(self, call_id, status, reason, *, decline_reason):
                calls.append(f"final_{call_id}_{status}_{decline_reason}")

            def send_bye(self, call_id):
                calls.append(f"bye_{call_id}")

            async def stop(self) -> None:
                calls.append("endpoint_stop")

        watcher = asyncio.create_task(asyncio.Event().wait())
        runtime_task = endpoint_lifecycle.create_runtime_task(hass, asyncio.Event().wait())
        endpoint = Endpoint()
        manager = Manager()
        owner = pbx_runtime_module.SipEndpointRuntime(projection=registry)
        owner.attach_component("conference_manager", manager, closer=manager.close)
        owner.attach_component("udp_listener", endpoint, closer=endpoint.stop)
        owner.activate()
        registry.bind_session_owner(owner)
        hass.runtime.sip = owner
        endpoint_lifecycle.sip_endpoint_manager = lambda _hass: endpoint
        registry.upsert("call", state="in_call")
        registry.upsert("inbound", state="in_call")
        registry.upsert("preanswered", state="ringing")
        registry.attach_relay("call", Relay())
        registry.attach_sip_client("call", "call", Client())
        registry.attach_client_watcher("call", watcher)
        self.assertIs(owner.client_watchers_snapshot().get("call"), watcher)
        registry.attach_media("inbound", {
            "rtp_reservation": Reservation("inbound"),
            "video_rtp_socket": VideoSocket(),
        })
        registry.attach_media("preanswered", {
            "rtp_reservation": Reservation("preanswered"),
        }, provisional=True)
        await endpoint_lifecycle.async_stop_sip_endpoint(hass)

        self.assertTrue(watcher.cancelled())
        self.assertTrue(runtime_task.cancelled())
        self.assertIn("final_pending_503_shutdown", calls)
        self.assertIn("bye_active", calls)
        self.assertIn("conference_local_hangup", calls)
        self.assertIn("relay_stop", calls)
        self.assertIn("client_terminate", calls)
        self.assertIn("client_close", calls)
        self.assertIn("video_socket_close", calls)
        self.assertIn("release_inbound", calls)
        self.assertIn("release_preanswered", calls)
        self.assertEqual(calls[-1], "endpoint_stop")
        self.assertFalse(registry.sessions)
        self.assertFalse(registry.relays)
        self.assertFalse(registry.sip_clients)

    async def test_endpoint_shutdown_mapping_survives_until_cancel_safe_stop(self) -> None:
        hass = _FakeHass()

        class Endpoint:
            def __init__(self) -> None:
                self.entered = asyncio.Event()
                self.release = asyncio.Event()
                self.stopped = False

            def snapshot(self):
                return types.SimpleNamespace(
                    pending_call_ids=(),
                    active_call_ids=(),
                )

            async def stop(self) -> None:
                self.entered.set()
                await self.release.wait()
                self.stopped = True

        endpoint = Endpoint()
        owner = pbx_runtime_module.SipEndpointRuntime()
        owner.attach_component("udp_listener", endpoint, closer=endpoint.stop)
        owner.activate()
        hass.runtime.sip = owner
        endpoint_lifecycle.sip_endpoint_manager = lambda _hass: endpoint
        stopping = asyncio.create_task(
            endpoint_lifecycle.async_stop_sip_endpoint(hass)
        )
        await asyncio.wait_for(endpoint.entered.wait(), timeout=1)
        stopping.cancel()
        await asyncio.sleep(0)
        stopping.cancel()
        await asyncio.sleep(0)

        self.assertFalse(stopping.done())
        self.assertIs(hass.runtime.sip, owner)
        self.assertIs(owner.component("udp_listener"), endpoint)

        endpoint.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await stopping

        self.assertTrue(endpoint.stopped)
        self.assertIsNone(hass.runtime.sip)

    async def test_trunk_shutdown_mapping_survives_until_cancel_safe_stop(self) -> None:
        hass = _FakeHass()

        class Trunk:
            def __init__(self) -> None:
                self.entered = asyncio.Event()
                self.release = asyncio.Event()
                self.stopped = False

            async def stop(self) -> None:
                self.entered.set()
                await self.release.wait()
                self.stopped = True

        trunk = Trunk()
        owner = pbx_runtime_module.SipEndpointRuntime()
        owner.attach_component("trunk", trunk, closer=trunk.stop)
        owner.activate()
        hass.runtime.sip = owner
        trunk_runtime.sip_trunk = lambda _hass: trunk
        stopping = asyncio.create_task(trunk_runtime.async_stop_sip_trunk(hass))
        await asyncio.wait_for(trunk.entered.wait(), timeout=1)
        stopping.cancel()
        await asyncio.sleep(0)
        stopping.cancel()
        await asyncio.sleep(0)

        self.assertFalse(stopping.done())
        self.assertIs(owner.component("trunk"), trunk)

        trunk.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await stopping

        self.assertTrue(trunk.stopped)
        self.assertIsNone(owner.component("trunk"))

    async def test_outbound_and_ha_legs_cannot_exceed_room_capacity(self) -> None:
        hass = _FakeHass()
        room = conference.ConferenceRoom(hass, name="Conference", local_ip="127.0.0.1")
        room.legs.update({f"leg-{index}": object() for index in range(conference.MAX_CONFERENCE_LEGS)})

        class Reservation:
            def __init__(self) -> None:
                self.released = False

            def release(self) -> None:
                self.released = True

        reservation = Reservation()
        client = types.SimpleNamespace(dialog=object())
        added = await room.add_client_leg(
            call_id="overflow",
            caller="Overflow",
            client=client,
            port_reservation=reservation,
        )
        self.assertFalse(added)
        self.assertTrue(reservation.released)
        self.assertIsNone(
            room.add_ha_softphone_leg(
                call_id="conference:overflow",
                endpoint_id="default",
            )
        )

    async def test_remote_rtp_reaches_ha_softphone_participant(self) -> None:
        hass = _FakeHass()
        manager = conference.ConferenceManager(hass, local_ip="127.0.0.1")
        fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
        invite = sip_listener.SipInvite(
            source_host="127.0.0.1",
            source_port=5060,
            request_uri=voip_sip.parse_sip_uri("sip:Conference@127.0.0.1"),
            caller_uri=voip_sip.parse_sip_uri("sip:Kitchen@127.0.0.1"),
            target="Conference",
            caller="Kitchen",
            call_id="call-1",
            cseq="1 INVITE",
            remote_sdp=(
                b"v=0\r\n"
                b"c=IN IP4 127.0.0.1\r\n"
                b"t=0 0\r\n"
                b"m=audio 45678 RTP/AVP 96\r\n"
                b"a=rtpmap:96 L16/16000/1\r\n"
                b"a=ptime:20\r\n"
                b"m=video 45680 RTP/AVP 102\r\n"
                b"a=rtpmap:102 H264/90000\r\n"
                b"a=fmtp:102 profile-level-id=42e01f;packetization-mode=1\r\n"
            ),
            send_format=fmt,
            recv_format=fmt,
            remote_rtp_host="127.0.0.1",
            remote_rtp_port=45678,
        )
        entry = types.SimpleNamespace(name="Conference", id="Conference")
        result = await manager.join(invite, entry, ring_ha=True)
        self.assertEqual(result.status, 200)
        self.assertIn("m=audio", result.answer_sdp)
        self.assertIn("m=video 0 RTP/AVP 102", result.answer_sdp)

        joined = manager.join_ha_softphone(
            "Conference",
            call_id=next(iter(manager.ha_calls)),
        )
        self.assertIsNotNone(joined)
        assert joined is not None
        softphone_call_id, queue = joined
        room = manager.rooms["Conference"]
        payload = _frame(1200)
        encoded = sip_client.RtpPayloadEncoder(fmt).encode(payload)
        room.handle_rtp(
            "call-1",
            rtp.build_packet(rtp.RtpPacket(payload_type=97, sequence=0, timestamp=0, ssrc=1, payload=encoded)),
            ("127.0.0.1", 46000),
        )
        room.handle_rtp(
            "call-1",
            rtp.build_packet(rtp.RtpPacket(payload_type=96, sequence=1, timestamp=0, ssrc=1, payload=encoded)),
            ("127.0.0.1", 46000),
        )
        self.assertEqual(room.legs["call-1"].remote_port, 46000)
        self.assertEqual(room.legs["call-1"].rx_packets, 1)
        heard = await asyncio.wait_for(queue.get(), timeout=1.0)
        self.assertEqual(_first_sample(heard), 1200)
        store = hass.runtime.softphones["default"]
        self.assertEqual(store["state"], "ringing")
        self.assertEqual(store["call_id"], "conference:Conference")

        await manager.leave_ha_softphone(
            "Conference",
            call_id=softphone_call_id,
        )
        await manager.leave_call("call-1", reason="remote_hangup")
        self.assertNotIn("Conference", manager.rooms)
        pool = hass.runtime.rtp_port_pool
        self.assertFalse(pool["used"])

    async def test_multiple_browser_phones_ring_join_and_leave_independently(self) -> None:
        hass = _FakeHass()
        endpoints = _install_browser_endpoints(hass, "kitchen", "hall")
        manager = conference.ConferenceManager(hass, local_ip="127.0.0.1")
        fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
        invite = sip_listener.SipInvite(
            source_host="127.0.0.1",
            source_port=5060,
            request_uri=voip_sip.parse_sip_uri("sip:Conference@127.0.0.1"),
            caller_uri=voip_sip.parse_sip_uri("sip:Door@127.0.0.1"),
            target="Conference",
            caller="Door",
            call_id="source-call",
            cseq="1 INVITE",
            remote_sdp=b"",
            send_format=fmt,
            recv_format=fmt,
            remote_rtp_host="127.0.0.1",
            remote_rtp_port=45690,
        )
        result = await manager.join(
            invite,
            types.SimpleNamespace(name="Conference", id="Conference"),
            ring_endpoint_ids=("kitchen", "hall"),
        )
        self.assertEqual(result.status, 200)

        calls_by_endpoint = {
            endpoint_id: call_id
            for call_id, (_room, endpoint_id) in manager.ha_calls.items()
        }
        self.assertEqual(set(calls_by_endpoint), {"kitchen", "hall"})
        self.assertNotEqual(calls_by_endpoint["kitchen"], calls_by_endpoint["hall"])
        self.assertEqual(
            hass.runtime.softphones["kitchen"]["state"],
            "ringing",
        )
        self.assertEqual(
            hass.runtime.softphones["hall"]["state"],
            "ringing",
        )

        kitchen_join = manager.join_ha_softphone(
            "Conference",
            endpoint_id="kitchen",
            call_id=calls_by_endpoint["kitchen"],
        )
        hall_join = manager.join_ha_softphone(
            "Conference",
            endpoint_id="hall",
            call_id=calls_by_endpoint["hall"],
        )
        self.assertIsNotNone(kitchen_join)
        self.assertIsNotNone(hall_join)
        assert kitchen_join is not None and hall_join is not None
        _kitchen_call_id, kitchen_queue = kitchen_join
        hall_call_id, hall_queue = hall_join

        manager.push_ha_audio(calls_by_endpoint["kitchen"], _frame(1400))
        heard = await _wait_for_sample(hall_queue, 1400)
        self.assertEqual(_first_sample(heard), 1400)
        # N-1 mixing never loops a participant's own microphone back to it.
        own_mix = await asyncio.wait_for(kitchen_queue.get(), timeout=1.0)
        self.assertEqual(_first_sample(own_mix), 0)

        await manager.leave_ha_softphone(
            "Conference",
            call_id=calls_by_endpoint["kitchen"],
        )
        self.assertFalse(endpoints.require("kitchen").active_call_id)
        self.assertEqual(endpoints.require("hall").active_call_id, hall_call_id)
        self.assertIn("source-call", manager.rooms["Conference"].legs)
        self.assertIn(hall_call_id, manager.rooms["Conference"].legs)

        await manager.leave_call("source-call", reason="remote_hangup")
        self.assertIn("Conference", manager.rooms)
        await manager.leave_ha_softphone("Conference", call_id=hall_call_id)
        self.assertFalse(endpoints.require("hall").active_call_id)
        self.assertNotIn("Conference", manager.rooms)

    async def test_one_browser_declining_conference_does_not_cancel_other_phone(self) -> None:
        hass = _FakeHass()
        endpoints = _install_browser_endpoints(hass, "kitchen", "hall")
        manager = conference.ConferenceManager(hass, local_ip="127.0.0.1")
        room = conference.ConferenceRoom(
            hass,
            name="Conference",
            local_ip="127.0.0.1",
        )
        manager.rooms[room.name] = room
        call_ids = manager.ring_ha_endpoints(
            "Conference",
            ("kitchen", "hall"),
            caller="Door",
        )
        calls_by_endpoint = {
            manager.ha_calls[call_id][1]: call_id for call_id in call_ids
        }

        declined = await manager.decline_ha_softphone(
            calls_by_endpoint["kitchen"],
            "kitchen",
        )
        self.assertTrue(declined)
        self.assertFalse(endpoints.require("kitchen").active_call_id)
        self.assertEqual(
            endpoints.require("hall").active_call_id,
            calls_by_endpoint["hall"],
        )
        self.assertEqual(
            hass.runtime.softphones["hall"]["state"],
            "ringing",
        )

        joined = manager.join_ha_softphone(
            "Conference",
            endpoint_id="hall",
            call_id=calls_by_endpoint["hall"],
        )
        self.assertIsNotNone(joined)
        await manager.leave_ha_softphone(
            "Conference",
            call_id=calls_by_endpoint["hall"],
        )

    async def test_conference_join_does_not_ring_ha_without_ring_flag(self) -> None:
        hass = _FakeHass()
        manager = conference.ConferenceManager(hass, local_ip="127.0.0.1")
        fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
        invite = sip_listener.SipInvite(
            source_host="127.0.0.1",
            source_port=5060,
            request_uri=voip_sip.parse_sip_uri("sip:Conference@127.0.0.1"),
            caller_uri=voip_sip.parse_sip_uri("sip:Kitchen@127.0.0.1"),
            target="Conference",
            caller="Kitchen",
            call_id="call-2",
            cseq="1 INVITE",
            remote_sdp=b"",
            send_format=fmt,
            recv_format=fmt,
            remote_rtp_host="127.0.0.1",
            remote_rtp_port=45679,
        )
        entry = types.SimpleNamespace(name="Conference", id="Conference")
        result = await manager.join(invite, entry)
        self.assertEqual(result.status, 200)
        self.assertNotIn("ha_softphone", hass.data[const.DOMAIN])
        await manager.leave_call("call-2", reason="remote_hangup")

    async def test_ha_softphone_can_start_empty_conference_and_leave_without_closing_peers(self) -> None:
        hass = _FakeHass()
        manager = conference.ConferenceManager(hass, local_ip="127.0.0.1")
        started = manager.start_ha_softphone("Conference")
        self.assertIsNotNone(started)
        assert started is not None
        softphone_call_id, _queue = started
        room = manager.rooms["Conference"]
        self.assertIn("conference:Conference", room.legs)
        self.assertEqual(room.legs["conference:Conference"].role, "ha")

        fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
        invite = sip_listener.SipInvite(
            source_host="127.0.0.1",
            source_port=5060,
            request_uri=voip_sip.parse_sip_uri("sip:Conference@127.0.0.1"),
            caller_uri=voip_sip.parse_sip_uri("sip:Kitchen@127.0.0.1"),
            target="Conference",
            caller="Kitchen",
            call_id="call-3",
            cseq="1 INVITE",
            remote_sdp=b"",
            send_format=fmt,
            recv_format=fmt,
            remote_rtp_host="127.0.0.1",
            remote_rtp_port=45680,
        )
        result = await manager.join(invite, types.SimpleNamespace(name="Conference", id="Conference"))
        self.assertEqual(result.status, 200)
        self.assertIn("call-3", room.legs)

        await manager.leave_ha_softphone(
            "Conference",
            call_id=softphone_call_id,
        )
        self.assertNotIn("conference:Conference", room.legs)
        self.assertIn("call-3", room.legs)

        await manager.leave_call("call-3", reason="remote_hangup")
        self.assertNotIn("Conference", manager.rooms)

        rejoined = manager.start_ha_softphone("Conference")
        self.assertIsNotNone(rejoined)
        assert rejoined is not None
        rejoined_call_id, _queue = rejoined
        self.assertNotEqual(rejoined_call_id, softphone_call_id)
        self.assertTrue(rejoined_call_id.startswith("conference:"))
        registry = endpoint_lifecycle.call_registry(hass)
        self.assertFalse(registry.is_terminated(rejoined_call_id))
        self.assertEqual(registry.sessions[rejoined_call_id].state, "in_call")

        await manager.leave_ha_softphone(
            "Conference",
            call_id=rejoined_call_id,
        )
        self.assertNotIn("Conference", manager.rooms)

    async def test_creator_leaving_does_not_close_room_with_remaining_participants(self) -> None:
        hass = _FakeHass()
        manager = conference.ConferenceManager(hass, local_ip="127.0.0.1")
        fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
        entry = types.SimpleNamespace(name="Conference", id="Conference")

        first = sip_listener.SipInvite(
            source_host="127.0.0.1",
            source_port=5060,
            request_uri=voip_sip.parse_sip_uri("sip:Conference@127.0.0.1"),
            caller_uri=voip_sip.parse_sip_uri("sip:Kitchen@127.0.0.1"),
            target="Conference",
            caller="Kitchen",
            call_id="owner-call",
            cseq="1 INVITE",
            remote_sdp=b"",
            send_format=fmt,
            recv_format=fmt,
            remote_rtp_host="127.0.0.1",
            remote_rtp_port=45681,
        )
        second = sip_listener.SipInvite(
            source_host="127.0.0.1",
            source_port=5060,
            request_uri=voip_sip.parse_sip_uri("sip:Conference@127.0.0.1"),
            caller_uri=voip_sip.parse_sip_uri("sip:Hall@127.0.0.1"),
            target="Conference",
            caller="Hall",
            call_id="second-call",
            cseq="1 INVITE",
            remote_sdp=b"",
            send_format=fmt,
            recv_format=fmt,
            remote_rtp_host="127.0.0.1",
            remote_rtp_port=45682,
        )

        self.assertEqual((await manager.join(first, entry)).status, 200)
        self.assertEqual((await manager.join(second, entry)).status, 200)
        room = manager.rooms["Conference"]
        self.assertEqual(set(room.legs), {"owner-call", "second-call"})

        await manager.leave_call("owner-call", reason="remote_hangup")
        self.assertIn("Conference", manager.rooms)
        self.assertEqual(set(room.legs), {"second-call"})
        self.assertFalse(room._closed)

        await manager.leave_call("second-call", reason="remote_hangup")
        self.assertNotIn("Conference", manager.rooms)
        pool = hass.runtime.rtp_port_pool
        self.assertFalse(pool["used"])


if __name__ == "__main__":
    unittest.main()
