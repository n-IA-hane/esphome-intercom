"""One canonical account offers its saved HA card and independent SIP Contacts."""

import asyncio
import json

import pytest

from custom_components.voip_stack.sip_registrar import SipAccount
from tools.ha_softphone_matrix import BareSip
from .test_canonical_account_calls_ha import (
    canonical_browser as canonical_browser, logical_browser as logical_browser,
    registered_phones as registered_phones, trunks as trunks, lab as lab,
)
from .test_browser_logical_availability_ha import (
    assert_no_browser_media_owner, browser_snapshot, end_call, open_browser_ws, public_phone,
)

pytestmark = pytest.mark.ha


@pytest.fixture
async def shared_account(canonical_browser, registered_phones, tmp_path):
    """Reuse existing bareSIP configurations, with two real REGISTER Contacts."""
    runtime = canonical_browser
    data = runtime.browser_subentry.data
    username, password = data["username"], data["password"]
    registrar = runtime.calls.component("registrar")
    registrar.update_accounts([*registrar.accounts.values(), SipAccount(username, data["name"], password,
        extension=str(data.get("extension") or ""))])
    peers = []
    try:
        original = (tmp_path / "transfer_b" / "config").read_text()
        for index in range(2):
            config = tmp_path / f"shared-contact-{index}"
            config.mkdir(mode=0o700)
            (config / "config").write_text(original.replace(str(tmp_path / "transfer_b"), str(config)))
            accounts = config / "accounts"
            accounts.write_text(f'<sip:{username}@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}>'
                f';auth_user={username};auth_pass={password};regint=60;answermode=manual\n')
            accounts.chmod(0o600)
            peers.append(await asyncio.to_thread(BareSip, config))
        assert len(registrar.registered_contacts(username)) == 2
        assert runtime.runtime.ha_bindings.has_binding("casa")
        yield runtime, registered_phones[0], peers
    finally:
        for peer in peers:
            if peer.proc.poll() is None:
                await asyncio.to_thread(peer.command, "/hangupall")
            await asyncio.to_thread(peer.close)


async def wait_for(predicate):
    async with asyncio.timeout(4):
        while not predicate():
            await asyncio.sleep(.01)


async def assert_no_active_calls(peers):
    """Independent UA state, not the PBX projection, proves ringing stopped.

    bareSIP4.6 src/ua.c:ua_print_calls prints the actual ua->calls count.
    This command only observes; it never hangs up or changes the call.
    """
    for peer in peers:
        await asyncio.to_thread(peer.command, "/listcalls")
        await asyncio.to_thread(peer.wait_for, "Active calls (0)", 3)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("logical_browser", [{"ringing_timeout": 6}], indirect=True)
