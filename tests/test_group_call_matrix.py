#!/usr/bin/env python3
"""Fast SIP/PBX behavior matrix for group calls and HA softphone state."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from unittest.mock import patch
from pathlib import Path

from tests.support.voip_matrix import (
    BUSY,
    CANCELLED,
    ENDED,
    IDLE,
    IN_CALL,
    RINGING,
    UNAVAILABLE,
    Endpoint,
    HA_AUDIO_FORMATS,
    MiniPbx,
    SCENARIO_NAMES,
    run_matrix,
)


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


class _FakeUnauthorized(Exception):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args)
        self.details = kwargs


class _FakeServiceValidationError(ValueError):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args)
        self.details = kwargs


def _install_ha_fakes() -> None:
    if "homeassistant" not in sys.modules:
        ha_pkg = types.ModuleType("homeassistant")
        ha_pkg.__path__ = []
        sys.modules["homeassistant"] = ha_pkg
    if "homeassistant.core" not in sys.modules:
        core = types.ModuleType("homeassistant.core")
        core.HomeAssistant = object
        core.ServiceCall = object
        core.callback = lambda fn: fn
        sys.modules["homeassistant.core"] = core
    if "homeassistant.config_entries" not in sys.modules:
        config_entries = types.ModuleType("homeassistant.config_entries")
        config_entries.ConfigEntry = object
        config_entries.ConfigSubentry = object
        sys.modules["homeassistant.config_entries"] = config_entries
    else:
        config_entries = sys.modules["homeassistant.config_entries"]
        config_entries.ConfigSubentry = getattr(
            config_entries, "ConfigSubentry", object
        )
    exceptions = sys.modules.setdefault(
        "homeassistant.exceptions", types.ModuleType("homeassistant.exceptions")
    )
    exceptions.ConfigEntryError = getattr(
        exceptions, "ConfigEntryError", RuntimeError
    )
    exceptions.Unauthorized = getattr(
        exceptions, "Unauthorized", _FakeUnauthorized
    )
    exceptions.UnknownUser = getattr(exceptions, "UnknownUser", _FakeUnauthorized)
    exceptions.ServiceValidationError = getattr(
        exceptions,
        "ServiceValidationError",
        _FakeServiceValidationError,
    )
    if "homeassistant.components" not in sys.modules:
        components = types.ModuleType("homeassistant.components")
        components.__path__ = []
        sys.modules["homeassistant.components"] = components
    if "homeassistant.components.websocket_api" not in sys.modules:
        websocket_api = types.ModuleType("homeassistant.components.websocket_api")
        websocket_api.ActiveConnection = object
        websocket_api.async_register_command = lambda *_args, **_kwargs: None
        websocket_api.websocket_command = lambda _schema: (lambda fn: fn)
        websocket_api.async_response = lambda fn: fn
        sys.modules["homeassistant.components.websocket_api"] = websocket_api
    # Use the real validator when available so this dynamic loader cannot
    # poison later tests through the process-wide module cache.
    if "voluptuous" not in sys.modules:
        try:
            import voluptuous  # noqa: F401
        except ImportError:
            vol = types.ModuleType("voluptuous")
            vol.Required = lambda key: key
            vol.Optional = lambda key, default=None: key
            sys.modules["voluptuous"] = vol


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


roster = _load_module("roster")
peer = _load_module("peer")
groups = _load_module("groups")
router = _load_module("router")
endpoint_routing = _load_module("endpoint_routing")
websocket_api = _load_module("websocket_api")
fsm = _load_module("fsm")
const = _load_module("const")
conference = _load_module("conference")
route_decisions = _load_module("route_decisions")
runtime_data_module = _load_module("runtime_data")
endpoint_registry_module = _load_module("endpoint_registry")
endpoint_lifecycle_module = _load_module("endpoint_lifecycle")


class _FakeConfig:
    location_name = "Casa"


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.contexts: list[object | None] = []

    def async_fire(self, event_type: str, event: dict, *, context=None) -> None:
        self.events.append((event_type, dict(event)))
        self.contexts.append(context)


class _FakeHass:
    def __init__(self) -> None:
        self.config = _FakeConfig()
        self.bus = _FakeBus()
        self.data: dict = {const.DOMAIN: {}}
        self.runtime = types.SimpleNamespace(
            tasks=set(),
            calls=None,
            endpoints=endpoint_registry_module.EndpointRegistry(),
            sip=None,
            rtp_port_pool={},
            next_rtp_port=0,
            softphones={},
            softphone_presence={},
            media=types.SimpleNamespace(
                sessions={"audio": {}, "video": {}},
                owners={"audio": {}, "video": {}},
                owners_for=lambda channel: self.runtime.media.owners[channel],
                sessions_for=lambda channel: self.runtime.media.sessions[channel],
                identity_locks={},
                transcoder=None,
                debug_capture_tasks=set(),
                debug_capture_dropped_writes=0,
            ),
        )
        endpoint_lifecycle_module.call_registry(self).activate()


def _test_runtime(hass):
    return hass.runtime


runtime_data_module.require_runtime_data = _test_runtime
runtime_data_module.runtime_data = _test_runtime
endpoint_lifecycle_module.require_runtime_data = _test_runtime
websocket_api.runtime_data = _test_runtime


class GroupCallMatrixTest(unittest.TestCase):
    def test_default_route_continues_without_claiming_a_browser_phone(self) -> None:
        hass = _FakeHass()
        registry = websocket_api.call_registry(hass)
        registry.upsert(
            "default-route",
            state="connecting",
            owner="router",
            caller="Door",
            callee="Fallback",
        )
        future = asyncio.new_event_loop().create_future()
        registry.set_pending_route(
            "default-route",
            {
                "future": future,
                "invite": types.SimpleNamespace(
                    caller="Door",
                    target="Fallback",
                ),
            },
        )

        with (
            patch.object(route_decisions, "preferred_browser_phone", return_value=None),
            patch.object(route_decisions, "publish_phone_projection") as publish,
        ):
            route_decisions.set_pending_route_decision(
                hass,
                {"call_id": "default-route", "action": "default"},
            )

        self.assertEqual(future.result()["action"], "default")
        publish.assert_not_called()

    def test_debug_snapshot_exposes_cleanup_ownership_only_when_enabled(self) -> None:
        hass = _FakeHass()
        hass.runtime.media.owners["audio"] = {"audio-call": object()}
        hass.runtime.media.owners["video"] = {"video-call": object()}
        hass.runtime.media.transcoder = types.SimpleNamespace(
            call_id="transcoded-call"
        )

        normal = websocket_api._ha_softphone_state(hass, "default")
        self.assertFalse(normal["debug_mode"])
        self.assertEqual(normal["media_debug"], {})

        with patch.object(websocket_api, "current_debug_mode", return_value=True):
            debug = websocket_api._ha_softphone_state(hass, "default")

        self.assertTrue(debug["debug_mode"])
        self.assertEqual(debug["media_debug"]["audio_ws_owner_call_ids"], ["audio-call"])
        self.assertEqual(debug["media_debug"]["video_ws_owner_call_ids"], ["video-call"])
        self.assertEqual(
            debug["media_debug"]["video_transcoder_call_id"],
            "transcoded-call",
        )

    def test_unregistered_sip_accounts_are_not_exported_to_esp_phonebooks(
        self,
    ) -> None:
        hass = _FakeHass()
        offline = types.SimpleNamespace(
            endpoint_id="sip:offline",
            username="offline",
            name="Offline SIP",
            extension="668",
            kind=types.SimpleNamespace(value="sip_account"),
            availability="offline",
            device_id="device-offline",
            capabilities=frozenset({"audio", "dtmf"}),
            conference_group="",
            conference_ring=False,
            ring_group="",
        )
        hass.data[const.DOMAIN]["endpoint_registry"] = types.SimpleNamespace(
            endpoints=(offline,),
            by_username=lambda _username: None,
        )

        old_manual = endpoint_routing.manual_roster_entries
        endpoint_routing.manual_roster_entries = lambda _hass: []
        try:
            entries = endpoint_routing.roster_from_peers(hass, [], [])
        finally:
            endpoint_routing.manual_roster_entries = old_manual

        self.assertNotIn("Offline SIP", {entry.name for entry in entries})

    def test_video_degradation_is_explicit_and_can_recover(self) -> None:
        hass = _FakeHass()
        websocket_api._set_ha_softphone_call_state(
            hass,
            fsm.CallState.RINGING.value,
            endpoint_id="default",
            call_id="video-call",
            direction="incoming",
            caller="Door",
            callee="Casa",
            video_offered=True,
        )
        offered = websocket_api._ha_softphone_state(hass, "default")
        self.assertTrue(offered["video_requested"])
        self.assertFalse(offered["video_negotiated"])
        self.assertEqual(offered["video_status"], "offered")

        websocket_api._set_ha_softphone_call_state(
            hass,
            fsm.CallState.IN_CALL.value,
            endpoint_id="default",
            call_id="video-call",
            direction="incoming",
            caller="Door",
            callee="Casa",
            video_active=False,
            video_negotiated=False,
            video_status="degraded",
            video_failure_reason="local_video_resources_unavailable",
        )
        degraded = websocket_api._ha_softphone_state(hass, "default")
        self.assertEqual(degraded["video_status"], "degraded")
        self.assertEqual(
            degraded["video_failure_reason"],
            "local_video_resources_unavailable",
        )

        websocket_api._set_ha_softphone_call_state(
            hass,
            fsm.CallState.IN_CALL.value,
            endpoint_id="default",
            call_id="video-call",
            direction="incoming",
            caller="Door",
            callee="Casa",
            video_active=True,
            video_negotiated=True,
            video_status="active",
            video_failure_reason="",
        )
        recovered = websocket_api._ha_softphone_state(hass, "default")
        self.assertEqual(recovered["video_status"], "active")
        self.assertEqual(recovered["video_failure_reason"], "")

    def test_call_event_preserves_explicit_local_leg_owner(self) -> None:
        hass = _FakeHass()
        registry = websocket_api.call_registry(hass)
        registry.upsert(
            "local-call",
            state="ringing",
            owner="local_bridge",
            ingress="trunk",
            origin="trunk",
            endpoint_id="office",
            source_endpoint_id="office",
            dest_endpoint_id="kitchen",
        )

        event = websocket_api._fire_call_event(
            hass,
            {
                "call_id": "local-call",
                "state": "ringing",
                "endpoint_id": "kitchen",
                "device_id": "kitchen-device",
                "direction": "incoming",
                "origin": "remote",
            },
            "session",
        )

        self.assertEqual(event["endpoint_id"], "kitchen")
        self.assertEqual(event["device_id"], "kitchen-device")
        self.assertEqual(event["source_endpoint_id"], "office")
        self.assertEqual(event["dest_endpoint_id"], "kitchen")
        self.assertEqual(event["schema_version"], 2)
        self.assertGreater(event["generation"], 0)
        self.assertEqual(event["pbx_phase"], "ringing")
        self.assertEqual(event["actor"], "remote")
        self.assertEqual(event["ingress"], "trunk")
        self.assertEqual(event["origin"], "trunk")

    def test_logbook_terminal_event_is_emitted_once_for_the_logical_call(self) -> None:
        hass = _FakeHass()
        registry = websocket_api.call_registry(hass)
        registry.upsert(
            "group-call",
            state="ringing",
            owner="router",
            caller="Door",
            callee="All rooms",
        )

        websocket_api._fire_call_event(
            hass,
            {
                "call_id": "group-call",
                "state": "cancelled",
                "caller": "Door",
                "callee": "All rooms",
                "endpoint_id": "losing-room",
            },
            "session",
        )
        registry.transition("group-call", state="terminating", owner="bridge")
        websocket_api._fire_call_event(
            hass,
            {
                "call_id": "group-call",
                "state": "idle",
                "caller": "Door",
                "callee": "All rooms",
            },
            "sip_bridge",
        )
        websocket_api._fire_call_event(
            hass,
            {
                "call_id": "group-call",
                "state": "idle",
                "caller": "Door",
                "callee": "All rooms",
            },
            "sip_bridge",
        )

        terminal = [
            event
            for event_type, event in hass.bus.events
            if event_type == websocket_api.SIP_CALL_ENDED_EVENT
        ]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]["state"], "idle")

    def test_esp_physical_state_is_not_a_second_logbook_call(self) -> None:
        hass = _FakeHass()

        websocket_api._fire_call_event(
            hass,
            {
                "call_id": "physical:esp:ws3",
                "state": "idle",
                "caller": "",
                "callee": "Casa",
                "duration_seconds": 2,
            },
            "esp",
        )

        self.assertFalse(
            any(
                event_type == websocket_api.SIP_CALL_ENDED_EVENT
                for event_type, _event in hass.bus.events
            )
        )

    def test_ring_group_decline_is_per_phone_and_cannot_be_reversed(self) -> None:
        async def scenario() -> None:
            hass = _FakeHass()
            registry = websocket_api.call_registry(hass)
            registry.upsert(
                "group-call",
                state="ringing",
                owner="router",
                caller="Door",
                callee="All rooms",
                route_kind="ring",
            )
            future = asyncio.get_running_loop().create_future()
            registry.set_pending_route("group-call", {
                "future": future,
                "invite": types.SimpleNamespace(
                    caller="Door",
                    target="All rooms",
                    send_format=conference.CONFERENCE_RTP_FORMAT,
                    recv_format=conference.CONFERENCE_RTP_FORMAT,
                ),
                "ring_group_endpoint_ids": ("kitchen", "hall"),
                "declined_endpoint_ids": set(),
            })

            route_decisions.set_pending_route_decision(
                hass,
                {
                    "call_id": "group-call",
                    "action": "decline",
                    "endpoint_id": "kitchen",
                },
            )
            self.assertFalse(future.done())
            self.assertEqual(
                hass.runtime.softphones["kitchen"]["state"],
                "idle",
            )
            self.assertEqual(
                hass.runtime.softphones["kitchen"][
                    "terminal_reason"
                ],
                "declined",
            )
            with self.assertRaises(
                sys.modules["homeassistant.exceptions"].ServiceValidationError
            ):
                route_decisions.set_pending_route_decision(
                    hass,
                    {
                        "call_id": "group-call",
                        "action": "answer_ha",
                        "endpoint_id": "kitchen",
                    },
                )

            route_decisions.set_pending_route_decision(
                hass,
                {
                    "call_id": "group-call",
                    "action": "answer_ha",
                    "endpoint_id": "hall",
                    "media_client_id": "hall-card",
                    "send_video": True,
                },
            )
            decision = await future
            self.assertEqual(decision["endpoint_id"], "hall")
            self.assertEqual(decision["media_client_id"], "hall-card")
            self.assertTrue(decision["send_video"])

        asyncio.run(scenario())

    def test_call_and_softphone_events_preserve_the_initiating_ha_context(self) -> None:
        hass = _FakeHass()
        registry = websocket_api.call_registry(hass)
        context = types.SimpleNamespace(user_id="user-a", id="context-a")
        registry.upsert("call-1", state="calling", owner="ha_softphone")
        registry.bind_controller("call-1", context=context)

        websocket_api._fire_call_event(
            hass,
            {"call_id": "call-1", "state": "calling", "direction": "outgoing"},
            "session",
        )
        websocket_api._publish_ha_softphone_state(
            hass,
            {"call_id": "call-1", "state": "calling"},
            endpoint_id="default",
        )

        self.assertTrue(hass.bus.contexts)
        self.assertTrue(all(item is context for item in hass.bus.contexts))

    def test_sip_target_profile_keeps_only_bidirectional_rtp_contracts(self) -> None:
        audio_format = endpoint_routing.AudioFormat
        remote_tx = [
            audio_format(32000, "s16le", 1, 10),
            audio_format(16000, "s16le", 1, 10),
        ]
        remote_rx = [
            audio_format(48000, "s16le", 1, 10),
            audio_format(16000, "s16le", 1, 10),
        ]

        send, recv = endpoint_routing.sip_target_audio_profile(
            remote_tx_formats=remote_tx,
            remote_rx_formats=remote_rx,
            target="standard-phone",
        )

        expected = [audio_format(16000, "s16le", 1, 10)]
        self.assertEqual(send, expected)
        self.assertEqual(recv, expected)

    def test_sip_target_profile_rejects_disjoint_tx_rx_contracts(self) -> None:
        audio_format = endpoint_routing.AudioFormat
        send, recv = endpoint_routing.sip_target_audio_profile(
            remote_tx_formats=[audio_format(16000, "s16le", 1, 10)],
            remote_rx_formats=[audio_format(48000, "s16le", 1, 10)],
            target="nonstandard-phone",
        )

        self.assertEqual((send, recv), ([], []))

    def test_peer_lookup_accepts_standard_sip_routing_identities(self) -> None:
        endpoint = peer.Peer(
            name="Waveshare S3 Audio",
            host="192.168.1.47",
            sip_uri_user="waveshare-s3",
            extension="418",
        )

        for identity in ("Waveshare S3 Audio", "waveshare-s3", "418"):
            with self.subTest(identity):
                self.assertIs(
                    endpoint_routing.peer_for_target(identity, [endpoint]),
                    endpoint,
                )
        self.assertIsNone(endpoint_routing.peer_for_target("unknown", [endpoint]))

    def test_voip_matrix_runner_all_scenarios_validate(self) -> None:
        results, errors = run_matrix()
        self.assertEqual(errors, [])
        self.assertEqual([item["scenario"] for item in results], list(SCENARIO_NAMES))

    def test_conference_websocket_audio_uses_canonical_room_format(self) -> None:
        self.assertEqual(conference.CONFERENCE_FORMAT.wire_token(), "16000:s16le:1:20")
        self.assertEqual(conference.CONFERENCE_RTP_FORMAT.audio_format.wire_token(), "16000:s16le:1:20")

    def _peers(self, *, ha_ring_group: str = "RG Casa", ha_conference_ring: bool = True):
        return [
            peer.Peer(
                name="Spotpear",
                host="192.168.1.31",
                sip_port=5060,
                rtp_port=40000,
                conference_group="CG Casa",
                conference_ring=False,
                ring_group="RG Casa",
                tx_formats=["16000:s16le:1:20"],
                rx_formats=["16000:s16le:1:20"],
            ),
            peer.Peer(
                name="WS3",
                host="192.168.1.47",
                sip_port=5060,
                rtp_port=40000,
                conference_group="CG Casa",
                conference_ring=False,
                ring_group="RG Casa",
                tx_formats=["48000:s16le:1:10", "16000:s16le:1:20"],
                rx_formats=["48000:s16le:1:10", "16000:s16le:1:20"],
            ),
            peer.Peer(
                name="Casa",
                host="192.168.1.10",
                local_ha=True,
                sip_port=5060,
                rtp_port=40000,
                conference_group="CG Casa",
                conference_ring=ha_conference_ring,
                ring_group=ha_ring_group,
                tx_formats=["48000:s16le:1:10", "16000:s16le:1:20"],
                rx_formats=["48000:s16le:1:10", "16000:s16le:1:20"],
            ),
        ]

    def _registered(self):
        return [
            roster.RosterEntry(
                id="Zoiper",
                name="Zoiper",
                sip_uri="sip:Zoiper@192.168.1.20:5062;transport=tcp",
                metadata={
                    "conference_group": "CG Casa",
                    "conference_ring": True,
                    "ring_group": "RG Casa",
                    "tx_formats": ["48000:s16le:1:10", "16000:s16le:1:20"],
                    "rx_formats": ["48000:s16le:1:10", "16000:s16le:1:20"],
                },
            )
        ]

    def _roster(self, peers=None, registered=None):
        hass = _FakeHass()
        old_manual = endpoint_routing.manual_roster_entries
        endpoint_routing.manual_roster_entries = lambda _hass: []
        try:
            return endpoint_routing.roster_from_peers(hass, list(peers if peers is not None else self._peers()), list(registered if registered is not None else self._registered()))
        finally:
            endpoint_routing.manual_roster_entries = old_manual

    def _mini_pbx(self, *, spotpear_auto: bool = False, ws3_auto: bool = False, ha_ring: bool = True) -> MiniPbx:
        pbx = MiniPbx(
            [
                Endpoint(
                    "Casa",
                    ring_group="RG Casa",
                    conference_group="CG Casa",
                    conference_ring=ha_ring,
                    auto_answer=False,
                    tx_formats=HA_AUDIO_FORMATS,
                    rx_formats=HA_AUDIO_FORMATS,
                ),
                Endpoint(
                    "Spotpear",
                    ring_group="RG Casa",
                    conference_group="CG Casa",
                    conference_ring=False,
                    auto_answer=spotpear_auto,
                ),
                Endpoint(
                    "WS3",
                    ring_group="RG Casa",
                    conference_group="CG Casa",
                    conference_ring=False,
                    auto_answer=ws3_auto,
                ),
                Endpoint(
                    "Zoiper",
                    ring_group="RG Casa",
                    conference_group="CG Casa",
                    conference_ring=True,
                    auto_answer=False,
                ),
            ]
        )
        pbx.rebuild_phonebook()
        return pbx

    def test_group_roster_matrix_is_dynamic_and_visible_in_the_central_roster(self) -> None:
        entries = self._roster()
        by_id = {entry.id: entry for entry in entries}

        self.assertIn("RG Casa", by_id)
        self.assertIn("CG Casa", by_id)
        self.assertEqual(by_id["RG Casa"].metadata["group_type"], groups.GROUP_TYPE_RING)
        self.assertEqual(by_id["RG Casa"].metadata["members"], ["Spotpear", "WS3", "Zoiper", "Casa"])
        self.assertEqual(by_id["CG Casa"].metadata["group_type"], groups.GROUP_TYPE_CONFERENCE)
        self.assertEqual(by_id["CG Casa"].metadata["members"], ["Spotpear", "WS3", "Zoiper", "Casa"])
        self.assertEqual(by_id["CG Casa"].metadata["ring_members"], ["Zoiper", "Casa"])

        visible = {
            (entry.name or entry.id)
            for entry in entries
            if entry.enabled and not (entry.metadata or {}).get("local_ha")
        }
        self.assertIn("RG Casa", visible)
        self.assertIn("CG Casa", visible)
        self.assertIn("Spotpear", visible)
        self.assertIn("WS3", visible)
        self.assertNotIn("Casa", visible)

        stale_entries = self._roster(peers=[self._peers()[2]], registered=[])
        stale_by_id = {entry.id: entry for entry in stale_entries}
        self.assertIn("RG Casa", stale_by_id)
        self.assertIn("CG Casa", stale_by_id)
        self.assertEqual(stale_by_id["RG Casa"].metadata["members"], ["Casa"])
        self.assertEqual(stale_by_id["CG Casa"].metadata["members"], ["Casa"])

    def test_group_routing_matrix_preserves_sip_pbx_roles(self) -> None:
        entries = self._roster()
        ha_uri = "sip:Casa@192.168.1.10:5060;transport=tcp"
        cases = [
            ("HA calls RG", router.resolve_ha_router("RG Casa", entries, trunk_ready=False), router.RouteAction.GROUP, "RG Casa", ""),
            ("HA calls CG", router.resolve_ha_router("CG Casa", entries, trunk_ready=False), router.RouteAction.GROUP, "CG Casa", ""),
            ("HA calls endpoint", router.resolve_ha_router("Spotpear", entries, trunk_ready=False), router.RouteAction.FORWARD, "Spotpear", "sip:Spotpear@192.168.1.31"),
            ("ESP calls RG", router.resolve_esp_origin("RG Casa", entries, ha_uri), router.RouteAction.BRIDGE, "RG Casa", "sip:RG_Casa@192.168.1.10;transport=tcp"),
            ("ESP calls CG", router.resolve_esp_origin("CG Casa", entries, ha_uri), router.RouteAction.BRIDGE, "CG Casa", "sip:CG_Casa@192.168.1.10;transport=tcp"),
            ("ESP calls endpoint", router.resolve_esp_origin("WS3", entries, ha_uri), router.RouteAction.DIRECT, "WS3", "sip:WS3@192.168.1.47"),
        ]
        for label, decision, action, target, sip_uri in cases:
            with self.subTest(label):
                self.assertEqual(decision.action, action)
                self.assertEqual(decision.target, target)
                if sip_uri:
                    self.assertEqual(decision.sip_uri, sip_uri)

    def test_ha_softphone_state_can_show_ring_group_winner_without_losing_dialed_target(self) -> None:
        hass = _FakeHass()
        websocket_api._set_ha_softphone_call_state(
            hass,
            fsm.CallState.IN_CALL.value,
            endpoint_id="default",
            session_device_id=const.HA_SOFTPHONE_DEVICE_ID,
            caller="Casa",
            callee="RG Casa",
            peer_name="Spotpear",
            direction="outgoing",
            call_id="ha-rg-1",
            dialed_target="RG Casa",
            connected_party="Spotpear",
            answered_by="Spotpear",
            route_kind=groups.GROUP_TYPE_RING,
            last_sip_event="SIP_RESPONSE",
            sip_status_code=200,
        )
        state = websocket_api._ha_softphone_state(hass, "default")
        self.assertEqual(state["callee"], "RG Casa")
        self.assertEqual(state["peer_name"], "Spotpear")
        self.assertEqual(state["dialed_target"], "RG Casa")
        self.assertEqual(state["connected_party"], "Spotpear")
        self.assertEqual(state["answered_by"], "Spotpear")
        self.assertEqual(state["contact"], "Spotpear")

        websocket_api._set_ha_softphone_call_state(
            hass,
            fsm.CallState.IDLE.value,
            endpoint_id="default",
            reason="local_hangup",
        )
        idle = websocket_api._ha_softphone_state(hass, "default")
        self.assertEqual(idle["state"], fsm.CallState.IDLE.value)
        self.assertEqual(idle["dialed_target"], "RG Casa")
        self.assertEqual(idle["connected_party"], "")
        self.assertEqual(idle["answered_by"], "")

    def test_ha_softphone_state_prefers_connected_party_over_group_peer_in_call(self) -> None:
        hass = _FakeHass()
        websocket_api._set_ha_softphone_call_state(
            hass,
            fsm.CallState.IN_CALL.value,
            endpoint_id="default",
            session_device_id=const.HA_SOFTPHONE_DEVICE_ID,
            caller="Casa",
            callee="RG Casa",
            peer_name="RG Casa",
            direction="outgoing",
            call_id="ha-rg-1",
            dialed_target="RG Casa",
            connected_party="Waveshare S3 Audio",
            answered_by="Waveshare S3 Audio",
            route_kind=groups.GROUP_TYPE_RING,
            last_sip_event="SIP_RESPONSE",
            sip_status_code=200,
        )
        state = websocket_api._ha_softphone_state(hass, "default")
        self.assertEqual(state["callee"], "RG Casa")
        self.assertEqual(state["peer_name"], "Waveshare S3 Audio")
        self.assertEqual(state["dialed_target"], "RG Casa")
        self.assertEqual(state["contact"], "Waveshare S3 Audio")

    def test_incoming_forward_keeps_remote_caller_as_card_peer(self) -> None:
        hass = _FakeHass()
        websocket_api._set_ha_softphone_call_state(
            hass,
            fsm.CallState.IN_CALL.value,
            endpoint_id="default",
            session_device_id=const.HA_SOFTPHONE_DEVICE_ID,
            caller="n-IA-hane",
            callee="Waveshare P4 Touch",
            peer_name="Waveshare P4 Touch",
            direction="incoming",
            call_id="zoiper-to-p4",
            dialed_target="Waveshare P4 Touch",
            connected_party="Waveshare P4 Touch",
            answered_by="Waveshare P4 Touch",
            last_sip_event="SIP_RESPONSE",
            sip_status_code=200,
        )

        state = websocket_api._ha_softphone_state(hass, "default")
        self.assertEqual(state["caller"], "n-IA-hane")
        self.assertEqual(state["peer_name"], "n-IA-hane")
        self.assertEqual(state["contact"], "n-IA-hane")
        self.assertEqual(state["connected_party"], "Waveshare P4 Touch")

    def test_ha_softphone_terminal_state_preserves_incoming_dialed_extension(self) -> None:
        hass = _FakeHass()
        websocket_api._set_ha_softphone_call_state(
            hass,
            fsm.CallState.RINGING.value,
            endpoint_id="default",
            session_device_id=const.HA_SOFTPHONE_DEVICE_ID,
            caller="Waveshare S3 Audio",
            callee="666",
            peer_name="Waveshare S3 Audio",
            direction="incoming",
            call_id="ws3-666",
        )
        websocket_api._set_ha_softphone_call_state(
            hass,
            fsm.CallState.IN_CALL.value,
            endpoint_id="default",
            session_device_id=const.HA_SOFTPHONE_DEVICE_ID,
            caller="Waveshare S3 Audio",
            callee="666",
            peer_name="Waveshare S3 Audio",
            direction="incoming",
            call_id="ws3-666",
            dialed_target="666",
        )
        websocket_api._set_ha_softphone_call_state(
            hass,
            fsm.CallState.IDLE.value,
            endpoint_id="default",
            session_device_id=const.HA_SOFTPHONE_DEVICE_ID,
            caller="Waveshare S3 Audio",
            callee="666",
            peer_name="Waveshare S3 Audio",
            direction="incoming",
            call_id="ws3-666",
            reason="local_hangup",
        )
        idle = websocket_api._ha_softphone_state(hass, "default")
        self.assertEqual(idle["peer_name"], "Waveshare S3 Audio")
        self.assertEqual(idle["dialed_target"], "666")
        self.assertEqual(idle["terminal_reason"], "local_hangup")

    def test_late_state_cannot_resurrect_terminated_softphone_call(self) -> None:
        hass = _FakeHass()
        registry = websocket_api.call_registry(hass)
        registry.upsert(
            "finished-call",
            state=fsm.CallState.IN_CALL.value,
            owner="ha_softphone",
        )
        websocket_api._set_ha_softphone_call_state(
            hass,
            fsm.CallState.IN_CALL.value,
            endpoint_id="default",
            call_id="finished-call",
            direction="incoming",
            caller="Door",
            callee="Casa",
        )
        asyncio.run(
            registry.terminate_call_wait(
                "finished-call",
                reason="remote_hangup",
            )
        )
        websocket_api._set_ha_softphone_call_state(
            hass,
            fsm.CallState.IDLE.value,
            endpoint_id="default",
            call_id="finished-call",
            direction="incoming",
            caller="Door",
            callee="Casa",
            reason="remote_hangup",
        )

        websocket_api._set_ha_softphone_call_state(
            hass,
            fsm.CallState.IN_CALL.value,
            endpoint_id="default",
            call_id="finished-call",
            direction="incoming",
            caller="Door",
            callee="Casa",
        )

        state = websocket_api._ha_softphone_state(hass, "default")
        self.assertEqual(state["state"], fsm.CallState.IDLE.value)
        self.assertEqual(state["call_id"], "finished-call")
        self.assertEqual(state["terminal_reason"], "remote_hangup")

    def test_new_call_replaces_orphaned_ringing_projection(self) -> None:
        hass = _FakeHass()
        websocket_api._set_ha_softphone_call_state(
            hass,
            fsm.CallState.RINGING.value,
            call_id="orphaned-call",
            endpoint_id="test",
            direction="incoming",
        )

        websocket_api._set_ha_softphone_call_state(
            hass,
            fsm.CallState.RINGING.value,
            call_id="current-call",
            endpoint_id="test",
            direction="incoming",
        )

        state = websocket_api._ha_softphone_state(hass, "test")
        self.assertEqual(state["state"], fsm.CallState.RINGING.value)
        self.assertEqual(state["call_id"], "current-call")

    def test_new_call_cannot_replace_live_ringing_projection(self) -> None:
        hass = _FakeHass()
        registry = websocket_api.call_registry(hass)
        registry.upsert(
            "live-call",
            state=fsm.CallState.RINGING.value,
            owner="ha_softphone",
        )
        websocket_api._set_ha_softphone_call_state(
            hass,
            fsm.CallState.RINGING.value,
            call_id="live-call",
            endpoint_id="test",
            direction="incoming",
        )

        websocket_api._set_ha_softphone_call_state(
            hass,
            fsm.CallState.RINGING.value,
            call_id="competing-call",
            endpoint_id="test",
            direction="incoming",
        )

        state = websocket_api._ha_softphone_state(hass, "test")
        self.assertEqual(state["call_id"], "live-call")

    def test_virtual_endpoint_phonebook_push_matrix(self) -> None:
        pbx = self._mini_pbx()
        first = pbx.phonebook
        self.assertEqual(first["RG Casa"]["members"], ["Casa", "Spotpear", "WS3", "Zoiper"])
        self.assertEqual(first["CG Casa"]["members"], ["Casa", "Spotpear", "WS3", "Zoiper"])
        self.assertEqual(first["CG Casa"]["ring_members"], ["Casa", "Zoiper"])
        self.assertEqual(len(pbx.pushes), 1)

        pbx.set_group_membership("Casa", ring_group="", conference_group="", conference_ring=False)
        self.assertEqual(pbx.phonebook["RG Casa"]["members"], ["Spotpear", "WS3", "Zoiper"])
        self.assertEqual(pbx.phonebook["CG Casa"]["ring_members"], ["Zoiper"])

        pbx.set_online("Spotpear", False)
        pbx.set_online("WS3", False)
        pbx.set_online("Zoiper", False)
        self.assertNotIn("RG Casa", pbx.phonebook)
        self.assertNotIn("CG Casa", pbx.phonebook)
        self.assertGreaterEqual(len(pbx.pushes), 4)

    def test_virtual_services_contacts_accounts_trunk_and_push_matrix(self) -> None:
        pbx = self._mini_pbx()
        initial_pushes = len(pbx.pushes)

        pbx.add_contact("Desk SIP", sip_uri="sip:desk@192.168.1.60:5060", ring_group="RG Casa")
        self.assertIn("Desk SIP", pbx.phonebook)
        self.assertIn("Desk SIP", pbx.phonebook["RG Casa"]["members"])
        self.assertEqual(len(pbx.pushes), initial_pushes + 1)

        pbx.create_sip_account(
            "MobileOffice",
            sip_uri="sip:MobileOffice@192.168.1.70:5062;transport=tcp",
            conference_group="CG Casa",
            conference_ring=True,
        )
        self.assertIn("MobileOffice", pbx.phonebook["CG Casa"]["members"])
        self.assertIn("MobileOffice", pbx.phonebook["CG Casa"]["ring_members"])

        pbx.remove_contact("Desk SIP")
        self.assertNotIn("Desk SIP", pbx.phonebook)
        self.assertNotIn("Desk SIP", pbx.phonebook["RG Casa"]["members"])

        pbx.remove_sip_account("MobileOffice")
        self.assertNotIn("MobileOffice", pbx.phonebook)
        self.assertNotIn("MobileOffice", pbx.phonebook["CG Casa"]["members"])

        no_trunk = pbx.call_trunk("Casa", "+390551234567")
        self.assertEqual(no_trunk.state, UNAVAILABLE)
        pbx.add_trunk("Wildix", "+")
        via_trunk = pbx.call_trunk("Casa", "+390551234567")
        self.assertEqual(via_trunk.state, IN_CALL)
        self.assertEqual(via_trunk.winner, "trunk")
        self.assertIn("INVITE_TRUNK", via_trunk.events)

    def test_contact_scrolling_and_selected_destination_matrix(self) -> None:
        pbx = self._mini_pbx()
        contacts = pbx.visible_contacts("WS3")
        self.assertIn("Spotpear", contacts)
        self.assertIn("RG Casa", contacts)
        self.assertIn("CG Casa", contacts)
        self.assertNotIn("WS3", contacts)

        for _ in range(len(contacts)):
            selected = pbx.current_contact("WS3")
            if selected == "Spotpear":
                break
            pbx.select_contact("WS3")
        self.assertEqual(pbx.current_contact("WS3"), "Spotpear")
        call = pbx.call_selected("WS3")
        self.assertEqual(call.kind, "direct")
        self.assertEqual(call.target, "Spotpear")
        self.assertEqual(call.state, RINGING)
        call = pbx.cancel_endpoint("WS3", "Spotpear")
        self.assertEqual(call.state, CANCELLED)

    def test_ring_group_manual_answer_cancel_and_bye_matrix(self) -> None:
        pbx = self._mini_pbx()
        call = pbx.call_ring_group("Casa", "RG Casa")
        self.assertEqual(call.state, RINGING)
        self.assertEqual(call.ringing, ["Spotpear", "WS3", "Zoiper"])
        self.assertEqual(pbx.endpoint("Casa").state, RINGING)

        call = pbx.caller_cancels_ring_group()
        self.assertEqual(call.state, CANCELLED)
        self.assertEqual(call.cancelled, ["Spotpear", "WS3", "Zoiper"])
        self.assertTrue(all(pbx.endpoint(name).state == IDLE for name in ("Casa", "Spotpear", "WS3", "Zoiper")))

        call = pbx.call_ring_group("Casa", "RG Casa")
        call = pbx.answer_ring_group("Spotpear")
        self.assertEqual(call.state, IN_CALL)
        self.assertEqual(call.winner, "Spotpear")
        self.assertEqual(call.cancelled, ["WS3", "Zoiper"])
        self.assertEqual(pbx.endpoint("Casa").peer, "Spotpear")
        self.assertEqual(pbx.endpoint("Spotpear").peer, "Casa")
        self.assertEqual(pbx.endpoint("WS3").state, IDLE)

        call = pbx.hangup_ring_group("Spotpear")
        self.assertEqual(call.state, ENDED)
        self.assertIn("BYE:Spotpear", call.events)
        self.assertIn("BYE:Casa", call.events)
        self.assertEqual(pbx.endpoint("Casa").state, IDLE)
        self.assertEqual(pbx.endpoint("Spotpear").state, IDLE)

    def test_ring_group_auto_answer_and_busy_matrix(self) -> None:
        pbx = self._mini_pbx(spotpear_auto=True)
        call = pbx.call_ring_group("Casa", "RG Casa")
        self.assertEqual(call.state, IN_CALL)
        self.assertEqual(call.winner, "Spotpear")
        self.assertEqual(call.cancelled, ["WS3", "Zoiper"])
        self.assertIn("200:Spotpear", call.events)

        busy = pbx.call_ring_group("Casa", "RG Casa")
        self.assertEqual(busy.state, BUSY)

    def test_direct_endpoint_auto_answer_dnd_cancel_bye_and_disconnect_matrix(self) -> None:
        pbx = self._mini_pbx(spotpear_auto=True)
        call = pbx.call_endpoint("WS3", "Spotpear")
        self.assertEqual(call.state, IN_CALL)
        self.assertEqual(call.winner, "Spotpear")
        self.assertEqual(pbx.endpoint("WS3").peer, "Spotpear")
        ended = pbx.hangup_endpoint("WS3")
        self.assertEqual(ended.state, ENDED)
        self.assertIn("BYE:WS3", ended.events)
        self.assertIn("BYE:Spotpear", ended.events)

        pbx.endpoint("Spotpear").auto_answer = False
        pbx.set_dnd("Spotpear", True)
        busy = pbx.call_endpoint("WS3", "Spotpear")
        self.assertEqual(busy.state, BUSY)
        self.assertIn("486", busy.events)
        pbx.set_dnd("Spotpear", False)

        ringing = pbx.call_endpoint("WS3", "Spotpear")
        self.assertEqual(ringing.state, RINGING)
        cancelled = pbx.cancel_endpoint("WS3", "Spotpear")
        self.assertEqual(cancelled.state, CANCELLED)
        self.assertEqual(pbx.endpoint("WS3").state, IDLE)
        self.assertEqual(pbx.endpoint("Spotpear").state, IDLE)

        in_call = pbx.call_endpoint("WS3", "Spotpear")
        self.assertEqual(pbx.answer_endpoint("Spotpear", "WS3").state, IN_CALL)
        self.assertEqual(in_call.target, "Spotpear")
        affected = pbx.disconnect("Spotpear")
        self.assertTrue(any("DISCONNECT:Spotpear" in result.events for result in affected))
        self.assertEqual(pbx.endpoint("WS3").state, IDLE)
        self.assertNotIn("Spotpear", pbx.phonebook)

    def test_error_matrix_unknown_dnd_offline_and_missing_trunk(self) -> None:
        pbx = self._mini_pbx()
        unknown = pbx.call("Casa", "Missing")
        self.assertEqual(unknown.state, UNAVAILABLE)
        self.assertIn("not_found", unknown.events)

        pbx.set_dnd("Spotpear", True)
        dnd = pbx.call_endpoint("WS3", "Spotpear")
        self.assertEqual(dnd.state, BUSY)
        self.assertIn("486", dnd.events)

        pbx.set_dnd("Spotpear", False)
        pbx.set_online("Spotpear", False)
        offline = pbx.call_endpoint("WS3", "Spotpear")
        self.assertEqual(offline.state, UNAVAILABLE)
        self.assertIn("callee_offline", offline.events)

        no_trunk = pbx.call_trunk("Casa", "+390551234567")
        self.assertEqual(no_trunk.state, UNAVAILABLE)
        self.assertIn("trunk_unavailable", no_trunk.events)

    def test_conference_group_join_ring_auto_answer_and_leave_matrix(self) -> None:
        pbx = self._mini_pbx()
        room = pbx.call_conference_group("Spotpear", "CG Casa")
        self.assertEqual(room.state, IN_CALL)
        self.assertEqual(room.participants, ["Spotpear"])
        self.assertEqual(room.ringing, ["Casa", "Zoiper"])
        self.assertEqual(pbx.endpoint("Spotpear").peer, "CG Casa")
        self.assertEqual(pbx.endpoint("Casa").state, RINGING)

        room = pbx.answer_conference_invite("Casa", "CG Casa")
        self.assertEqual(room.participants, ["Spotpear", "Casa"])
        self.assertEqual(pbx.endpoint("Casa").state, IN_CALL)

        room = pbx.call_conference_group("WS3", "CG Casa")
        self.assertEqual(room.participants, ["Spotpear", "Casa", "WS3"])
        self.assertNotIn("WS3", room.ringing)
        self.assertEqual(pbx.endpoint("WS3").state, IN_CALL)

        room = pbx.leave_conference("Casa", "CG Casa")
        self.assertEqual(room.state, IN_CALL)
        self.assertEqual(room.participants, ["Spotpear", "WS3"])
        self.assertEqual(pbx.endpoint("Casa").state, IDLE)

        room = pbx.leave_conference("Spotpear", "CG Casa")
        self.assertEqual(room.state, IN_CALL)
        room = pbx.leave_conference("WS3", "CG Casa")
        self.assertEqual(room.state, ENDED)
        self.assertIn("room_ended", room.events)

    def test_conference_group_no_ring_members_still_allows_manual_join(self) -> None:
        pbx = self._mini_pbx(ha_ring=False)
        pbx.set_group_membership("Zoiper", conference_ring=False)
        room = pbx.call_conference_group("Spotpear", "CG Casa")
        self.assertEqual(room.participants, ["Spotpear"])
        self.assertEqual(room.ringing, [])

        room = pbx.call_conference_group("WS3", "CG Casa")
        self.assertEqual(room.participants, ["Spotpear", "WS3"])
        self.assertEqual(pbx.endpoint("WS3").state, IN_CALL)

    def test_chaos_matrix_leaves_no_active_runtime_state(self) -> None:
        results, errors = run_matrix(("chaos",))
        self.assertEqual(errors, [])
        result = results[0]
        self.assertEqual(result["double_answer_rejected"], 100)
        self.assertEqual(result["owner_leave_kept_room"], 100)
        self.assertEqual(result["runtime"]["active_endpoints"], {})
        self.assertIsNone(result["runtime"]["active_ring"])
        self.assertEqual(result["runtime"]["active_conferences"], {})


if __name__ == "__main__":
    unittest.main()
