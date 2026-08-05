#!/usr/bin/env python3
"""Backend contract checks for the HA SIP softphone."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.architecture
INIT = ROOT / "custom_components" / "voip_stack" / "__init__.py"
ENDPOINT_RUNTIME = ROOT / "custom_components" / "voip_stack" / "endpoint_runtime.py"
INVITE_ROUTER = ROOT / "custom_components" / "voip_stack" / "invite_router.py"
INBOUND_BRIDGE = (
    ROOT
    / "custom_components"
    / "voip_stack"
    / "inbound_routing"
    / "bridge.py"
)
RING_GROUP_ORCHESTRATOR = (
    ROOT / "custom_components" / "voip_stack" / "ring_group_orchestrator.py"
)
MEDIA_RENEGOTIATION = (
    ROOT / "custom_components" / "voip_stack" / "media_renegotiation.py"
)
OUTBOUND_LIFECYCLE = (
    ROOT / "custom_components" / "voip_stack" / "outbound_lifecycle.py"
)
ESPHOME_STATE_BRIDGE = (
    ROOT / "custom_components" / "voip_stack" / "esphome_state_bridge.py"
)
CALL_SCOPE = ROOT / "custom_components" / "voip_stack" / "call_scope.py"
SERVICE_ENDPOINTS = (
    ROOT / "custom_components" / "voip_stack" / "service_endpoints.py"
)
SIP_BRIDGE = ROOT / "custom_components" / "voip_stack" / "sip_bridge.py"
SERVICES = ROOT / "custom_components" / "voip_stack" / "services.py"
ENDPOINT_ROUTING = ROOT / "custom_components" / "voip_stack" / "endpoint_routing.py"
SENSOR = ROOT / "custom_components" / "voip_stack" / "sensor.py"
VIDEO_WS = ROOT / "custom_components" / "voip_stack" / "video_ws_view.py"
CONFIG_FLOW = ROOT / "custom_components" / "voip_stack" / "config_flow.py"
CONFIG_ENTRY_RUNTIME = (
    ROOT / "custom_components" / "voip_stack" / "config_entry_runtime.py"
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


def _function_body(source: str, function_name: str) -> str:
    match = re.search(
        rf"\n(?:async def|def) {re.escape(function_name)}\([^)]*\)(?: -> [^:]+)?:",
        source,
    )
    if not match:
        raise AssertionError(f"function {function_name} not found")
    start = match.end()
    next_def = re.search(r"\n(?:async def|def) \w+\(", source[start:])
    end = start + next_def.start() if next_def else len(source)
    return source[start:end]


class HaSoftphoneBackendContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = INIT.read_text()
        cls.outbound_lifecycle = OUTBOUND_LIFECYCLE.read_text()
        cls.esphome_state_bridge = ESPHOME_STATE_BRIDGE.read_text()
        cls.call_scope = CALL_SCOPE.read_text()
        cls.service_endpoints = SERVICE_ENDPOINTS.read_text()
        cls.config_entry_runtime = CONFIG_ENTRY_RUNTIME.read_text()
        cls.softphone_termination = SOFTPHONE_TERMINATION.read_text()
        cls.softphone_answer = SOFTPHONE_ANSWER.read_text()
        cls.softphone_originate = SOFTPHONE_ORIGINATE.read_text()

    def test_hangup_publishes_authoritative_idle_state(self) -> None:
        body = _function_body(
            self.softphone_termination, "async_hangup_browser_call"
        )
        self.assertIn("_ha_softphone_store(hass, endpoint_id)", body)
        self.assertIn("_set_ha_softphone_call_state(", body)
        state_update = body.split("_set_ha_softphone_call_state(", 1)[1]
        self.assertIn("CallState.IDLE.value", state_update)
        self.assertIn("TerminalReason.LOCAL_HANGUP.value", state_update)
        self.assertIn("last_sip_event=", state_update)

    def test_hangup_does_not_depend_on_card_side_inference(self) -> None:
        body = _function_body(
            self.softphone_termination, "async_hangup_browser_call"
        )
        self.assertNotIn("card", body.lower())
        self.assertNotIn("frontend", body.lower())

    def test_hangup_preserves_canonical_outbound_direction(self) -> None:
        body = _function_body(
            self.softphone_termination, "async_hangup_browser_call"
        )
        direction = body.split("direction = str(", 1)[1].split("\n    )", 1)[0]
        self.assertLess(
            direction.index('softphone_store.get("direction")'),
            direction.index('("incoming" if active_session is not None else "")'),
        )

    def test_bridge_hangup_uses_central_terminal_projection(self) -> None:
        body = _function_body(
            self.softphone_termination, "async_hangup_browser_call"
        )
        bridge_branch = body.split("if bridge_handled:", 1)[1].split("return", 1)[0]
        self.assertNotIn("_set_sip_bridge_call_state(", bridge_branch)
        self.assertNotIn("_set_ha_softphone_call_state(", bridge_branch)

    def test_bridge_teardown_clears_only_its_matching_softphone_session(self) -> None:
        body = _function_body(
            self.softphone_termination, "async_terminate_sip_bridge_session"
        )
        self.assertIn('softphone_call_id = str(softphone.get("call_id") or "")', body)
        self.assertIn("if handled and source_call_id == softphone_call_id:", body)
        matching_branch = body.split(
            "if handled and source_call_id == softphone_call_id:", 1
        )[1]
        self.assertIn("_set_ha_softphone_call_state(", matching_branch)
        self.assertIn("CallState.IDLE.value", matching_branch)
        self.assertIn('last_sip_event="SIP_BYE"', matching_branch)
        self.assertIn("projected_call_ids.intersection", body)
        self.assertIn("_set_sip_bridge_call_state(", body)

    def test_inbound_bridge_completion_does_not_mutate_ha_softphone(self) -> None:
        bridge_path = _function_body(
            INBOUND_BRIDGE.read_text(),
            "route_sip_bridge",
        )
        self.assertIn("_set_sip_bridge_call_state(", bridge_path)
        self.assertNotIn("_set_ha_softphone_call_state(", bridge_path)

    def test_ha_softphone_busy_excludes_bridge_runtime_maps(self) -> None:
        ws = (
            ROOT / "custom_components" / "voip_stack" / "websocket_api.py"
        ).read_text()
        state_body = _function_body(ws, "_ha_softphone_state")
        busy_expr = state_body.split('"busy":', 1)[1].split(",", 1)[0]
        self.assertIn("session_device_id", busy_expr)
        self.assertNotIn("pending_transactions", busy_expr)
        self.assertNotIn("active_dialogs", busy_expr)

    def test_shutdown_revokes_media_owners_without_rebinding_maps(self) -> None:
        ws = (
            ROOT / "custom_components" / "voip_stack" / "websocket_api.py"
        ).read_text()
        body = _function_body(ws, "_async_shutdown_all")
        self.assertIn("async_revoke_media_owners", body)
        self.assertIn("await asyncio.gather(*owner_shutdowns)", body)
        self.assertNotIn('bucket.pop("audio_ws_owners"', body)
        self.assertNotIn('bucket.pop("video_ws_owners"', body)

    def test_shutdown_does_not_cancel_inflight_executor_capture_writes(self) -> None:
        ws = (
            ROOT / "custom_components" / "voip_stack" / "websocket_api.py"
        ).read_text()
        body = _function_body(ws, "_async_shutdown_all")
        capture_cleanup = body.split("capture_tasks =", 1)[1].split(
            "store = _ha_softphone_store", 1
        )[0]
        self.assertIn("await asyncio.wait(capture_tasks, timeout=2.0)", capture_cleanup)
        self.assertIn("will finish in background", capture_cleanup)
        self.assertNotIn("task.cancel()", capture_cleanup)
        self.assertIn("_set_ha_softphone_call_state(", body)
        self.assertIn("CallState.IDLE.value", body)
        self.assertIn('last_sip_event="shutdown"', body)
        self.assertIn("if active_call_id or active_state in {", body)
        idle_branch = body.split("else:", 1)[1]
        self.assertIn(
            "_publish_ha_softphone_state(hass, endpoint_id=endpoint_id)",
            idle_branch,
        )
        self.assertNotIn("_fire_call_event", idle_branch)
        self.assertIn("media.debug_capture_tasks", body)

    def test_topology_details_are_exposed_only_in_debug_mode(self) -> None:
        ws = (
            ROOT / "custom_components" / "voip_stack" / "websocket_api.py"
        ).read_text()
        state_body = _function_body(ws, "_ha_softphone_state")
        for key in ("rtp_relays", "sip_client_dialogs", "sip_trunk"):
            self.assertIn(
                f'"{key}": runtime["{key}"] if debug_mode else {{}}',
                state_body,
            )

    def test_idle_softphone_counters_do_not_fall_back_to_other_relays(self) -> None:
        ws = (
            ROOT / "custom_components" / "voip_stack" / "websocket_api.py"
        ).read_text()
        body = _function_body(ws, "_runtime_counter")
        self.assertIn("return store_value", body)
        self.assertNotIn("store_value or runtime_value", body)
        self.assertNotIn("max(store_value, runtime_value)", body)

    def test_all_phone_call_state_sensors_normalize_every_terminal_state(self) -> None:
        sensor = SENSOR.read_text()
        phone_sensor = sensor.split(
            "class PhoneEndpointCallStateSensor", 1
        )[1].split("class VoipPhonebookSensor", 1)[0]

        self.assertIn("state in TERMINAL_CALL_STATES", phone_sensor)
        self.assertIn('"protocol_error"', sensor)
        self.assertNotIn("state in {", phone_sensor)

    def test_video_reorder_timeout_and_teardown_publish_final_loss(self) -> None:
        body = VIDEO_WS.read_text(encoding="utf-8")

        input_timeout = body.index(
            "except TimeoutError:", body.index("async def rtp_to_transcoder")
        )
        direct_timeout = body.index(
            "except TimeoutError:", body.index("async def rtp_to_access_units")
        )
        final_store = body.index(
            "store_counters(force=True)",
            body.index('counters["video_rtp_dropped_packets"]'),
        )
        self.assertIn(
            "_sync_video_reorder_counters(",
            body[input_timeout : input_timeout + 500],
        )
        self.assertIn(
            "_sync_video_reorder_counters(",
            body[direct_timeout : direct_timeout + 500],
        )
        self.assertIn(
            "_sync_video_reorder_counters(", body[final_store - 300 : final_store]
        )

    def test_nonfatal_video_rtcp_and_keepalive_failures_are_observable(self) -> None:
        body = VIDEO_WS.read_text(encoding="utf-8")

        self.assertIn('counters["video_rtcp_send_errors"] += 1', body)
        self.assertIn('record_rtcp_send_error("keyframe feedback", err)', body)
        self.assertIn('record_rtcp_send_error("report", err)', body)
        self.assertIn("failures & (failures - 1) == 0", body)
        self.assertIn('counters["video_keepalive_task_errors"] += 1', body)
        self.assertIn("_observe_nonfatal_video_task", body)

    def test_dependent_browser_video_is_paced_without_dropping_a_gop(self) -> None:
        body = VIDEO_WS.read_text(encoding="utf-8")
        sender = body.split("async def browser_to_rtp()", 1)[1].split(
            "async def symmetric_rtp_keepalive()", 1
        )[0]
        limiter = sender.split("max_framerate = send_format.max_framerate", 1)[1]
        limiter = limiter.split("def media_current()", 1)[0]

        self.assertIn('if send_format.encoding == "JPEG":', limiter)
        self.assertIn("continue", limiter)
        self.assertIn("await asyncio.wait_for(", limiter)
        self.assertIn("closed.wait()", limiter)
        self.assertLess(
            limiter.index('if send_format.encoding == "JPEG":'),
            limiter.index("await asyncio.wait_for("),
        )
        self.assertIn("rtcp_task.add_done_callback", body)
        self.assertIn("keepalive_task.add_done_callback", body)

    def test_video_media_floods_yield_and_share_one_rtp_source_lock(self) -> None:
        body = VIDEO_WS.read_text(encoding="utf-8")

        self.assertIn("rtp_send_lock = asyncio.Lock()", body)
        self.assertGreaterEqual(body.count("async with rtp_send_lock:"), 4)
        self.assertGreaterEqual(
            body.count(">= _VIDEO_RTP_RECEIVE_BURST_PACKETS"), 4
        )
        self.assertIn("build_keepalive(", body)
        self.assertIn("sequence = int(rtp_source.sequence)", body)
        self.assertIn("browser video TX drops=%s", body)
        self.assertIn("video RTP RX drops=%s", body)
        self.assertIn("video transcode input drops=%s", body)

    def test_video_rtcp_reports_cover_send_only_and_use_current_rtp_clock(self) -> None:
        body = VIDEO_WS.read_text(encoding="utf-8")
        reports = body.split("    async def rtcp_reports()", 1)[1].split(
            "\n    async def rtcp_to_browser_feedback", 1
        )[0]

        initial_guard = reports.split("report_kwargs =", 1)[0]
        self.assertNotIn("latched_ssrc is None", initial_guard)
        self.assertIn('if counters["video_rtp_tx_packets"]:', reports)
        self.assertIn(
            "build_sender_compound(\n                    ssrc,\n                    latched_ssrc,",
            reports,
        )
        self.assertIn("rtp_timestamp=outbound_clock.current(monotonic_now)", reports)
        self.assertIn("elif latched_ssrc is not None:", reports)
        self.assertIn("extended_sequence.observe(packet.sequence)", body)
        self.assertIn("extended_sequence.reset()", body)

    def test_video_pipeline_changes_restart_the_media_owner(self) -> None:
        body = VIDEO_WS.read_text(encoding="utf-8")
        refresh = body.split("    async def refresh_media_state", 1)[1].split(
            "\n    try:", 1
        )[0]

        self.assertIn("_video_pipeline_signature(session)", refresh)
        self.assertIn('payload["restart_required"] = True', refresh)
        self.assertIn('payload["restart_reason"] = "video_pipeline_changed"', refresh)
        self.assertIn('counters["video_pipeline_restarts"] += 1', refresh)
        self.assertIn("return False", refresh)
        self.assertGreaterEqual(
            body.count("if not await refresh_media_state("),
            5,
        )

    def test_audio_reinvite_rebuilds_codec_timing_and_capture_generation(self) -> None:
        audio_ws = (
            ROOT / "custom_components" / "voip_stack" / "audio_ws_view.py"
        ).read_text()
        refresh = audio_ws.split("    async def refresh_media_state", 1)[1].split(
            "\n    try:", 1
        )[0]

        self.assertIn("next_decoder = RtpPayloadDecoder(session.recv_format)", refresh)
        self.assertIn("next_encoder = RtpPayloadEncoder(session.send_format)", refresh)
        self.assertIn("tx_frame_delay = next_frame_delay", refresh)
        self.assertIn("tx_silence_pcm = next_silence_pcm", refresh)
        self.assertIn(
            "_schedule_debug_capture_write(hass, debug_capture, counters)", refresh
        )
        self.assertIn("debug_capture = _DebugAudioCapture(", refresh)

        outbound = _function_body(
            self.softphone_originate, "async_originate_browser_call"
        )
        self.assertIn("commit_audio_session_update(", outbound)
        self.assertNotIn("audio_contract_changed", outbound)

        inbound = MEDIA_RENEGOTIATION.read_text()
        self.assertIn("commit_audio_session_update(", inbound)
        self.assertNotIn("audio_contract_changed", inbound)
        self.assertIn("protocol.reconfigure_frame_ms(", audio_ws)

    def test_conference_audio_websocket_tracks_call_lifetime(self) -> None:
        audio_ws = (
            ROOT / "custom_components" / "voip_stack" / "audio_ws_view.py"
        ).read_text()
        conference_audio = audio_ws.split("async def _run_conference_audio_session", 1)[
            1
        ]

        self.assertIn(
            "_listen_for_call_end(\n        hass, session.call_id, endpoint_id",
            conference_audio,
        )
        self.assertIn("lifetime_task", conference_audio)
        self.assertIn("remove_call_listener()", conference_audio)

    def test_missing_roster_formats_do_not_force_implicit_16k_default(self) -> None:
        endpoint_routing = ENDPOINT_ROUTING.read_text()
        body = _function_body(endpoint_routing, "roster_entry_formats")
        self.assertIn("if entry is None:", body)
        self.assertIn("return []", body)
        self.assertIn('if value in (None, ""):', body)
        self.assertIn("if not raw.strip():", body)

    def test_services_register_async_handlers_not_coroutine_returning_lambdas(
        self,
    ) -> None:
        services = SERVICES.read_text()
        self.assertIn("async def _handle(call: ServiceCall) -> object:", services)
        self.assertNotIn("lambda call:", services)
        self.assertNotIn("call_handler(", services)

    def test_phonebook_sensor_treats_unknown_as_unavailable(self) -> None:
        sensor = SENSOR.read_text()
        self.assertIn('UNAVAILABLE_STATES = {"", "unknown", "unavailable"}', sensor)
        self.assertIn("_state_is_available(old_state)", sensor)
        self.assertIn("_state_is_available(new_state)", sensor)

    def test_stale_call_id_cannot_replace_active_softphone_session(self) -> None:
        ws = (
            ROOT / "custom_components" / "voip_stack" / "websocket_api.py"
        ).read_text()
        body = _function_body(ws, "_set_ha_softphone_call_state")
        self.assertIn("next_call_id != previous_call_id", body)
        self.assertIn("previous_state", body)
        self.assertIn("Ignoring stale HA softphone", body)
        guard = body.split("next_call_id != previous_call_id", 1)[1].split(
            "if terminal:", 1
        )[0]
        self.assertIn("CallState.IN_CALL.value", guard)
        self.assertIn("return", guard)

    def test_esphome_roster_service_registration_refreshes_phonebook(self) -> None:
        self.assertIn("EVENT_SERVICE_REGISTERED", self.config_entry_runtime)
        body = _function_body(
            self.config_entry_runtime,
            "register_phonebook_service_event_sync",
        )
        self.assertIn('"phonebook_service_event_unsub"', body)
        self.assertIn('event.data.get("domain") != "esphome"', body)
        self.assertIn('service.endswith("_set_roster_json")', body)
        self.assertIn("async_refresh_and_push_phonebook(hass)", body)
        self.assertNotIn("retry", body.lower())

    def test_softphone_rtp_latches_source_port_and_ssrc(self) -> None:
        audio_ws = (
            ROOT / "custom_components" / "voip_stack" / "audio_ws_view.py"
        ).read_text()
        self.assertIn("latched_rtp_source", audio_ws)
        self.assertIn("latched_rtp_ssrc", audio_ws)
        self.assertIn("remote_rtp_port = source[1]", audio_ws)
        self.assertIn("packet.ssrc != latched_rtp_ssrc", audio_ws)
        self.assertIn("(remote_rtp_host, remote_rtp_port)", audio_ws)
        self.assertIn("session.signaling_host", audio_ws)

    def test_video_rtp_setup_failure_releases_prebound_rtp_and_rtcp(self) -> None:
        video_ws = VIDEO_WS.read_text()
        body = _function_body(video_ws, "_run_video_session")
        setup_failure = body.split(
            "except (OSError, RuntimeError, ValueError) as err:", 1
        )[1].split("browser_format =", 1)[0]
        socket_cleanup = body.split("def close_detached_sockets()", 1)[1].split(
            "try:", 1
        )[0]
        self.assertIn("session.rtp_socket.close()", socket_cleanup)
        self.assertIn("session.rtp_socket = None", socket_cleanup)
        self.assertIn("session.rtcp_socket.close()", socket_cleanup)
        self.assertIn("session.rtcp_socket = None", socket_cleanup)
        self.assertIn("close_detached_sockets()", setup_failure)

    def test_video_rtcp_accepts_separately_advertised_host(self) -> None:
        video_ws = VIDEO_WS.read_text()
        start = video_ws.index("    async def rtcp_to_browser_feedback()")
        body = video_ws[
            start : video_ws.index("    try:\n        if session.rtcp_socket", start)
        ]
        allowed = body.split("allowed_hosts = {", 1)[1].split("}", 1)[0]
        self.assertIn("session.remote_rtcp_host", allowed)
        self.assertIn("remote_rtcp_host", allowed)
        self.assertIn("remote_rtcp_host_explicit", video_ws)
        self.assertIn(
            "latched_rtcp_source is None and not remote_rtcp_host_explicit",
            video_ws,
        )

    def test_final_audio_counters_preserve_terminal_sip_event(self) -> None:
        audio_ws = (
            ROOT / "custom_components" / "voip_stack" / "audio_ws_view.py"
        ).read_text()
        start = audio_ws.index("    def publish_counters(")
        body = audio_ws[start : audio_ws.index("    def negotiation_payload(", start)]
        update = body.split("update = {", 1)[1].split("if bool(hass.data.get", 1)[0]
        self.assertNotIn('"last_sip_event": "rtp_media"', update.split("}", 1)[0])
        self.assertIn("if current_call_id:", update)
        self.assertIn('update["last_sip_event"] = "rtp_media"', update)

    def test_audio_negotiation_failure_closes_bound_rtp_transport(self) -> None:
        audio_ws = (
            ROOT / "custom_components" / "voip_stack" / "audio_ws_view.py"
        ).read_text()
        body = _function_body(audio_ws, "_run_audio_session")
        negotiation = body.split("await ws.send_json(negotiation_payload())", 1)[1]
        before_attach = negotiation.split('"HA softphone audio websocket attached', 1)[
            0
        ]
        self.assertIn("except asyncio.CancelledError:", before_attach)
        self.assertGreaterEqual(before_attach.count("transport.close()"), 3)

    def test_audio_media_telemetry_never_reemits_sip_lifecycle_events(self) -> None:
        audio_ws = (
            ROOT / "custom_components" / "voip_stack" / "audio_ws_view.py"
        ).read_text()
        counters_start = audio_ws.index("    def publish_counters(")
        counters = audio_ws[
            counters_start : audio_ws.index(
                "    def negotiation_payload(", counters_start
            )
        ]
        conference_start = audio_ws.index("async def _run_conference_audio_session(")
        conference = audio_ws[conference_start:]

        self.assertNotIn("_fire_call_event", counters)
        self.assertIn(
            "_publish_ha_softphone_state(hass, endpoint_id=endpoint_id)", counters
        )
        self.assertNotIn("_fire_call_event", conference)
        self.assertIn(
            "_publish_ha_softphone_state(hass, endpoint_id=endpoint_id)", conference
        )

    def test_final_video_snapshot_includes_pending_rtcp_queue_drops(self) -> None:
        video_ws = VIDEO_WS.read_text()
        start = video_ws.index("    def store_counters(*, force: bool = False)")
        end = video_ws.index("    def queue_access_unit", start)
        body = video_ws[start:end]
        drain = body.index("_sync_video_protocol_drop_counters(")
        persist = body.index("store.update(counters)")
        self.assertLess(drain, persist)
        sync_start = video_ws.index("def _sync_video_protocol_drop_counters(")
        sync_end = video_ws.index("def _sync_video_reorder_counters(", sync_start)
        sync_body = video_ws[sync_start:sync_end]
        self.assertIn("rtcp_protocol.take_drop_counts()", sync_body)
        self.assertIn('counters["video_rtcp_drop_queue"] += queue_drops', sync_body)
        debug_line = next(
            line
            for line in body.splitlines()
            if "if debug_mode(hass):" in line
        )
        publish_line = next(
            line
            for line in body.splitlines()
            if "_publish_ha_softphone_state(hass, endpoint_id=endpoint_id)" in line
        )
        self.assertEqual(
            len(debug_line) - len(debug_line.lstrip()),
            len(publish_line) - len(publish_line.lstrip()),
        )

    def test_softphone_tx_uses_one_deadline_per_frame_without_double_wait(self) -> None:
        audio_ws = (
            ROOT / "custom_components" / "voip_stack" / "audio_ws_view.py"
        ).read_text()
        self.assertIn(
            "tx_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)", audio_ws
        )
        self.assertIn("pcm = tx_queue.get_nowait()", audio_ws)
        self.assertNotIn("while not tx_queue.empty():", audio_ws)
        self.assertIn("payload = rtp_encoder.encode(pcm)", audio_ws)
        self.assertIn("tx_queue.put_nowait(pcm)", audio_ws)
        self.assertNotIn(
            "await asyncio.wait_for(tx_queue.get(), timeout=frame_delay)", audio_ws
        )
        self.assertNotIn("asyncio.wait_for(closed.wait()", audio_ws)
        self.assertIn("await asyncio.sleep(sleep_for)", audio_ws)
        self.assertIn("next_send = loop.time() + frame_delay", audio_ws)

    def test_softphone_tx_uses_negotiated_rtp_timestamp_clock(self) -> None:
        audio_ws = (
            ROOT / "custom_components" / "voip_stack" / "audio_ws_view.py"
        ).read_text()
        body = _function_body(audio_ws, "_run_audio_session")
        self.assertIn("session.send_format.rtp_timestamp_step", body)
        self.assertNotIn(
            "session.send_format.audio_format.nominal_frame_samples",
            body,
        )

    def test_softphone_start_is_serialized_and_ring_group_claims_state_before_io(
        self,
    ) -> None:
        prepare = _function_body(
            self.outbound_lifecycle,
            "async_prepare_ha_outbound_call",
        )
        self.assertIn("artifacts.softphone_start_locks.setdefault(", prepare)
        self.assertIn("endpoint_id, asyncio.Lock()", prepare)
        self.assertIn("async with start_lock:", prepare)

        endpoint_runtime = ENDPOINT_RUNTIME.read_text()
        start = endpoint_runtime.index("async def _start_ring_group_from_ha(")
        end = endpoint_runtime.index(
            "\n    pbx_runtime.ring_conference_members_from_ha",
            start,
        )
        ring_start = endpoint_runtime[start:end]
        publish = ring_start.index("_set_ha_softphone_call_state(")
        snapshot = ring_start.index("peers = await _async_build_peer_snapshot(hass)")
        self.assertLess(publish, snapshot)
        self.assertIn("PEER_SNAPSHOT_FAILED", ring_start)

    def test_initial_outbound_state_precedes_final_response_watcher(self) -> None:
        """A queued 200 OK must not be overwritten by the earlier 180 result."""

        outbound = _function_body(
            self.softphone_originate, "async_originate_browser_call"
        )
        initial_result = outbound.index(
            'if public_result == CallState.REMOTE_RINGING.value or result == "ringing":'
        )
        watcher = outbound.index("await _track_outbound_sip_client(")
        self.assertLess(initial_result, watcher)
        self.assertIn("fast peer can place 180 and 200", outbound)

    def test_outbound_state_carries_resolved_target_device_identity(self) -> None:
        """The card must not recover the physical peer from SIP display text."""

        outbound = _function_body(
            self.softphone_originate, "async_originate_browser_call"
        )
        self.assertIn('getattr(target_endpoint, "device_id", "")', outbound)
        self.assertIn('entry_metadata.get("device_id")', outbound)
        self.assertGreaterEqual(
            outbound.count("target_device_id=target_device_id"),
            6,
        )
        tracker = _function_body(
            self.outbound_lifecycle,
            "async_track_outbound_sip_client",
        )
        self.assertIn(
            'target_device_id: str = ""',
            self.outbound_lifecycle,
        )
        self.assertGreaterEqual(
            tracker.count("target_device_id=target_device_id"),
            3,
        )

    def test_final_200_commits_registry_before_publishing_in_call(self) -> None:
        tracker = _function_body(
            self.outbound_lifecycle,
            "async_track_outbound_sip_client",
        )
        watcher = tracker.split("async def _watch_sip_lifecycle()", 1)[1]
        accepted = watcher.split(
            "if public_final == CallState.IN_CALL.value and client.dialog is not None:",
            1,
        )[1].split("elif public_final", 1)[0]
        self.assertIn(
            "registry.sip_clients.get(client.dialog_ids.call_id) is not client",
            watcher,
        )
        self.assertIn("registry.upsert(", accepted)
        self.assertIn("registry.add_leg(", accepted)
        self.assertLess(
            accepted.index("registry.add_leg("),
            accepted.index("_set_ha_softphone_call_state("),
        )
        self.assertIn("state=CallState.IN_CALL.value", accepted)

    def test_outbound_call_claims_source_and_physical_destination_atomically(self) -> None:
        outbound = _function_body(
            self.softphone_originate, "async_originate_browser_call"
        )
        claims = outbound.rsplit("registry = _call_registry(hass)", 1)[1].split(
            "_bind_service_call_controller", 1
        )[0]
        self.assertIn("registry.claim_endpoint(", claims)
        self.assertIn('role="source"', claims)
        self.assertIn('role="destination"', claims)
        self.assertIn("except EndpointBusyError as err:", claims)
        self.assertIn("registry.finish_and_pop(", claims)

        destination_policy = outbound.split(
            "target_endpoint = _logical_endpoint_for_route", 1
        )[1].split("local_ip =", 1)[0]
        self.assertIn("target_endpoint.dnd", destination_policy)
        self.assertIn("target_endpoint.active_call_id", destination_policy)
        self.assertIn("EndpointAvailability.AVAILABLE", destination_policy)

    def test_outbound_failure_and_invite_exception_release_call_ownership(self) -> None:
        tracker = _function_body(
            self.outbound_lifecycle,
            "async_track_outbound_sip_client",
        )
        immediate_failure = tracker.split(
            'if result not in {"ringing", "in_call"}:', 1
        )[1].split("registry.sip_clients[client.dialog_ids.call_id]", 1)[0]
        self.assertIn("registry.finish_and_pop(", immediate_failure)
        self.assertLess(
            immediate_failure.index("registry.finish_and_pop("),
            immediate_failure.index("await client.close()"),
        )

        outbound = _function_body(
            self.softphone_originate, "async_originate_browser_call"
        )
        invite_error = outbound.split(
            "except Exception as err:  # noqa: BLE001 - isolate one outbound SIP leg.",
            1,
        )[1].split("if registry.sip_clients.get", 1)[0]
        self.assertIn("registry.detach_client", invite_error)
        self.assertIn("_set_ha_softphone_call_state(", invite_error)
        self.assertIn("registry.finish_and_pop(", invite_error)

    def test_esp_state_mirrors_physical_busy_ownership_into_logical_endpoint(self) -> None:
        bridge = _function_body(
            self.esphome_state_bridge,
            "async_emit_state_event",
        )
        self.assertIn("endpoint_registry.sync_transport_call(", bridge)
        self.assertIn('fallback_call_id=f"physical:{endpoint.endpoint_id}"', bridge)
        self.assertIn("canonical_state in HA_SOFTPHONE_ACTIVE_STATES", bridge)
        self.assertIn('payload["call_id"] = transport_call_id', bridge)

    def test_video_hold_resume_keeps_per_call_camera_authorization(self) -> None:
        answer = _function_body(self.softphone_answer, "async_answer_browser_call")
        self.assertIn(
            '"camera_send_authorized": bool(camera_send_enabled)', answer
        )
        authorization = answer.split('"camera_send_authorized":', 1)[1].split(
            ",", 1
        )[0]
        self.assertNotIn("invite.video_format", authorization)

        media_update = MEDIA_RENEGOTIATION.read_text()
        self.assertIn(
            'allow_video_send = bool(media.get("camera_send_authorized", False))',
            media_update,
        )
        self.assertNotIn("previous_video_direction", media_update)
        self.assertIn('"video_active": bool(', media_update)
        self.assertIn("media_endpoint_id = str(", media_update)
        self.assertIn("_ha_softphone_store(hass, media_endpoint_id)", media_update)
        self.assertIn("endpoint_id=media_endpoint_id", media_update)
        self.assertNotIn("_ha_softphone_store(hass)", media_update)

    def test_browser_video_send_is_independent_from_receive_transcoding(self) -> None:
        video_ws = VIDEO_WS.read_text()
        can_send = video_ws[
            video_ws.index("    def can_send(self) -> bool:")
            : video_ws.index("    @property\n    def can_receive", video_ws.index("    def can_send(self) -> bool:"))
        ]
        self.assertIn("self.camera_send_enabled", can_send)
        self.assertIn("self.send_video_format", can_send)
        self.assertNotIn("requires_receive_transcoding", can_send)
        self.assertNotIn("requires_transcoding", video_ws)

    def test_direct_calls_publish_remote_connected_identity(self) -> None:
        answer = _function_body(self.softphone_answer, "async_answer_browser_call")
        self.assertIn("connected_party=invite.caller", answer)
        self.assertIn("answered_by=invite.caller", answer)

        outbound = _function_body(
            self.outbound_lifecycle,
            "async_track_outbound_sip_client",
        )
        self.assertIn('getattr(client, "connected_party", "") or target', outbound)
        self.assertIn("peer_name=connected_party", outbound)
        self.assertIn("connected_party=connected_party", outbound)

    def test_inbound_answer_preserves_logical_phone_as_callee(self) -> None:
        answer = _function_body(self.softphone_answer, "async_answer_browser_call")
        self.assertIn(
            "session = registry.sessions.get(registry.resolve_session_id(call_id))",
            answer,
        )
        self.assertIn("resolved_callee = str(", answer)
        self.assertIn("callee=resolved_callee", answer)
        self.assertIn("dialed_target=invite.target", answer)

    def test_call_membership_accepts_both_destination_metadata_spellings(self) -> None:
        body = _function_body(self.call_scope, "call_endpoint_ids")
        self.assertIn('"source_endpoint_id"', body)
        self.assertIn('"dest_endpoint_id"', body)
        self.assertIn('"target_endpoint_id"', body)

    def test_phone_services_accept_only_the_device_selector(self) -> None:
        services = SERVICES.read_text()
        self.assertIn('phone_selector_fields = {vol.Optional("device_id"):', services)
        for schema_name in (
            "sip_answer_schema",
            "sip_decline_schema",
            "sip_hangup_schema",
            "sip_call_schema",
            "sip_forward_schema",
            "set_dnd_schema",
            "set_auto_answer_schema",
            "set_send_video_schema",
            "set_ha_softphone_settings_schema",
        ):
            schema = services.split(f"{schema_name} = vol.Schema(", 1)[1].split(
                "extra=vol.PREVENT_EXTRA", 1
            )[0]
            self.assertIn("**phone_selector_fields", schema)
            self.assertNotIn('vol.Optional("endpoint_id")', schema)
            self.assertNotIn('vol.Optional("entity_id")', schema)

    def test_websocket_state_subscriptions_keep_internal_endpoint_scoping(self) -> None:
        websocket = (
            ROOT / "custom_components" / "voip_stack" / "websocket_api.py"
        ).read_text()
        selector = _function_body(websocket, "_endpoint_id_from_selector")
        self.assertIn("for entity in _values(entity_id)", selector)
        self.assertIn("endpoint = _by_current_entity_id(entity)", selector)
        self.assertIn("registry.by_entity_id(entity_id)", selector)
        self.assertIn("er.async_get(hass).async_get(entity_id)", selector)
        self.assertIn("registry.by_device_id(device)", selector)
        self.assertIn("len(selected_ids) > 1", selector)
        self.assertIn("selected_ids != {resolved_id}", selector)

        service_descriptions = (
            ROOT / "custom_components" / "voip_stack" / "services.yaml"
        ).read_text()
        service_names = (
            "answer",
            "decline",
            "hangup",
            "call",
            "forward",
            "set_dnd",
            "set_auto_answer",
            "set_send_video",
            "set_ha_softphone_settings",
        )
        for index, service_name in enumerate(service_names):
            start = service_descriptions.index(f"\n{service_name}:\n")
            later_starts = [
                service_descriptions.find(f"\n{name}:\n", start + 1)
                for name in service_names[index + 1 :]
            ]
            later_starts = [position for position in later_starts if position >= 0]
            end = min(later_starts) if later_starts else len(service_descriptions)
            description = service_descriptions[start:end]
            self.assertIn("device_id:", description)
            self.assertNotIn("endpoint_id:", description)
            self.assertNotIn("entity_id:", description)

    def test_invalid_new_sip_username_stays_a_form_validation_error(self) -> None:
        config_flow = CONFIG_FLOW.read_text()
        start = config_flow.index("    def _normalized_common(")
        normalized = config_flow[
            start : config_flow.index("    def _finish(", start)
        ]
        self.assertIn(
            "elif CONF_PHONE_USERNAME not in errors:", normalized
        )
        guarded = normalized.split(
            "elif CONF_PHONE_USERNAME not in errors:", 1
        )[1].split("data = {", 1)[0]
        self.assertIn("endpoint_id = new_sip_account_endpoint_id()", guarded)

    def test_dnd_targets_browser_and_registered_sip_phone_devices(self) -> None:
        configured_start = self.service_endpoints.index(
            "def service_configured_endpoint("
        )
        configured = self.service_endpoints[
            configured_start : self.service_endpoints.index(
                "def browser_endpoint_name(", configured_start
            )
        ]
        self.assertIn("EndpointKind.BROWSER", configured)
        self.assertIn("EndpointKind.SIP_ACCOUNT", configured)
        self.assertIn('device_id = str(call.data.get("device_id")', configured)
        self.assertIn("registry.by_device_id(device_id)", configured)
        self.assertNotIn('call.data.get("endpoint_id")', configured)
        self.assertNotIn('call.data.get("entity_id")', configured)

        dnd = _function_body(self.source, "_handle_set_dnd_service")
        settings = _function_body(
            self.source, "_handle_set_ha_softphone_settings_service"
        )
        self.assertIn("_service_configured_endpoint(hass, call)", dnd)
        self.assertIn(
            "_service_browser_endpoint(hass, call, strict=True)", settings
        )

    def test_browser_preference_services_use_the_canonical_phone_writer(self) -> None:
        preference = _function_body(
            self.source, "_handle_browser_preference_service"
        )
        self.assertIn(
            "_service_browser_endpoint(hass, call, strict=True)", preference
        )
        self.assertIn("await async_set_ha_softphone_settings(", preference)
        self.assertIn("**{preference: enabled}", preference)

        websocket = (
            ROOT / "custom_components" / "voip_stack" / "websocket_api.py"
        ).read_text()
        settings = _function_body(
            websocket, "async_set_ha_softphone_settings"
        )
        self.assertIn('store["auto_answer"] = bool(auto_answer)', settings)
        self.assertIn('store["send_video"] = bool(send_video)', settings)
        self.assertIn(
            "await _async_save_ha_softphone_store(hass, endpoint_id)", settings
        )

    def test_video_bridge_projects_destination_h264_level_to_source_leg(self) -> None:
        routing_sources = (
            INVITE_ROUTER.read_text() + RING_GROUP_ORCHESTRATOR.read_text()
        )
        sip_bridge = SIP_BRIDGE.read_text()
        self.assertIn("source_video = (", sip_bridge)
        self.assertIn("video_answer_contract(", sip_bridge)
        self.assertIn(
            "relay.left.video_format = source_directional.send",
            sip_bridge,
        )
        self.assertIn(
            "relay.left.local_video_format = source_directional.recv",
            sip_bridge,
        )
        self.assertIn("video_format=source_video", sip_bridge)
        self.assertIn(
            "video_answer = configure_answered_invite_video_relay(",
            routing_sources,
        )

    def test_outbound_softphone_accepts_live_peer_media_updates(self) -> None:
        outbound = _function_body(
            self.softphone_originate, "async_originate_browser_call"
        )
        self.assertIn("async def _prepare_softphone_media_update", outbound)
        self.assertIn(
            "client.on_media_update = _prepare_softphone_media_update",
            outbound,
        )
        self.assertIn("commit_audio_session_update(", outbound)
        self.assertIn("commit_video_session_update(", outbound)
        self.assertIn('"video_active": bool(', outbound)

    def test_ha_originated_ring_group_exposes_browser_audio_through_existing_relay(
        self,
    ) -> None:
        audio_ws = (
            ROOT / "custom_components" / "voip_stack" / "audio_ws_view.py"
        ).read_text()
        ring_group = RING_GROUP_ORCHESTRATOR.read_text()
        self.assertIn('"rtp_loopback": True', ring_group)
        self.assertIn('"remote_rtp_port": source_relay_port', ring_group)
        self.assertIn('"send_format": invite.recv_format', ring_group)
        self.assertIn('"recv_format": invite.send_format', ring_group)
        self.assertIn('item.get("rtp_loopback")', audio_ws)
        self.assertIn("local_rtp_port=0", audio_ws)
        self.assertIn(
            "rtp.AudioRtpSenderState.create(ssrc=session.local_ssrc)",
            audio_ws,
        )

    def test_rtp_sender_identity_survives_browser_media_handoff(self) -> None:
        audio_ws = (
            ROOT / "custom_components" / "voip_stack" / "audio_ws_view.py"
        ).read_text()
        video_ws = (
            ROOT / "custom_components" / "voip_stack" / "video_ws_view.py"
        ).read_text()
        sip_client = (
            ROOT / "custom_components" / "voip_stack" / "sip_client.py"
        ).read_text()

        self.assertIn("self.audio_rtp_source = None", sip_client)
        self.assertIn("self.video_rtp_source = None", sip_client)
        self.assertIn('item["audio_rtp_source"] = rtp_source', audio_ws)
        self.assertIn("client.audio_rtp_source = rtp_source", audio_ws)
        self.assertIn("rtp_source.sequence = sequence", audio_ws)
        self.assertIn("rtp_source.timestamp = timestamp", audio_ws)
        self.assertIn("client.video_rtp_source = rtp_source", video_ws)
        self.assertIn("rtp_source=rtp_source", video_ws)

    def test_conference_checks_softphone_busy_and_websocket_does_not_own_call(
        self,
    ) -> None:
        call_service = _function_body(
            self.softphone_originate, "async_originate_browser_call"
        )
        conference_branch = call_service.split('if group_type == "conference":', 1)[
            1
        ].split(
            "route_uri = route.sip_uri",
            1,
        )[0]
        self.assertIn(
            "await _async_prepare_ha_outbound_call(hass, endpoint_id)",
            conference_branch,
        )

        audio_ws = (
            ROOT / "custom_components" / "voip_stack" / "audio_ws_view.py"
        ).read_text()
        conference_audio = _function_body(audio_ws, "_run_conference_audio_session")
        self.assertNotIn("leave_ha_softphone", conference_audio)
        self.assertNotIn("registry.softphone_media.pop", conference_audio)
        self.assertNotIn("registry.finish_and_pop", conference_audio)
        self.assertIn("conference_media_handoff", conference_audio)
        hangup = _function_body(
            self.softphone_termination, "async_hangup_browser_call"
        )
        self.assertIn("await manager.leave_ha_softphone(", hangup)
        self.assertIn("conference_room,", hangup)
        self.assertIn("call_id=call_id", hangup)


if __name__ == "__main__":
    unittest.main()
