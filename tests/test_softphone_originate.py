"""Behavioral tests for outbound softphone call routing."""

from __future__ import annotations

import asyncio
from enum import Enum
import importlib.util
from pathlib import Path
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "softphone_originate.py"


class _ServiceValidationError(Exception):
    def __init__(self, message: str, **kwargs) -> None:
        super().__init__(message)
        self.translation_domain = kwargs.get("translation_domain")
        self.translation_key = kwargs.get("translation_key")
        self.translation_placeholders = kwargs.get("translation_placeholders")


class _RouteAction(Enum):
    ANSWER_HA = "answer_ha"
    TRUNK = "trunk"
    REJECT = "reject"
    GROUP = "group"
    ASSIST = "assist"
    AUTOMATION = "automation"
    VOICEMAIL = "voicemail"
    PARKING = "parking"
    DIRECT = "direct"
    FORWARD = "forward"
    BRIDGE = "bridge"

    @property
    def is_local_application(self):
        return self in {self.ASSIST, self.AUTOMATION, self.VOICEMAIL, self.PARKING}


class _Availability(Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    OFFLINE = "offline"


class _EndpointKind(Enum):
    BROWSER = "browser"
    ESPHOME = "esphome"

    @property
    def is_softphone(self):
        return self is self.BROWSER



class _OfflinePolicy(Enum):
    WAIT = "wait"
    FORWARD = "forward"


@pytest.fixture
def softphone_originate(monkeypatch):
    package_name = "voip_stack_softphone_originate_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(MODULE.parent)]
    monkeypatch.setitem(sys.modules, package_name, package)

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    core.ServiceCall = type("ServiceCall", (), {})
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.ServiceValidationError = _ServiceValidationError
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", exceptions)

    dependencies = {
        "entity_identity": {"phonebook_state": lambda hass: hass.states.get("sensor.voip_phonebook")},
        "trunk_selection": {
            "select_call_trunk": lambda hass, **_kwargs: SimpleNamespace(
                identity="", config=sys.modules[f"{module.__package__}.config"].trunk_config(hass), client=sys.modules[f"{module.__package__}.runtime_data"].sip_trunk(hass),
            ),
        },
        "inbound_routing.targets": {
            # This suite isolates origination; real registry/dial-plan policy
            # decisions are exercised by test_phone_routing_ha.py.
            "resolve_phone_target": lambda **kwargs: SimpleNamespace(
                decision=kwargs["decision"], endpoint=None, failure=None,
            ),
        },
        "call_projection": {},
        "audio_format": {"HA_TRUNK_AUDIO_FORMATS": []},
        "authorization": {"async_require_service_admin": AsyncMock()},
            "config": {
                "transport_config": Mock(return_value={}),
                "trunk_config": Mock(return_value={}),
                "trunk_enabled": Mock(return_value=False),
                "trunk_identity_uri": lambda cfg: (
                    f"sip:{cfg['trunk_username']}@"
                    f"{cfg.get('trunk_domain') or cfg.get('trunk_server')}"
                ),
                "trunk_leg_identity": lambda cfg, fallback="": str(
                    cfg.get("trunk_username") or fallback
                ).strip(),
            },
        "const": {
            "CONF_SIP_VIDEO": "sip_video",
            "CONF_TRUNK_AUTH_USERNAME": "trunk_auth_username",
            "CONF_TRUNK_DOMAIN": "trunk_domain",
            "CONF_TRUNK_OUTBOUND_PROXY": "trunk_outbound_proxy",
            "CONF_TRUNK_PASSWORD": "trunk_password",
            "CONF_TRUNK_PORT": "trunk_port",
            "CONF_TRUNK_SERVER": "trunk_server",
            "CONF_TRUNK_TRANSPORT": "trunk_transport",
            "CONF_TRUNK_USERNAME": "trunk_username",
            "CONF_VIDEO_CAMERA_SEND": "video_camera_send",
            "DOMAIN": "voip_stack",
            "HA_PEER_FALLBACK_NAME": "Home Assistant",
            "HA_SOFTPHONE_DEVICE_ID": "ha-device",
        },
        "endpoint_lifecycle": {
            "call_registry": Mock(),
            "create_runtime_task": Mock(),
        },
        "endpoint_termination": {"EndpointTerminationHandler": Mock()},
        "endpoint_session": {
            "TerminationInitiator": SimpleNamespace(
                LOCAL_USER="local_user",
                RUNTIME="runtime",
            )
        },
        "endpoint_registry": {
            "EndpointBusyError": type("EndpointBusyError", (Exception,), {})
        },
        "endpoint_routing": {
            "ha_router_decision": Mock(),
            "device_formats": Mock(return_value=[]),
            "roster_entry_formats": Mock(return_value=[]),
            "sip_target_audio_profile": Mock(return_value=([], [])),
            "sip_target_rtp_audio_profile": Mock(return_value=None),
            "sip_target_has_unspecified_audio": Mock(return_value=False),
            "supports_directional_audio_payloads": Mock(return_value=False),
        },
        "esphome_actions": {
            "async_call_action": AsyncMock(),
            "async_resolve_source_device": AsyncMock(return_value=None),
            "async_resolve_target_device": AsyncMock(return_value=None),
        },
        "fsm": {
            "CallState": SimpleNamespace(
                IDLE=SimpleNamespace(value="idle"),
                IN_CALL=SimpleNamespace(value="in_call"),
            ),
            "TerminalReason": SimpleNamespace(),
            "sip_public_state": Mock(),
            "sip_terminal_reason": Mock(),
        },
        "media_ports": {
            "allocate_sip_rtp_port": Mock(return_value=40000),
            "bind_sip_rtp_socket": Mock(),
            "reserve_sip_video_media": Mock(),
        },
            "outbound_lifecycle": {
                "observe_outbound_call_result": lambda registry, call_id, **kwargs: registry.upsert(call_id, owner="ha_softphone", **kwargs),
                "HA_SOFTPHONE_ACTIVE_STATES": frozenset({"calling", "in_call"}),
                "attach_outbound_connected_identity_state": Mock(),
                "async_prepare_ha_outbound_call": AsyncMock(),
                "async_track_outbound_sip_client": AsyncMock(),
            },
        "peer_snapshot": {"async_advertise_host": AsyncMock(return_value="127.0.0.1")},
        "phone_endpoint": {
            "DEFAULT_ENDPOINT_ID": "default",
            "EndpointAvailability": _Availability,
            "EndpointKind": _EndpointKind,
            "OfflinePolicy": _OfflinePolicy,
        },
        "router": {
            "RouteAction": _RouteAction,
            "RouteReason": SimpleNamespace(DIRECT_URI="direct_uri", TRUNK_UNAVAILABLE="trunk_unavailable"),
            "ha_uri_for": Mock(),
            "resolve_ha_router": Mock(),
        },
            "runtime_data": {
                "call_runtime_artifacts": lambda hass: hass.artifacts,
            "endpoint_directory": lambda hass: hass.data.get("voip_stack", {}).get(
                "endpoint_registry",
                SimpleNamespace(get=lambda _endpoint_id: None),
            ),
            "sip_registrar": lambda hass: hass.data.get("voip_stack", {}).get(
                "sip_registrar"
            ),
            "sip_trunk": lambda hass: hass.data.get("voip_stack", {}).get(
                "sip_trunk"
            ),
            "require_runtime_data": lambda hass: hass.runtime,
        },
        "service_endpoints": {
            "async_require_phone_service_control": AsyncMock(),
            "browser_endpoint_name": Mock(return_value="Casa"),
            "service_browser_endpoint": Mock(return_value=("default", None)),
        },
        "sip_runtime": {
            "enable_reused_tcp_connection": Mock(),
            "uri_transport": Mock(),
        },
        "softphone_commands": {"bind_service_call_controller": Mock()},
        "websocket_api": {
            "_fire_call_event": Mock(),
            "_ha_softphone_store": Mock(return_value={}),
            "_set_ha_softphone_call_state": Mock(),
        },
        "roster": {"parse_roster_json": Mock(return_value=[])},
        "sip": {"parse_sip_uri": Mock()},
        "sip_client": {"SIP_TIMER_B": 32.0, "SipCallClient": object},
        "local_softphone_bridge": {
            "LocalBridgeError": type("LocalBridgeError", (Exception,), {})
        },
        "local_softphone_runtime": {"start_local_softphone_call": Mock()},
    }
    for name, values in dependencies.items():
        dependency = types.ModuleType(f"{package_name}.{name}")
        for key, value in values.items():
            setattr(dependency, key, value)
        monkeypatch.setitem(sys.modules, dependency.__name__, dependency)

    call_projection = sys.modules[f"{package_name}.call_projection"]

    def publish_phone_projection(hass, session, endpoint_id, **details) -> bool:
        sys.modules[f"{package_name}.websocket_api"]._set_ha_softphone_call_state(
            hass,
            session.state,
            endpoint_id=endpoint_id,
            caller=session.caller,
            callee=session.callee,
            call_id=session.call_id,
            **details,
        )
        return True

    call_projection.publish_phone_projection = publish_phone_projection

    module_name = f"{package_name}.softphone_originate"
    spec = importlib.util.spec_from_file_location(module_name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


def _call(hass, **data):
    return SimpleNamespace(hass=hass, data=data, context=object())


def test_provider_leg_uses_trunk_account_identity(softphone_originate) -> None:
    assert (
        softphone_originate._trunk_leg_identity(
            {"trunk_username": "+41440000000"},
            "Kitchen tablet",
        )
        == "+41440000000"
    )


def test_sip_start_failure_logs_location_without_exception_secrets(
    softphone_originate, monkeypatch, caplog,
) -> None:
    module = softphone_originate
    endpoint = SimpleNamespace(
        endpoint_id="caller", device_id="browser", sip_uri_user="browser",
        availability=_Availability.AVAILABLE, supports=lambda _cap: True,
    )
    hass = SimpleNamespace(
        data={"voip_stack": {}}, states=SimpleNamespace(get=lambda _: None),
    )
    entry = SimpleNamespace(metadata={}, sip_uri="sip:door@192.0.2.10", display_name="Door")
    route = SimpleNamespace(action=_RouteAction.DIRECT, reason=None, entry=entry, sip_uri=entry.sip_uri)
    module.ha_router_decision = Mock(return_value=route)
    module._async_resolve_browser_destination = AsyncMock(return_value=(route, "Door", None))
    module._get_transport_config = Mock(return_value={"sip_port": 5060, "sip_video": False})
    module.CallState.CALLING = SimpleNamespace(value="calling")
    module.TerminalReason.TRANSPORT_UNREACHABLE = SimpleNamespace(value="transport_unreachable")
    registry = module._call_registry.return_value
    registry.upsert.return_value = SimpleNamespace(
        call_id="test-call", state="calling", owner="ha_softphone",
        caller="Casa", callee="Door", generation=1,
    )
    failure = ValueError("credential-DO-NOT-LOG")
    client = SimpleNamespace(dialog_ids=SimpleNamespace(call_id="test-call"), invite=AsyncMock(side_effect=failure))
    sys.modules[f"{module.__package__}.sip_client"].SipCallClient = Mock(return_value=client)
    sys.modules[f"{module.__package__}.sip_runtime"].configure_local_contact = Mock()
    sys.modules[f"{module.__package__}.endpoint_session"].CleanupStage = SimpleNamespace(OBSERVER="observer")
    for name, attributes in {
        "browser_dialog_updates": {"attach_browser_dialog_updates": Mock()},
        "dtmf_events": {"attach_direct_client_dtmf_events": Mock()},
        "browser_playback": {"BrowserPlayback": Mock(return_value=SimpleNamespace(close=Mock()))},
    }.items():
        dependency = types.ModuleType(f"{module.__package__}.{name}")
        dependency.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, dependency.__name__, dependency)
    terminate = AsyncMock()
    module.EndpointTerminationHandler = Mock(return_value=SimpleNamespace(terminate_reason=terminate))

    with pytest.raises(_ServiceValidationError) as caught:
        asyncio.run(module.async_originate_browser_call(
            _call(hass, destination="Door"), endpoint_id="caller", browser_endpoint=endpoint,
        ))

    assert caught.value.translation_key == "sip_call_start_failed"
    assert caught.value.__cause__ is failure
    terminate.assert_awaited_once_with("test-call", "transport_unreachable", "runtime")
    assert "SIP call setup failed type=ValueError" in caplog.text
    assert "softphone_originate.py:async_originate_browser_call:" in caplog.text
    assert "credential-DO-NOT-LOG" not in caplog.text
    assert str(ROOT) not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("profile", ["directional_unspecified", "directional_explicit", "registered_generic"])
def test_registered_peer_offer_preserves_directional_profile(
    softphone_originate, monkeypatch, profile,
) -> None:
    module = softphone_originate
    media = importlib.import_module(f"{module.__package__}.core.sdp")
    audio = importlib.import_module(f"{module.__package__}.core.audio_format")
    directional = profile != "registered_generic"
    endpoint = SimpleNamespace(
        endpoint_id="caller", device_id="browser", sip_uri_user="browser",
        availability=_Availability.AVAILABLE, supports=lambda _cap: True,
    )
    metadata = {"registered": True, "endpoint_kind": "esphome" if directional else "sip_account",
                "tx_formats": [], "rx_formats": [], "sip_audio_tx_formats": [],
                "sip_audio_rx_formats": [], "sdp_features": ["directional_audio_v1"] if directional else []}
    entry = SimpleNamespace(metadata=metadata, sip_uri="sip:esphome-ha@192.0.2.10:5060;transport=udp", display_name="Panel")
    route = SimpleNamespace(action=_RouteAction.DIRECT, reason=None, entry=entry, sip_uri=entry.sip_uri)
    hass = SimpleNamespace(data={"voip_stack": {}}, states=SimpleNamespace(get=lambda _: None))
    module.ha_router_decision = Mock(return_value=route)
    module._async_resolve_browser_destination = AsyncMock(return_value=(route, "Panel", None))
    module._get_transport_config = Mock(return_value={"sip_port": 5060, "sip_video": False})
    module._supports_directional_audio_payloads = Mock(return_value=directional)
    defaults = [fmt for fmt in audio.HA_SIP_PCM_TX_FORMATS if fmt.frame_ms == 10]
    module._sip_target_audio_profile = Mock(return_value=(defaults, defaults))
    if profile == "directional_explicit":
        send = media.RtpPcmFormat(96, "L16", 48000, 1, 10)
        recv = media.RtpPcmFormat(97, "L16", 16000, 1, 10)
        module._sip_target_rtp_audio_profile = Mock(return_value=SimpleNamespace(
            send_formats=(send.audio_format,), recv_formats=(recv.audio_format,),
            send_rtp_formats=(send,), recv_rtp_formats=(recv,),
            sdp_features=frozenset({"directional_audio_v1"}),
        ))
    module.CallState.CALLING = SimpleNamespace(value="calling")
    module.TerminalReason.TRANSPORT_UNREACHABLE = SimpleNamespace(value="transport_unreachable")
    module.EndpointTerminationHandler = Mock(return_value=SimpleNamespace(terminate_reason=AsyncMock()))
    module._call_registry.return_value.upsert.return_value = SimpleNamespace(
        call_id="test-call", state="calling", owner="ha_softphone",
        caller="Casa", callee="Panel", generation=1,
    )
    sys.modules[f"{module.__package__}.sip_runtime"].configure_local_contact = Mock()
    sys.modules[f"{module.__package__}.endpoint_session"].CleanupStage = SimpleNamespace(OBSERVER="observer")
    for name, attributes in {
        "browser_dialog_updates": {"attach_browser_dialog_updates": Mock()},
        "dtmf_events": {"attach_direct_client_dtmf_events": Mock()},
        "browser_playback": {"BrowserPlayback": Mock(return_value=SimpleNamespace(close=Mock()))},
    }.items():
        dependency = types.ModuleType(f"{module.__package__}.{name}")
        dependency.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, dependency.__name__, dependency)
    captured = {}

    class OfferBuilt(BaseException):
        pass

    def client_factory(**kwargs):
        captured.update(kwargs)

        async def invite(**_kwargs):
            captured["offer"] = media.build_offer_directional(
                "192.0.2.1", "192.0.2.1", kwargs["local_rtp_port"],
                kwargs["supported_send_formats"], kwargs["supported_recv_formats"],
                send_rtp_formats=kwargs["supported_send_rtp_formats"],
                recv_rtp_formats=kwargs["supported_recv_rtp_formats"],
                include_common_codecs=kwargs["include_common_codecs"],
                allow_directional_payloads=kwargs["allow_directional_audio_payloads"],
            )
            raise OfferBuilt

        return SimpleNamespace(dialog_ids=SimpleNamespace(call_id="test-call"), invite=invite)

    sys.modules[f"{module.__package__}.sip_client"].SipCallClient = client_factory
    with pytest.raises(OfferBuilt):
        asyncio.run(module.async_originate_browser_call(
            _call(hass, destination="Panel"), endpoint_id="caller", browser_endpoint=endpoint,
        ))
    parsed = media.offered_pcm_formats(captured["offer"])
    assert {fmt.frame_ms for fmt in parsed} == ({10} if directional else {20})
    if profile == "directional_explicit":
        assert captured["supported_send_rtp_formats"] == (send,)
        assert captured["supported_recv_rtp_formats"] == (recv,)
        assert "a=x-voip-stack-flow:96 send\r\n" in captured["offer"]
        assert "a=x-voip-stack-flow:97 recv\r\n" in captured["offer"]
    elif directional:
        assert any(fmt.encoding == "L16" and fmt.sample_rate == 48000 for fmt in parsed)
    else:
        assert captured["supported_send_formats"] == list(audio.HA_TRUNK_AUDIO_FORMATS)
        assert any(fmt.encoding == "PCMA" for fmt in parsed)
        assert any(fmt.encoding == "PCMU" for fmt in parsed)


