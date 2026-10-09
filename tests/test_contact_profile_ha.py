"""Public contact profile selectors, persistence and legacy metadata behavior."""

from pathlib import Path
from unittest.mock import AsyncMock

from homeassistant.exceptions import ServiceValidationError
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

pytestmark = pytest.mark.ha


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    import custom_components

    previous = custom_components.__path__
    custom_components.__path__ = [*previous, str(Path(__file__).parents[1] / "custom_components")]
    yield
    custom_components.__path__ = previous


@pytest.fixture
def entry(hass, monkeypatch):
    from custom_components.voip_stack import phonebook_services

    monkeypatch.setattr(phonebook_services, "_runtime_route_mappings", lambda _: [])
    hass.config.components.update({"assist_pipeline", "ffmpeg", "http", "lovelace", "media_source", "network"})
    config = MockConfigEntry(domain="voip_stack", data={}, version=6)
    config.add_to_hass(hass)
    return config


async def create_contact(hass, entry, name="Dahua PCM", **fields):
    form = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "contact"), context={"source": "user"},
    )
    form = await hass.config_entries.subentries.async_configure(form["flow_id"], {"type": "contact"})
    return await hass.config_entries.subentries.async_configure(form["flow_id"], {
        "name": name, "sip_uri": "sip:8001@192.0.2.20", **fields,
    })


async def test_gui_create_edit_and_reset_profile_persists_in_one_contact(hass, entry):
    result = await create_contact(hass, entry, sip_profile="dahua", dahua_audio="pcm")
    assert result["type"] == "create_entry"
    child = next(iter(entry.subentries.values()))
    assert child.data["metadata"] == {"sip_profile": "dahua", "dahua_audio": "pcm"}
    for profile, audio, expected in (
        ("dahua", "symmetric", {"sip_profile": "dahua", "dahua_audio": "symmetric"}),
        ("dahua", "standard", {"sip_profile": "dahua", "dahua_audio": "standard"}),
        ("dahua", "auto", {"sip_profile": "dahua"}),
        ("auto", "auto", {}),
    ):
        form = await hass.config_entries.subentries.async_init(
            (entry.entry_id, "contact"),
            context={"source": "reconfigure", "subentry_id": child.subentry_id},
        )
        values = form["data_schema"]({})
        assert values["sip_profile"] == child.data["metadata"].get("sip_profile", "auto")
        result = await hass.config_entries.subentries.async_configure(form["flow_id"], {
            **values, "sip_profile": profile, "dahua_audio": audio,
        })
        assert result["type"] == "abort"
        child = entry.subentries[child.subentry_id]
        assert child.data["metadata"] == expected
        assert len(entry.subentries) == 1


async def test_gui_rejects_inconsistent_profile_with_actionable_error(hass, entry):
    result = await create_contact(hass, entry, sip_profile="auto", dahua_audio="pcm")
    assert result["type"] == "form"
    assert result["errors"] == {"base": "invalid_sip_profile"}
    assert not entry.subentries


async def test_add_contact_service_supports_two_profiles_for_same_uri(hass, entry):
    import voluptuous as vol

    from custom_components.voip_stack import phonebook_services, services
    from custom_components.voip_stack.contact_config import contact_dicts

    refresh = AsyncMock()
    await services.async_register_services(hass, phonebook_services.build_phonebook_service_handlers(refresh))
    for name, audio in (("Dahua standard", "standard"), ("Dahua PCM", "pcm")):
        await hass.services.async_call("voip_stack", "add_contact", {
            "name": name, "sip_uri": "sip:8001@192.0.2.20",
            "sip_profile": "dahua", "dahua_audio": audio,
        }, blocking=True)
    contacts = contact_dicts(entry)
    assert len(contacts) == 2
    assert {item["metadata"]["dahua_audio"] for item in contacts} == {"standard", "pcm"}
    assert all("registered" not in item["metadata"] and "user_agent" not in item["metadata"] for item in contacts)
    assert refresh.await_count == 2
    with pytest.raises(ServiceValidationError, match="Dahua SIP profile"):
        await hass.services.async_call("voip_stack", "add_contact", {
            "name": "Invalid", "sip_uri": "sip:8001@192.0.2.20", "dahua_audio": "pcm",
        }, blocking=True)
    assert len(contact_dicts(entry)) == 2
    for field in ("sip_profile", "dahua_audio"):
        with pytest.raises(vol.Invalid):
            await hass.services.async_call("voip_stack", "add_contact", {
                "name": "Invalid", "sip_uri": "sip:8001@192.0.2.20", field: "unsupported",
            }, blocking=True)
    assert len(contact_dicts(entry)) == 2


@pytest.mark.parametrize("fields", [
    {"sip_profile": "unknown"}, {"dahua_audio": "unknown"},
    {"dahua_audio": "pcm"}, {"type": "automation", "sip_profile": "dahua"},
])
def test_invalid_profile_data_is_rejected(fields):
    from custom_components.voip_stack.phonebook_services import ContactProfileError, contact_from_data

    with pytest.raises(ContactProfileError):
        contact_from_data({"name": "Test", **fields})