async def test_sip_winner_preserves_shared_account_claim_and_ha_is_observer_only(
    shared_account, hass_ws_client, record_property, tmp_path,
):
    runtime, caller, peers = shared_account
    observer = await open_browser_ws(runtime, hass_ws_client)
    try:
        target = f'sip:221@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}'
        await asyncio.to_thread(caller.command, f"/dial {target}")
        for peer in peers:
            await asyncio.to_thread(peer.wait_for, "Incoming call", 3)
        async with asyncio.timeout(4):
            while (initial := await public_phone(runtime))["state"] != "ringing":
                await asyncio.sleep(.01)
        call_id = initial["calls"][0]["call_id"]
        assert_no_browser_media_owner(runtime)
        await asyncio.to_thread(peers[0].command, "/accept")
        await asyncio.to_thread(caller.wait_for, "Call established", 3)
        await asyncio.to_thread(peers[0].wait_for, "Call established", 3)
        # Both independent UAs have accepted the actual200/ACK exchange. Do not
        # wait for the very account projection this regression must verify.
        session = runtime.calls.get_session(call_id)
        assert session is not None
        phone = await public_phone(runtime)
        snapshot = await browser_snapshot(observer)
        evidence = {"call_id": session.call_id, "owner": session.owner, "session_state": session.state,
            "account_claim_retained": "casa" in session.endpoint_claims,
            "account_state": phone["state"], "browser_snapshot_state": snapshot.get("state"),
            "actual_contacts": len(runtime.calls.component("registrar").registered_contacts(runtime.browser_subentry.data["username"])),
            "callee_console_answered": "Call established" in peers[0].read(),
            "caller_console_answered_once": caller.read().count("Call established") == 1,
            "can_attach_media": snapshot.get("can_attach_media"),
            "ha_legs": [{"id": leg.leg_id, "state": leg.state, "phase": leg.phase.value}
                        for leg in session.legs.values() if leg.role == "ha_softphone"]}
        output = tmp_path
        output.mkdir(parents=True, exist_ok=True)
        (output / "shared-account-sip-winner.json").write_text(json.dumps(evidence, indent=2) + "\n")
        for key, value in evidence.items():
            record_property(key, value)
        assert "casa" in session.endpoint_claims, evidence
        assert phone["state"] == "in_call", evidence
        assert snapshot["state"] == "in_call", evidence
        assert snapshot["can_attach_media"] is False, evidence
        ha_legs = [leg for leg in session.legs.values() if leg.role == "ha_softphone"]
        assert len(ha_legs) == 1 and ha_legs[0].phase.value == "closed", evidence
        assert runtime.calls.resource_for(call_id, "softphone_media") is None
        assert runtime.calls.resource_for(call_id, "softphone_media_endpoint:casa") is None
        assert_no_browser_media_owner(runtime)
        assert caller.read().count("Call established") == 1
        await assert_no_active_calls(peers[1:])
    finally:
        await observer.close()
        await end_call(runtime, caller)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("logical_browser", [{"ringing_timeout": 6}], indirect=True)
async def test_ha_winner_cancels_both_shared_sip_contacts(shared_account, hass_ws_client):
    runtime, caller, peers = shared_account
    observer = await open_browser_ws(runtime, hass_ws_client)
    try:
        target = f'sip:221@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}'
        await asyncio.to_thread(caller.command, f"/dial {target}")
        for peer in peers:
            await asyncio.to_thread(peer.wait_for, "Incoming call", 3)
        async with asyncio.timeout(4):
            while (initial := await public_phone(runtime))["state"] != "ringing":
                await asyncio.sleep(.01)
        call_id, generation = initial["calls"][0]["call_id"], initial["calls"][0]["generation"]
        await runtime.hass.services.async_call("voip_stack", "answer", {"device_id": "browser-casa",
            "call_id": call_id, "media_client_id": "shared-ha-winner"}, blocking=True)
        await asyncio.to_thread(caller.wait_for, "Call established", 3)
        session = runtime.calls.get_session(call_id)
        assert session is not None and session.generation == generation and "casa" in session.endpoint_claims
        snapshot = await browser_snapshot(observer)
        assert snapshot["state"] == "in_call" and snapshot["can_attach_media"] is True
        assert caller.read().count("Call established") == 1
        assert all("Call established" not in peer.read() for peer in peers)
        await assert_no_active_calls(peers)
        await runtime.hass.services.async_call("voip_stack", "hangup", {
            "device_id": "browser-casa", "call_id": call_id, "expected_generation": generation}, blocking=True)
    finally:
        await observer.close()
        await end_call(runtime, caller)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("logical_browser", [{"ringing_timeout": 6}], indirect=True)
async def test_source_cancel_stops_both_shared_contacts_and_releases_all_owners(shared_account):
    runtime, caller, peers = shared_account
    target = f'sip:221@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}'
    await asyncio.to_thread(caller.command, f"/dial {target}")
    try:
        for peer in peers:
            await asyncio.to_thread(peer.wait_for, "Incoming call", 3)
        assert_no_browser_media_owner(runtime)
        await end_call(runtime, caller)
        await assert_no_active_calls(peers)
        assert all("Call established" not in peer.read() for peer in peers)
        assert not runtime.calls.endpoint_claims_snapshot()
        assert not runtime.calls.relays_snapshot()
    finally:
        await end_call(runtime, caller)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("logical_browser", [{"ringing_timeout": 6}], indirect=True)