def test_direct_leg_preserves_local_display_name(softphone_originate) -> None:
    assert softphone_originate._trunk_leg_identity({}, "Kitchen tablet") == (
        "Kitchen tablet"
    )


def test_provider_identity_uri_uses_logical_domain(softphone_originate) -> None:
    assert (
        softphone_originate._trunk_identity_uri(
            {
                "trunk_username": "+41440000000",
                "trunk_domain": "voip.example",
                "trunk_server": "registrar.example",
            },
        )
        == "sip:+41440000000@voip.example"
    )


def test_provider_identity_uri_falls_back_to_server(softphone_originate) -> None:
    assert (
        softphone_originate._trunk_identity_uri(
            {
                "trunk_username": "427",
                "trunk_domain": "",
                "trunk_server": "pbx.example",
            },
        )
        == "sip:427@pbx.example"
    )


def test_browser_to_browser_uses_local_bridge_before_network(
    softphone_originate,
) -> None:
    hass = SimpleNamespace(
        data={"voip_stack": {}},
        config=SimpleNamespace(location_name="Casa"),
        states=SimpleNamespace(get=Mock(return_value=None)),
    )
    source_endpoint = SimpleNamespace(
        endpoint_id="casa",
        device_id="device-casa",
        name="Casa",
        availability=_Availability.AVAILABLE,
        supports=Mock(return_value=True),
    )
    destination_endpoint = SimpleNamespace(endpoint_id="test", name="Test")
    route = SimpleNamespace(action=_RouteAction.ANSWER_HA, entry=object(), reason=None)
    softphone_originate._require_phone_service_control = AsyncMock()
    softphone_originate._get_transport_config = Mock(
        return_value={"video_camera_send": True}
    )
    softphone_originate.ha_router_decision = Mock(return_value=route)
    softphone_originate._async_resolve_browser_destination = AsyncMock(
        return_value=(route, "Test", destination_endpoint)
    )
    softphone_originate._async_prepare_ha_outbound_call = AsyncMock()
    snapshot = SimpleNamespace(call_id="local-1", video_enabled=True)
    local_runtime = sys.modules[
        f"{softphone_originate.__package__}.local_softphone_runtime"
    ]
    local_runtime.start_local_softphone_call = Mock(return_value=snapshot)
    call = _call(
        hass,
        destination="Test",
        media_client_id="browser-casa",
        send_video=True,
    )

    asyncio.run(
        softphone_originate.async_originate_browser_call(
            call,
            endpoint_id="casa",
            browser_endpoint=source_endpoint,
        )
    )

    local_runtime.start_local_softphone_call.assert_called_once_with(
        hass,
        "casa",
        "test",
        request_video=True,
        enable_caller_video_send=True,
        caller_owner_id="browser-casa",
        context=call.context,
    )
    softphone_originate._ha_advertise_host.assert_not_awaited()