def test_legacy_dahua_contact_keeps_metadata_without_manufactured_audio_override():
    from custom_components.voip_stack.phonebook_services import contact_from_data

    metadata = {"sip_profile": "dahua", "sip_video_codec": "h264", "registered": True}
    contact = contact_from_data({"name": "Door", "metadata": metadata})
    assert contact.metadata == metadata
    assert "dahua_audio" not in contact.metadata
    assert "user_agent" not in contact.metadata


async def test_public_contact_profiles_survive_storage_and_drive_real_sip_offer(hass, entry, socket_enabled):
    """Persist public settings, resolve each destination and inspect wire SDP."""
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import Mock

    from homeassistant.config_entries import ConfigSubentry
    from custom_components.voip_stack import phonebook_services, services
    from custom_components.voip_stack.contact_config import contact_dicts
    from custom_components.voip_stack.core import sdp, sip
    from custom_components.voip_stack.endpoint_dialing import EndpointDialer, OutboundLegPolicy
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.endpoint_routing import EndpointRouteResolver
    from custom_components.voip_stack.roster import parse_roster_json
    from custom_components.voip_stack.router import RouteAction, resolve_ha_router
    from custom_components.voip_stack.runtime_data import VoipStackRuntime

    captured = []
    class Peer(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            request = sip.parse_message(raw)
            if request.method == "INVITE":
                captured.append(request.body)
                headers = [(key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
                headers.append(("To", request.header("To") + ";tag=profile-test"))
                self.transport.sendto(sip.build_response(486, "Busy Here", headers), address)

    transport, _ = await hass.loop.create_datagram_endpoint(Peer, local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]
    uri = f"sip:8001@127.0.0.1:{port}"
    entry.runtime_data = VoipStackRuntime(
        transport_config={"sip_port": 5099, "rtp_port": 40000},
        assist_config={}, trunk_config={}, endpoints=EndpointRegistry(), phones=Mock(),
    )
    await services.async_register_services(hass, phonebook_services.build_phonebook_service_handlers(AsyncMock()))
    for name, fields in (
        ("Door standard", {"sip_uri": uri, "dahua_audio": "standard"}),
        ("Door PCM", {"sip_uri": uri, "dahua_audio": "pcm"}),
        ("Door via trunk", {"number": "8001", "dahua_audio": "pcm"}),
    ):
        await hass.services.async_call("voip_stack", "add_contact", {
            "name": name, "sip_profile": "dahua", **fields,
        }, blocking=True)

    # Recreate native subentry records from serialized persisted data, rather
    # than passing the service request directly into the signaling code.
    restored = MockConfigEntry(domain="voip_stack", data={}, version=6)
    restored.add_to_hass(hass)
    for saved in json.loads(json.dumps(contact_dicts(entry))):
        hass.config_entries.async_add_subentry(restored, ConfigSubentry(
            data=saved, title=saved["name"], subentry_type="contact", unique_id=saved["id"],
        ))
    contacts = parse_roster_json(contact_dicts(restored))
    dialer = EndpointDialer(
        hass, "127.0.0.1", entry.runtime_data.transport_config,
        EndpointRouteResolver(hass, "127.0.0.1", 5099),
        lambda *_args, **_kwargs: "UDP", lambda *_args, **_kwargs: False,
    )
    try:
        for target, action, pcm in (
            ("Door standard", RouteAction.FORWARD, False),
            ("Door PCM", RouteAction.FORWARD, True),
            ("Door via trunk", RouteAction.TRUNK, True),
            ("9999", RouteAction.TRUNK, False),
        ):
            route = resolve_ha_router(target, contacts, trunk_ready=True)
            assert route.action is action
            attempt = dialer.prepare_outbound_leg(
                member=target, peers=[], roster_entries=contacts, local_name="Browser",
                local_rtp_port_index=0, uri_override=route.sip_uri or uri,
                roster_entry_override=route.entry,
                port_reservation=SimpleNamespace(ports=(40000, 40002)),
                policy=OutboundLegPolicy(force_common_audio=action is RouteAction.TRUNK, reuse_registered_flow=False),
            )
            assert attempt is not None
            try:
                await attempt.client.invite(target="8001", remote_host="127.0.0.1", remote_sip_port=port, timeout=2)
                offer = captured[-1]
                offered = sdp.offered_pcm_formats(offer, allow_dahua_pcm=True)
                assert any(item.encoding == "PCM" for item in offered) is pcm
                assert {"PCMU", "PCMA", "L16"} <= {item.encoding for item in offered}
                assert b"a=ptime:20\r\n" in offer
                assert attempt.client.peer_user_agent == ""
            finally:
                await attempt.client.close()
        assert len(captured) == 4
    finally:
        transport.close()