async def test_ha_winner_acknowledges_and_retires_late_sip_200_without_losing_claim(
    canonical_browser, registered_phones, hass_ws_client, tmp_path,
):
    """The existing independent peer sends200INVITE only upon receivingCANCEL."""
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.core.sip_auth import build_digest_authorization

    runtime, caller = canonical_browser, registered_phones[0]
    peer = runtime.phones[0]
    peer.auto_answer, peer.answer_on_cancel = False, True
    account = runtime.browser_subentry.data
    registrar = runtime.calls.component("registrar")
    registrar.update_accounts([*registrar.accounts.values(), SipAccount(account["username"], account["name"], account["password"],
        extension=str(account.get("extension") or ""))])
    host, port = peer.transport.get_extra_info("sockname")[:2]
    listener = ("127.0.0.1", runtime.runtime.transport_config["sip_port"])
    uri, registration_id = f"sip:127.0.0.1:{listener[1]}", "shared-late-register"

    async def register(sequence, authorization=""):
        headers = [("Via", f"SIP/2.0/UDP {host}:{port};branch=z9hG4bK-shared-late-{sequence};rport"),
            ("From", f'<sip:{account["username"]}@127.0.0.1>;tag=late-register'),
            ("To", f'<sip:{account["username"]}@127.0.0.1>'), ("Call-ID", registration_id),
            ("CSeq", f"{sequence} REGISTER"), ("Expires", "120"),
            ("Contact", f'<sip:{account["username"]}@{host}:{port}>')]
        if authorization:
            headers.append(("Authorization", authorization))
        peer.transport.sendto(sip.build_request("REGISTER", uri, headers), listener)
        await wait_for(lambda: any(not item.is_request and item.header("Call-ID") == registration_id
                                  and item.header("CSeq") == f"{sequence} REGISTER" for item in peer.requests))
        return next(item for item in peer.requests if not item.is_request and item.header("Call-ID") == registration_id
                    and item.header("CSeq") == f"{sequence} REGISTER")
    challenge = await register(1)
    assert challenge.status_code == 401
    authorization = build_digest_authorization(challenge_header=challenge.header("WWW-Authenticate"),
        username=account["username"], password=account["password"], method="REGISTER", uri=uri)
    assert (await register(2, authorization)).status_code == 200
    assert len(registrar.registered_contacts(account["username"])) == 1
    observer = await open_browser_ws(runtime, hass_ws_client)
    try:
        await asyncio.to_thread(caller.command, f'/dial sip:221@127.0.0.1:{listener[1]}')
        await asyncio.wait_for(peer.invited.wait(), 3)
        async with asyncio.timeout(4):
            while (initial := await public_phone(runtime))["state"] != "ringing":
                await asyncio.sleep(.01)
        call_id = initial["calls"][0]["call_id"]
        await runtime.hass.services.async_call("voip_stack", "answer", {"device_id": "browser-casa",
            "call_id": call_id, "media_client_id": "shared-late-ha-winner"}, blocking=True)
        await asyncio.wait_for(peer.bye.wait(), 3)
        await asyncio.to_thread(caller.wait_for, "Call established", 3)
        methods = [item.method for item in peer.requests if item.is_request
                   and item.header("Call-ID") == peer.last_request.header("Call-ID")]
        assert methods.index("CANCEL") < methods.index("ACK") < methods.index("BYE"), methods
        assert peer.answered.is_set()
        session = runtime.calls.get_session(call_id)
        assert session is not None and "casa" in session.endpoint_claims
        state = await browser_snapshot(observer)
        assert state["state"] == "in_call" and state["can_attach_media"] is True
        assert caller.read().count("Call established") == 1
        output = tmp_path
        (output / "shared-account-late200.json").write_text(json.dumps({"call_id": call_id,
            "methods": methods, "late_200_on_cancel": peer.answered.is_set(), "account_claim_retained": True,
            "ha_can_attach": state["can_attach_media"]}, indent=2) + "\n")
        await runtime.hass.services.async_call("voip_stack", "hangup", {"device_id": "browser-casa",
            "call_id": call_id}, blocking=True)
    finally:
        await observer.close()
        await end_call(runtime, caller)