def test_offline_browser_phone_remains_a_local_ringing_destination(
    softphone_originate,
) -> None:
    destination = SimpleNamespace(
        endpoint_id="casa",
        kind=_EndpointKind.BROWSER,
        name="Casa",
        availability=_Availability.OFFLINE,
        dnd=False,
        active_call_id="",
    )
    endpoint_registry = SimpleNamespace(get=Mock(return_value=destination))
    hass = SimpleNamespace(data={"voip_stack": {"endpoint_registry": endpoint_registry}})
    route = SimpleNamespace(
        action=_RouteAction.ANSWER_HA,
        entry=SimpleNamespace(metadata={"endpoint_id": "casa"}),
    )

    resolved = asyncio.run(
        softphone_originate._async_resolve_browser_destination(
            hass,
            route=route,
            target="Casa",
            source_endpoint_id="test",
        )
    )

    assert resolved == (route, "Casa", destination)


@pytest.mark.parametrize(
    ('profile', 'camera', 'global_video', 'source_video', 'kind', 'peer_video', 'camera_allowed', 'expected'),
    [
        ('dahua', False, True, True, '', True, True, 'recvonly_mode0'),
        ('dahua', False, True, True, 'sip_account', True, True, 'recvonly_mode0'),
        ('dahua', True, True, True, '', True, True, 'sendrecv_mode1'),
        ('', False, True, True, '', True, True, 'recvonly_mode1'),
        ('', True, True, True, '', True, True, 'sendrecv_mode1'),
        ('dahua', False, False, True, '', True, True, 'audio_only'),
        ('dahua', False, True, False, '', True, True, 'audio_only'),
        ('dahua', False, True, True, 'sip_account', False, True, 'audio_only'),
        ('', True, True, True, 'esphome', False, True, 'audio_only'),
        ('', True, True, True, 'esphome', True, True, 'sendrecv_mode1'),
        ('dahua', False, True, True, 'esphome', True, True, 'recvonly_mode1'),
        ('dahua', False, True, True, '', True, False, 'recvonly_mode0'),
        ('', True, True, True, '', True, False, 'recvonly_mode1'),
    ],
)
@pytest.mark.parametrize("video_codec", ["h264", "jpeg"])
def test_outgoing_peer_video_offer_respects_camera_and_capabilities(
    softphone_originate, profile, camera, global_video, source_video,
    kind, peer_video, camera_allowed, expected, video_codec,
):
    module = softphone_originate
    endpoint = SimpleNamespace(
        endpoint_id='caller', device_id='browser', sip_uri_user='browser',
        availability=_Availability.AVAILABLE,
        supports=lambda capability: source_video if capability == 'video' else True,
    )
    metadata = {'sip_profile': profile, 'sip_video_codec': video_codec, 'registered': True}
    if kind:
        metadata['endpoint_kind'] = kind
        metadata['endpoint_id'] = 'destination'
    target = SimpleNamespace(
        kind=SimpleNamespace(is_softphone=False), dnd=False, active_call_id='',
        availability=_Availability.AVAILABLE, device_id='peer',
        supports=lambda capability: peer_video if capability == 'video' else True,
    )
    hass = SimpleNamespace(
        data={'voip_stack': {'endpoint_registry': SimpleNamespace(get=lambda _: target if kind else None)}},
        states=SimpleNamespace(get=lambda _: None),
    )
    entry = SimpleNamespace(metadata=metadata, sip_uri='sip:door@192.0.2.10', display_name='Door')
    route = SimpleNamespace(action=_RouteAction.DIRECT, reason=None, entry=entry, sip_uri=entry.sip_uri)
    module.ha_router_decision = Mock(return_value=route)
    module._async_resolve_browser_destination = AsyncMock(return_value=(route, 'Door', None))
    module._get_transport_config = Mock(return_value={
        'sip_port': 5060, 'sip_video': global_video, 'video_camera_send': camera_allowed,
    })
    module.reserve_sip_video_media = Mock(return_value=(SimpleNamespace(ports=(40000, 40002)), object(), object()))
    captured = {}
    class OfferCaptured(Exception):
        pass
    def capture(**kwargs):
        captured.update(kwargs)
        raise OfferCaptured
    sys.modules[f'{module.__package__}.sip_client'].SipCallClient = capture
    with pytest.raises(OfferCaptured):
        asyncio.run(module.async_originate_browser_call(
            _call(hass, destination='Door', send_video=camera),
            endpoint_id='caller', browser_endpoint=endpoint,
        ))
    if expected == 'audio_only':
        assert captured['video_formats'] == ()
        assert captured['local_video_rtp_port'] == 0
        module.reserve_sip_video_media.assert_not_called()
    else:
        fmt, = captured['video_formats']
        assert captured['local_video_rtp_port'] == 40002
        assert fmt.encoding == video_codec.upper()
        assert captured['video_direction'] == ('sendrecv' if expected == 'sendrecv_mode1' else 'recvonly')
        if video_codec == 'jpeg':
            assert fmt.payload_type == 26
        elif expected == 'recvonly_mode0':
            assert captured['video_direction'] == 'recvonly'
            assert fmt.packetization_mode == 0
            assert fmt.profile_level_id == '42001f'
            assert not fmt.level_asymmetry_allowed
        else:
            assert captured['video_direction'] == ('recvonly' if expected == 'recvonly_mode1' else 'sendrecv')
            assert fmt.packetization_mode == 1


