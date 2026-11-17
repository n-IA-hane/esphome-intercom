"""Paging authority comes from verified ingress, never a phonebook name."""

from types import SimpleNamespace
import asyncio

import pytest

from .test_queue_call_ha import lab as lab, trunks as trunks, queue_lab as queue_lab
from .test_sip_capabilities_ha import wait_until

pytestmark = pytest.mark.ha


async def prepare_paging(lab):
    from custom_components.voip_stack.paging import PagingRuntime
    from custom_components.voip_stack.paging_config import paging_group_entries
    from custom_components.voip_stack.trunk_runtime import async_stop_sip_trunk
    await async_stop_sip_trunk(lab.hass)
    lab.runtime.trunk_configs.clear()
    lab.runtime.trunk_config = {}
    lab.runtime.default_trunk_id = ""
    identity = "780e47a5b85c4563a3390db63be9fd3f"
    data = {"paging_group_id": identity, "name": "Paging", "extension": "402",
            "members": ["sip:bedroom0", "sip:bedroom1"], "answer_timeout": 1, "timeout": 10}
    entry = SimpleNamespace(subentries={identity: SimpleNamespace(subentry_type="paging", subentry_id=identity, data=data)})
    owner = PagingRuntime(lab.hass, entry)
    lab.calls.adopt_component("paging", owner, closer=owner.close)
    lab.runtime.paging = owner
    lab.roster.extend(paging_group_entries(entry))
    for phone in lab.phones:
        phone.direction = "recvonly"
    return owner


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("name,address", [("Source", "127.0.0.3"), ("Unknown", "127.0.0.3")])
async def test_unidentified_origin_never_acquires_named_phone_or_pages(queue_lab, name, address):
    lab = queue_lab
    owner = await prepare_paging(lab)
    claims = []
    remove = lab.runtime.endpoints.subscribe(lambda event: claims.append(event.endpoint.active_call_id)
        if event.endpoint.endpoint_id == "source" and event.endpoint.active_call_id else None)
    client, _audio = await lab.caller(name, address)
    try:
        result = await client.invite(target="402", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3, audio_direction="sendonly")
        if result == "ringing":
            await client.wait_for_final(timeout=3)
        assert client.last_sip_status_code == 403
        assert not claims
        assert not owner.jobs
        assert all(not phone.invites for phone in lab.phones)
    finally:
        remove()
        await client.terminate()
    await wait_until(lambda: not lab.calls.sessions)
    assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_verified_static_phone_can_page_without_changing_autoanswer(queue_lab):
    lab = queue_lab
    owner = await prepare_paging(lab)
    client, _audio = await lab.caller()
    result = await client.invite(target="402", remote_host="127.0.0.1",
        remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3, audio_direction="sendonly")
    if result == "ringing":
        result = await client.wait_for_final(timeout=3)
    assert result == "in_call"
    session = lab.calls.get_session(client.dialog_ids.call_id)
    assert session.metadata["source_endpoint_id"] == "source"
    assert all(len(phone.invites) == 1 and phone.invites[0].header("Answer-Mode") == "Auto" for phone in lab.phones)
    await client.terminate()
    await asyncio.wait_for(session.terminated.wait(), 3)
    assert not owner.jobs and not lab.calls.sessions


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_ordinary_unknown_call_does_not_claim_known_nominal_phone(queue_lab):
    lab = queue_lab
    claimed = []
    remove = lab.runtime.endpoints.subscribe(lambda event: claimed.append(event.endpoint.active_call_id)
        if event.endpoint.endpoint_id == "source" and event.endpoint.active_call_id else None)
    client, _audio = await lab.caller("Source", "127.0.0.3")
    try:
        result = await client.invite(target="bedroom0", remote_host="127.0.0.1",
            remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
        if result == "ringing":
            await client.wait_for_final(timeout=3)
        assert not claimed
    finally:
        remove()
        await client.terminate()
    await wait_until(lambda: not lab.calls.sessions)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("identified,trunk", [("", False), ("source", True)])
async def test_paging_entry_defensively_rejects_nominal_endpoint_and_trunk(queue_lab, identified, trunk):
    from custom_components.voip_stack.paging import route_paging_call
    lab = queue_lab
    result = await route_paging_call(SimpleNamespace(hass=lab.hass),
        SimpleNamespace(identified_source_endpoint_id=identified, received_via_trunk=trunk),
        None, lab.calls, lab.runtime.endpoints.get("source"))
    assert result.status == 403
    assert not lab.calls.sessions and not lab.runtime.endpoints.get("source").active_call_id


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_forwarded_live_paging_cannot_promote_nominal_source(queue_lab):
    from custom_components.voip_stack.paging import start_paging_application
    from homeassistant.exceptions import ServiceValidationError
    lab = queue_lab
    session = lab.calls.upsert("unverified-forward", owner="automation", state="in_call", source_endpoint_id="source")
    with pytest.raises(ServiceValidationError, match="verified internal"):
        start_paging_application(lab.hass, session,
            SimpleNamespace(identified_source_endpoint_id="", received_via_trunk=False),
            None, "127.0.0.1")
    await lab.calls.terminate_call_wait(session.call_id, reason="test_done")
    assert not any(phone.invites for phone in lab.phones)
