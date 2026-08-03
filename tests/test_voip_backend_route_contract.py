#!/usr/bin/env python3
"""Static backend contracts for SIP route handling."""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path
import unittest

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.architecture
BACKEND = ROOT / "custom_components" / "voip_stack" / "endpoint_runtime.py"
MEDIA_RENEGOTIATION = (
    ROOT / "custom_components" / "voip_stack" / "media_renegotiation.py"
)
INIT = ROOT / "custom_components" / "voip_stack" / "__init__.py"
AUDIO_WS = ROOT / "custom_components" / "voip_stack" / "audio_ws_view.py"
CARD_JS = ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-card.js"
ENDPOINT_ROUTING = ROOT / "custom_components" / "voip_stack" / "endpoint_routing.py"
ENDPOINT_DIALING = ROOT / "custom_components" / "voip_stack" / "endpoint_dialing.py"
SIP_BRIDGE = ROOT / "custom_components" / "voip_stack" / "sip_bridge.py"
SENSOR = ROOT / "custom_components" / "voip_stack" / "sensor.py"
PEER_SNAPSHOT = ROOT / "custom_components" / "voip_stack" / "peer_snapshot.py"
SERVICES = ROOT / "custom_components" / "voip_stack" / "services.py"
WEBSOCKET_API = ROOT / "custom_components" / "voip_stack" / "websocket_api.py"
ACCOUNT_SERVICES = ROOT / "custom_components" / "voip_stack" / "account_services.py"
SERVICES_YAML = ROOT / "custom_components" / "voip_stack" / "services.yaml"
ICONS_JSON = ROOT / "custom_components" / "voip_stack" / "icons.json"
CONFIG_FLOW = ROOT / "custom_components" / "voip_stack" / "config_flow.py"
STRINGS_JSON = ROOT / "custom_components" / "voip_stack" / "strings.json"
AUTOMATION_ROUTING = ROOT / "custom_components" / "voip_stack" / "automation_routing.py"
ASSIST_ENDPOINT = ROOT / "custom_components" / "voip_stack" / "assist_endpoint.py"
ENDPOINT_TERMINATION = (
    ROOT / "custom_components" / "voip_stack" / "endpoint_termination.py"
)
SERVICE_ENDPOINTS = ROOT / "custom_components" / "voip_stack" / "service_endpoints.py"
ESPHOME_ACTIONS = ROOT / "custom_components" / "voip_stack" / "esphome_actions.py"
SOFTPHONE_COMMANDS = (
    ROOT / "custom_components" / "voip_stack" / "softphone_commands.py"
)
SOFTPHONE_TERMINATION = (
    ROOT / "custom_components" / "voip_stack" / "softphone_termination.py"
)
SOFTPHONE_ANSWER = (
    ROOT / "custom_components" / "voip_stack" / "softphone_answer.py"
)
SOFTPHONE_ORIGINATE = (
    ROOT / "custom_components" / "voip_stack" / "softphone_originate.py"
)
OUTBOUND_ATTEMPTS = (
    ROOT / "custom_components" / "voip_stack" / "outbound_attempts.py"
)
DTMF_EVENTS = ROOT / "custom_components" / "voip_stack" / "dtmf_events.py"
CONFIG_ENTRY_RUNTIME = (
    ROOT / "custom_components" / "voip_stack" / "config_entry_runtime.py"
)
PBX_ROUTING = ROOT / "custom_components" / "voip_stack" / "pbx_routing.py"
TRUNK_DTMF = ROOT / "custom_components" / "voip_stack" / "trunk_dtmf.py"
TRUNK_ROUTING = ROOT / "custom_components" / "voip_stack" / "trunk_routing.py"
TRUNK_INBOUND_ROUTER = (
    ROOT / "custom_components" / "voip_stack" / "trunk_inbound_router.py"
)
CALL_FORWARDER = ROOT / "custom_components" / "voip_stack" / "call_forwarder.py"
FORWARD_GROUP_CANDIDATES = (
    ROOT
    / "custom_components"
    / "voip_stack"
    / "forward_group_candidates.py"
)
RING_GROUP_ORCHESTRATOR = (
    ROOT / "custom_components" / "voip_stack" / "ring_group_orchestrator.py"
)
RING_GROUP_CANDIDATES = (
    ROOT / "custom_components" / "voip_stack" / "ring_group_candidates.py"
)
RING_GROUP_FORK = (
    ROOT / "custom_components" / "voip_stack" / "ring_group_fork.py"
)
CONFERENCE_RINGING = (
    ROOT / "custom_components" / "voip_stack" / "conference_ringing.py"
)
INVITE_ROUTER = ROOT / "custom_components" / "voip_stack" / "invite_router.py"
INBOUND_SOFTPHONE = (
    ROOT
    / "custom_components"
    / "voip_stack"
    / "inbound_routing"
    / "softphone.py"
)
INBOUND_BRIDGE = (
    ROOT
    / "custom_components"
    / "voip_stack"
    / "inbound_routing"
    / "bridge.py"
)
INBOUND_LOCAL = (
    ROOT
    / "custom_components"
    / "voip_stack"
    / "inbound_routing"
    / "local.py"
)
INBOUND_TRUNK = (
    ROOT
    / "custom_components"
    / "voip_stack"
    / "inbound_routing"
    / "trunk.py"
)
INBOUND_AUTOMATION = (
    ROOT
    / "custom_components"
    / "voip_stack"
    / "inbound_routing"
    / "automation.py"
)
INBOUND_TARGETS = (
    ROOT
    / "custom_components"
    / "voip_stack"
    / "inbound_routing"
    / "targets.py"
)


class VoipBackendRouteContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = BACKEND.read_text()
        cls.init_source = INIT.read_text()
        cls.service_endpoints = SERVICE_ENDPOINTS.read_text()
        cls.esphome_actions = ESPHOME_ACTIONS.read_text()
        cls.softphone_commands = SOFTPHONE_COMMANDS.read_text()
        cls.softphone_answer = SOFTPHONE_ANSWER.read_text()
        cls.softphone_originate = SOFTPHONE_ORIGINATE.read_text()
        cls.outbound_attempts = OUTBOUND_ATTEMPTS.read_text()
        cls.dtmf_events = DTMF_EVENTS.read_text()
        cls.endpoint_routing = ENDPOINT_ROUTING.read_text()
        cls.endpoint_dialing = ENDPOINT_DIALING.read_text()
        cls.config_entry_runtime = CONFIG_ENTRY_RUNTIME.read_text()
        cls.pbx_routing = PBX_ROUTING.read_text()
        cls.trunk_dtmf = TRUNK_DTMF.read_text()
        cls.trunk_routing = TRUNK_ROUTING.read_text()
        cls.trunk_inbound_router = TRUNK_INBOUND_ROUTER.read_text()
        cls.call_forwarder = CALL_FORWARDER.read_text()
        cls.forward_group_candidates = FORWARD_GROUP_CANDIDATES.read_text()
        cls.ring_group = RING_GROUP_ORCHESTRATOR.read_text()
        cls.ring_group_candidates = RING_GROUP_CANDIDATES.read_text()
        cls.ring_group_fork = RING_GROUP_FORK.read_text()
        cls.conference_ringing = CONFERENCE_RINGING.read_text()
        cls.invite_router = INVITE_ROUTER.read_text()
        cls.inbound_softphone = INBOUND_SOFTPHONE.read_text()
        cls.inbound_bridge = INBOUND_BRIDGE.read_text()
        cls.inbound_local = INBOUND_LOCAL.read_text()
        cls.inbound_trunk = INBOUND_TRUNK.read_text()
        cls.inbound_automation = INBOUND_AUTOMATION.read_text()
        cls.inbound_targets = INBOUND_TARGETS.read_text()
        cls.assist_endpoint = ASSIST_ENDPOINT.read_text()
        cls.endpoint_termination = ENDPOINT_TERMINATION.read_text()
        spec = importlib.util.spec_from_file_location(
            "voip_stack_automation_routing_test", AUTOMATION_ROUTING
        )
        assert spec is not None and spec.loader is not None
        cls.automation_routing = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.automation_routing)

    def test_default_answer_ha_invite_rings_until_explicit_answer(self) -> None:
        source = self.invite_router
        marker = (
            "if not force_ha_softphone and decision.action is RouteAction.ANSWER_HA:"
        )
        fallback = "return answer_inbound_ha_softphone("
        self.assertIn(marker, source)
        self.assertLess(source.index(marker), source.index(fallback))
        answer_ha_branch = source[source.index(marker) : source.index(fallback)]
        self.assertIn("return defer_browser_softphone_invite(", answer_ha_branch)
        self.assertIn(
            "defer_invite=_defer_invite_to_ha_softphone",
            answer_ha_branch,
        )
        self.assertIn(
            "route_kind=decision.action.value",
            self.inbound_softphone,
        )
        self.assertIn("callee=resolved_callee", self.inbound_softphone)
        self.assertIn(
            'return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)',
            self.inbound_softphone,
        )
        self.assertNotIn('return SipInviteResult(200, "OK"', answer_ha_branch)
        self.assertIn("def _defer_invite_to_ha_softphone(", self.source)
        self.assertIn("registry.pending_invites[invite.call_id] = invite", self.source)
        self.assertIn("_set_ha_softphone_call_state(", self.source)
        self.assertIn("CallState.RINGING.value", self.source)

    def test_browser_presence_never_owns_the_logical_call_lifetime(self) -> None:
        self.assertNotIn("def _schedule_ha_softphone_offline_wait(", self.source)
        defer = self.source[
            self.source.index("def _defer_invite_to_ha_softphone(") :
            self.source.index("def _inbound_route_decision(")
        ]
        self.assertNotIn("offline_wait_seconds", defer)
        self.assertNotIn("ha_softphone_presence_events", defer)

    def test_ha_phone_forward_hands_off_the_existing_sip_dialog(self) -> None:
        forward = self.call_forwarder
        run = forward[
            forward.index("async def _run_forward()") :
        ]
        reservation_start = run.index(
            'reservation = (preanswered or {}).get("rtp_reservation")'
        )
        browser_branch = run[
            run.index("if decision.action is RouteAction.ANSWER_HA:") :
            reservation_start
        ]

        self.assertIn("if decision.action is RouteAction.ANSWER_HA:", forward)
        self.assertIn("_logical_endpoint_for_member(", forward)
        self.assertIn("cannot forward a call to itself", forward)
        self.assertNotIn("target_browser_endpoint.offline_policy", forward)
        self.assertIn("registry.claim_endpoint(", browser_branch)
        self.assertIn("registry.release_endpoint_claim(", browser_branch)
        self.assertIn("_defer_invite_to_ha_softphone(", browser_branch)
        self.assertIn('last_sip_event="ROUTE_FORWARD"', browser_branch)
        self.assertTrue(browser_branch.rstrip().endswith("return"))
        self.assertNotIn("RtpPortReservation.allocate", browser_branch)
        self.assertNotIn("SipCallClient(", browser_branch)
        self.assertNotIn("registry.pending_invites.pop", browser_branch)

    def test_forwarded_browser_ringing_preserves_offer_and_target_capability(
        self,
    ) -> None:
        publish = self.source[
            self.source.index("def _publish_pending_ha_softphone_ringing(") :
            self.source.index("def _defer_invite_to_ha_softphone(")
        ]
        for field in (
            'endpoint.supports("video")',
            "dialed_target=invite.target",
            "video_offered=video_enabled",
            "invite.video_format.wire_token()",
            "invite.send_video_format.wire_token()",
            "invite.recv_video_format.wire_token()",
        ):
            self.assertIn(field, publish)
        self.assertIn(
            "200 if invite.call_id in registry.preanswered else 180",
            publish,
        )

    def test_forwarded_standard_sip_video_prefers_direct_then_transcodes(
        self,
    ) -> None:
        forward = self.call_forwarder
        video_start = forward.index("forward_video_enabled = bool(")
        video = forward[
            video_start :
            forward.index("relay = build_invite_client_relay(", video_start)
        ]

        self.assertIn("cfg.get(CONF_SIP_VIDEO, False)", video)
        self.assertIn('source_route_endpoint.supports("video")', video)
        self.assertIn('target_route_endpoint.supports("video")', video)
        self.assertIn("build_pending_invite_video_relay(", video)
        self.assertIn("configure_answered_invite_video_relay(", video)
        self.assertIn("video_bridge_offer_formats(", video)
        self.assertIn("enable_transcoding=video_transcoding_enabled", video)
        self.assertIn("generic_video_relay=video_relay is not None", video)
        bridge_video = SIP_BRIDGE.read_text()
        self.assertEqual(
            bridge_video.count("sdp.video_formats_passthrough_compatible("), 2
        )
        self.assertIn(
            "sdp.video_answer_contract(invite.video_format, remote_video)",
            bridge_video,
        )
        self.assertIn("sdp.video_offer_answer_directional(", bridge_video)
        self.assertIn("no direct or transcoded codec", video)

    def test_browser_to_browser_call_uses_local_bridge_before_network_discovery(
        self,
    ) -> None:
        call_service = self.softphone_originate
        resolve_destination = call_service.index(
            "await _async_resolve_browser_destination("
        )
        local_start = call_service.index("start_local_softphone_call(")
        local_return = call_service.index("return", local_start)
        advertise_host = call_service.index(
            "local_ip = await _ha_advertise_host(hass)"
        )

        self.assertLess(resolve_destination, local_start)
        self.assertLess(local_start, local_return)
        self.assertLess(local_return, advertise_host)
        local_branch = call_service[local_start:local_return]
        self.assertIn("caller_owner_id=str(", local_branch)
        self.assertIn('call.data.get("media_client_id")', local_branch)
        self.assertIn("request_video=bool(", local_branch)
        self.assertIn("browser_endpoint.supports(\"video\")", local_branch)
        self.assertIn("enable_caller_video_send=bool(", local_branch)
        self.assertIn('call.data.get("send_video", False)', local_branch)

    def test_call_service_uses_one_device_selector_for_esp_or_browser_source(
        self,
    ) -> None:
        start = self.service_endpoints.index("def service_browser_endpoint(")
        body = self.service_endpoints[
            start : self.service_endpoints.index(
                "def service_configured_endpoint(", start
            )
        ]

        self.assertIn('call.data.get("device_id")', body)
        self.assertIn("registry.by_device_id(device_id)", body)
        self.assertIn("endpoint.kind is not EndpointKind.BROWSER", body)
        self.assertNotIn('call.data.get("entity_id")', body)
        self.assertNotIn('call.data.get("endpoint_id")', body)
        self.assertNotIn("source_device_id", body)

    def test_nested_ha_actions_preserve_service_context_and_exact_entity_scope(
        self,
    ) -> None:
        press = self.esphome_actions[
            self.esphome_actions.index("async def async_press_device_button(") :
            self.esphome_actions.index("async def async_call_action(")
        ]
        action = self.esphome_actions[
            self.esphome_actions.index("async def async_call_action(") :
            self.esphome_actions.index("def has_action(")
        ]
        answer = self.softphone_commands[
            self.softphone_commands.index("async def async_try_esp_answer(") :
            self.softphone_commands.index("async def async_try_esp_end_call(")
        ]
        set_dnd = self.init_source[
            self.init_source.index("async def _handle_set_dnd_service(") :
            self.init_source.index(
                "async def _handle_set_ha_softphone_settings_service("
            )
        ]

        for helper in (press, action):
            self.assertIn("context=None", helper)
            self.assertIn("context=context", helper)
        self.assertIn("action_entity_ids=(call_button,) if call_button else ()", answer)
        self.assertIn("context=call.context", answer)
        self.assertIn('if str(entity_id).startswith("switch.")', set_dnd)
        self.assertIn("action_entity_ids=dnd_entities", set_dnd)

    def test_ring_group_answer_resolves_route_before_forward_guard(self) -> None:
        answer = self.softphone_answer
        pending_route = answer.index("if call_id and call_id in pending_routes(hass):")
        forward_guard = answer.index('raise ServiceValidationError(f"call_id {call_id} is being forwarded")')
        self.assertLess(pending_route, forward_guard)

    def test_ring_group_decline_resolves_leg_before_forward_cancellation(self) -> None:
        decline = self.softphone_commands[
            self.softphone_commands.index("async def async_decline_browser_call(") :
        ]
        pending_route = decline.index(
            "if call_id and call_id in pending_routes(hass):"
        )
        forward_cancel = decline.index("forward_task.cancel()")
        self.assertLess(pending_route, forward_cancel)

    def test_mid_call_dtmf_is_an_event_not_a_second_dialplan(self) -> None:
        websocket_source = WEBSOCKET_API.read_text()
        self.assertIn('SIP_DTMF_EVENT = "voip_stack.dtmf"', websocket_source)
        self.assertIn("def attach_dtmf_event_bridge(", self.dtmf_events)
        self.assertIn(
            '"source_leg": "caller" if source_is_caller else "callee"',
            self.dtmf_events,
        )
        self.assertIn(
            'client.on_info_dtmf = lambda digit: _emit("right", digit, "sip_info")',
            self.dtmf_events,
        )
        self.assertIn('callback("left", digit, "sip_info")', self.dtmf_events)
        self.assertIn("relay.relay_dtmf(side, digit)", self.dtmf_events)
        self.assertNotIn("send_dtmf_info", self.source)
        # Five established HA-anchored bridge paths keep in-call DTMF.
        self.assertEqual(
            self.source.count("_attach_dtmf_event_bridge(")
            + self.invite_router.count("_attach_dtmf_event_bridge(")
            + self.inbound_bridge.count("attach_dtmf_event_bridge(")
            + self.trunk_inbound_router.count("attach_dtmf_event_bridge(")
            + self.call_forwarder.count("_attach_dtmf_event_bridge(")
            + self.ring_group.count("_attach_dtmf_event_bridge("),
            5,
        )
        self.assertNotIn("dtmf_sequence", self.source)

    def test_logical_endpoint_busy_is_enforced_for_browser_and_sip_routes(self) -> None:
        source = self.invite_router
        answer_ha = (
            "if not force_ha_softphone and decision.action is RouteAction.ANSWER_HA:"
        )
        self.assertIn(answer_ha, source)
        answer_ha_branch = self.inbound_softphone

        self.assertIn("except EndpointBusyError:", answer_ha_branch)
        self.assertIn(
            "decline_reason=TerminalReason.BUSY.value",
            answer_ha_branch,
        )
        target_guard = self.inbound_targets
        self.assertIn("endpoint.active_call_id", target_guard)
        self.assertIn("endpoint.active_call_id != invite.call_id", target_guard)
        sip_route_start = self.inbound_bridge.index("logical_source_endpoint =")
        sip_route = self.inbound_bridge[
            sip_route_start : self.inbound_bridge.index(
                "async def finish_bridge",
                sip_route_start,
            )
        ]
        self.assertIn("registry.claim_endpoint(", sip_route)
        self.assertIn('role="source"', sip_route)
        self.assertIn("adopt_transport=True", sip_route)
        self.assertIn('role="destination"', sip_route)
        self.assertIn("except EndpointBusyError:", sip_route)
        self.assertGreaterEqual(sip_route.count("registry.finish_and_pop("), 3)

    def test_ha_softphone_dnd_declines_with_dnd_reason(self) -> None:
        start = self.inbound_targets.index("if endpoint.dnd:")
        dnd_branch = self.inbound_targets[
            start : self.inbound_targets.index(
                "if endpoint.active_call_id", start
            )
        ]
        self.assertIn('decline_reason="dnd"', dnd_branch)
        self.assertIn("486,", dnd_branch)
        self.assertIn('"Busy Here"', dnd_branch)
        self.assertNotIn("TerminalReason.BUSY.value", dnd_branch)

    def test_retransmitted_invite_is_not_rejected_as_busy(self) -> None:
        source = self.invite_router
        busy_guard = source.split("if invite.call_id in route_bucket:", 1)[1].split(
            "if decision.action is RouteAction.ASSIST", 1
        )[0]
        self.assertIn('return SipInviteResult(100, "Trying"', busy_guard)
        self.assertIn("if invite.call_id in pending:", busy_guard)
        self.assertIn('return SipInviteResult(180, "Ringing"', busy_guard)
        self.assertNotIn("HA SIP endpoint is busy", busy_guard)
        self.assertNotIn("other_routes", busy_guard)
        self.assertNotIn("other_pending", busy_guard)
        self.assertNotIn("ha_softphone_active", busy_guard)
        self.assertNotIn("if route_bucket or pending", busy_guard)

    def test_registered_sip_callers_bypass_route_requested(self) -> None:
        pre_route = self.invite_router
        registered_branch = self.inbound_automation
        self.assertIn(
            "registered_entries = _registered_roster_entries(hass)", pre_route
        )
        self.assertIn(
            "registered_source = registrar.registration_matches_source(",
            pre_route,
        )
        self.assertIn(
            "caller_identity,\n        invite.source_host,\n        invite.source_port,\n        invite.signaling_transport,",
            pre_route,
        )
        self.assertIn("or not automation_routing_enabled", registered_branch)
        self.assertIn(
            "caller_roster_entry = _roster_entry_for_target(caller_identity, roster_entries)",
            pre_route,
        )
        self.assertIn("not caller_is_trusted_endpoint", pre_route)
        self.assertIn("and decision.action is RouteAction.TRUNK", pre_route)
        self.assertIn('decline_reason="unauthenticated_trunk"', pre_route)
        self.assertIn(
            "SIP caller uses central dialplan without automation window",
            registered_branch,
        )
        route_requested_branch = registered_branch[
            registered_branch.index("hass = runtime.hass") :
        ]
        self.assertIn("CallState.CONNECTING.value", route_requested_branch)
        self.assertIn("route_request=True", route_requested_branch)
        self.assertIn(
            "payload = await asyncio.wait_for(",
            route_requested_branch,
        )
        self.assertIn(
            "timeout=SIP_ROUTE_DECISION_TIMEOUT",
            route_requested_branch,
        )

    def test_initial_destination_has_a_dedicated_service(self) -> None:
        services = SERVICES.read_text()
        services_yaml = SERVICES_YAML.read_text()
        init_source = self.init_source
        self.assertIn('"select_inbound_destination"', services)
        self.assertIn("select_inbound_destination_schema", services)
        self.assertIn("select_inbound_destination:", services_yaml)
        self.assertIn(
            "async def _handle_select_inbound_destination_service", init_source
        )
        self.assertIn(
            'data["action"] = "forward"',
            init_source[
                init_source.index(
                    "async def _handle_select_inbound_destination_service"
                ) : init_source.index("async def _handle_sip_forward_service")
            ],
        )

    def test_route_requested_exposes_fallback_and_deadline(self) -> None:
        routing_sources = (
            self.source
            + self.invite_router
            + self.inbound_automation
            + self.trunk_routing
        )
        self.assertGreaterEqual(routing_sources.count("fallback_destination="), 2)
        self.assertGreaterEqual(routing_sources.count("decision_deadline="), 2)
        self.assertIn(
            '"Inbound route selected call_id=%s source=%s destination=%s fallback=%s"',
            self.invite_router,
        )

    def test_inbound_config_describes_one_priority_order(self) -> None:
        strings = json.loads(STRINGS_JSON.read_text())
        step = strings["config"]["step"]["trunk"]
        self.assertEqual(
            step["data"]["trunk_inbound_default_target"],
            "Fallback destination",
        )
        self.assertEqual(
            strings["selector"]["trunk_inbound_mode"]["options"],
            {
                "direct": "Route immediately",
                "dtmf": "Collect extension with DTMF",
            },
        )
        automation_help = step["data_description"]["automation_routing_enabled"]
        self.assertIn("Before using the fallback destination", automation_help)
        self.assertIn("Explicit DTMF digits always keep priority", automation_help)

    def test_forward_to_registered_account_keeps_contact_uri(self) -> None:
        call_service = self.softphone_originate
        force_bridge = call_service[
            call_service.index('force_ha_bridge or bool(call.data.get("ha_bridge", False))') :
            call_service.index("use_trunk = route.action is RouteAction.TRUNK", 1)
        ]
        self.assertIn(
            'if route.entry is not None and route.entry.metadata.get("registered"):',
            force_bridge,
        )
        self.assertIn("bridge_uri = route.sip_uri", force_bridge)
        self.assertIn(
            "bridge_uri = ha_uri_for(route.target or target, contacts)", force_bridge
        )
        self.assertIn(
            "route = replace(route, action=RouteAction.BRIDGE, sip_uri=bridge_uri)",
            force_bridge,
        )

    def test_softphone_video_send_requires_global_and_per_call_opt_in(self) -> None:
        answer_service = SOFTPHONE_ANSWER.read_text()
        call_service = SOFTPHONE_ORIGINATE.read_text()

        expected_gate = ') and bool(call.data.get("send_video", False))'
        self.assertIn(expected_gate, answer_service)
        self.assertIn('call.data.get("send_video", False)', call_service)
        self.assertIn(
            'video_direction=("sendrecv" if camera_send_enabled else "recvonly")',
            call_service,
        )
        self.assertIn("allow_send=(", answer_service)
        self.assertIn(
            "camera_send_enabled\n                and browser_video_send_supported",
            answer_service,
        )
        self.assertIn(
            "transport_config(hass).get(CONF_VIDEO_CAMERA_SEND, False)",
            answer_service,
        )
        local_answer = answer_service[
            answer_service.index("camera_send_requested = bool(") :
            answer_service.index("bucket = hass.data.setdefault(DOMAIN, {})")
        ]
        self.assertIn("CONF_VIDEO_CAMERA_SEND", local_answer)
        self.assertIn('call.data.get("send_video", False)', local_answer)
        self.assertIn("enable_video_send=camera_send_requested", local_answer)
        for marker in (
            "start_local_softphone_call(",
            "await start_ring_group(",
        ):
            branch_start = call_service.index(marker)
            branch = call_service[branch_start : branch_start + 1800]
            self.assertIn("CONF_VIDEO_CAMERA_SEND", branch)
            self.assertIn('call.data.get("send_video", False)', branch)

    def test_video_backpressure_recovers_at_a_keyframe(self) -> None:
        video_ws = (
            ROOT / "custom_components" / "voip_stack" / "video_ws_view.py"
        ).read_text()
        self.assertIn('control.get("type") == "request_key_frame"', video_ws)
        self.assertIn('request_key_frame(loop.time())', video_ws)
        self.assertIn('counters["video_browser_keyframe_requests"] += 1', video_ws)
        self.assertIn("while True:\n                try:\n                    access_units.get_nowait()", video_ws)
        self.assertIn("if not access_unit.key_frame:", video_ws)
        self.assertIn("needs_key_frame = True", video_ws)

    def test_entryless_sip_uri_route_is_guarded_and_uses_fallback_uri(self) -> None:
        bridge_path = self.inbound_bridge
        self.assertIn(
            "decision.entry is not None and decision.entry.sip_uri", bridge_path
        )
        self.assertIn(
            "decision.entry is not None",
            bridge_path,
        )
        self.assertIn(
            'and not decision.entry.metadata.get("local_ha")',
            bridge_path,
        )
        self.assertIn(
            "parse_sip_uri(decision.sip_uri) if decision.sip_uri else None", bridge_path
        )
        self.assertNotIn("elif decision.entry.sip_uri", bridge_path)
        self.assertNotIn("elif not decision.entry.metadata.get", bridge_path)

    def test_bridge_relay_uses_bounded_port_pool_and_release_callback(self) -> None:
        routing_sources = (
            self.source
            + self.invite_router
            + self.inbound_bridge
            + self.trunk_inbound_router
            + self.call_forwarder
        )
        self.assertIn("RtpPortReservation.allocate(hass)", routing_sources)
        self.assertNotIn('bucket.get("sip_rtp_next_port"', routing_sources)
        self.assertIn(
            "on_release=lambda ports: _release_sip_rtp_port_pair(hass, ports)",
            routing_sources,
        )
        self.assertNotIn(
            "_release_sip_rtp_port_pair(hass, (source_relay_port, dest_relay_port))",
            routing_sources,
        )
        self.assertIn("class OutboundLeg", self.outbound_attempts)
        self.assertIn(
            "async def async_close_client_and_release(",
            self.outbound_attempts,
        )
        self.assertIn(
            "async def async_close_outbound_leg(",
            self.outbound_attempts,
        )
        self.assertIn("attempt.ports.release()", self.outbound_attempts)
        self.assertIn("winner.ports.detach()", routing_sources)
        self.assertIn("bridge_ports.detach()", routing_sources)
        self.assertNotIn(
            "await client.close()\n            bridge_ports.release()", routing_sources
        )
        self.assertNotIn(
            "await client.close()\n                    bridge_ports.release()",
            routing_sources,
        )

    def test_group_identity_capacity_and_local_ws_errors_are_isolated(self) -> None:
        self.assertIn('call_id = f"ha-{secrets.token_hex(16)}"', self.source)
        self.assertNotIn('call_id = f"ha-{int(time.time() * 1000):x}"', self.source)
        self.assertIn(
            "len(browser_endpoint_ids) + len(attempts) < available_legs",
            self.conference_ringing,
        )
        self.assertIn(
            "if len(browser_endpoint_ids) + len(attempts) >= available_legs:",
            self.conference_ringing,
        )
        media_session = (
            Path(__file__).resolve().parents[1]
            / "custom_components"
            / "voip_stack"
            / "media_ws_session.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            "except (ValueError, LocalCallStateError) as err:",
            media_session,
        )

    def test_bridge_invite_does_not_register_after_caller_cancel(self) -> None:
        generic_bridge = self.inbound_bridge.index(
            "result = await client.invite(",
        )
        bridge_path = self.inbound_bridge[
            generic_bridge : self.inbound_bridge.index(
                'if result not in {"ringing", "in_call"}:', generic_bridge
            )
        ]
        self.assertIn(
            'invite.call_id in bucket.get("trunk_closed_calls", set())', bridge_path
        )
        self.assertIn(
            'bucket["trunk_closed_calls"].discard(invite.call_id)', bridge_path
        )
        self.assertIn(
            "await async_close_client_and_release(client, bridge_ports, bye=True)",
            bridge_path,
        )
        self.assertIn(
            'return SipInviteResult(\n            487,\n            "Request Terminated"',
            bridge_path,
        )
        self.assertNotIn(
            "await attempt.client.close()\n                    attempt.ports.release()",
            self.invite_router,
        )

    def test_group_routes_have_dedicated_dispatch_not_generic_bridge(self) -> None:
        on_invite = self.invite_router
        routeable = on_invite[
            on_invite.index("routeable_sip_target =") : on_invite.index(
                "if not force_ha_softphone and (bridge_to_trunk or routeable_sip_target):"
            )
        ]
        self.assertNotIn("RouteAction.GROUP", routeable)
        self.assertIn("if decision.action is RouteAction.GROUP:", on_invite)
        self.assertIn("return await route_local_group(", on_invite)
        self.assertIn("ring_endpoint_ids = tuple(", self.inbound_local)
        self.assertIn("runtime.browser_leg_for_member(", self.inbound_local)
        self.assertIn("ring_endpoint_ids=ring_endpoint_ids", self.inbound_local)
        self.assertIn("async def _ring_conference_members_from_ha", self.source)
        self.assertIn(
            'hass.data.setdefault(DOMAIN, {})["async_ring_conference_members"] = _ring_conference_members_from_ha',
            self.source,
        )
        self.assertIn("caller=_ha_peer_name(hass)", self.source)
        self.assertIn("source_host=local_ip", self.source)
        self.assertIn("runtime.ring_conference_members(", self.inbound_local)
        self.assertIn(
            "runtime.run_ring_group_call(",
            self.inbound_local,
        )
        self.assertIn(
            'return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)',
            self.inbound_local,
        )

    def test_ha_softphone_runtime_settings_publish_virtual_endpoint(self) -> None:
        config_flow = CONFIG_FLOW.read_text()
        strings = STRINGS_JSON.read_text()
        init_py = INIT.read_text()
        websocket = (
            ROOT / "custom_components" / "voip_stack" / "websocket_api.py"
        ).read_text()
        sensor = SENSOR.read_text()
        peer_snapshot = PEER_SNAPSHOT.read_text()
        const = (ROOT / "custom_components" / "voip_stack" / "const.py").read_text()

        for token in (
            "CONF_HA_RING_GROUP",
            "CONF_HA_CONFERENCE_GROUP",
            "CONF_HA_CONFERENCE_RING",
        ):
            self.assertNotIn(token, config_flow)
            self.assertNotIn(token, websocket)
            self.assertNotIn(token, init_py)
            self.assertNotIn(token, const)
        self.assertNotIn('"ha_ring_group"', strings)
        self.assertNotIn('"ha_conference_group"', strings)
        self.assertNotIn('"ha_conference_ring"', strings)
        self.assertIn("async_set_ha_softphone_settings", websocket)
        self.assertNotIn("WS_TYPE_SET_HA_SOFTPHONE_SETTINGS", websocket)
        self.assertNotIn("WS_TYPE_SET_HA_SOFTPHONE_GROUPS", websocket)
        self.assertNotIn("WS_TYPE_SET_HA_SOFTPHONE_DND", websocket)
        self.assertIn(
            '"set_ha_softphone_settings": _handle_set_ha_softphone_settings_service',
            init_py,
        )
        self.assertIn(
            'hass.services.async_register(\n        DOMAIN,\n        "set_ha_softphone_settings"',
            SERVICES.read_text(),
        )
        self.assertIn("_ha_softphone_extension", websocket)
        self.assertIn("HA_SOFTPHONE_ENDPOINT_ENTITY_ID", const)
        self.assertIn("class HaSoftphoneEndpointSensor", sensor)
        self.assertIn("HA_SIP_PCM_FORMATS", sensor)
        self.assertIn("HA_ENDPOINT_AUDIO_FORMATS", sensor)
        self.assertIn("HA_SIP_PCM_FORMATS[:8]", sensor)
        self.assertIn('tx = ";".join(HA_ENDPOINT_AUDIO_FORMATS)', sensor)
        self.assertNotIn("HA_ENDPOINT_FORMATS", sensor)
        self.assertIn('self._attr_native_value = "online"', sensor)
        self.assertIn('"endpoint": endpoint', sensor)
        self.assertIn('"extension": extension', sensor)
        self.assertIn('"ring_group": groups["ring_group"]', sensor)
        self.assertIn('"conference_group": groups["conference_group"]', sensor)
        self.assertNotIn(
            "f\"{extension}|{groups['conference_group']}|{groups['ring_group']}|\"",
            sensor.replace("\n", ""),
        )
        self.assertIn("old_endpoint", sensor)
        self.assertIn("new_endpoint", sensor)
        self.assertIn("voip_ring_groups", sensor)
        self.assertIn("voip_conference_groups", sensor)
        self.assertIn("voip_ring_on_conference", sensor)
        self.assertIn("new_set.add(HA_SOFTPHONE_ENDPOINT_ENTITY_ID)", sensor)
        self.assertIn(
            "hass.states.get(HA_SOFTPHONE_ENDPOINT_ENTITY_ID)", peer_snapshot
        )
        self.assertIn("ha_endpoint_state.attributes or {}", peer_snapshot)
        self.assertIn('get("endpoint")', peer_snapshot)
        self.assertIn("parse_voip_endpoint", peer_snapshot)
        self.assertNotIn("async_prune_ha_softphone_groups", sensor)
        self.assertNotIn("async_prune_ha_softphone_groups", websocket)
        self.assertNotIn("local_ha_seen = False", websocket)
        self.assertNotIn("HA_SOFTPHONE_GROUPS_UPDATED_EVENT", websocket)
        self.assertNotIn("HA_SOFTPHONE_GROUPS_UPDATED_EVENT", sensor)
        self.assertIn("def _clean_group_token", websocket)
        self.assertIn('.replace("|", " ")', websocket)
        self.assertIn('.replace(";", " ")', websocket)
        self.assertIn("[:32]", websocket)
        self.assertIn('for raw in str(value or "").split(",")', websocket)

    def test_phonebook_snapshot_keeps_enabled_softphones_without_connected_cards(
        self,
    ) -> None:
        peer_snapshot = PEER_SNAPSHOT.read_text()
        self.assertIn(
            "endpoint.availability is not EndpointAvailability.UNAVAILABLE",
            peer_snapshot,
        )
        self.assertNotIn(
            "endpoint.availability is EndpointAvailability.AVAILABLE",
            peer_snapshot,
        )
        self.assertIn(
            "if not browser_endpoints and ha_endpoint is not None:",
            peer_snapshot,
        )

    def test_ha_softphone_settings_use_phone_subentries_as_the_only_store(self) -> None:
        websocket = WEBSOCKET_API.read_text()
        self.assertIn('runtime["config_entry_id"]', websocket)
        self.assertIn("update_phone_subentry(", websocket)
        self.assertNotIn("_HA_SOFTPHONE_OPTION_KEYS", websocket)
        self.assertNotIn("HA_SOFTPHONE_STORE_KEY", websocket)
        self.assertNotIn("options={**entry.options, **persisted}", websocket)

        init_py = INIT.read_text()
        setup = init_py[
            init_py.index("async def async_setup_entry(") : init_py.index(
                "async def async_unload_entry("
            )
        ]
        self.assertIn("for subentry in phone_subentries(entry):", setup)
        self.assertIn("await _async_load_ha_softphone_store(", setup)
        self.assertIn("endpoint_id=endpoint.endpoint_id", setup)
        self.assertIn("endpoint_data=dict(subentry.data)", setup)

    def test_config_flow_has_no_softphone_group_or_ring_fallback_policy(self) -> None:
        config_flow = CONFIG_FLOW.read_text()
        strings = STRINGS_JSON.read_text()
        const = (ROOT / "custom_components" / "voip_stack" / "const.py").read_text()

        for token in (
            "CONF_RING_GROUP_FALLBACK",
            "CONF_HA_RING_GROUP",
            "CONF_HA_CONFERENCE_GROUP",
            "CONF_HA_CONFERENCE_RING",
            "ring_group_fallback",
            "ha_ring_group",
            "ha_conference_group",
            "ha_conference_ring",
        ):
            self.assertNotIn(token, config_flow)
            self.assertNotIn(token, strings)
            self.assertNotIn(token, const)

    def test_ha_softphone_can_join_conference_group_without_sip_self_invite(
        self,
    ) -> None:
        call_service = SOFTPHONE_ORIGINATE.read_text()
        self.assertIn("if route.action is RouteAction.GROUP:", call_service)
        self.assertIn("manager.start_ha_softphone(", call_service)
        self.assertIn("endpoint_id=endpoint_id", call_service)
        self.assertIn('"async_ring_conference_members"', call_service)
        self.assertIn(
            "ring_members(route.entry, owner_call_id=call_id)", call_service
        )
        self.assertIn('last_sip_event="LOCAL_CONFERENCE_JOIN"', call_service)
        self.assertIn(
            "RouteAction.GROUP",
            call_service[call_service.index("if not use_trunk and") :],
        )

    def test_ha_softphone_starts_ring_group_without_sip_self_invite(self) -> None:
        runtime = BACKEND.read_text()
        call_service = SOFTPHONE_ORIGINATE.read_text()
        self.assertIn('"async_start_ring_group_from_ha"', call_service)
        self.assertIn("await start_ring_group(", call_service)
        self.assertIn('context=getattr(call, "context", None)', call_service)
        self.assertIn(
            "return",
            call_service[
                call_service.index('if group_type == "ring":') : call_service.index(
                    'if group_type == "conference":'
                )
            ],
        )
        self.assertIn(
            'hass.data.setdefault(DOMAIN, {})["async_start_ring_group_from_ha"] = _start_ring_group_from_ha',
            runtime,
        )
        self.assertIn('last_sip_event="LOCAL_RING_GROUP"', runtime)
        self.assertIn("fmt.nominal_frame_bytes <= 1200", runtime)
        self.assertIn("endpoint_id: str = DEFAULT_ENDPOINT_ID", runtime)
        self.assertIn("origin_endpoint_id=endpoint_id", runtime)

    def test_ha_softphone_routes_assist_through_local_pbx_listener(self) -> None:
        call_service = SOFTPHONE_ORIGINATE.read_text()
        assist = call_service[
            call_service.index("elif route.action is RouteAction.ASSIST:") :
            call_service.index("if use_trunk:")
        ]
        self.assertIn("ha_uri_for(route.target or target, contacts)", assist)
        self.assertIn("action=RouteAction.BRIDGE", assist)
        self.assertIn("sip_uri=route_uri", assist)

    def test_ad_hoc_sip_uri_requires_admin_before_network_resolution(self) -> None:
        call_service = SOFTPHONE_ORIGINATE.read_text()
        route_guard = call_service.index("if route.reason is RouteReason.DIRECT_URI:")
        admin_check = call_service.index("await async_require_service_admin(hass, call)")
        parse_uri = call_service.index("uri = parse_sip_uri(route_uri)")

        self.assertLess(route_guard, admin_check)
        self.assertLess(admin_check, parse_uri)

    def test_ha_direct_esp_calls_prefer_roster_audio_profile_over_device_registry(
        self,
    ) -> None:
        call_service = SOFTPHONE_ORIGINATE.read_text()
        tx_profile = call_service.split("remote_tx_formats =", 1)[1].split(
            "remote_rx_formats =", 1
        )[0]
        rx_profile = call_service.split("remote_rx_formats =", 1)[1].split(
            "sip_send_formats, sip_recv_formats", 1
        )[0]
        self.assertIn("_roster_entry_formats(", tx_profile)
        self.assertIn('route.entry, "tx_formats"', tx_profile)
        self.assertIn('_device_formats(dest_device, "tx_formats")', tx_profile)
        self.assertIn("_roster_entry_formats(", rx_profile)
        self.assertIn('route.entry, "rx_formats"', rx_profile)
        self.assertIn('_device_formats(dest_device, "rx_formats")', rx_profile)
        self.assertNotIn(
            "if dest_device is not None\n        else _roster_entry_formats",
            call_service,
        )

    def test_phonebook_sensor_exposes_only_the_central_roster_for_the_card(
        self,
    ) -> None:
        routing = ENDPOINT_ROUTING.read_text()
        sensor = SENSOR.read_text()
        card = CARD_JS.read_text()

        self.assertIn('"tx_formats": list(peer.tx_formats or [])', routing)
        self.assertIn('"rx_formats": list(peer.rx_formats or [])', routing)
        self.assertIn('"group_type": group.group_type', routing)
        self.assertNotIn("def softphone_targets_from_roster", routing)
        self.assertNotIn("softphone_targets_json", sensor)
        self.assertNotIn("softphone_targets_json", card)
        self.assertIn("_targetFromRosterEntry(entry)", card)
        self.assertIn("metadata.local_ha", card)

    def test_ha_softphone_media_path_can_join_conference_without_card_logic(
        self,
    ) -> None:
        originate = SOFTPHONE_ORIGINATE.read_text()
        answer = SOFTPHONE_ANSWER.read_text()
        audio_ws = AUDIO_WS.read_text()
        termination = SOFTPHONE_TERMINATION.read_text()

        self.assertIn('call_id.startswith("conference:")', answer)
        self.assertIn("manager.join_ha_softphone(", answer)
        self.assertIn("call_id=call_id", answer)
        self.assertIn("manager.start_ha_softphone(", originate)
        self.assertIn("endpoint_id=endpoint_id", originate)
        self.assertIn('"conference_queue": queue', answer)
        self.assertIn('last_sip_event="LOCAL_CONFERENCE_JOIN"', originate)
        self.assertIn('conference_queue = item.get("conference_queue")', audio_ws)
        self.assertIn("_run_conference_audio_session", audio_ws)
        self.assertIn("manager.push_ha_audio(session.call_id, pcm)", audio_ws)
        self.assertIn("await manager.leave_ha_softphone(", termination)
        self.assertIn("conference_room,", termination)
        self.assertIn("call_id=call_id", termination)
        card = CARD_JS.read_text()
        self.assertNotIn("conference_manager", card)
        self.assertNotIn("_ringConference", card)
        self.assertNotIn("_run_conference_audio_session", card)

    def test_conference_tracks_leg_roles_and_participant_lifetime(self) -> None:
        conference = (
            ROOT / "custom_components" / "voip_stack" / "conference.py"
        ).read_text()
        self.assertIn("role: str", conference)
        self.assertIn('role="owner" if was_empty else "manual"', conference)
        self.assertIn('role="auto_invited"', self.conference_ringing)
        self.assertNotIn('await self.close(reason="owner_left")', conference)
        self.assertNotIn("self._owner_call_id", conference)
        self.assertIn("port_reservation: RtpPortReservation", conference)

    def test_sip_endpoint_account_list_service_is_registered_and_documented(
        self,
    ) -> None:
        services = SERVICES.read_text()
        account_services = ACCOUNT_SERVICES.read_text()
        services_yaml = SERVICES_YAML.read_text()
        icons_json = ICONS_JSON.read_text()

        self.assertIn('handler_for("list_accounts")', services)
        self.assertIn("supports_response=SupportsResponse.ONLY", services)
        self.assertIn('"list_accounts": list_accounts', account_services)
        self.assertIn("list_accounts:", services_yaml)
        self.assertIn('"list_accounts"', icons_json)
        self.assertIn("SIP Endpoint Accounts", services_yaml)
        self.assertNotIn("persistent_notification", account_services)
        self.assertNotIn("SIP Softphone Account", services_yaml)
        self.assertNotIn("SIP Softphone Accounts", services_yaml)

    def test_add_contact_accepts_group_membership_metadata(self) -> None:
        services = SERVICES.read_text()
        services_yaml = SERVICES_YAML.read_text()

        self.assertIn('"conference_group"', services)
        self.assertIn('"conference_ring"', services)
        self.assertIn('"ring_group"', services)
        self.assertIn("conference_group:", services_yaml)
        self.assertIn("conference_ring:", services_yaml)
        self.assertIn("ring_group:", services_yaml)

    def test_registered_sip_endpoint_accounts_accept_group_membership_metadata(
        self,
    ) -> None:
        services = SERVICES.read_text()
        account_services = ACCOUNT_SERVICES.read_text()
        services_yaml = SERVICES_YAML.read_text()

        account_schema = services[
            services.index("sip_account_create_schema") : services.index(
                "sip_account_name_schema"
            )
        ]
        self.assertIn('"conference_group"', account_schema)
        self.assertIn('"conference_ring"', account_schema)
        self.assertIn('"ring_group"', account_schema)
        self.assertIn('"extension"', account_schema)
        self.assertIn("extension=str(call.data.get", account_services)
        self.assertIn('"extension": str(item.get("extension")', account_services)
        self.assertIn("conference_group=str(call.data.get", account_services)
        self.assertIn("conference_ring=bool(call.data.get", account_services)
        self.assertIn("ring_group=str(call.data.get", account_services)
        self.assertIn(
            "extension:", services_yaml[services_yaml.index("create_account:") :]
        )
        self.assertIn(
            "conference_group:", services_yaml[services_yaml.index("create_account:") :]
        )
        self.assertIn(
            "conference_ring:", services_yaml[services_yaml.index("create_account:") :]
        )
        self.assertIn(
            "ring_group:", services_yaml[services_yaml.index("create_account:") :]
        )

    def test_create_account_returns_generated_secret_without_publishing_it(
        self,
    ) -> None:
        account_services = ACCOUNT_SERVICES.read_text()
        create_account = account_services[
            account_services.index(
                "async def create_account("
            ) : account_services.index("async def remove_account(")
        ]
        self.assertNotIn("persistent_notification", create_account)
        self.assertIn('response["password"] = password', create_account)
        self.assertIn('"password_generated": generated_password', create_account)
        self.assertIn(
            'provided_password = str(call.data.get("password") or "")', create_account
        )
        self.assertNotIn(
            'provided_password = str(call.data.get("password") or "").strip()',
            create_account,
        )
        self.assertNotIn("_fire_call_event", create_account)

    def test_rotated_account_password_is_a_private_service_response(self) -> None:
        account_services = ACCOUNT_SERVICES.read_text()
        rotate = account_services[
            account_services.index(
                "async def rotate_account_password("
            ) : account_services.index("async def set_account_enabled(")
        ]
        self.assertNotIn("persistent_notification", rotate)
        self.assertNotIn("_fire_call_event", rotate)
        self.assertIn('return {"username": username, "password": password}', rotate)

    def test_ring_group_timeout_has_no_configurable_ha_fallback(self) -> None:
        config_flow = CONFIG_FLOW.read_text()
        strings = STRINGS_JSON.read_text()

        self.assertNotIn("CONF_RING_GROUP_FALLBACK", config_flow)
        self.assertNotIn(
            'SelectSelectorConfig(options=["reject", "answer_ha"])', config_flow
        )
        self.assertNotIn('"ring_group_fallback"', strings)
        self.assertNotIn("CONF_RING_GROUP_FALLBACK", self.source)
        self.assertNotIn(
            "_defer_invite_to_ha_softphone(invite, route_kind=GROUP_TYPE_RING",
            self.source,
        )

    def test_trunk_dtmf_uses_phonebook_extensions_not_manual_route_map(self) -> None:
        config_flow = CONFIG_FLOW.read_text()
        strings = STRINGS_JSON.read_text()
        const = (ROOT / "custom_components" / "voip_stack" / "const.py").read_text()

        for source in (
            config_flow,
            strings,
            const,
            self.source,
            self.invite_router,
            self.inbound_trunk,
        ):
            self.assertNotIn("CONF_TRUNK_DTMF_ROUTES", source)
            self.assertNotIn("trunk_dtmf_routes", source)
            self.assertNotIn("parse_dtmf_route_map", source)
        self.assertIn("def dtmf_extension_routes(", self.pbx_routing)
        trunk_route = self.trunk_inbound_router
        self.assertIn("routes = dtmf_extension_routes(roster_entries)", trunk_route)
        self.assertIn("route_hint = destination or digits", trunk_route)
        self.assertIn("collect_trunk_dtmf(", trunk_route)
        self.assertIn("collect_info_digits(", self.trunk_dtmf)
        self.assertIn("DtmfCollector(", self.trunk_dtmf)
        self.assertIn("asyncio.FIRST_COMPLETED", self.trunk_dtmf)
        after_collect = trunk_route.split(
            'bucket.setdefault("trunk_info_queues", {}).pop(invite.call_id, None)',
            1,
        )[1]
        self.assertIn('invite.call_id in bucket.get("trunk_closed_calls", set())', after_collect)
        self.assertIn("bridge_ports.release()", after_collect)
        self.assertIn("remote_host=invite.remote_rtp_host", self.trunk_dtmf)
        preanswer = self.inbound_trunk
        self.assertIn(
            'bucket.setdefault("trunk_closed_calls", set()).discard(invite.call_id)',
            preanswer,
        )
        task_prelude = trunk_route.split(
            "configured_trunk = trunk_config(hass)", 1
        )[0]
        self.assertNotIn("discard(invite.call_id)", task_prelude)

    def test_ring_group_treats_ha_member_as_parallel_contender(self) -> None:
        ring_group = (
            self.ring_group
            + self.ring_group_candidates
            + self.ring_group_fork
        )
        self.assertIn(
            "await async_prepare_ring_group_candidates(",
            self.ring_group,
        )
        self.assertIn("browser_legs: list[BrowserLeg]", ring_group)
        self.assertIn("runtime.browser_leg_for_member(", ring_group)
        browser_lookup = ring_group.split(
            "browser_leg = runtime.browser_leg_for_member(",
            1,
        )[1].split(")", 1)[0]
        for argument in ("member", "peers", "roster_entries"):
            self.assertIn(argument, browser_lookup)
        self.assertIn('role="group_candidate"', ring_group)
        self.assertIn("_set_ha_softphone_call_state(", ring_group)
        self.assertIn("async def _wait_browser()", ring_group)
        self.assertIn(
            'control_candidate_id = "browser:route-control"',
            ring_group,
        )
        self.assertIn("DialCandidate(", ring_group)
        self.assertIn("_dial_control", ring_group)
        self.assertIn("registry.attach_media(invite.call_id, media)", ring_group)

    def test_initial_automation_group_selection_keeps_ha_members(self) -> None:
        forward = self.call_forwarder
        candidates = self.forward_group_candidates
        group_fork = self.ring_group_fork
        trunk_route = self.trunk_inbound_router
        self.assertIn("initial_selection: bool = False", forward)
        self.assertIn("prepare_forward_group_candidates(", forward)
        self.assertIn("if not initial_selection:", candidates)
        self.assertIn("browser_endpoint_can_ring(endpoint)", candidates)
        self.assertIn("browser_legs: list[BrowserLeg]", candidates)
        self.assertIn('role="group_candidate"', candidates)
        self.assertIn("_publish_pending_ha_softphone_ringing(", forward)
        self.assertIn("build_ring_group_fork(", forward)
        self.assertIn("async def _wait_browser()", group_fork)
        self.assertIn('"in_call_browser"', group_fork)
        self.assertIn("DialForkController(", forward)
        self.assertIn('"answer",', forward)
        browser_answer = forward[
            forward.index('"answer",') : forward.index(
                "answer_commits.discard(call_id)", forward.index('"answer",')
            )
        ]
        self.assertIn('"device_id": winner.device_id', browser_answer)
        self.assertNotIn('"endpoint_id": winner.endpoint_id', browser_answer)
        self.assertIn("initial_selection=True", trunk_route)

        self.assertIn("def browser_endpoint_can_ring(", self.pbx_routing)
        self.assertIn(
            "Browser presence is media availability, not phone existence",
            self.pbx_routing,
        )

    def test_initial_automation_can_select_the_default_ha_phone(self) -> None:
        forward = self.call_forwarder
        self.assertIn(
            "not initial_selection\n"
            "            and target_browser_endpoint is not None\n"
            "            and target_browser_endpoint.endpoint_id == session_endpoint_id",
            forward,
        )

    def test_ring_group_simultaneous_results_are_deterministic(self) -> None:
        ring_group = self.ring_group
        fork_source = (
            ROOT / "custom_components" / "voip_stack" / "dial_fork.py"
        ).read_text()
        self.assertIn("DialForkController(", ring_group)
        self.assertIn("strategy=ring_policy.strategy", ring_group)
        self.assertIn(
            "sorted(candidates, key=lambda item: (item.tier, item.order, item.candidate_id))",
            fork_source,
        )
        self.assertIn("completed_controls", fork_source)
        self.assertIn("DialDisposition.SOURCE_CANCELLED", fork_source)
        arbitration_start = fork_source.index("while pending_controls:")
        self.assertIn(
            "if branch_result is None and branch_task.done():",
            fork_source[arbitration_start:],
        )
        self.assertLess(
            fork_source.index("completed_controls", arbitration_start),
            fork_source.index(
                "result = branch_result or await branch_task",
                arbitration_start,
            ),
        )
        self.assertIn("_reduce_failures(failures)", fork_source)

    def test_ring_group_winner_clears_pending_route_before_active_hangup(self) -> None:
        ring_group = self.ring_group
        browser_winner = ring_group[
            ring_group.index(
                "if browser_winner and isinstance(winner, BrowserLeg):"
            ) : ring_group.index("if not isinstance(winner, OutboundLeg):")
        ]
        self.assertIn(
            "_pending_routes(hass).pop(invite.call_id, None)", browser_winner
        )
        bridge_index = ring_group.index("registry.register_bridge(")
        self.assertNotEqual(
            ring_group.rfind(
                "_pending_routes(hass).pop(invite.call_id, None)",
                0,
                bridge_index,
            ),
            -1,
        )

        hangup = SOFTPHONE_TERMINATION.read_text()
        self.assertIn('future = routes[call_id].get("future")', hangup)
        self.assertIn("if future is not None and future.done():", hangup)
        self.assertIn("routes.pop(call_id, None)", hangup)

    def test_ring_group_ha_winner_publishes_connected_party_to_esp_mirrors(self) -> None:
        ring_group = self.ring_group
        ha_winner = ring_group[
            ring_group.index(
                "if browser_winner and isinstance(winner, BrowserLeg):"
            ) : ring_group.index("if not isinstance(winner, OutboundLeg):")
        ]
        self.assertIn("connected_party = winner.name", ha_winner)
        self.assertIn("_set_sip_bridge_call_state(", ha_winner)
        self.assertIn("callee=entry.display_name", ha_winner)
        self.assertIn("peer_name=connected_party", ha_winner)
        self.assertIn("dialed_target=entry.display_name", ha_winner)
        self.assertIn("connected_party=connected_party", ha_winner)
        self.assertIn("answered_by=connected_party", ha_winner)

    def test_ring_group_external_winner_publishes_connected_party(self) -> None:
        ring_group = self.ring_group
        external_winner = ring_group[
            ring_group.index("client = winner.client") : ring_group.index(
                "await async_watch_sip_bridge_destination("
            )
        ]

        self.assertIn(
            "dialed_target = entry.display_name or invite.target", external_winner
        )
        self.assertIn(
            'connected_party = str(winner.member or "").strip() or invite.target',
            external_winner,
        )
        self.assertIn("callee=dialed_target", external_winner)
        self.assertIn("peer_name=connected_party", external_winner)
        self.assertIn("dialed_target=dialed_target", external_winner)
        self.assertIn("connected_party=connected_party", external_winner)
        self.assertIn("answered_by=connected_party", external_winner)
        self.assertIn("if ha_origin:", external_winner)
        self.assertIn('"endpoint_id": origin_endpoint_id', external_winner)
        self.assertIn("endpoint_id=origin_endpoint_id", external_winner)
        self.assertIn("session_device_id=origin_device_id", external_winner)
        self.assertIn("if ha_origin:", external_winner)
        self.assertIn("_set_ha_softphone_call_state(", external_winner)

        websocket = WEBSOCKET_API.read_text()
        state = websocket[
            websocket.index("def _ha_softphone_state(") : websocket.index(
                "def _set_ha_softphone_call_state("
            )
        ]
        terminal = websocket[
            websocket.index("def _set_ha_softphone_call_state(") : websocket.index(
                "def _set_sip_bridge_call_state("
            )
        ]
        self.assertIn('"dialed_target": dialed_target', state)
        self.assertIn('store.get("last_terminal_dialed_target", "")', state)
        for field in ("connected_party", "answered_by"):
            self.assertIn(f'"{field}": store.get("{field}", "")', state)
            self.assertIn(f'"{field}",', terminal)

    def test_esp_origin_forward_to_same_source_host_is_rejected(self) -> None:
        self.assertIn("and sip_endpoints_equal(", self.inbound_bridge)
        self.assertIn("invite.source_host,", self.inbound_bridge)
        self.assertIn("invite.source_port,", self.inbound_bridge)
        self.assertIn('"Busy Here"', self.inbound_bridge)
        self.assertIn(
            "decline_reason=TerminalReason.BUSY.value", self.inbound_bridge
        )

    def test_local_loop_detection_uses_host_and_listener_port(self) -> None:
        helper = self.endpoint_routing[
            self.endpoint_routing.index("def is_local_listener_uri(") :
            self.endpoint_routing.index("def logical_endpoint_for_member(")
        ]
        self.assertIn("uri.host == local_ip", helper)
        self.assertIn(
            "int(uri.port or sip_port) == int(sip_port)",
            helper,
        )
        self.assertIn(
            "self.route_resolver.is_local_listener_uri(uri)",
            self.endpoint_dialing,
        )

    def test_early_router_cancel_publishes_one_terminal_event(self) -> None:
        terminated = self.endpoint_termination
        self.assertIn("elif session is not None:", terminated)
        self.assertIn("the logical session still owes observers one terminal event", terminated)
        self.assertIn('"CANCEL"', terminated)
        self.assertIn("route_kind=session.route_kind", terminated)

    def test_softphone_settings_changes_do_not_emit_call_lifecycle_events(self) -> None:
        websocket = WEBSOCKET_API.read_text()
        group_update = websocket[
            websocket.index(
                "async def async_set_ha_softphone_settings("
            ) : websocket.index("def _sip_runtime_snapshot(")
        ]
        init_py = INIT.read_text()
        dnd_service = init_py[
            init_py.index("async def _handle_set_dnd_service(") : init_py.index(
                "async def _handle_set_ha_softphone_settings_service("
            )
        ]
        settings_service = init_py[
            init_py.index(
                "async def _handle_set_ha_softphone_settings_service("
            ) : init_py.index("async def _handle_sip_call_target_service(")
        ]

        self.assertNotIn("_fire_call_event", group_update)
        self.assertIn('if not groups["conference_group"]:', group_update)
        self.assertIn('groups["conference_ring"] = False', group_update)
        self.assertNotIn("websocket_set_ha_softphone_dnd", websocket)
        self.assertNotIn("websocket_set_ha_softphone_groups", websocket)
        self.assertNotIn("websocket_set_ha_softphone_settings", websocket)
        self.assertNotIn("_fire_call_event", dnd_service)
        self.assertNotIn("_fire_call_event", settings_service)

    def test_softphone_state_has_a_dedicated_authoritative_stream(self) -> None:
        websocket = WEBSOCKET_API.read_text()
        self.assertIn(
            'HA_SOFTPHONE_STATE_EVENT = "voip_stack.ha_softphone_state"',
            websocket,
        )
        self.assertIn(
            'WS_TYPE_SUBSCRIBE_HA_SOFTPHONE = f"{DOMAIN}/subscribe_ha_softphone_state"',
            websocket,
        )
        self.assertIn("def _publish_ha_softphone_state(", websocket)
        self.assertIn("def websocket_subscribe_ha_softphone_state(", websocket)
        fire_event = websocket[
            websocket.index("def _fire_call_event(") : websocket.index(
                "def _ha_softphone_store("
            )
        ]
        self.assertIn('event["scope"] = scope', fire_event)
        self.assertNotIn('event.get("scope") or scope', fire_event)

        terminal = websocket[
            websocket.index("def _set_ha_softphone_call_state(") : websocket.index(
                "def _set_sip_bridge_call_state("
            )
        ]
        self.assertIn("if terminal and state != CallState.IDLE.value:", terminal)
        self.assertIn('store["state"] = CallState.IDLE.value', terminal)
        self.assertLess(
            terminal.index("_fire_call_event(hass, payload, \"session\")"),
            terminal.index("if terminal and state != CallState.IDLE.value:"),
        )
        self.assertLess(
            terminal.index("if terminal and state != CallState.IDLE.value:"),
            terminal.index(
                "_publish_ha_softphone_state(hass, endpoint_id=endpoint_id)"
            ),
        )

        release = websocket[
            websocket.index("def _release_ha_softphone_claim(") : websocket.index(
                "def _ha_softphone_groups("
            )
        ]
        self.assertIn("_set_ha_softphone_call_state(", release)
        self.assertIn("TerminalReason.FORWARDED.value", release)

    def test_explicit_browser_selector_returns_registry_canonical_id(self) -> None:
        websocket = WEBSOCKET_API.read_text()
        selector = websocket[
            websocket.index("def _endpoint_id_from_selector(") : websocket.index(
                "def async_register_websocket_api("
            )
        ]
        self.assertIn(
            "resolved_id = _normalise_endpoint_id(endpoint.endpoint_id)",
            selector,
        )
        self.assertIn(
            "resolved_id.casefold() == DEFAULT_ENDPOINT_ID.casefold()",
            selector,
        )

    def test_phone_subentry_live_sync_updates_legacy_endpoint_sensor(self) -> None:
        update_listener = self.config_entry_runtime[
            self.config_entry_runtime.index("async def async_config_entry_updated(") :
            self.config_entry_runtime.index(
                "def register_phonebook_service_event_sync("
            )
        ]
        self.assertIn(
            'endpoint_sensor = bucket.get("ha_softphone_endpoint_sensor")',
            update_listener,
        )
        self.assertIn("await endpoint_sensor.async_update()", update_listener)

    def test_trunk_without_dtmf_preanswer_does_not_allocate_relay_ports(self) -> None:
        on_invite = self.invite_router
        trunk_branch = self.inbound_trunk
        no_dtmf_branch = trunk_branch[
            trunk_branch.index("if not dtmf_preanswer:") :
            trunk_branch.index("hass = runtime.hass")
        ]
        dtmf_branch = trunk_branch[trunk_branch.index("hass = runtime.hass") :]
        self.assertIn("return None", no_dtmf_branch)
        self.assertNotIn("RtpPortReservation.allocate", no_dtmf_branch)
        self.assertIn("RtpPortReservation.allocate(hass)", dtmf_branch)

        preprocess = on_invite[: on_invite.index("bucket = hass.data.setdefault")]
        self.assertIn("trunk_direct_preprocessed = True", preprocess)
        self.assertLess(
            on_invite.index("trunk_direct_preprocessed = True"),
            on_invite.index("if decision.action is RouteAction.GROUP:"),
        )

    def test_route_requests_expose_pbx_ingress_provenance(self) -> None:
        on_invite = self.inbound_automation
        self.assertIn(
            'ingress="trunk" if trunk_invite else "extension"',
            on_invite,
        )
        self.assertIn(
            'origin="trunk" if trunk_invite else "extension"',
            on_invite,
        )

    def test_trunk_dtmf_route_request_preserves_trunk_provenance(self) -> None:
        route_request = self.trunk_routing
        self.assertIn('ingress="trunk"', route_request)
        self.assertIn('origin="trunk"', route_request)
        self.assertIn('route_kind="trunk"', route_request)

    def test_automation_group_destination_reenters_canonical_dispatch(self) -> None:
        on_invite = self.invite_router
        override = on_invite[on_invite.index("fallback_destination =") :]
        group_dispatch = override[
            override.index("if decision.action is RouteAction.GROUP:") :
            override.index('    _LOGGER.info(\n        "Inbound route selected')
        ]
        self.assertIn("return await route_local_group(", group_dispatch)
        self.assertIn("runtime.run_ring_group_call(", self.inbound_local)
        self.assertIn("conference_manager(", self.inbound_local)
        self.assertIn("routed_invite = replace(", group_dispatch)

    def test_ha_origin_ring_group_uses_local_media_without_sip_answer(self) -> None:
        ring_group = self.ring_group
        winner_bridge = ring_group[
            ring_group.index("if ha_origin:\n                relay =") :
            ring_group.index(
                "_set_sip_bridge_call_state(",
                ring_group.index("if ha_origin:\n                relay ="),
            )
        ]
        self.assertIn("build_local_client_relay(", winner_bridge)
        self.assertIn("else:\n                relay = build_invite_client_relay(", winner_bridge)
        self.assertIn(
            "if not ha_origin:\n            answer = build_answer_directional(",
            winner_bridge,
        )

    def test_offline_browser_remains_a_logical_ringing_destination(self) -> None:
        target_checks = self.inbound_targets
        self.assertNotIn(
            "endpoint.offline_policy is OfflinePolicy.UNAVAILABLE",
            target_checks,
        )
        self.assertIn("endpoint.kind is not EndpointKind.BROWSER", target_checks)

    def test_video_invites_preserve_video_during_dtmf_preanswer(self) -> None:
        trunk_branch = self.inbound_trunk
        self.assertNotIn("and invite.video_format is None", trunk_branch)
        self.assertIn("reserve_sip_video_media(hass)", trunk_branch)
        self.assertIn('"local_video_rtp_port": source_video_port', trunk_branch)
        self.assertIn("video_port=source_video_port", trunk_branch)
        self.assertIn("preanswer_video_direction = (", trunk_branch)
        self.assertIn("allow_send=True", trunk_branch)
        self.assertIn("video_direction=preanswer_video_direction", trunk_branch)

    def test_dtmf_can_route_to_an_additional_ha_softphone(self) -> None:
        runner = self.trunk_inbound_router
        browser_route = runner[
            runner.index("decision = runtime.route_resolver.route(") :
        ]
        self.assertIn("if decision.action is RouteAction.ANSWER_HA:", browser_route)
        self.assertIn("await runtime.forward_existing_call(", browser_route)
        self.assertIn("destination=destination", browser_route)

    def test_dtmf_route_to_current_master_is_assignment_not_self_forward(self) -> None:
        runner = self.trunk_inbound_router
        same_endpoint = runner[
            runner.index("current_endpoint_id = str(") :
            runner.index(
                "await runtime.forward_existing_call(",
                runner.index("current_endpoint_id = str("),
            )
        ]
        self.assertIn("target_endpoint.endpoint_id == current_endpoint_id", same_endpoint)
        self.assertIn("runtime.defer_invite_to_softphone(", same_endpoint)
        self.assertIn('last_sip_event="DTMF_ROUTE"', same_endpoint)
        self.assertIn(
            "registry = call_registry(hass)",
            runner.split("configured_trunk =", 1)[0],
        )

    def test_dtmf_sip_bridge_tracks_destination_dialog_termination(self) -> None:
        runner = self.trunk_inbound_router
        self.assertIn("async_watch_sip_bridge_destination(", runner)
        self.assertIn("lifecycle_task=finish_task", runner)
        self.assertIn("terminate_sip_bridge=runtime.terminate_sip_bridge", runner)

    def test_every_sip_bridge_registration_has_a_lifecycle_owner(self) -> None:
        missing: list[str] = []
        registrations = 0
        component = ROOT / "custom_components" / "voip_stack"
        for path in component.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "register_bridge"
                ):
                    continue
                registrations += 1
                if not any(
                    keyword.arg == "lifecycle_task" for keyword in node.keywords
                ):
                    missing.append(f"{path.relative_to(ROOT)}:{node.lineno}")
        self.assertGreaterEqual(registrations, 5)
        self.assertEqual(missing, [])

    def test_detached_dtmf_route_failure_releases_call_and_sends_bye(self) -> None:
        guarded = self.source[
            self.source.index("async def _run_trunk_inbound_route_guarded(") :
            self.source.index(
                "async def _run_ring_group_call(",
                self.source.index("async def _run_trunk_inbound_route_guarded("),
            )
        ]
        self.assertIn("except asyncio.CancelledError:", guarded)
        self.assertIn("registry.pending_invites.pop", guarded)
        self.assertIn(
            "registry.take_media(invite.call_id, provisional=True)", guarded
        )
        self.assertIn("_release_media_reservation(preanswered)", guarded)
        self.assertIn("_sip_send_bye(hass, invite.call_id)", guarded)
        self.assertIn("registry.finish_and_pop(", guarded)
        creator = self.inbound_trunk[
            self.inbound_trunk.index("create_runtime_task(") :
            self.inbound_trunk.index("return SipInviteResult(200")
        ]
        self.assertIn("runtime.run_trunk_inbound_route_guarded(", creator)

    def test_unknown_dtmf_route_finishes_the_preanswered_session(self) -> None:
        runner = self.trunk_inbound_router
        rejected = runner[
            runner.index("elif decision.action is RouteAction.REJECT:") :
            runner.index(
                "else:\n        destination =",
                runner.index("elif decision.action is RouteAction.REJECT:"),
            )
        ]
        self.assertIn("send_bye(hass, invite.call_id)", rejected)
        self.assertIn("bridge_ports.release()", rejected)
        self.assertIn("registry.finish_and_pop(", rejected)
        self.assertIn("state=CallState.TRANSPORT_UNREACHABLE.value", rejected)
        self.assertLess(
            rejected.index("bridge_ports.release()"),
            rejected.index("registry.finish_and_pop("),
        )

    def test_answer_ha_keeps_an_explicit_dtmf_extension(self) -> None:
        runner = self.trunk_inbound_router
        self.assertIn(
            "destination = route_hint or decision.target or default_target",
            runner,
        )

    def test_remote_bridge_termination_closes_winning_leg_and_relay(self) -> None:
        terminated = self.endpoint_termination
        bridge_branch = terminated[
            terminated.index("if relay is not None or client is not None:") :
        ]
        bridge_branch = bridge_branch[: bridge_branch.index("if (")]
        pre_cleanup = terminated[
            : terminated.index("if relay is not None or client is not None:")
        ]
        self.assertIn(
            "session = registry.sessions.get(registry.resolve_session_id(call_id))",
            pre_cleanup,
        )
        self.assertIn("event_caller = (", pre_cleanup)
        self.assertIn("invite.caller", pre_cleanup)
        self.assertIn("session.caller", pre_cleanup)
        self.assertIn("session.callee", pre_cleanup)
        self.assertIn("else invite.target", pre_cleanup)
        self.assertIn("await async_cleanup_sip_runtime(", bridge_branch)
        self.assertIn("relay=relay", bridge_branch)
        self.assertIn("client=client", bridge_branch)
        self.assertIn("watcher=watcher", bridge_branch)
        self.assertIn("terminate_client=True", bridge_branch)
        self.assertIn("caller=event_caller", bridge_branch)
        self.assertIn("callee=event_callee", bridge_branch)
        self.assertIn("target=event_callee", bridge_branch)

    def test_remote_softphone_termination_uses_owning_logical_endpoint(self) -> None:
        terminated = self.endpoint_termination
        terminal_branch = terminated[
            terminated.index("session_metadata =") : terminated.index(
                "if relay is not None or client is not None:"
            )
        ]
        self.assertIn(
            'session_metadata.get("endpoint_id") or DEFAULT_ENDPOINT_ID',
            terminal_branch,
        )
        self.assertIn(
            "softphone_store = _ha_softphone_store(self.hass, session_endpoint_id)",
            terminal_branch,
        )
        self.assertIn("endpoint_id=session_endpoint_id", terminated)
        self.assertIn("session_device_id=session_device_id", terminated)
        self.assertNotIn(
            'softphone_store = bucket.get("ha_softphone", {})', terminated
        )

    def test_config_entry_reload_restores_runtime_event_listeners(self) -> None:
        init_py = INIT.read_text()
        setup = init_py[
            init_py.index("async def _async_setup_shared(") : init_py.index(
                "async def async_setup("
            )
        ]
        initialized = setup[
            setup.index('if bucket.get("initialized"):') : setup.index(
                'bucket["initialized"] = True'
            )
        ]
        self.assertIn("_register_esp_state_event_bridge(hass)", initialized)
        self.assertIn("_register_phonebook_service_event_sync(hass)", initialized)

    def test_trunk_dtmf_has_priority_over_automation_override(self) -> None:
        runner = self.trunk_inbound_router
        collector = runner.index("selection = await collect_trunk_dtmf(")
        override = runner.index(
            "if not digits and configured_trunk.get(CONF_AUTOMATION_ROUTING_ENABLED):"
        )
        self.assertLess(collector, override)
        self.assertNotIn("route_future", runner[:override])
        self.assertIn("async_request_inbound_destination(", runner[override:])
        self.assertIn("await asyncio.wait_for(", self.trunk_routing)

    def test_preanswered_forward_failure_resumes_ha_ringing(self) -> None:
        forward = self.call_forwarder
        self.assertIn("or call_id in registry.preanswered", forward)
        restore = forward[
            forward.index("async def _restore_or_terminate(") :
            forward.index("async def _run_forward(")
        ]
        self.assertIn("if ha_claimed:", restore)
        self.assertIn("CallState.RINGING.value", restore)
        self.assertIn('last_sip_event="ROUTE_RESUME"', restore)

    def test_in_dialog_dtmf_uses_the_canonical_call_envelope(self) -> None:
        bridge = self.dtmf_events
        for field in (
            '"schema_version": CALL_EVENT_SCHEMA_VERSION',
            '"actor": "sip_bridge"',
            '"ingress": call_origin',
            '"event_type": "dtmf"',
            '"automation_control": "ha_anchored"',
            "registry.event_fields(call_id, state)",
        ):
            self.assertIn(field, bridge)

    def test_route_request_publishes_a_canonical_connecting_state(self) -> None:
        route_branch = self.inbound_automation
        self.assertIn("CallState.CONNECTING.value", route_branch)
        self.assertIn("route_request=True", route_branch)
        self.assertNotIn('"route_requested",\n                caller=', route_branch)

    def test_connecting_route_request_maps_to_native_event_type(self) -> None:
        self.assertEqual(
            self.automation_routing.automation_event_type(
                {
                    "state": "connecting",
                    "direction": "incoming",
                    "route_request": True,
                }
            ),
            "route_requested",
        )

    def test_call_state_sensor_accepts_terminal_for_its_active_call(self) -> None:
        sensor = SENSOR.read_text()
        self.assertIn("and call_id != self._active_call_id", sensor)
        self.assertIn('if terminal and terminal_reason != "forwarded":', sensor)
        self.assertIn(
            "not terminal\n            and call_id == self._active_call_id",
            sensor,
        )

    def test_inbound_bridge_publishes_remote_ringing_with_direction(self) -> None:
        bridge = self.inbound_bridge[
            self.inbound_bridge.rindex('if result == "ringing":') :
        ]
        self.assertIn("CallState.REMOTE_RINGING.value", bridge)
        self.assertIn('direction="incoming"', bridge)

    def test_inbound_assist_bridge_preserves_direction(self) -> None:
        self.assertIn('direction="incoming"', self.assist_endpoint)

    def test_direct_ha_alias_resolves_through_phonebook_name(self) -> None:
        router = self.source[
            self.source.index("def _inbound_route_decision(") :
            self.source.index("async def _async_forward_existing_call(")
        ]
        self.assertIn("route_target = invite.routing_target", router)
        self.assertIn(
            "target = _ha_peer_name(hass) if _is_ha_target(route_target) else route_target",
            router,
        )

    def test_dtmf_cancellation_precedes_automation_window(self) -> None:
        runner = self.trunk_inbound_router
        cancellation = runner.index(
            'if invite.call_id in bucket.get("trunk_closed_calls", set()):'
        )
        automation = runner.index(
            "if not digits and configured_trunk.get(CONF_AUTOMATION_ROUTING_ENABLED):"
        )
        self.assertLess(cancellation, automation)

    def test_dtmf_preanswer_selects_final_response_by_negotiated_transport(
        self,
    ) -> None:
        invite = self.inbound_trunk[
            self.inbound_trunk.index(
                "dtmf_formats = sip_sdp.offered_dtmf_formats("
            ) :
        ]
        self.assertIn("confirm_for_sip_info = dtmf_format is None", invite)
        self.assertIn(
            'return SipInviteResult(200, "OK", answer_sdp=answer, to_tag="")',
            invite,
        )
        self.assertIn('183,\n        "Session Progress"', invite)
        self.assertIn("defer_final=True", invite)

    def test_assist_handoff_preserves_session_transport_provenance(self) -> None:
        self.assertIn('existing_metadata.get("ingress")', self.assist_endpoint)
        self.assertIn('existing_metadata.get("origin")', self.assist_endpoint)
        self.assertIn("ingress=call_ingress", self.assist_endpoint)
        self.assertIn("origin=call_ingress", self.assist_endpoint)

    def test_route_override_publishes_resolved_callee(self) -> None:
        route = self.invite_router[
            self.invite_router.index("resolved_callee = str(") :
        ]
        self.assertIn("callee=resolved_callee", route)
        self.assertIn("peer_name=resolved_callee", self.inbound_bridge)

    def test_softphone_snapshot_exposes_video_rtp_diagnostics(self) -> None:
        websocket = WEBSOCKET_API.read_text()
        counters = websocket[
            websocket.index("_MEDIA_COUNTER_KEYS = (") : websocket.index(
                "def _runtime_counter("
            )
        ]
        for name in (
            "video_rtp_tx_packets",
            "video_rtp_rx_packets",
            "video_rtp_dropped_packets",
            "video_drop_addr",
            "video_drop_payload_type",
            "video_drop_error",
            "video_reordered_packets",
            "video_lost_packets",
            "video_duplicate_packets",
            "video_symmetric_rtp_keepalives",
            "video_symmetric_rtp_keepalive_payload_type",
            "video_access_unit_queue_max",
            "video_access_unit_queue_drops",
            "video_browser_keyframe_requests",
            "video_rtcp_rx_packets",
            "video_rtcp_tx_packets",
            "video_rtcp_pli_rx",
            "video_rtcp_fir_rx",
            "video_rtcp_keyframe_requests_to_browser",
            "video_jpeg_normalized",
            "video_jpeg_normalizer_errors",
        ):
            self.assertIn(f'"{name}"', counters)
        self.assertIn('"media_debug": media_debug', websocket)

    def test_video_reinvite_resets_generation_owned_media_state(self) -> None:
        video_ws = (
            ROOT / "custom_components" / "voip_stack" / "video_ws_view.py"
        ).read_text()
        media_update = MEDIA_RENEGOTIATION.read_text()
        start = media_update.index("async def _commit_softphone_update()")
        commit = media_update[
            start : media_update.index("return SipInviteResult(", start)
        ]
        self.assertIn("registry.video_parameter_sets.pop(call_id, None)", commit)
        self.assertIn("commit_video_session_update(", commit)
        self.assertIn("[*parameter_sets, *_sdp_parameter_sets(browser_format)]", video_ws)
        self.assertIn("outbound_clock.reset_browser()", video_ws)
        self.assertIn("observed_generation = -1", video_ws)

    def test_declined_rtcp_mux_is_not_used_by_media_runtime(self) -> None:
        listener = (
            ROOT / "custom_components" / "voip_stack" / "sip_listener.py"
        ).read_text()
        client = (
            ROOT / "custom_components" / "voip_stack" / "sip_client.py"
        ).read_text()
        legacy = 'remote_video_rtcp_mux=(bool(remote_video["rtcp_mux"])'
        self.assertNotIn(legacy, listener)
        self.assertNotIn(legacy, client)
        self.assertIn("RemoteMediaTarget.from_section(", listener)
        self.assertIn("RemoteMediaTarget.from_section(", client)
        self.assertIn("rtcp_mux=False", listener)
        self.assertIn("rtcp_mux=False", client)
        self.assertIn("as_remote_video_fields()", listener)
        self.assertIn("as_remote_video_fields()", client)


if __name__ == "__main__":
    unittest.main()