@pytest.mark.parametrize("action,uri", [
    (_RouteAction.TRUNK, ""),
    (_RouteAction.DIRECT, "sip:441234567890@provider.example"),
])
def test_restricted_browser_rejects_trunk_before_call_start(softphone_originate, action, uri):
    module = softphone_originate
    hass = SimpleNamespace(data={"voip_stack": {}}, states=SimpleNamespace(get=lambda _: None))
    source = SimpleNamespace(endpoint_id="caller", device_id="browser", availability=_Availability.AVAILABLE)
    route = SimpleNamespace(action=action, sip_uri=uri, entry=None, reason=None)
    module._get_transport_config = Mock(return_value={"external_call_blocked_endpoints": ["caller"]})
    sys.modules[f"{module.__package__}.config"].trunk_config = Mock(return_value={"trunk_server": "provider.example"})
    hass.runtime = SimpleNamespace(trunk_configs={}, trunk_config=sys.modules[f"{module.__package__}.config"].trunk_config(hass), sip=None)
    module.ha_router_decision = Mock(return_value=route)
    module._async_resolve_browser_destination = AsyncMock(return_value=(route, "441234567890", None))
    module._async_prepare_ha_outbound_call = AsyncMock()
    with pytest.raises(_ServiceValidationError) as caught:
        asyncio.run(module.async_originate_browser_call(
            _call(hass, destination="441234567890"), endpoint_id="caller", browser_endpoint=source,
        ))
    assert caught.value.translation_key == "external_calls_disabled"
    module._async_prepare_ha_outbound_call.assert_not_awaited()


