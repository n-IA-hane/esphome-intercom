"""Measured P4 calls consume native account identities and full diagnostics."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tools.hil_measurement import MeasurementError
from tools.live_voip_qualification import HaWs
from tools.p4_hil_measurement import P4HilMeasurement


def native_adapter(*, contact_host="192.0.2.21", duplicate=False):
    subject = object.__new__(P4HilMeasurement)
    subject.args = SimpleNamespace(p4_host="192.0.2.21", p4_extension="430", local_ip="192.0.2.30",
                                   sip_host="192.0.2.10", ha_entry_id="native-entry")
    subject.peer_uri = "sip:430@192.0.2.10:5060"
    row = {"endpoint_id": "P4 account", "endpoint_type": "sip_account",
           "extension": "430", "device_id": "physical-adapter", "host": ""}
    contact = {"id": "P4 account", "extension": "430",
               "sip_uri": f"sip:esphome-ha@{contact_host}:5060;transport=udp",
               "metadata": {"endpoint_id": "P4 account", "device_id": "native-account",
                            "registered": True, "sip_contacts": [
                                {"uri": f"sip:esphome-ha@{contact_host}:5060;transport=udp"}]}}

    async def command(message):
        if message["type"] == "voip_stack/list_devices":
            return {"result": {"devices": [row, dict(row)] if duplicate else [row]}}
        assert message == {"type": "get_states"}
        return {"result": [{"entity_id": "sensor.voip_phonebook",
                            "attributes": {"roster_json": {"contacts": [contact]}}}]}

    subject.ws = SimpleNamespace(command=command)
    subject.measurement = SimpleNamespace(directory=lambda *_: None)
    return subject


async def test_native_sip_account_resolves_the_actual_registered_p4_contact():
    result = await native_adapter().directory("pre")
    assert result["endpoints"]["p4"] == {
        "host": "192.0.2.21", "device_id": "native-account",
        "destination": "sip:esphome-ha@192.0.2.21:5060;transport=udp"}


@pytest.mark.parametrize("options", [{"contact_host": "192.0.2.99"}, {"duplicate": True}])
async def test_native_account_directory_rejects_wrong_or_ambiguous_contacts(options):
    with pytest.raises(MeasurementError):
        await native_adapter(**options).directory("pre")


async def test_runtime_snapshot_uses_the_diagnostics_owner_and_selected_account():
    subject = native_adapter()
    subject.ws.softphone_state = AsyncMock(return_value={"state": "idle", "active_dialogs": 99})
    counts = {key: 0 for key in (
        "sessions", "legs", "pending_routes", "pending_invites", "preanswered", "softphone_media",
        "sip_clients", "client_watchers", "relays", "bridges", "endpoint_claims",
        "active_audio_sessions", "active_video_sessions", "audio_ws_owners", "video_ws_owners",
        "media_identity_locks", "allocated_rtp_ports", "forward_tasks", "call_deadlines",
        "runtime_tasks", "video_transcoders")}
    resources = {"resource_counts": counts, "call_scoped_quiescent": True}
    subject.ha = SimpleNamespace(diagnostics=AsyncMock(return_value={"data": {"runtime": {
        "signaling": {"active_dialogs": 0}, "resources": resources}}}))
    result = await subject.snapshot()
    assert result["state"] == "idle" and result["active_dialogs"] == 0
    assert result["runtime_resources"] == resources
    assert len(result["runtime_resources"]["resource_counts"]) == 21
    subject.ha.diagnostics.assert_awaited_once_with("native-entry")


async def test_ha_websocket_reads_the_explicit_native_account():
    ws = HaWs("http://127.0.0.1:8123", "fixture-only", device_id="native-account")
    ws.command = AsyncMock(return_value={"result": {"state": "idle"}})
    assert await ws.softphone_state() == {"state": "idle"}
    ws.command.assert_awaited_once_with({"type": "voip_stack/ha_softphone_state", "device_id": "native-account"})
