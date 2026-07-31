#!/usr/bin/env python3
"""Static contract checks for the Lovelace voip card.

These tests pin the phase-1 VoIP UI split:

* `ha_softphone` owns browser audio and HA-originated calls.
* ESP cards are pure mirrors and only press the ESP entities.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CARD = ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-card.js"
CARD_EDITOR = CARD.with_name("voip-stack-card-editor.js")
CARD_MODEL = CARD.with_name("voip-stack-card-model.js")
PHONEBOOK_CARD = ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-phonebook-card.js"
ENDPOINT_DEVICE = ROOT / "custom_components" / "voip_stack" / "endpoint_device.py"


def _method_body(source: str, method_name: str) -> str:
    match = re.search(rf"\n\s+{re.escape(method_name)}\([^)]*\)\s*\{{", source)
    if not match:
        raise AssertionError(f"method {method_name} not found")
    start = match.end()
    depth = 1
    i = start
    while i < len(source) and depth:
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    if depth:
        raise AssertionError(f"method {method_name} body not closed")
    return source[start : i - 1]


class FrontendCardContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = CARD.read_text()
        cls.editor_source = CARD_EDITOR.read_text()
        cls.model_source = CARD_MODEL.read_text()

    def test_esp_contact_call_is_a_pure_button_press(self) -> None:
        body = _method_body(self.source, "async _startCall")
        self.assertIn("const softphoneAction = this._isHaSoftphoneMode()", body)
        esp_branch = body.split("if (softphoneAction)", 1)[1]
        esp_branch = esp_branch.split("catch (err)", 1)[0]
        self.assertIn('this._pressEspButton(this._callButtonEntityId, "Call")', esp_branch)
        self.assertIn("this._mirrorKeypadOpen", esp_branch)
        self.assertIn('this._hass.callService(domain, service, { dest: manualTarget })', esp_branch)
        self.assertNotIn("_startP2P", esp_branch)
        self.assertNotIn("destination === this._getHaName()", esp_branch)

    def test_esp_keypad_has_separate_manual_buffer_and_never_writes_destination(self) -> None:
        self.assertIn("this._mirrorManualTarget", self.source)
        self.assertIn("this._mirrorKeypadOpen", self.source)
        self.assertIn('this._mirrorManualTarget = ""', self.source)
        self.assertIn("_destinationEntityId = e.destination || null", self.source)
        self.assertNotIn('this._hass.callService("text", "set_value", { entity_id: this._destinationEntityId', self.source)
        self.assertNotIn('this._setTextEntity(this._destinationEntityId', self.source)
        toggle = _method_body(self.source, "_toggleKeypad")
        self.assertIn("!this._isHaSoftphoneMode() && !this._startCallService", toggle)
        keypress = _method_body(self.source, "_pressKeypadKey")
        self.assertNotIn("this._isHaSoftphoneMode()", keypress)

    def test_esp_manual_terminal_destination_does_not_replace_contact_cycler(self) -> None:
        render = _method_body(self.source, "_render")
        cycler = _method_body(self.source, "_contactCyclerDestination")
        self.assertIn("this._contactCyclerDestination(destination)", render)
        self.assertIn("this._isHaSoftphoneMode()", cycler)
        self.assertIn("!this._lastEndInfo", cycler)
        self.assertIn("this._lastKnownMirrorDestination = destination", cycler)
        self.assertIn("this._lastEndInfo ? this._lastKnownMirrorDestination || destination : destination", cycler)

    def test_esp_answer_call_is_a_pure_button_press(self) -> None:
        body = _method_body(self.source, "async _answer")
        self.assertIn("const softphoneAction = this._isHaSoftphoneMode()", body)
        esp_branch = body.split("if (softphoneAction)", 1)[1]
        esp_branch = esp_branch.split("catch (err)", 1)[0]
        self.assertIn('this._pressEspButton(this._callButtonEntityId, "Call")', esp_branch)
        self.assertNotIn("answer_esp_call", esp_branch)
        self.assertNotIn("voip_stack/answer", esp_branch)

    def test_ha_softphone_mode_is_the_only_softphone_context(self) -> None:
        body = _method_body(self.source, "_isSoftphoneContext")
        self.assertIn("this._isHaSoftphoneMode()", body)
        self.assertNotIn("this._isConfiguredSoftphone()", body)
        self.assertNotIn("this._isHaName(this._getDestination())", body)
        self.assertNotIn("_callMode", self.source)

    def test_card_default_mode_is_esp_mirror_not_hybrid(self) -> None:
        body = _method_body(self.source, "_isHaSoftphoneMode")
        self.assertIn('"esp_mirror"', body)
        self.assertNotIn('"hybrid"', body)

    def test_card_picker_device_models_match_backend_contract(self) -> None:
        backend = ENDPOINT_DEVICE.read_text()
        expected = {
            "BROWSER_PHONE_DEVICE_MODEL": "home assistant softphone",
            "SIP_ACCOUNT_DEVICE_MODEL": "sip account",
        }
        for constant, frontend_value in expected.items():
            match = re.search(rf'^{constant} = "([^"]+)"$', backend, re.MULTILINE)
            self.assertIsNotNone(match, constant)
            assert match is not None
            self.assertEqual(match.group(1).lower(), frontend_value)
            self.assertIn(f'deviceModel === "{frontend_value}"', self.source)

    def test_ha_softphone_uses_its_authoritative_state_stream(self) -> None:
        call_event = _method_body(self.source, "_onCallEvent")
        self.assertIn('scope === "sip_bridge"', call_event)
        self.assertIn("this._onMirroredBridgeStateEvent(event)", call_event)
        self.assertNotIn('scope === "session"', call_event)
        softphone = _method_body(self.source, "_onSoftphoneState")
        self.assertIn("this._applySoftphoneSnapshot(state)", softphone)
        self.assertNotIn("_eventConcernsThisCard", softphone)
        self.assertNotIn("_onSipStateEvent", self.source)

    def test_logical_softphone_wire_contract_is_endpoint_scoped(self) -> None:
        engine = (ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-engine.js").read_text()
        session_model = (
            ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-session-model.js"
        ).read_text()
        subscription = _method_body(engine, "_ensureSoftphoneScopeSubscription")
        state_match = _method_body(engine, "_softphoneStateMatches")
        start = _method_body(engine, "async startHaSoftphone")
        audio_url = _method_body(engine, "async _wsUrl")
        video = (ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-video.js").read_text()

        self.assertIn("request.endpoint_id = record.selector.endpoint_id", subscription)
        self.assertIn("request.device_id = record.selector.device_id", subscription)
        self.assertIn(
            "softphoneStateMatches(state, selector, subscriptionSelector)",
            state_match,
        )
        self.assertIn("stateEndpoint === wanted.endpoint_id", session_model)
        self.assertIn(
            "wanted.endpoint_id === DEFAULT_SOFTPHONE_ENDPOINT_ID",
            session_model,
        )
        self.assertIn('type: "call_service"', start)
        self.assertIn('domain: "voip_stack"', start)
        self.assertIn('service: "call"', start)
        self.assertIn("return_response: true", start)
        self.assertNotIn("request.service_data.endpoint_id", start)
        self.assertIn("request.service_data.device_id = deviceId", start)
        self.assertIn("endpoint_id=${encodeURIComponent", audio_url)
        self.assertIn("endpoint_id=${encodeURIComponent", video)

        card_state = _method_body(self.source, "_onSoftphoneState")
        loader = _method_body(self.source, "async _loadSoftphoneState")
        self.assertIn("this._softphoneSnapshotMatches(state)", card_state)
        self.assertIn("...this._softphoneRequestScope()", loader)
        attach = _method_body(self.source, "_ensureHaSoftphoneAudioPath")
        self.assertIn("voipStackEngine.endpointId !== endpointId", attach)

    def test_engine_retries_startup_subscriptions_until_integration_is_ready(self) -> None:
        engine = (ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-engine.js").read_text()
        configure = _method_body(engine, "configure")
        ensure = _method_body(engine, "_ensureBusSubscriptions")
        retry = _method_body(engine, "_scheduleBusSubscriptionRetry")

        self.assertIn("this._ensureBusSubscriptions(conn)", configure)
        self.assertIn("this._ensureSoftphoneScopeSubscription(conn, record)", ensure)
        self.assertIn("this._busSubscribePending", ensure)
        scoped = _method_body(engine, "_ensureSoftphoneScopeSubscription")
        self.assertIn("this._softphoneBusSubscribePending", scoped)
        self.assertIn("this._scheduleBusSubscriptionRetry(conn)", scoped)
        self.assertIn("request.endpoint_id", scoped)
        self.assertIn("setTimeout", retry)
        self.assertIn("this._ensureBusSubscriptions(conn)", retry)

    def test_esp_mirror_terminal_bridge_event_uses_dialed_target_and_reason(self) -> None:
        body = _method_body(self.source, "_onMirroredBridgeStateEvent")
        self.assertIn("this._eventConcernsThisCard(data)", body)
        self.assertIn('"busy"', body)
        self.assertIn("data.terminal_reason || data.reason || state", body)
        self.assertIn("terminalPeerLabel(data)", body)
        self.assertIn(
            'this._captureEndReason("terminal", reason, data.actor || "remote", peer)',
            body,
        )

    def test_ha_softphone_terminal_label_uses_remote_peer(self) -> None:
        apply_snapshot = _method_body(self.source, "_applySoftphoneSnapshot")
        self.assertIn("terminalPeerLabel(snapshot)", apply_snapshot)

    def test_ha_softphone_dnd_status_outweighs_terminal_history(self) -> None:
        render = _method_body(self.source, "_render")
        idle_branch = render.split('case "idle":', 1)[1].split('case "calling":', 1)[0]
        self.assertLess(
            idle_branch.index("this._softphoneDnd"),
            idle_branch.index("this._lastEndInfo"),
        )
        self.assertIn('statusText = "Do Not Disturb"', idle_branch)
        self.assertIn("Incoming calls to Home Assistant are declined.", idle_branch)

    def test_ha_softphone_targets_come_from_shared_roster(self) -> None:
        body = _method_body(self.source, "_softphoneTargets")
        self.assertIn("this._rosterEntries", body)
        self.assertIn("this._targetFromRosterEntry(entry)", body)
        self.assertIn("metadata.local_ha", body)
        self.assertNotIn("filter", body)
        self.assertNotIn("_availableDevices", body)

    def test_ha_softphone_targets_are_the_central_roster_with_only_self_exclusion(self) -> None:
        load = _method_body(self.source, "_loadSharedRoster")
        targets = _method_body(self.source, "_softphoneTargets")
        self.assertIn("roster_json", load)
        self.assertNotIn("softphone_targets_json", self.source)
        self.assertIn("metadata.local_ha", targets)
        self.assertNotIn("entry.address || entry.sip_uri", self.source)
        self.assertNotIn("_isCallableRosterEntry", self.source)

    def test_shared_roster_is_not_reparsed_on_unrelated_hass_updates(self) -> None:
        load = _method_body(self.source, "_loadSharedRoster")
        self.assertIn("this._rosterSourceKey === sourceKey", load)
        self.assertIn("this._rosterSourceKey = sourceKey", load)
        self.assertLess(
            load.index("this._rosterSourceKey === sourceKey"),
            load.index("JSON.parse(raw)"),
        )

    def test_softphone_destination_options_are_not_rebuilt_on_call_state_renders(self) -> None:
        render = _method_body(self.source, "_renderSoftphoneDestinationSelect")
        self.assertIn("this._softphoneTargetOptionsKey === optionsKey", render)
        self.assertLess(
            render.index("this._softphoneTargetOptionsKey === optionsKey"),
            render.index("select.replaceChildren(...options)"),
        )

    def test_ha_softphone_group_controls_are_dynamic_backend_state(self) -> None:
        load = _method_body(self.source, "_loadSharedRoster")
        groups = _method_body(self.source, "_availableSoftphoneGroups")
        render = _method_body(self.source, "_render")
        permission = _method_body(self.source, "_canConfigureHaSoftphone")
        setter = _method_body(self.source, "async _setHaSoftphoneSettings")
        dnd = _method_body(self.source, "async _toggleDnd")
        self.assertIn("roster_json", load)
        self.assertIn("metadata?.group_type", groups)
        self.assertIn('"voip_stack", "set_ha_softphone_settings"', setter)
        self.assertIn('"voip_stack", "set_dnd"', dnd)
        self.assertNotIn('"voip_stack/set_ha_softphone_settings"', self.source)
        self.assertNotIn('"voip_stack/set_ha_softphone_dnd"', self.source)
        self.assertIn("extension: this._softphoneExtension", setter)
        self.assertIn('id = "ha-softphone-extension"', self.source)
        self.assertIn('type = "text"', self.source)
        self.assertIn('setAttribute("list", "ha-softphone-ring-group-options")', self.source)
        self.assertIn('setAttribute("list", "ha-softphone-conference-group-options")', self.source)
        self.assertNotIn("_populateGroupSelect", self.source)
        self.assertNotIn("conference_manager", self.source)
        self.assertNotIn("_ringConference", self.source)
        self.assertIn("this._hass?.user?.is_admin === true", permission)
        self.assertIn("this._canConfigureHaSoftphone()", render)
        self.assertIn("if (!this._canConfigureHaSoftphone())", setter)

    def test_disabled_logical_phone_is_not_rendered_as_callable(self) -> None:
        render = _method_body(self.source, "_render")
        self.assertIn("this._softphoneSnapshot?.enabled !== false", render)
        self.assertIn('statusText = "Phone unavailable"', render)
        disabled = render.split("if (!softphoneEnabled)", 1)[1].split(
            "else if", 1
        )[0]
        self.assertNotIn("showCall = true", disabled)

    def test_esp_mirror_settings_write_exposed_esp_entities(self) -> None:
        finder = _method_body(self.source, "async _findEntityIds")
        self.assertIn("e.auto_answer", finder)
        self.assertIn("e.dnd", finder)
        self.assertIn("e.voip_ring_groups", finder)
        self.assertIn("e.voip_conference_groups", finder)
        self.assertIn("e.voip_conference_ring", finder)
        self.assertIn("e.voip_extension", finder)
        self.assertIn("e.start_call_service", finder)
        self.assertNotIn("deviceInfo.route_id", finder)
        self.assertNotIn("`esphome.${deviceInfo.route_id}_start_call`", self.source)
        set_text = _method_body(self.source, "async _setTextEntity")
        set_switch = _method_body(self.source, "async _setSwitchEntity")
        group_setter = _method_body(self.source, "async _setGroupSetting")
        auto_answer = _method_body(self.source, "async _toggleAutoAnswer")
        self.assertIn('"text", "set_value"', set_text)
        self.assertIn('"switch", enabled ? "turn_on" : "turn_off"', set_switch)
        self.assertIn("async _setExtensionSetting", self.source)
        self.assertIn("this._extensionTextEntityId", self.source)
        self.assertIn("this._ringGroupsTextEntityId", group_setter)
        self.assertIn("this._conferenceGroupsTextEntityId", group_setter)
        self.assertIn("this._conferenceRingSwitchEntityId", group_setter)
        self.assertIn("this._autoAnswerSwitchEntityId", auto_answer)

    def test_ha_softphone_actions_target_only_the_ha_softphone(self) -> None:
        service_scope = _method_body(self.source, "_softphoneServiceScope")
        self.assertIn("device_id: deviceId", service_scope)
        self.assertNotIn("{ endpoint_id:", service_scope)

        answer = _method_body(self.source, "async _answer")
        ha_answer = answer.split("if (softphoneAction)", 1)[1].split(
            'await this._pressEspButton(this._callButtonEntityId, "Call")', 1
        )[0]
        self.assertIn('"voip_stack", "answer"', ha_answer)
        self.assertIn("...this._softphoneServiceScope()", ha_answer)
        self.assertIn("call_id: callId", ha_answer)
        self.assertNotIn('type: "voip_stack/answer"', ha_answer)
        self.assertNotIn("voipStackEngine.resumeSession(sessionInfo, HA_SOFTPHONE_DEVICE_ID", ha_answer)
        self.assertNotIn("this._sessionDeviceId()", ha_answer)

        decline = _method_body(self.source, "async _decline")
        ha_decline = decline.split("if (softphoneAction)", 1)[1].split("} else {", 1)[0]
        self.assertIn('"voip_stack", "decline"', ha_decline)
        self.assertIn("...this._softphoneServiceScope()", ha_decline)
        self.assertIn("call_id: callId", ha_decline)
        self.assertNotIn("this._sessionDeviceId()", ha_decline)

        hangup = _method_body(self.source, "async _hangup")
        softphone_hangup = hangup.split("if (wasSoftphone)", 1)[1].split("} else {", 1)[0]
        self.assertIn('"voip_stack", "hangup"', softphone_hangup)
        self.assertIn("...this._softphoneServiceScope()", softphone_hangup)
        self.assertIn("call_id: callId", softphone_hangup)
        self.assertNotIn("this._sessionDeviceId()", softphone_hangup)

    def test_hangup_preempts_pending_outbound_start(self) -> None:
        render = _method_body(self.source, "_render")
        hangup = _method_body(self.source, "async _hangup")
        start = _method_body(self.source, "async _startCall")

        self.assertIn("els.hangupBtn.disabled = this._stopping", render)
        self.assertNotIn("els.hangupBtn.disabled = buttonDisabled", render)
        self.assertIn('case "connecting":', render)
        self.assertIn("showHangup = true", render.split("if (this._starting)", 1)[1])
        self.assertIn("++this._callOperationId", hangup)
        self.assertIn("this._starting = false", hangup)
        self.assertIn("const operationId = ++this._callOperationId", start)
        self.assertIn("operationId === this._callOperationId", start)
        self.assertLess(
            start.index("const operationId = ++this._callOperationId"),
            start.index("await this._getDeviceInfo()"),
        )

    def test_ha_terminal_reason_is_transient_and_deduplicated(self) -> None:
        apply_snapshot = _method_body(self.source, "_applySoftphoneSnapshot")
        render = _method_body(self.source, "_render")

        self.assertIn("this._lastSoftphoneTerminalKey", apply_snapshot)
        self.assertIn("this._captureEndReason(", apply_snapshot)
        self.assertIn("this._isHaSoftphoneMode() && this._lastEndInfo", render)
        self.assertNotIn("this._softphoneSnapshot?.terminal_reason", render)

    def test_ha_softphone_rejects_older_snapshots_for_the_same_call(self) -> None:
        normalise = _method_body(self.source, "_normaliseSoftphoneSnapshot")
        apply_snapshot = _method_body(self.source, "_applySoftphoneSnapshot")

        self.assertIn("sequence: Number(payload.sequence || 0)", normalise)
        self.assertIn("revision: Number(payload.revision || 0)", normalise)
        self.assertIn("current?.call_id === snapshot.call_id", apply_snapshot)
        self.assertIn("snapshot.sequence < currentSequence", apply_snapshot)
        self.assertIn("snapshot.sequence === currentSequence", apply_snapshot)
        self.assertIn("Number(current.revision || 0) > snapshot.revision", apply_snapshot)
        self.assertIn("return false", apply_snapshot)

    def test_phonebook_is_an_internal_main_card_mode(self) -> None:
        source = PHONEBOOK_CARD.read_text()
        self.assertIn('customElements.define("voip-stack-phonebook-view"', source)
        self.assertNotIn('customElements.define("voip-phonebook-card"', source)
        self.assertNotIn('type: "voip-phonebook-card"', source)
        self.assertIn('phonebookOpt.value = "phonebook"', self.editor_source)
        self.assertIn('this._isPhonebookMode()', self.source)
        self.assertIn('document.createElement("voip-stack-phonebook-view")', self.source)

    def test_phonebook_card_is_scrollable_safe_and_roster_driven(self) -> None:
        source = PHONEBOOK_CARD.read_text()
        self.assertIn('overflow-y: auto', source)
        self.assertIn('attributes?.roster_json', source)
        self.assertIn('localeCompare', source)
        self.assertIn('contact.enabled !== false', source)
        self.assertIn('link.href = `tel:', source)
        self.assertIn('name.textContent = this._name(contact)', source)
        self.assertNotIn("innerHTML", source)
        self.assertIn("background: transparent", source)
        self.assertNotIn("code-editor-background-color", source)

    def test_main_voip_module_loads_phonebook_card_with_same_cache_version(self) -> None:
        self.assertIn(
            'import(`./voip-phonebook-card.js?v=${encodeURIComponent(VOIP_STACK_MODULE_VERSION)}`)',
            self.source,
        )

    def test_phone_cards_support_native_sections_resizing(self) -> None:
        grid = _method_body(self.source, "getGridOptions")
        self.assertIn("columns: 12", grid)
        self.assertIn("rows: 7", grid)
        self.assertIn("min_columns: 6", grid)
        self.assertIn("min_rows: 4", grid)
        self.assertIn("min_columns: 4", grid)
        self.assertIn("min_rows: 3", grid)
        self.assertEqual(grid.count("max_rows: 8"), 2)
        self.assertIn("new ResizeObserver(() => this._measureLayout())", self.source)
        self.assertIn("const width = card.clientWidth", self.source)
        self.assertIn("const height = card.clientHeight", self.source)
        self.assertIn('--voip-button-size', self.source)
        self.assertGreaterEqual(self.source.count('document.createElement("ha-card")'), 2)
        self.assertNotIn('const card = document.createElement("div")', self.source)
        self.assertIn("overflow-y: auto", self.source)
        self.assertIn("height: 100%", self.source)

    def test_phone_card_masonry_size_matches_default_sections_height(self) -> None:
        size = _method_body(self.source, "getCardSize")
        self.assertIn("return 7", size)

    def test_esp_mirror_does_not_render_sip_rtp_counters(self) -> None:
        render = _method_body(self.source, "_render")
        stats_branch = render.split("// Stats line", 1)[1].split("// Error", 1)[0]
        self.assertIn("this._isHaSoftphoneMode()", stats_branch)
        self.assertIn("voipStackEngine.statsText()", stats_branch)
        self.assertNotIn("voip_sip_snapshot", self.source)
        self.assertNotIn("rtp_tx_packets", self.source)
        self.assertNotIn("rtp_rx_packets", self.source)

    def test_ha_softphone_in_call_state_attaches_browser_audio(self) -> None:
        body = _method_body(self.source, "_onSoftphoneState")
        self.assertIn("this._applySoftphoneSnapshot(state)", body)
        self.assertIn("this._ensureHaSoftphoneAudioPath(state)", body)

    def test_terminal_ha_softphone_event_always_closes_engine(self) -> None:
        cleanup = _method_body(self.source, "_cleanupAfterTerminalSession")
        self.assertIn("voipStackEngine.active", cleanup)
        self.assertIn('voipStackEngine.close("terminal")', cleanup)
        self.assertNotIn("this._hasBrowserAudioPath()", cleanup)

    def test_deep_link_answer_handles_ha_softphone_session_ringing(self) -> None:
        apply_snapshot = _method_body(self.source, "_applySoftphoneSnapshot")
        self.assertIn("this._maybeAnswerFromUrl()", apply_snapshot)

        maybe_answer = _method_body(self.source, "_maybeAnswerFromUrl")
        self.assertNotIn("if (this._isHaSoftphoneMode() ||", maybe_answer)
        self.assertIn("if (!this._isHaSoftphoneMode()) return", maybe_answer)
        self.assertIn("snap.direction", maybe_answer)
        self.assertIn("snap.call_id", maybe_answer)
        self.assertIn("callId: String(snap.call_id)", maybe_answer)
        self.assertIn("requirePersistentPermission: false", maybe_answer)

    def test_softphone_actions_capture_call_identity_before_async_work(self) -> None:
        answer = _method_body(self.source, "async _answer")
        decline = _method_body(self.source, "async _decline")
        hangup = _method_body(self.source, "async _hangup")
        auto_answer = _method_body(self.source, "async _tryAutoAnswer")

        for body in (answer, decline, hangup):
            self.assertLess(body.index("const callId ="), body.index("await this._getDeviceInfo()"))
            self.assertIn("const operationId = ++this._callOperationId", body)
        self.assertIn("this._sessionCallId() !== callId", answer)
        self.assertIn("this._sessionCallId() !== callId", decline)
        self.assertIn("const ownedCallId = String(voipStackEngine.softphoneCallId", hangup)
        self.assertIn("settleServiceWithin(", hangup)
        self.assertIn("voipStackEngine.suspendVideoForHangup(", hangup)
        self.assertLess(
            hangup.index("voipStackEngine.suspendVideoForHangup("),
            hangup.index("await this._getDeviceInfo()"),
        )
        self.assertIn('void voipStackEngine.close("hangup")', hangup)
        self.assertIn("this._sessionCallId() !== callId", auto_answer)
        self.assertIn("await this._answer({ callId, videoPermission })", auto_answer)

    def test_terminal_snapshot_is_not_rejected_by_revision_guard(self) -> None:
        apply_snapshot = _method_body(self.source, "_applySoftphoneSnapshot")
        self.assertIn("const terminalSnapshot = [", apply_snapshot)
        self.assertGreaterEqual(apply_snapshot.count("!terminalSnapshot &&"), 2)

    def test_video_answer_preflights_camera_and_auto_answer_never_prompts(self) -> None:
        answer = _method_body(self.source, "async _answer")
        start = _method_body(self.source, "async _startHaSoftphoneCall")
        auto_answer = _method_body(self.source, "async _tryAutoAnswer")
        engine = (
            ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-engine.js"
        ).read_text()
        permission = _method_body(engine, "async prepareVideoCameraPermission")

        self.assertIn("this._softphoneSnapshot?.send_video", answer)
        wants_video = answer.split("const wantsVideo = Boolean(", 1)[1].split(
            ");", 1
        )[0]
        self.assertNotIn("video_offered", wants_video)
        self.assertIn("this._softphoneSnapshot?.send_video", start)
        self.assertNotIn("targetSupportsVideo", start)
        self.assertIn("endpointId: this._getSoftphoneEndpointId()", answer)
        self.assertIn("endpointId: this._getSoftphoneEndpointId()", start)
        self.assertIn("persistentOnly: true", auto_answer)
        self.assertIn("this._softphoneSnapshot?.send_video", auto_answer)
        self.assertIn("endpointId: this._getSoftphoneEndpointId()", auto_answer)
        self.assertIn("navigator.permissions?.query", permission)
        self.assertIn('permission.state === "granted"', permission)
        self.assertIn("if (persistentOnly)", permission)
        self.assertIn("navigator.mediaDevices.getUserMedia", permission)
        self.assertIn("track.stop()", permission)

    def test_outbound_video_uses_standard_sdp_negotiation(self) -> None:
        target = _method_body(self.source, "_targetFromRosterEntry")
        start = _method_body(self.source, "async _startHaSoftphoneCall")

        self.assertIn("targetFromRosterEntry(entry)", target)
        self.assertIn("this._softphoneSnapshot?.send_video", start)
        self.assertNotIn("targetSupportsVideo", start)

    def test_deep_link_answer_is_not_part_of_esp_mirror_state_updates(self) -> None:
        setter = _method_body(self.source, "set hass")
        self.assertNotIn("this._maybeAnswerFromUrl(newEspState)", setter)

    def test_reconfigure_discards_stale_device_entity_bindings(self) -> None:
        config = _method_body(self.source, "setConfig")
        reset = _method_body(self.source, "_resetDeviceBindings")
        finder = _method_body(self.source, "async _findEntityIds")
        resolver = _method_body(self.source, "async _getDeviceInfo")

        self.assertIn("oldSelector !== newSelector || oldMode !== newMode", config)
        self.assertIn("this._resetDeviceBindings()", config)
        self.assertIn('this._startCallService = ""', reset)
        self.assertIn('"_voipStateEntityId"', reset)
        self.assertIn("expectedSelector !== this._getConfigSelector()", finder)
        self.assertIn("expectedSelector !== this._getConfigSelector()", resolver)
        self.assertGreaterEqual(
            finder.count("expectedSelector !== this._getConfigSelector()"),
            2,
        )

    def test_existing_entity_and_device_card_bindings_remain_supported(self) -> None:
        """Pin old entity dashboards while new configs use Device Registry IDs."""

        selector = _method_body(self.source, "_getConfigSelector")
        device_id = _method_body(self.source, "_getConfigDeviceId")
        editor = _method_body(self.editor_source, "_deviceChanged")

        self.assertIn("this.config?.entity_id || this.config?.device_id", selector)
        self.assertIn("this._resolvedDeviceId || this._getConfigSelector()", device_id)
        self.assertIn("newConfig.device_id = deviceId", editor)
        self.assertIn("delete newConfig.entity_id", editor)
        self.assertIn('device_id: deviceId', self.source)

    def test_device_discovery_is_single_flight_with_bounded_startup_retry(self) -> None:
        finder = _method_body(self.source, "async _findEntityIds")
        scheduler = _method_body(self.source, "_scheduleDeviceBindingsLoad")
        resolver = _method_body(self.source, "async _getDeviceInfo")
        disconnect = _method_body(self.source, "disconnectedCallback")

        self.assertIn("this._deviceBindingsLoading || this._deviceBindingsRetryTimer", finder)
        self.assertIn("this._deviceBindingsLoading = true", finder)
        self.assertIn("this._deviceBindingsLoading = false", finder)
        self.assertIn("this._scheduleDeviceBindingsLoad()", finder)
        self.assertIn("this._deviceBindingsRetryTimer = setTimeout", scheduler)
        self.assertIn("this._isUnknownCommandError(err)", resolver)
        self.assertIn("clearTimeout(this._deviceBindingsRetryTimer)", disconnect)
        softphone_state = _method_body(self.source, "async _loadSoftphoneState")
        self.assertIn("const connection = this._hass.connection", softphone_state)
        self.assertIn("!this._isHaSoftphoneMode()", softphone_state)
        self.assertIn("this._hass?.connection !== connection", softphone_state)

    def test_detached_card_transfers_page_owned_media_without_reclaiming_ui(self) -> None:
        controller = _method_body(self.source, "_isSoftphoneController")
        config = _method_body(self.source, "setConfig")
        disconnect = _method_body(self.source, "disconnectedCallback")
        self.assertIn("this.isConnected", controller)
        self.assertIn('oldMode === "ha_softphone" && newMode !== "ha_softphone"', config)
        self.assertIn("voipStackEngine.releaseSoftphoneController(", config)
        self.assertIn("voipStackEngine.releaseVideoCanvas(this)", config)
        self.assertIn("this._unsubSoftphoneState()", config)
        start = _method_body(self.source, "async _startHaSoftphoneCall")
        answer = _method_body(self.source, "async _answer")
        self.assertNotIn("this._callOperationId++", disconnect)
        self.assertIn("shouldAbort: () => operationId !== this._callOperationId", start)
        self.assertNotIn("!this.isConnected", start)
        self.assertNotIn('reason: "superseded"', answer)
        hass_setter = _method_body(self.source, "set hass")
        connected = _method_body(self.source, "connectedCallback")
        self.assertGreaterEqual(hass_setter.count("this.isConnected"), 2)
        self.assertIn("this._subscribeBusEvents()", connected)
        self.assertIn("this._loadSoftphoneState()", connected)
        self.assertIn("this._render()", connected)

    def test_active_panel_does_not_label_answering_as_in_call(self) -> None:
        render = _method_body(self.source, "_render")
        self.assertIn('normalizedState === "answering"', render)
        self.assertIn('? "Answering"', render)
        self.assertIn('normalizedState === "terminating"', render)
        self.assertIn('case "terminating":', render)
        self.assertIn('statusText = "Ending call..."', render)

    def test_anonymous_incoming_calls_ring_and_receive_only_autoanswer_needs_no_mic(self) -> None:
        incoming = _method_body(self.source, "_isIncomingSoftphoneRing")
        ringtone = _method_body(self.source, "_syncRingtoneRequest")
        autoanswer = _method_body(self.source, "async _tryAutoAnswer")
        self.assertNotIn("_getCallerName", incoming)
        self.assertIn("this._softphoneSnapshot?.call_id", incoming)
        self.assertNotIn("!this._autoAnswer", ringtone)
        self.assertIn('["sendonly", "sendrecv"].includes(audioDirection)', autoanswer)
        self.assertIn("requirePersistentPermission &&", autoanswer)
        self.assertIn("needsMicrophone &&", autoanswer)

    def test_frontend_has_no_esp_call_control_ws_commands(self) -> None:
        engine = (ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-engine.js").read_text()
        for token in (
            "ENGINE_TRANSITIONS",
            "startP2P",
            "answerEspCall",
            "answerHaSoftphone",
            'this._setState("CALLING")',
            'this._setState("RINGING")',
            'type: "start"',
            'type: "answer"',
            'type: "stop"',
            'type: "hangup"',
            "answer_esp_call",
        ):
            self.assertNotIn(token, engine)

    def test_ha_softphone_browser_audio_survives_hidden_tabs(self) -> None:
        engine = (ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-engine.js").read_text()
        self.assertNotIn("hidden_timeout", engine)
        self.assertNotIn('document.addEventListener("visibilitychange"', engine)

    def test_browser_audio_websocket_is_bounded_and_stale_close_is_isolated(self) -> None:
        engine = (ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-engine.js").read_text()
        playback = (
            ROOT
            / "custom_components"
            / "voip_stack"
            / "frontend"
            / "voip-stack-playback-processor.js"
        ).read_text()
        capture = (
            ROOT
            / "custom_components"
            / "voip_stack"
            / "frontend"
            / "voip-stack-processor.js"
        ).read_text()

        self.assertIn("this._ws.bufferedAmount >= maxBufferedBytes", engine)
        self.assertIn("this._stats.tx_dropped++", engine)
        self.assertIn("if (this._ws !== ws) return", engine)
        self.assertIn("if (this._connectPromise === connectPromise)", engine)
        self.assertIn("connectGeneration !== this._connectGeneration ||", engine)
        self.assertIn("this._deviceId !== deviceId ||", engine)
        self.assertIn("this._callId !== wantedCallId", engine)
        self.assertIn("Audio WebSocket superseded before connect", engine)
        self.assertIn("const callId = String(reply?.call_id || \"\")", engine)
        self.assertIn("await this._connect(deviceId, callId, endpointId)", engine)
        setup = _method_body(engine, "async _setupAudioOrAbort")
        self.assertIn("let connected = false", setup)
        self.assertIn("connected = true", setup)
        self.assertIn("if (!connected)", setup)
        self.assertIn("this._mediaAttachOwnedByOther(beforeAttach, callId)", setup)
        self.assertIn("this._mediaAttachOwnedByOther(afterFailure, callId)", setup)
        self.assertIn("this.releaseSoftphoneSession(callId, endpointId)", setup)
        self.assertIn('await this.close("media_attach_conflict")', setup)
        self.assertLess(
            setup.index("if (!connected)"),
            setup.index('reason: "media_incompatible"'),
        )
        self.assertIn("deviceId === HA_SOFTPHONE_DEVICE_ID || !!endpointId", setup)
        self.assertIn("this._endpointId === endpointId", setup)
        self.assertIn("this._callId === callId", setup)
        self.assertNotIn("raw.slice(1)", engine)
        self.assertIn("byteOffset: 1", engine)
        self.assertIn("new DataView(buffer, byteOffset, frameBytes)", playback)
        self.assertIn("this._dropFrames = this._maxStartFrames + 1", playback)
        self.assertIn("if (underrunThisQuantum) this._started = false", playback)
        self.assertIn('pcmFormat === "s24le_in_s32") return view.getInt32(offset, true) / 8388608', playback)
        self.assertIn("s * 0x800000 : s * 0x7fffff", capture)
        self.assertNotIn("0x7fffff00", capture)
        self.assertIn("await this.resumeSession(mediaInfo, deviceId", engine)
        self.assertNotIn("const previousAttach = this._sessionAttachPromise", engine)
        self.assertNotIn("if (previousAttach) await previousAttach.catch", engine)
        self.assertIn("if (this._sessionAttachKey !== attachKey) return", engine)
        setup = _method_body(engine, "async _setupAudioOrAbort")
        self.assertIn("await this._setupAudio(", setup)
        self.assertIn("{ ...(reply || {}), ...(negotiated || {}) }", setup)
        after_setup = setup.split("await this._setupAudio(", 1)[1]
        self.assertIn("this._sessionAttachKey !== attachKey", after_setup)
        self.assertNotIn('await this.close("superseded", true)', after_setup)
        self.assertIn('await this.close("switch", true, true)', engine)
        self.assertIn("const connectGeneration = ++this._connectGeneration", engine)
        self.assertIn("connectGeneration !== this._connectGeneration", engine)
        self.assertIn('if (!preserveAttach) this._sessionAttachKey = ""', engine)
        self.assertIn("if (this._sessionAttachPromise !== trackedPromise) return", engine)
        self.assertIn("const audioCleanup = this._cleanupAudio", engine)
        self.assertIn("await settleWithin(Promise.allSettled([", engine)
        self.assertIn("previousCleanup || Promise.resolve()", engine)
        self.assertIn("MEDIA_CLEANUP_TIMEOUT_MS", engine)
        self.assertIn("await this._waitForMediaCleanup()", engine)
        self.assertIn("this._mediaCleanupPromise = currentCleanup", engine)
        self.assertIn("get mediaCleanupPending()", engine)
        self.assertIn("(this._video.active || this._video.callId)", engine)

    def test_browser_audio_applies_negotiated_direction_and_media_updates(self) -> None:
        engine = (
            ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-engine.js"
        ).read_text()
        media_model = (
            ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-media-model.js"
        ).read_text()
        self.assertIn('this._audioDirection = "sendrecv"', engine)
        self.assertIn("negotiated?.audio_direction", engine)
        self.assertIn("if (!this._canSendAudio()) return", engine)
        self.assertIn("!this._canReceiveAudio()", engine)
        self.assertIn("void this._reconcileAudioMedia(msg)", engine)
        self.assertIn("_desiredAudioPaths(audioMode, audioDirection)", engine)
        self.assertIn("desiredAudioPaths(audioMode, audioDirection)", engine)
        self.assertIn("Audio WebSocket negotiation timed out", engine)
        self.assertIn('"sendrecv", "sendonly", "recvonly", "inactive"', media_model)

    def test_dynamic_call_controls_expose_accessible_state(self) -> None:
        source = CARD.read_text()
        self.assertIn('statusRow.setAttribute("aria-live", "polite")', source)
        self.assertIn('err.setAttribute("role", "alert")', source)
        self.assertIn('prevBtn.setAttribute("aria-label", "Previous destination")', source)
        self.assertIn('nextBtn.setAttribute("aria-label", "Next destination")', source)
        self.assertIn('els.keypadBtn.setAttribute("aria-expanded"', source)
        self.assertIn('els.settingsBtn.setAttribute("aria-expanded"', source)

    def test_ring_group_mirror_replaces_group_with_answering_endpoint(self) -> None:
        source = CARD.read_text()
        handler = _method_body(source, "_onMirroredBridgeStateEvent")
        self.assertIn('state === "in_call" || state === "answering"', handler)
        self.assertIn(
            "data.connected_party || data.answered_by || data.peer_name",
            handler,
        )
        self.assertIn('this._mirroredConnectedPeer = ""', handler)
        self.assertIn(
            "(!this._isHaSoftphoneMode() && this._mirroredConnectedPeer)",
            source,
        )

    def test_softphone_native_contact_popup_keeps_readable_system_contrast(self) -> None:
        source = CARD.read_text()
        self.assertIn(".destination-select option {", source)
        self.assertIn("color: CanvasText;", source)
        self.assertIn("background-color: Canvas;", source)

    def test_editor_lists_mirrors_and_logical_softphones_and_cleans_retry_timer(self) -> None:
        editor = self.editor_source
        self.assertIn("const selectableDevices = this._devices.filter", editor)
        self.assertIn("this._isSoftphoneDevice(device)", editor)
        self.assertIn("newConfig.endpoint_id = selected.endpoint_id", editor)
        self.assertIn("Default Home Assistant softphone", editor)
        self.assertIn(
            'String(device.endpoint_id || "") !== DEFAULT_SOFTPHONE_ENDPOINT_ID',
            editor,
        )
        self.assertIn("const configuredMissingPhone = softphoneMode", editor)
        self.assertIn("Missing phone:", editor)
        self.assertIn("disconnectedCallback()", editor)
        self.assertIn("clearTimeout(this._devicesRetryTimer)", editor)
        self.assertIn(
            'if (!window.customCards.some(card => card.type === "voip-stack-card"))',
            self.source,
        )

    def test_main_card_loads_editor_with_same_cache_version(self) -> None:
        self.assertIn(
            'import(`./voip-stack-card-editor.js?v=${encodeURIComponent(VOIP_STACK_MODULE_VERSION)}`)',
            self.source,
        )

    def test_softphone_media_ownership_survives_card_recreation_in_same_tab(self) -> None:
        engine = (ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-engine.js").read_text()
        self.assertIn('const SOFTPHONE_MEDIA_SESSION_KEY = "voip_stack_owned_softphone_call"', engine)
        self.assertIn('const SOFTPHONE_MEDIA_SESSIONS_KEY = "voip_stack_owned_softphone_calls"', engine)
        self.assertIn('const MEDIA_CLIENT_GLOBAL_KEY = "__voipStackMediaClientId"', engine)
        self.assertIn('const MEDIA_CLIENT_SESSION_KEY = "voip_stack_media_client_id"', engine)
        self.assertIn("globalThis[MEDIA_CLIENT_GLOBAL_KEY]", engine)
        self.assertIn("sessionStorage.getItem(MEDIA_CLIENT_SESSION_KEY)", engine)
        self.assertIn("sessionStorage.setItem(MEDIA_CLIENT_SESSION_KEY", engine)
        self.assertIn("backend pins every call to this value", engine)
        self.assertIn("client_id=${encodeURIComponent(this._mediaClientId)}", engine)
        self.assertIn("sessionStorage.getItem(SOFTPHONE_MEDIA_SESSION_KEY)", engine)
        self.assertIn("sessionStorage.setItem(SOFTPHONE_MEDIA_SESSION_KEY", engine)
        self.assertIn("sessionStorage.removeItem(SOFTPHONE_MEDIA_SESSION_KEY)", engine)
        self.assertIn("ownsSoftphoneSession(callId, endpointId", engine)
        self.assertIn("releaseSoftphoneSession(callId = \"\", endpointId", engine)
        self.assertIn("_cleanupAfterTerminalSession(snapshot)", self.source)
        state_loader = self.source.split("async _loadSoftphoneState()", 1)[1].split(
            "_cycleSoftphoneTarget(", 1
        )[0]
        self.assertIn(
            "this._ensureHaSoftphoneAudioPath(this._softphoneSnapshot || snapshot)",
            state_loader,
        )

    def test_sip_video_keeps_send_and_receive_paths_independent(self) -> None:
        video = (
            ROOT
            / "custom_components"
            / "voip_stack"
            / "frontend"
            / "voip-stack-video.js"
        ).read_text()
        engine = (ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-engine.js").read_text()
        self.assertIn("window.isSecureContext", video)
        self.assertIn("MAX_PENDING_DECODE_BYTES", video)
        self.assertIn("MAX_DECODE_QUEUE_FRAMES", video)
        self.assertIn("this._decoder.decodeQueueSize", video)
        self.assertIn("this._dropUntilKeyFrame", video)
        self.assertIn('type: "request_key_frame"', video)
        self.assertIn("this._canReceive = true", video)
        self.assertIn("this._canSend = true", video)
        self.assertIn("if (!usablePaths)", video)
        self.assertIn("partial media support", video)
        self.assertIn("async _cleanupSender()", video)
        self.assertIn("_cleanupReceiver()", video)
        self.assertIn("new Worker(this._codecWorkerUrl()", video)
        self.assertIn("requestAnimationFrame", video)
        self.assertIn("MEDIA_CLEANUP_TIMEOUT_MS", video)
        self.assertIn("this._generation", video)
        self.assertIn("SIP video session was superseded", video)
        self.assertIn("get videoVisible()", engine)
        self.assertIn("voipStackEngine.videoVisible", self.source)
        self.assertIn("ha-card.card.video-active > .button-container", self.source)
        self.assertIn("void this._ensureVideo(statePayload)", engine)
        self.assertIn("this._videoAttachGeneration", engine)
        self.assertIn("this._videoAttachPromise", engine)
        self.assertIn("this._videoAttachCallId === wantedCallId", engine)
        self.assertIn("video.callId === wantedCallId", engine)
        self.assertIn("import(`./voip-stack-video.js", engine)
        self.assertNotIn('from "./voip-stack-video.js"', engine)

    def test_sip_video_layout_is_bounded_and_responsive(self) -> None:
        self.assertIn(".card.video-active { overflow: hidden;", self.source)
        self.assertIn(
            ".card > :where(:not(.video-canvas):not(.native-camera):not(.video-shade))",
            self.source,
        )
        self.assertNotIn(
            ".card > :not(.video-canvas):not(.native-camera):not(.video-shade)",
            self.source,
        )
        self.assertIn("ha-card.card.video-active > .button-container", self.source)
        self.assertIn("bottom: 0;", self.source)
        self.assertIn("width: 100%;", self.source)
        self.assertIn(".video-active .voip-button.hangup {", self.source)
        self.assertIn("box-sizing: border-box;", self.source)
        self.assertIn("overflow: hidden;", self.source)
        self.assertIn(".video-active .hangup-copy {", self.source)
        self.assertIn("flex: 1 1 auto;", self.source)
        self.assertIn("min-width: 0;", self.source)
        self.assertIn(".video-active .hangup-peer {", self.source)
        self.assertIn("text-overflow: ellipsis;", self.source)
        self.assertIn("object-fit: contain", self.source)
        self.assertIn("max-width: 100%", self.source)
        self.assertNotIn("video-auto-height", self.source)
        self.assertNotIn("aspect-ratio: 16 / 9", self.source)
        self.assertIn(".video-active .hangup-stats:not([hidden]) {", self.source)
        self.assertIn("text-overflow: ellipsis;", self.source)
        self.assertNotIn(".video-active .stats.video-debug {", self.source)
        render = _method_body(self.source, "_render")
        self.assertIn("this.config?.show_extended_info", render)
        self.assertIn("els.hangupStats.hidden = !showVideoStats", render)

    def test_video_hangup_bar_tracks_the_real_call_phase(self) -> None:
        render = _method_body(self.source, "_render")
        self.assertIn("els.hangupState.textContent", render)
        self.assertIn('? "Ending"', render)
        self.assertIn('? "Calling"', render)
        self.assertIn('normalizedState === "remote_ringing"', render)
        self.assertIn('? "Ringing"', render)
        self.assertIn(': "In call"', render)
        self.assertIn(
            "answerBtn, declineBtn, hangupBtn, hangupState, hangupPeer, hangupStats, hangupDuration",
            self.source,
        )

    def test_video_negotiation_failure_is_extended_information_only(self) -> None:
        render = _method_body(self.source, "_render")
        marker = 'statusReason = `Video unavailable: ${videoFailureReason}`;'
        before_marker = render[: render.index(marker)]
        condition = before_marker[before_marker.rfind("if (") :]
        self.assertIn("this.config?.show_extended_info", condition)

    def test_unregistered_sip_accounts_are_not_callable_card_contacts(self) -> None:
        targets = _method_body(self.source, "_softphoneTargets")
        self.assertIn('metadata.endpoint_kind || ""', targets)
        self.assertIn('=== "sip_account"', targets)
        self.assertIn("metadata.registered !== true", targets)


if __name__ == "__main__":
    unittest.main()
