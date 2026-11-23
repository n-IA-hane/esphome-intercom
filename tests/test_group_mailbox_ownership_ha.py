"""A member mailbox cannot claim a ringing group or queue owned call."""

import asyncio
from dataclasses import asdict, replace

import pytest
from homeassistant.components import tts
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import callback

from custom_components.voip_stack.call_queues import QueuePolicy
from custom_components.voip_stack.group_config import normalize_group_data
from custom_components.voip_stack.phone_policy import PhonePolicy
from custom_components.voip_stack.roster import RosterEntry, dump_roster_json
from custom_components.voip_stack.store import config_entry
from custom_components.voip_stack.websocket_api import CALL_EVENT
from .test_sip_phone_transfers_ha import registered_phones as registered_phones, assert_peer_audio_tones
from .test_trunk_calls_ha import lab as lab, trunks as trunks
from .test_queue_call_ha import queue_lab as queue_lab
from .test_voicemail_ha import configured_tts_provider as configured_tts_provider
from .test_browser_logical_availability_ha import resources

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": ["transfer_b", "transfer_c"]}], indirect=True)
@pytest.mark.parametrize("kind", ["ring", "queue"])
async def test_member_noanswer_mailbox_does_not_steal_group_winner_media(
    registered_phones, queue_lab, tmp_path, monkeypatch, kind,
):
    lab = queue_lab
    entry = config_entry(lab.hass)
    lab.manager.entry = entry
    name = "Protection"
    queue_policy = QueuePolicy(max_wait=8, ring_timeout=3, no_agent_wait=1)
    group = normalize_group_data({"kind": kind, "name": name, "extension": "402",
        "members": ["transfer_b", "transfer_c"],
        "origin": "manual", "policy": asdict(queue_policy) if kind == "queue" else {}}, identity=lab.identity)
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(subentry_type="group",
        title=name, unique_id="group:"+lab.identity, data=group))
    lab.hass.config_entries.async_update_entry(entry, data={**entry.data, "voicemail": {
        "enabled": True, "tts_entity_id": "tts.test", "language": "en"}})
    lab.hass.config.media_dirs = {"local": str(tmp_path / "media")}
    member_policy = {"ringing_timeout": 1, "no_answer": {"kind": "voicemail", "target": "transfer_b"}}
    for member in ("transfer_b", "transfer_c"):
        membership = {"queues": [name]} if kind == "queue" else {"ring_group": name}
        data = {"name": member, "username": member, "kind": "sip_account", **membership}
        if member == "transfer_b":
            data.update(call_policy=member_policy, voicemail={"enabled": True, "max_seconds": 2})
        account = next(row for row in entry.subentries.values()
                       if row.subentry_type == "phone" and row.data.get("username") == member)
        lab.hass.config_entries.async_update_subentry(entry, account,
            data={**account.data, **data})
        lab.runtime.endpoints.update(member, **({"queues": (name,)} if kind == "queue" else {"ring_group": name}))
    lab.runtime.endpoints.update("transfer_b", policy=PhonePolicy.from_data(member_policy))
    if kind == "queue":
        lab.manager.entry = entry
        lab.manager.definitions[lab.identity] = replace(lab.manager.definitions[lab.identity],
            name=name, members=("transfer_b", "transfer_c"), extension="402")
        lab.manager.policies[lab.identity] = queue_policy
        lab.manager.agent_states[lab.identity] = {member: "available" for member in ("transfer_b", "transfer_c")}
        lab.roster[:] = [row for row in lab.roster if row.id != lab.contact.id]
        lab.roster.append(lab.manager.roster_entries()[0])
    else:
        lab.roster.append(RosterEntry(name, name=name, metadata={"group_type": "ring",
            "members": ["transfer_b", "transfer_c"]}))
    lab.runtime.directory.replace(lab.roster)
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    class Speech:
        def async_set_message(self, _text):
            pass
        async def async_stream_result(self):
            yield bytes(640)
    monkeypatch.setattr(tts, "async_create_stream", lambda *_args: Speech())
    recorded = []
    @callback
    def observe(event):
        if event.data.get("event_type") == "voicemail_recording":
            recorded.append(event.data)
    detach = lab.hass.bus.async_listen(CALL_EVENT, observe)
    caller, protected, winner = registered_phones
    try:
        await asyncio.to_thread(caller.command, "/dial " + name)
        await asyncio.to_thread(protected.wait_for, "Incoming call", 3)
        await asyncio.to_thread(winner.wait_for, "Incoming call", 3)
        session = next(iter(lab.calls.sessions.values()))
        # Cross the configured personal deadline while the group still owns
        # unanswered branches; its own three-second ring budget remains open.
        await asyncio.sleep(1.1)
        assert session.live and session.owner != "voicemail" and not recorded
        await asyncio.to_thread(winner.command, "/accept")
        async with asyncio.timeout(3):
            while session.state != "in_call" or session.metadata.get("dest_endpoint_id") != "transfer_c":
                await asyncio.sleep(.01)
        assert "Call established" not in protected.read()
        relay = lab.calls.resource_for(session.call_id, "relay")
        async with asyncio.timeout(3):
            while relay.left_tx_packets < 30 or relay.right_tx_packets < 30:
                await asyncio.sleep(.01)
        assert not recorded
        await asyncio.to_thread(caller.hangup)
        await asyncio.wait_for(session.terminated.wait(), 3)
        assert resources(lab)["call_scoped_quiescent"]
        assert not lab.manager.waiting and not lab.manager.reservations
        for peer in registered_phones:
            await asyncio.to_thread(peer.close)
        assert_peer_audio_tones(tmp_path, (("transfer_a", 650), ("transfer_c", 450)))
    finally:
        detach()
