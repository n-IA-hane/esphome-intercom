"""The shipped SIP event owner admits office BLF load and backs off refusals."""

import asyncio
import time

import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.endpoint_registry import EndpointRegistry
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.runtime_data import VoipStackRuntime
from custom_components.voip_stack.sip_application import SipApplicationMethods
from custom_components.voip_stack.sip_registrar import SipAccount, SipRegistrar, SipRegistration

pytestmark = pytest.mark.ha


@pytest.fixture
async def office_blf(hass):
    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(hass)
    endpoints = EndpointRegistry()
    for index in range(30):
        user = f"target{index}"
        endpoints.register(PhoneEndpoint(endpoint_id=user, username=user, name=user, kind="sip_account"))
    calls = SipEndpointRuntime(allow_dark_sessions=True)
    calls.activate()
    calls.bind_endpoint_registry(endpoints)
    entry.runtime_data = VoipStackRuntime(transport_config={}, assist_config={}, trunk_config={},
        endpoints=endpoints, phones=None, sip=calls)
    accounts = [SipAccount(f"watcher{i}", f"Watcher {i}", "fixture-only") for i in range(20)]
    registrar = SipRegistrar(enabled=True, accounts=accounts, local_ip="127.0.0.1", local_sip_port=5060)
    for index, account in enumerate(accounts):
        registrar.registrations[account.username] = SipRegistration(username=account.username,
            contact_uri=f"sip:{account.username}@127.0.0.{index+2}:5060", source_host=f"127.0.0.{index+2}",
            source_port=5060, transport="UDP", expires_at=time.time()+300)
    methods = SipApplicationMethods(hass, registrar)
    async def sender(_request, _address, _result):
        return sip.SipMessage(status_code=200, reason="OK")
    async def admit(index, *, tag="", sequence=1, expires=60, owner=None):
        principal = index % 20 if owner is None else owner
        user, target = f"watcher{principal}", f"target{(index//20)%30}"
        request = sip.parse_message(sip.build_request("SUBSCRIBE", f"sip:{target}@127.0.0.1", [
            ("From", f"<sip:{user}@127.0.0.1>;tag=watcher"),
            ("To", f"<sip:{target}@127.0.0.1>" + (f";tag={tag}" if tag else "")),
            ("Call-ID", f"watch-{index}"), ("CSeq", f"{sequence} SUBSCRIBE"),
            ("Contact", f"<sip:{user}@127.0.0.{principal+2}:5060>"),
            ("Event", "dialog"), ("Expires", str(expires)), ("Accept", "application/dialog-info+xml")]))
        result = await methods.handle(request, (f"127.0.0.{principal+2}", 5060), "UDP", sender)
        if result.status == 200:
            result.follow_up_scheduler()
        return result
    try:
        yield methods, admit
    finally:
        await methods.stop()
        await calls.shutdown()


async def test_twenty_phones_with_thirty_watches_fit_the_shipped_limit(office_blf):
    methods, admit = office_blf
    for index in range(600):
        assert (await admit(index)).status == 200, index
        if index % 20 == 0:
            await asyncio.sleep(0)
    assert len(methods.subscriptions) == 600
    assert not methods.rejected_subscriptions


async def test_shared_capacity_backoff_refresh_release_and_recovery(office_blf):
    methods, admit = office_blf
    for index in range(2048):
        assert (await admit(index)).status == 200, index
        if index % 20 == 0:
            await asyncio.sleep(0)
    await asyncio.sleep(0)
    tasks = len(methods.tasks)
    for index, retry in enumerate((5, 10, 20, 40, 60, 60)):
        response = await admit(100000 + index * 20)
        assert response.status == 503
        assert dict(response.headers)["Retry-After"] == str(retry)
    assert len(methods.tasks) == tasks, "Rejections must not allocate one task each"
    # Another authenticated subscriber has an independent refusal history.
    response = await admit(100001)
    assert dict(response.headers)["Retry-After"] == "5"
    current = methods.subscriptions["watch-0:watcher:dialog:"]
    assert (await admit(0, tag=current.to_tag, sequence=2)).status == 200
    assert (await admit(0, tag=current.to_tag, sequence=3, expires=0)).status == 200
    async with asyncio.timeout(2):
        while methods.notifiers:
            await asyncio.sleep(0)
    assert (await admit(200000)).status == 200
    response = await admit(300000)
    assert response.status == 503 and dict(response.headers)["Retry-After"] == "5"


async def test_single_phone_limit_stays_132(office_blf):
    methods, admit = office_blf
    for index in range(132):
        assert (await admit(index, owner=0)).status == 200
    response = await admit(133, owner=0)
    assert response.status == 503 and dict(response.headers)["Retry-After"] == "5"
    assert len(methods.subscriptions) == 132
