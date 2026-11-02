"""PBX applications are available without global opt-in or idle media workers."""
import asyncio
from types import SimpleNamespace

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.voip_stack import async_migrate_entry
from custom_components.voip_stack.parking_config import parking_settings, normalize_parking_config
from custom_components.voip_stack.mailbox import async_setup_mailboxes, mailbox_snapshot
from custom_components.voip_stack.store import config_entry
from custom_components.voip_stack.voicemail_config import require_voicemail_config
from .test_trunk_calls_ha import trunks as trunks, lab as lab
from .test_voicemail_ha import custom_integrations as custom_integrations

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("legacy", [None, False, True])
def test_parking_defaults_do_not_depend_on_old_global_switch(legacy):
    config = {} if legacy is None else {"enabled": legacy}
    result = normalize_parking_config(config)
    assert result.get("access") == "700"
    assert result["slots"] == [str(n) for n in range(701, 721)]
    assert "enabled" not in result


async def test_migration_preserves_resource_configuration_and_drops_only_global_flags(hass):
    entry = MockConfigEntry(domain="voip_stack", version=6, minor_version=3, data={
        "parking": {"enabled": False, "access": "800", "slots": ["801"], "hold_media": "media-source://media_source/local/hold.wav"},
        "voicemail": {"enabled": False, "tts_entity_id": "tts.existing", "language": "it"},
        "esphome_mailboxes": {"esp": {"enabled": True, "pin_hash": "existing", "max_seconds": 40}},
    })
    entry.add_to_hass(hass)
    assert await async_migrate_entry(hass, entry)
    assert entry.minor_version == 4
    assert entry.data["parking"] == {"access": "800", "slots": ["801"], "hold_media": "media-source://media_source/local/hold.wav"}
    assert entry.data["voicemail"] == {"tts_entity_id": "tts.existing", "language": "it"}
    assert entry.data["esphome_mailboxes"]["esp"]["enabled"]
    saved = dict(entry.data)
    assert await async_migrate_entry(hass, entry)
    assert dict(entry.data) == saved


async def test_installation_needs_neither_tts_nor_application_switches(hass, custom_integrations):
    hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    form = await hass.config_entries.flow.async_init("voip_stack", context={"source": "user"})
    values = form["data_schema"]({})
    assert "parking_enabled" not in values and "voicemail_enabled" not in values
    result = await hass.config_entries.flow.async_configure(form["flow_id"], values)
    assert result["type"] == "create_entry"
    assert not result["data"].get("voicemail", {}).get("tts_entity_id")


async def test_reconfigure_has_separate_resource_panels(lab, custom_integrations):
    lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    entry = config_entry(lab.hass)
    menu = await lab.hass.config_entries.flow.async_init("voip_stack", context={"source": "reconfigure", "entry_id": entry.entry_id})
    assert menu["type"] == "menu"
    assert set(menu["menu_options"]) == {"user", "parking", "voicemail", "phone_services"}
    form = await lab.hass.config_entries.flow.async_configure(menu["flow_id"], {"next_step_id": "parking"})
    assert form["step_id"] == "parking" and "enabled" not in form["data_schema"]({})


async def test_available_applications_do_not_open_an_unused_archive(lab, tmp_path):
    lab.hass.config.media_dirs = {"local": str(tmp_path)}
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={"voicemail": {"enabled": True, "tts_entity_id": "tts.example", "language": "en"}})
    await async_setup_mailboxes(lab.hass, entry)
    assert parking_settings(lab.hass)["enabled"]
    assert lab.calls.component("parking") is None
    assert lab.calls.component("media_archive") is None
    assert not (tmp_path / "voip_stack").exists()
    assert not any(t.get_name() == "parking-hold-audio" for t in asyncio.all_tasks())
    assert mailbox_snapshot(lab.hass, "unused")["available"]
    assert not mailbox_snapshot(lab.hass, "unused")["enabled"]


async def test_existing_route_wins_over_default_parking_namespace(lab):
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(
        subentry_type="contact", unique_id="contact:701", title="Existing phone", data={"id": "existing", "number": "701"}))
    settings = parking_settings(lab.hass)
    assert not settings["enabled"] and settings["unavailable_reason"] == "route_conflict"
    lab.hass.config_entries.async_remove_subentry(entry, next(iter(entry.subentries)))
    assert parking_settings(lab.hass)["enabled"]


