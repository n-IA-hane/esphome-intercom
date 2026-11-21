"""Canonical codec settings reach existing SIP ingress and outbound owners."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import Context

from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
from custom_components.voip_stack.sip_ingress import SipIngressPolicy
from custom_components.voip_stack.store import config_entry

from .test_automation_originate_ha import lab as lab

pytestmark = pytest.mark.ha


def configure(lab, account_audio=None):
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data,
        "audio_codecs": [{"codec": "PCMU", "enabled": True}, {"codec": "L16", "enabled": False}],
        "video_codecs": [],
    })
    data = {"endpoint_id": "codec-browser", "name": "Codec browser", "kind": "sip_account", "username": "codec"}
    if account_audio is not None:
        data["audio_codecs"] = account_audio
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type="phone",
        title=data["name"], data=data, unique_id="phone:codec-browser"))
    return entry


@pytest.mark.parametrize("account_audio,expected", [(None, ("PCMU",)), (["PCMA"], ("PCMA",))])
async def test_registered_ingress_inherits_global_and_account_codec_rows(lab, account_audio, expected):
    entry = configure(lab, account_audio)
    policy = SipIngressPolicy(lab.hass, {}, "127.0.0.1", None)
    initial = SimpleNamespace(received_via_trunk=False, identified_source_endpoint_id="codec-browser",
                              authenticated_username="codec")
    selected = policy.codec_policy_for_invite(initial)
    assert selected.audio == expected and selected.video == ()
    assert selected.audio_ordered and selected.video_ordered
    assert entry.data["audio_codecs"][1]["enabled"] is False


@pytest.mark.parametrize("account_audio,expected", [(None, "PCMU"), (["PCMA"], "PCMA")])
async def test_browser_outbound_sdp_uses_effective_account_codec_policy(lab, monkeypatch, account_audio, expected):
    from custom_components.voip_stack import softphone_originate
    from custom_components.voip_stack.browser_phone_projection import _ha_softphone_state
    from custom_components.voip_stack.roster import dump_roster_json

    configure(lab, account_audio)
    source = PhoneEndpoint("codec-browser", "Codec browser", "browser", device_id="codec-browser-device")
    lab.runtime.endpoints.register(source)
    lab.runtime.endpoints.update("sip:bedroom0", availability="available")
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    peer = lab.phones[0]

    def answer():
        offered = sdp.offered_pcm_formats(peer.last_request.body)
        selected = next((item for item in offered if item.encoding == expected), offered[0])
        body = sdp.build_answer_directional("127.0.0.1", "127.0.0.1", peer.rtp_socket.getsockname()[1],
            selected, selected, remote_sdp=peer.last_request.body).encode()
        peer.respond(peer.last_request, peer.remote, 200, body)
        peer.answered.set()

    monkeypatch.setattr(peer, "answer", answer)
    await lab.hass.services.async_call("voip_stack", "call", {
        "device_id": source.device_id, "destination": "bedroom0",
    }, blocking=True)
    async with asyncio.timeout(2):
        while _ha_softphone_state(lab.hass, source.endpoint_id)["state"] != "in_call":
            await asyncio.sleep(.01)
    current = _ha_softphone_state(lab.hass, source.endpoint_id)
    assert {item.encoding for item in sdp.offered_pcm_formats(peer.invites[-1].body)} == {expected}
    assert not sdp.offered_video_formats(peer.invites[-1].body)
    await lab.hass.services.async_call("voip_stack", "hangup", {
        "device_id": source.device_id, "call_id": current["call_id"],
    }, blocking=True)
    assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("audio,expected", [(None, ("PCMU",)), (["PCMA"], ("PCMA",))])
async def test_trunk_policy_inherits_global_media_without_mutating_storage(lab, audio, expected):
    from custom_components.voip_stack.codec_settings import effective_codec_policy
    from custom_components.voip_stack.config import normalized_trunk_config

    configure(lab)
    raw = {} if audio is None else {"trunk_audio_codecs": audio}
    line = normalized_trunk_config(raw)
    lab.runtime.trunk_configs["line"] = line
    selected = effective_codec_policy(lab.hass, trunk_id="line")
    assert selected.audio == expected and selected.video == ()
    assert selected.audio_ordered and selected.video_ordered
    assert "trunk_video_codecs" not in line


async def test_sip_target_leg_uses_destination_account_codec_policy(lab, monkeypatch):
    from .test_automation_originate_ha import dial

    entry = configure(lab)
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type="phone", title="Bedroom",
        data={"endpoint_id": "sip:bedroom0", "username": "bedroom0", "name": "Bedroom", "kind": "sip_account",
              "audio_codecs": ["PCMA"]}, unique_id="phone:sip:bedroom0"))
    peer = lab.phones[0]

    def answer():
        offered = sdp.offered_pcm_formats(peer.last_request.body)
        selected = next((item for item in offered if item.encoding == "PCMA"), offered[0])
        body = sdp.build_answer_directional("127.0.0.1", "127.0.0.1", peer.rtp_socket.getsockname()[1],
            selected, selected, remote_sdp=peer.last_request.body).encode()
        peer.respond(peer.last_request, peer.remote, 200, body)
        peer.answered.set()

    monkeypatch.setattr(peer, "answer", answer)
    context = Context()
    await dial(lab, context)
    assert {item.encoding for item in sdp.offered_pcm_formats(peer.invites[-1].body)} == {"PCMA"}
    await lab.hass.services.async_call("voip_stack", "hangup", {}, blocking=True, context=context)
    assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")
