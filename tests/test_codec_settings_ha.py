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
from .test_trunk_calls_ha import trunks as trunks
from .test_sip_phone_transfers_ha import registered_phones as registered_phones

pytestmark = pytest.mark.ha


def configure(lab, account_audio=None):
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data,
        "audio_codecs": [{"codec": "PCMU", "enabled": True}, {"codec": "L16", "enabled": False}],
        "video_codecs": [],
    })
    data = {"endpoint_id": "codec", "name": "Codec browser", "kind": "sip_account", "username": "codec"}
    if account_audio is not None:
        data["audio_codecs"] = account_audio
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type="phone",
        title=data["name"], data=data, unique_id="phone:codec"))
    return entry


@pytest.mark.parametrize("account_audio,expected", [(None, ("PCMU",)), (["PCMA"], ("PCMA",))])
async def test_registered_ingress_inherits_global_and_account_codec_rows(lab, account_audio, expected):
    entry = configure(lab, account_audio)
    policy = SipIngressPolicy(lab.hass, {}, "127.0.0.1", None)
    initial = SimpleNamespace(received_via_trunk=False, identified_source_endpoint_id="codec",
                              authenticated_username="codec")
    selected = policy.codec_policy_for_invite(initial)
    assert selected.audio == expected and selected.video == ()
    assert selected.audio_ordered and selected.video_ordered
    assert entry.data["audio_codecs"][1]["enabled"] is False


@pytest.mark.parametrize("account_audio,destination_audio,expected", [
    (None, None, "PCMU"), (["PCMA"], None, "PCMA"), (["PCMA"], ["L16"], "L16"),
])
async def test_browser_outbound_sdp_uses_effective_account_codec_policy(lab, monkeypatch, account_audio, destination_audio, expected):
    from custom_components.voip_stack import softphone_originate
    from custom_components.voip_stack.browser_phone_projection import _ha_softphone_state
    from custom_components.voip_stack.roster import dump_roster_json

    entry = configure(lab, account_audio)
    if destination_audio is not None:
        destination = next(row for row in entry.subentries.values() if row.data.get("username") == "bedroom0")
        lab.hass.config_entries.async_update_subentry(entry, destination,
            data={**destination.data, "audio_codecs": destination_audio})
    source = PhoneEndpoint("codec", "Codec browser", "browser", username="codec", device_id="codec-browser-device")
    lab.runtime.endpoints.register(source)
    lab.runtime.endpoints.update("bedroom0", availability="available")
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
    destination = next(row for row in entry.subentries.values() if row.data.get("username") == "bedroom0")
    lab.hass.config_entries.async_update_subentry(entry, destination,
        data={**destination.data, "audio_codecs": ["PCMA"]})
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


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"l16": ("transfer_b",)}], indirect=True)
async def test_registered_pcma_source_reaches_l16_destination_without_policy_intersection(trunks, registered_phones, tmp_path):
    import wave
    import numpy as np
    from .test_sip_phone_transfers_ha import assert_peer_audio_tones

    lab = trunks
    entry = config_entry(lab.hass)
    for identity, codec in (("transfer_a", "PCMA"), ("transfer_b", "L16")):
        account = next(row for row in entry.subentries.values() if row.data.get("username") == identity)
        lab.hass.config_entries.async_update_subentry(entry, account,
            data={**account.data, "audio_codecs": [codec]})
    caller, target, _other = registered_phones
    try:
        await asyncio.to_thread(caller.dial, "transfer_b")
        session, = lab.calls.sessions.values()
        source = lab.calls.artifact_for(session.call_id, "caller_invite")
        destination = lab.calls.sip_client_for(lab.calls.bridge_link_for(session.call_id))
        assert source.send_format.encoding == source.recv_format.encoding == "PCMA"
        assert destination.dialog.send_format.encoding == destination.dialog.recv_format.encoding == "L16"
        target_shape = (destination.dialog.send_format.sample_rate, destination.dialog.send_format.channels)
        relay = lab.calls.resource_for(session.call_id, "relay")
        async with asyncio.timeout(3):
            while min(relay.left_tx_packets, relay.right_tx_packets) < 30:
                await asyncio.sleep(.01)
    finally:
        await asyncio.to_thread(caller.hangup)
        async with asyncio.timeout(3):
            while lab.calls.sessions:
                await asyncio.sleep(.01)
    for peer in (caller, target):
        await asyncio.to_thread(peer.close)
    assert_peer_audio_tones(tmp_path, (("transfer_a", 550),))
    recordings = list((tmp_path / "transfer_b").glob("*-dec.wav"))
    assert recordings
    for path in recordings:
        with wave.open(str(path), "rb") as audio:
            assert audio.getsampwidth() == 2
            assert (audio.getframerate(), audio.getnchannels()) == target_shape
            samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").reshape(-1, audio.getnchannels())
        samples = samples[-target_shape[0] // 2:]
        assert len(samples) >= target_shape[0] // 4
        times = np.arange(len(samples)) / target_shape[0]
        for channel in samples.T:
            powers = {frequency: abs(np.dot(channel, np.exp(2j * np.pi * frequency * times)))
                      for frequency in (450, 550, 650)}
            assert max(powers, key=powers.get) == 450 and powers[450] > 10000
    assert not lab.runtime.rtp_port_pool.get("used")