@pytest.mark.parametrize("via_trunk", [False, True])
@pytest.mark.parametrize("audio_choice,expect_pcm", [("auto", False), ("standard", False), ("pcm", True)])
def test_static_dahua_contact_builds_full_offer_for_direct_and_trunk(
    softphone_originate, via_trunk, audio_choice, expect_pcm,
):
    """Real SDP generation proves persisted contact choices survive routing."""
    module = softphone_originate
    endpoint = SimpleNamespace(
        endpoint_id="caller", device_id="browser", sip_uri_user="browser",
        availability=_Availability.AVAILABLE, supports=lambda _cap: True,
    )
    hass = SimpleNamespace(
        data={"voip_stack": {"sip_trunk": SimpleNamespace(ready=True)}},
        states=SimpleNamespace(get=lambda _: None),
    )
    metadata = {"sip_profile": "dahua", "dahua_audio": audio_choice}
    entry = SimpleNamespace(metadata=metadata, sip_uri="" if via_trunk else "sip:8001@192.0.2.10", display_name="Door")
    route = SimpleNamespace(
        action=_RouteAction.TRUNK if via_trunk else _RouteAction.DIRECT,
        reason=None, entry=entry, sip_uri=entry.sip_uri, target="8001",
    )
    module.ha_router_decision = Mock(return_value=route)
    module._async_resolve_browser_destination = AsyncMock(return_value=(route, "8001", None))
    module._get_transport_config = Mock(return_value={"sip_port": 5060, "sip_video": True, "video_camera_send": True})
    sys.modules[f"{module.__package__}.config"].trunk_config = Mock(return_value={
        "trunk_server": "pbx.example.test", "trunk_port": 5060, "trunk_transport": "udp", "trunk_username": "ha",
    })
    module._trunk_enabled = Mock(return_value=True)
    module.reserve_sip_video_media = Mock(return_value=(SimpleNamespace(ports=(40000, 40002)), object(), object()))
    captured = {}

    class OfferCaptured(Exception):
        pass

    def capture(**kwargs):
        captured.update(kwargs)
        raise OfferCaptured

    sys.modules[f"{module.__package__}.sip_client"].SipCallClient = capture
    with pytest.raises(OfferCaptured):
        asyncio.run(module.async_originate_browser_call(
            _call(hass, destination="8001", send_video=False), endpoint_id="caller", browser_endpoint=endpoint,
        ))
    assert captured["include_dahua_pcm"] is expect_pcm
    assert captured["include_common_codecs"] is True
    assert captured["supported_send_rtp_formats"] is None
    assert captured["supported_recv_rtp_formats"] is None
    assert {fmt.frame_ms for fmt in captured["supported_send_formats"]} == {20}
    assert captured["peer_user_agent"] == ""  # No fabricated registration/User-Agent.
    media = importlib.import_module(f"{module.__package__}.core.sdp")
    offer = media.build_offer_directional(
        captured["local_ip"], captured["local_ip"], captured["local_rtp_port"],
        captured["supported_send_formats"], captured["supported_recv_formats"],
        include_common_codecs=captured["include_common_codecs"],
        include_dahua_pcm=captured["include_dahua_pcm"],
        video_port=captured["local_video_rtp_port"], video_formats=captured["video_formats"],
        video_direction=captured["video_direction"],
    )
    audio = media.offered_pcm_formats(offer, allow_dahua_pcm=True)
    assert any(fmt.encoding == "PCMU" for fmt in audio)
    assert any(fmt.encoding == "PCMA" for fmt in audio)
    assert any(fmt.encoding == "L16" and fmt.sample_rate == 16000 for fmt in audio)
    assert any(fmt.encoding == "PCM" for fmt in audio) is expect_pcm
    assert "a=ptime:20\r\n" in offer
    video = media.parse_video_sdp(offer)
    assert video["direction"] == "recvonly"
    assert "packetization-mode=0" in offer
    assert "profile-level-id=42001f" in offer