async def test_missing_or_unavailable_tts_only_rejects_voice_operation(lab, monkeypatch):
    from custom_components.voip_stack import voicemail_config
    entry = config_entry(lab.hass)
    with pytest.raises(ServiceValidationError, match="Configure"):
        require_voicemail_config(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={"voicemail": {"tts_entity_id": "tts.missing", "language": "en"}})
    monkeypatch.setattr(voicemail_config, "voice_engine", lambda *_: None)
    with pytest.raises(ServiceValidationError, match="unavailable"):
        require_voicemail_config(lab.hass)
    monkeypatch.setattr(voicemail_config, "voice_engine", lambda *_: SimpleNamespace(available=True))
    assert require_voicemail_config(lab.hass)["tts_entity_id"] == "tts.missing"


async def test_conflicting_codes_create_and_clear_repairs(lab):
    from homeassistant.helpers import issue_registry as ir
    from custom_components.voip_stack.repairs import async_sync_pbx_configuration_issues
    entry = config_entry(lab.hass)
    for address, issue in (("701", "parking_configuration"), ("*97", "mailbox_route_conflict")):
        child = ConfigSubentry(subentry_type="contact", title=address, unique_id="conflict:"+address,
                              data={"id": "existing", "name": address})
        lab.hass.config_entries.async_add_subentry(entry, child)
        async_sync_pbx_configuration_issues(lab.hass)
        assert ir.async_get(lab.hass).async_get_issue("voip_stack", issue) is not None
        lab.hass.config_entries.async_remove_subentry(entry, child.subentry_id)
        async_sync_pbx_configuration_issues(lab.hass)
        assert ir.async_get(lab.hass).async_get_issue("voip_stack", issue) is None


async def test_parking_edit_preserves_current_call_and_transport(lab, custom_integrations, monkeypatch):
    from unittest.mock import AsyncMock
    from custom_components.voip_stack.config_entry_runtime import entry_runtime_signature, async_config_entry_updated
    from custom_components.voip_stack.parking import get_parking_lot
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    entry = config_entry(lab.hass)
    lab.runtime.entry_runtime_signature = entry_runtime_signature(entry)
    reload = AsyncMock()
    monkeypatch.setattr(lab.hass.config_entries, "async_reload", reload)
    lot = get_parking_lot(lab.hass)
    session = lab.calls.upsert("parked-config", state="in_call", owner="parking")
    parked = lot.reserve(session, slot="701")
    lab.hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    try:
        menu = await lab.hass.config_entries.flow.async_init("voip_stack", context={"source":"reconfigure", "entry_id":entry.entry_id})
        form = await lab.hass.config_entries.flow.async_configure(menu["flow_id"], {"next_step_id":"parking"})
        rejected = await lab.hass.config_entries.flow.async_configure(form["flow_id"], form["data_schema"]({"slots":"702"}))
        assert rejected["errors"] == {"base":"parking_slots_in_use"}
        accepted = await lab.hass.config_entries.flow.async_configure(form["flow_id"], form["data_schema"]({"slots":"701,702", "timeout":120}))
        assert accepted["type"] == "abort"
        await async_config_entry_updated(lab.hass, entry)
        reload.assert_not_called()
        assert lot.calls["701"] is parked and session.live
        assert lot.slots == ("701", "702")
    finally:
        await lab.calls.request_termination(session.call_id, TerminationIntent("test_complete"))


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_unconfigured_spoken_menu_returns_sip_failure_without_claims(trunks):
    import socket
    from custom_components.voip_stack.sip_client import SipCallClient
    from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol
    from custom_components.voip_stack.core.audio_format import AudioFormat
    lab = trunks
    audio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    audio.bind(("127.0.0.2", 0))
    client = SipCallClient(local_ip="127.0.0.2", local_name="Caller", local_uri_user="caller", local_sip_port=5060,
                           local_rtp_port=audio.getsockname()[1], supported_formats=[AudioFormat(16000, frame_ms=20)])
    client.transport, client.protocol = await lab.hass.loop.create_datagram_endpoint(
        lambda: SipDatagramQueueProtocol(client.queue), local_addr=("127.0.0.2", 0))
    client.local_sip_port = client.transport.get_extra_info("sockname")[1]
    try:
        result = await client.invite(target="*98", remote_host="127.0.0.1", remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        assert result != "in_call" and client.last_sip_status_code == 503
        assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
    finally:
        await client.close()
        audio.close()
