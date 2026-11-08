"""Discover native ESP phones without a long public endpoint state."""

import pytest
from homeassistant.helpers import entity_registry as er

from .test_managed_phones_ha import (
    custom_integration_path as custom_integration_path,
    phone_lab as phone_lab,
    managed as managed,
    phone_record,
    bind_account,
)

pytestmark = pytest.mark.ha


async def test_state_presence_discovers_unknown_route_without_inventing_formats(phone_lab):
    from custom_components.voip_stack.device_resolver import VoipDeviceResolver
    from custom_components.voip_stack.peer_snapshot import device_is_phonebook_available
    from custom_components.voip_stack.endpoint_routing import sip_target_offer_common_audio
    from custom_components.voip_stack.peer import Peer

    lab = phone_lab
    device, = await VoipDeviceResolver(lab.hass).list_devices()
    assert device["device_id"] == lab.device.id
    assert device["name"] == "Ingresso"
    assert device["route_id"] == "phone_node"
    assert device["entities"]["voip_state"]
    assert "voip_endpoint" not in device["entities"]
    assert device["sip_port"] is None and device["sip_transport"] == ""
    assert device["audio_mode"] is None
    assert device["sip_video_codec"] is None
    assert device["tx_formats"] == device["rx_formats"] == []
    assert not device_is_phonebook_available(lab.hass, device)
    assert sip_target_offer_common_audio(Peer(name="Entrance", host="127.0.0.2",
        endpoint_kind="esphome", device=device), None)
    # A missing/invalid legacy sensor cannot suppress device identity.
    legacy = er.async_get(lab.hass).async_get_or_create(
        "sensor", "esphome", "84c7bb000001-voip_endpoint", config_entry=lab.native,
        device_id=lab.device.id)
    lab.hass.states.async_set(legacy.entity_id, "unavailable")
    state_id = device["entities"]["voip_state"]
    lab.hass.states.async_set(state_id, "unavailable")
    again, = await VoipDeviceResolver(lab.hass).list_devices()
    assert again["device_id"] == device["device_id"]
    assert not device_is_phonebook_available(lab.hass, again)


async def test_state_entity_must_belong_to_esphome_and_legacy_alone_is_not_discovery(phone_lab):
    from custom_components.voip_stack.device_resolver import VoipDeviceResolver

    lab = phone_lab
    registry = er.async_get(lab.hass)
    state = next(item for item in registry.entities.values() if "voip_state" in item.unique_id)
    registry.async_remove(state.entity_id)
    for platform, role in (("esphome", "voip_endpoint"), ("voip_stack", "voip_state")):
        registry.async_get_or_create("sensor", platform, "other-" + role,
            config_entry=lab.native, device_id=lab.device.id)
    assert await VoipDeviceResolver(lab.hass).list_devices() == []


@pytest.mark.parametrize("transport", ["udp", "tcp"])
@pytest.mark.parametrize("identity_key", ["device_id", "endpoint_id"])
async def test_direct_sip_uses_explicit_existing_contact_and_survives_entity_rename(phone_lab, transport, identity_key):
    from custom_components.voip_stack.device_resolver import VoipDeviceResolver
    from custom_components.voip_stack.peer_snapshot import device_is_phonebook_available
    from custom_components.voip_stack.roster import RosterEntry, merge_roster_overrides
    from custom_components.voip_stack.store import store_manual_roster_entries

    lab = phone_lab
    identity = lab.device.id if identity_key == "device_id" else f"esphome:{lab.device.id}"
    contact = RosterEntry("direct-phone", name="Direct phone",
        sip_uri=f"sip:phone_node@127.0.0.2:5077;transport={transport}",
        metadata={identity_key: identity})
    store_manual_roster_entries(lab.hass, [contact])
    device, = await VoipDeviceResolver(lab.hass).list_devices()
    assert (device["host"], device["sip_port"], device["sip_transport"]) == ("127.0.0.2", 5077, transport)
    assert device["audio_mode"] is None and device["sip_audio_tx_formats"] == []
    assert device_is_phonebook_available(lab.hass, device)
    registry = er.async_get(lab.hass)
    registry.async_update_entity(device["entities"]["voip_state"], new_entity_id="sensor.renamed_phone_state")
    renamed, = await VoipDeviceResolver(lab.hass).list_devices()
    assert renamed["entities"]["voip_state"] == "sensor.renamed_phone_state"
    assert renamed["route_id"] == "phone_node"
    discovered = RosterEntry("phone_node", name="Ingresso", metadata={identity_key: identity})
    merged = merge_roster_overrides([discovered], [contact])
    assert len(merged) == 1 and merged[0].sip_uri == contact.sip_uri