@pytest.mark.parametrize("allow_video", [True, False])
def test_trunk_video_reception_is_independent_from_camera_capture(softphone_originate, allow_video):
    module = softphone_originate
    endpoint = SimpleNamespace(
        endpoint_id="caller", device_id="browser", sip_uri_user="browser",
        availability=_Availability.AVAILABLE, supports=lambda _cap: True,
    )
    hass = SimpleNamespace(
        data={"voip_stack": {"sip_trunk": SimpleNamespace(ready=True)}},
        states=SimpleNamespace(get=lambda _: None),
    )
    route = SimpleNamespace(action=_RouteAction.TRUNK, reason=None, entry=None, sip_uri="", target="441234567890")
    module.ha_router_decision = Mock(return_value=route)
    module._async_resolve_browser_destination = AsyncMock(return_value=(route, route.target, None))
    module._get_transport_config = Mock(return_value={"sip_port": 5060, "sip_video": True, "video_camera_send": True})
    sys.modules[f"{module.__package__}.config"].trunk_config = Mock(return_value={"trunk_server": "pbx.example.test", "trunk_port": 5060, "trunk_transport": "udp", "trunk_username": "ha", "trunk_video_codecs": ["H264"] if allow_video else []})
    module._trunk_enabled = Mock(return_value=True)
    module.reserve_sip_video_media = Mock(return_value=(SimpleNamespace(ports=(40000, 40002)), object(), object()))
    captured = {}

    class OfferCaptured(Exception):
        pass

    def capture(**kwargs):
        captured.update(kwargs)
        raise OfferCaptured

    sys.modules[f"{module.__package__}.sip_client"].SipCallClient = capture
    with pytest.raises(OfferCaptured):
        asyncio.run(module.async_originate_browser_call(
            _call(hass, destination=route.target, send_video=False), endpoint_id="caller", browser_endpoint=endpoint,
        ))
    assert bool(captured["video_formats"]) is allow_video
    assert captured["local_video_rtp_port"] == (40002 if allow_video else 0)
    assert captured["video_direction"] == "recvonly"
    assert captured["include_dahua_pcm"] is False
    assert {fmt.frame_ms for fmt in captured["supported_send_formats"]} == {20}
    if not allow_video:
        module.reserve_sip_video_media.assert_not_called()