async def test_authenticated_contact_and_options_are_authoritative_without_endpoint(managed):
    from custom_components.voip_stack.core import sdp
    from custom_components.voip_stack.device_resolver import VoipDeviceResolver

    lab = managed
    await lab.controller.async_sync()
    phone = phone_record(lab)
    await bind_account(lab, phone)
    binding, = lab.registrar.registered_contacts(phone.username)
    body = ("v=0\r\no=- 1 1 IN IP4 127.0.0.2\r\ns=Native\r\nc=IN IP4 127.0.0.2\r\nt=0 0\r\n"
        "m=audio 0 RTP/AVP 96 97\r\na=rtpmap:96 L16/16000/1\r\na=rtpmap:97 L16/48000/1\r\n"
        "a=x-voip-stack-flow:96 send\r\na=x-voip-stack-flow:97 recv\r\n"
        "a=x-voip-stack-independent-ptime:1\r\na=x-voip-stack-audio-tx:L16/16000/1/10\r\n"
        "a=x-voip-stack-audio-rx:L16/48000/1/10\r\na=ptime:10\r\n"
        "m=video 0 RTP/AVP 26\r\na=rtcp:0 IN IP4 127.0.0.2\r\na=sendrecv\r\n")
    binding.capabilities = sdp.options_capabilities(body)
    device, = await VoipDeviceResolver(lab.hass).list_devices()
    assert device["endpoint_id"] == phone.endpoint_id
    assert device["registered"] and device["sip_capabilities_known"]
    assert (device["host"], device["sip_port"], device["sip_transport"]) == ("127.0.0.2", 41000, "udp")
    assert device["audio_mode"] == "full_duplex"
    assert device["sip_audio_tx_formats"] == ["L16/16000/1/10"]
    assert device["sip_audio_rx_formats"] == ["L16/48000/1/10"]
    assert device["sip_video_codec"] == "jpeg" and "video" in device["capabilities"]
    with pytest.raises(sdp.SdpError):
        sdp.offered_video_formats(body)  # RTCP zero is not an ordinary media endpoint.
    assert "voip_endpoint" not in device["entities"]


@pytest.mark.parametrize("direction,mode", [("sendonly", "mic_only"), ("recvonly", "speaker_only")])
def test_options_derives_one_way_role_without_inventing_packet_time(direction, mode):
    from custom_components.voip_stack.core import sdp

    body = ("v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\ns=Capabilities\r\n"
        "c=IN IP4 192.0.2.1\r\nt=0 0\r\nm=audio 0 RTP/AVP 96\r\n"
        f"a=rtpmap:96 L16/16000/1\r\na={direction}\r\n")
    caps = sdp.options_capabilities(body)
    assert caps["audio_mode"] == mode
    assert caps["sip_audio_tx_formats"] == caps["sip_audio_rx_formats"] == []


def test_ha_pcm_preferences_keep_requested_cadence_and_optional_twenty_ms():
    from custom_components.voip_stack.core.audio_format import HA_SIP_PCM_TX_FORMATS, HA_SIP_PCM_RX_FORMATS

    assert HA_SIP_PCM_TX_FORMATS[0].wire_token() == "48000:s16le:1:10"
    assert HA_SIP_PCM_RX_FORMATS[0].wire_token() == "16000:s16le:1:10"
    assert any(fmt.sample_rate == 16000 and fmt.frame_ms == 20 for fmt in HA_SIP_PCM_RX_FORMATS)


def test_unusable_optional_video_does_not_erase_audio_capabilities():
    from custom_components.voip_stack.core import sdp

    body = ("v=0\r\no=- 1 1 IN IP4 192.0.2.1\r\ns=Capabilities\r\n"
        "c=IN IP4 192.0.2.1\r\nt=0 0\r\nm=audio 0 RTP/AVP 96\r\n"
        "a=rtpmap:96 L16/16000/1\r\na=sendrecv\r\na=ptime:10\r\n"
        "m=video 0 RTP/AVP invalid\r\n")
    caps = sdp.options_capabilities(body)
    assert caps["audio_mode"] == "full_duplex"
    assert caps["sip_audio_tx_formats"] == caps["sip_audio_rx_formats"] == ["L16/16000/1/10"]
    assert "sip_video_codec" not in caps


async def test_card_directory_availability_requires_route_and_current_state(phone_lab):
    from custom_components.voip_stack.websocket_api import _get_voip_devices
    from custom_components.voip_stack.phone_endpoint import EndpointAvailability
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.store import store_manual_roster_entries

    lab = phone_lab
    device, = await _get_voip_devices(lab.hass)
    endpoint_id = device["endpoint_id"]
    assert lab.runtime.endpoints.get(endpoint_id).availability is EndpointAvailability.OFFLINE
    store_manual_roster_entries(lab.hass, [RosterEntry("direct-phone", name="Entrance",
        sip_uri="sip:phone_node@127.0.0.2:5077;transport=tcp",
        metadata={"device_id": lab.device.id})])
    routed, = await _get_voip_devices(lab.hass)
    assert routed["endpoint_id"] == endpoint_id
    assert lab.runtime.endpoints.get(endpoint_id).availability is EndpointAvailability.AVAILABLE
    state_id = routed["entities"]["voip_state"]
    lab.hass.states.async_set(state_id, "unavailable")
    offline, = await _get_voip_devices(lab.hass)
    assert offline["endpoint_id"] == endpoint_id
    assert lab.runtime.endpoints.get(endpoint_id).availability is EndpointAvailability.OFFLINE
    lab.hass.states.async_set(state_id, "idle")
    await _get_voip_devices(lab.hass)
    assert lab.runtime.endpoints.get(endpoint_id).availability is EndpointAvailability.AVAILABLE