@pytest.mark.parametrize("action", [_RouteAction.ASSIST, _RouteAction.AUTOMATION, _RouteAction.VOICEMAIL, _RouteAction.PARKING])
def test_every_local_application_uses_configured_pbx_listener_without_phonebook_hack(softphone_originate, action):
    from dataclasses import dataclass
    import importlib

    module = softphone_originate
    @dataclass
    class Decision:
        action: object
        target: str = "application"
        sip_uri: str = ""
        entry: object = None
        reason: object = None
    route = Decision(action, entry=SimpleNamespace(display_name="Application", metadata={}, sip_uri=""))
    hass = SimpleNamespace(data={"voip_stack": {}}, states=SimpleNamespace(get=lambda _: None))
    endpoint = SimpleNamespace(endpoint_id="caller", device_id="browser", sip_uri_user="browser",
                               availability=_Availability.AVAILABLE, supports=lambda _: False)
    module.ha_router_decision = Mock(return_value=route)
    module._async_resolve_browser_destination = AsyncMock(return_value=(route, route.target, None))
    module._get_transport_config = Mock(return_value={"sip_port": 15060})
    core = importlib.import_module(f"{module.__package__}.core.sip")
    original = core.parse_sip_uri
    class Resolved(Exception):
        pass
    def capture(value):
        assert value == "sip:application@127.0.0.1:15060"
        raise Resolved
    core.parse_sip_uri = capture
    try:
        with pytest.raises(Resolved):
            asyncio.run(module.async_originate_browser_call(
                _call(hass, destination="application"), endpoint_id="caller", browser_endpoint=endpoint,
            ))
    finally:
        core.parse_sip_uri = original
    module.ha_router_decision.assert_called_once_with(hass, "application", [], source_endpoint_id="caller", trunk_id="")
