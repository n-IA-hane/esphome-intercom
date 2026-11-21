"""One canonical account offers its saved HA card and independent SIP Contacts."""

import asyncio
from contextlib import asynccontextmanager
import json
import secrets
import shutil
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.voip_stack.core import sip
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
    sip_ports = set()
    try:
        original = (tmp_path / "transfer_b" / "config").read_text()
        for index in range(2):
            config = tmp_path / f"shared-contact-{index}"
            config.mkdir(mode=0o700)
            # Each copied UA owns a distinct UDP/TCP listener. The source
            # transfer_b still owns the explicit sip_listen in its config.
            for _ in range(128):
                sip_port = 31000 + secrets.randbelow(1000)
                try:
                    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp, socket.socket() as tcp:
                        udp.bind(("127.0.0.1", sip_port))
                        tcp.bind(("127.0.0.1", sip_port))
                except OSError:
                    continue
                break
            else:
                raise RuntimeError("No free UDP/TCP port in the registered phone range")
            content = original.replace(str(tmp_path / "transfer_b"), str(config))
            content = "\n".join(line for line in content.splitlines()
                                if line.split(maxsplit=1)[:1] != ["sip_listen"])
            (config / "config").write_text(content + f"\nsip_listen 127.0.0.1:{sip_port}\n")
            accounts = config / "accounts"
            accounts.write_text(f'<sip:{username}@127.0.0.1:{runtime.runtime.transport_config["sip_port"]}>'
                f';auth_user={username};auth_pass={password};regint=60;answermode=manual\n')
            accounts.chmod(0o600)
            peers.append(await asyncio.to_thread(BareSip, config))
            sip_ports.add(sip_port)
        assert len(registrar.registered_contacts(username)) == 2
        assert len(sip_ports) == 2
        assert {sip.parse_sip_uri(contact.contact_uri).port
                for contact in registrar.registered_contacts(username)} == sip_ports
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


@asynccontextmanager
async def no_digits_trunk(lab, monkeypatch, unused_udp_port_factory, tmp_path, fallback, *, calls=1):
    """Originate from an independent provider at the trusted trunk IP."""
    from custom_components.voip_stack import trunk_dtmf
    from custom_components.voip_stack.config import normalized_trunk_config
    from custom_components.voip_stack.sip_trunk import SipTrunkClient
    from custom_components.voip_stack.trunk_runtime import trunk_client_config

    executable = shutil.which("sipp")
    if executable is None:
        pytest.skip("SIPp is required for the independent trunk caller")
    monkeypatch.setattr(trunk_dtmf, "_FIRST_DIGIT_TIMEOUT", 0.1)
    lab.runtime.preferred_phone_device_id = "browser-casa"
    port = lab.runtime.transport_config["sip_port"]
    config = normalized_trunk_config({
        "trunk_enabled": True, "trunk_register": False, "trunk_server": "127.0.0.2",
        "trunk_allowed_ips": ["127.0.0.2/32"], "trunk_inbound_mode": "dtmf",
        "trunk_dtmf_enabled": True, "trunk_dtmf_timeout_ms": 100,
        "trunk_inbound_default_target": fallback,
    })
    provider = SipTrunkClient(config=trunk_client_config(config), local_ip="127.0.0.1",
                              local_sip_port=port, trunk_id="office")
    lab.runtime.trunk_configs["office"] = config
    lab.calls.adopt_component("trunk:office", provider, closer=provider.stop)
    caller = await asyncio.create_subprocess_exec(
        executable, "-sn", "uac", f"127.0.0.1:{port}", "-s", "450", "-i", "127.0.0.2",
        "-p", str(unused_udp_port_factory()), "-mi", "127.0.0.2", "-mp", str(unused_udp_port_factory()),
        "-d", "1200", "-m", str(calls), "-l", "1", "-nostdin", "-timeout", "12s", "-timeout_error",
        "-trace_msg", "-message_file", str(tmp_path / "trunk-sip.log"), cwd=tmp_path, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        yield caller
    finally:
        if caller.returncode is None:
            caller.terminate()
            await caller.wait()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("logical_browser", [{"ringing_timeout": 8, "extension": "998"}], indirect=True)
@pytest.mark.parametrize(("answering_client", "forward_when"), [
    ("browser", None), ("handset", None), ("handset", "always"), ("handset", "unavailable"),
])
async def test_dtmf_trunk_fallback_offers_each_client_after_account_policy(
    shared_account, hass_ws_client, monkeypatch, unused_udp_port_factory, tmp_path, answering_client, forward_when,
):
    """The site-named fallback can be the mixed account or an offline phone forwarding to it."""
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
    from custom_components.voip_stack.phone_policy import PhonePolicy
    from .test_browser_logical_availability_ha import resources

    lab, _, handsets = shared_account
    fallback = "Casa"
    if forward_when:
        fallback = "Unregistered office line"
        lab.runtime.endpoints.register(PhoneEndpoint(
            "away", fallback, EndpointKind.SIP_ACCOUNT, username="away",
            policy=PhonePolicy.from_data({forward_when: {"kind": "contact", "target": "casa"}}),
        ))
    monkeypatch.setattr(lab.hass.config, "location_name", fallback)
    observer = await open_browser_ws(lab, hass_ws_client)
    try:
        async with no_digits_trunk(lab, monkeypatch, unused_udp_port_factory, tmp_path, fallback) as caller:
            for handset in handsets:
                await asyncio.to_thread(handset.wait_for, "Incoming call", 4)
            ringing = await browser_snapshot(observer)
            assert ringing["state"] == "ringing" and ringing["can_answer"]
            call_id = ringing["call_id"]
            if answering_client == "browser":
                await lab.hass.services.async_call("voip_stack", "answer", {
                    "device_id": "browser-casa", "call_id": call_id, "media_client_id": "office-card",
                }, blocking=True)
            else:
                await asyncio.to_thread(handsets[0].command, "/accept")
                await asyncio.to_thread(handsets[0].wait_for, "Call established", 4)
            await wait_for(lambda: lab.calls.get_session(call_id).state == "in_call")
            current = await browser_snapshot(observer)
            assert current["call_id"] == call_id
            assert current["can_attach_media"] is (answering_client == "browser")
            assert await asyncio.wait_for(caller.wait(), 5) == 0
            await wait_for(lambda: not lab.calls.sessions)
            await assert_no_active_calls(handsets)
            assert resources(lab)["call_scoped_quiescent"]
    finally:
        await observer.close()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize(("fallback", "forbidden"), [("HA", False), ("Disabled site phone", True)])
async def test_trunk_compatibility_alias_cannot_override_a_disabled_account(
    shared_account, hass_ws_client, monkeypatch, unused_udp_port_factory, tmp_path, fallback, forbidden,
):
    from custom_components.voip_stack.phone_endpoint import EndpointAvailability, EndpointKind, PhoneEndpoint
    from .test_browser_logical_availability_ha import resources

    lab, _, handsets = shared_account
    if forbidden:
        monkeypatch.setattr(lab.hass.config, "location_name", fallback)
        lab.runtime.endpoints.register(PhoneEndpoint(
            "blocked", fallback, EndpointKind.SIP_ACCOUNT, username="blocked",
            availability=EndpointAvailability.UNAVAILABLE,
        ))
    observer = await open_browser_ws(lab, hass_ws_client)
    try:
        async with no_digits_trunk(lab, monkeypatch, unused_udp_port_factory, tmp_path, fallback) as caller:
            if forbidden:
                # The stock UAC offers no telephone-event. HA answers before
                # collecting SIP INFO, so rejection must end that dialog.
                assert await asyncio.wait_for(caller.wait(), 4) != 0
                wire = (tmp_path / "trunk-sip.log").read_text()
                assert "SIP/2.0 200 OK" in wire and "BYE sip:sipp@127.0.0.2" in wire
                assert "SIP/2.0 180" not in wire
                assert (await browser_snapshot(observer))["state"] == "idle"
            else:
                async with asyncio.timeout(4):
                    while (ringing := await browser_snapshot(observer))["state"] != "ringing":
                        await asyncio.sleep(.01)
                await lab.hass.services.async_call("voip_stack", "answer", {
                    "device_id": "browser-casa", "call_id": ringing["call_id"], "media_client_id": "legacy-card",
                }, blocking=True)
                assert await asyncio.wait_for(caller.wait(), 5) == 0
            assert all("Incoming call" not in handset.read() for handset in handsets)
            await wait_for(lambda: not lab.calls.sessions)
            assert resources(lab)["call_scoped_quiescent"]
    finally:
        await observer.close()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("logical_browser", [{"ringing_timeout": 6}], indirect=True)
async def test_sip_winner_preserves_shared_account_claim_and_ha_is_observer_only(
    shared_account, hass_ws_client, record_property, tmp_path, hass_read_only_user,
):
    runtime, caller, peers = shared_account
    runtime.runtime.endpoints.update("casa", name="Home reception")
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
        await wait_for(lambda: "connected_party" in session.metadata)
        assert session.metadata["connected_party"] == "Home reception"
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
        assert snapshot["can_control"] is True, evidence
        from custom_components.voip_stack.websocket_api import _client_phone_state
        readonly = _client_phone_state(runtime.hass, SimpleNamespace(user=hass_read_only_user), "casa")
        assert readonly["state"] == "in_call" and readonly["can_control"] is False
        model = Path(__file__).resolve().parents[1] / "custom_components/voip_stack/frontend/voip-stack-card-model.js"
        subprocess.run(["node", "--input-type=module", "-e",
            f"import {{phoneCallPresentation}} from {model.as_uri()!r};let input='';for await(const chunk of process.stdin)input+=chunk;"
            "for(const snapshot of JSON.parse(input))if(phoneCallPresentation(snapshot).state!=='in_call')throw Error('SIP observer lost its call');"],
            input=json.dumps([snapshot, readonly]), text=True, check=True)
        (tmp_path / "account-observer-payloads.json").write_text(json.dumps([snapshot, readonly], indent=2))
        ha_legs = [leg for leg in session.legs.values() if leg.role == "ha_softphone"]
        assert len(ha_legs) == 1 and ha_legs[0].phase.value == "closed", evidence
        assert runtime.calls.resource_for(call_id, "softphone_media") is None
        assert runtime.calls.resource_for(call_id, "softphone_media_endpoint:casa") is None
        assert_no_browser_media_owner(runtime)
        assert caller.read().count("Call established") == 1
        await assert_no_active_calls(peers[1:])
        relay = runtime.calls.resource_for(call_id, "relay")
        async with asyncio.timeout(3):
            while min(relay.left_tx_packets, relay.right_tx_packets) < 30:
                await asyncio.sleep(.01)
    finally:
        await observer.close()
        await end_call(runtime, caller)
    # Finalize the independent UA's PCM recorders after the real BYE barrier.
    for peer in (caller, peers[0]):
        await asyncio.to_thread(peer.close)
    from .test_sip_phone_transfers_ha import assert_peer_audio_tones
    assert_peer_audio_tones(tmp_path, (("transfer_a", 550), ("shared-contact-0", 450)))


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_b"}], indirect=True)
async def test_different_account_winner_removes_the_losing_account_projection(
    canonical_browser, registered_phones, hass_ws_client, tmp_path,
):
    from custom_components.voip_stack.dialog_info import snapshot
    from custom_components.voip_stack.phone_state import phone_states
    from custom_components.voip_stack.store import config_entry
    from dataclasses import replace

    lab = canonical_browser
    entry = config_entry(lab.hass)
    record = lab.browser_subentry
    lab.hass.config_entries.async_update_subentry(entry, record, data={**record.data, "ring_group": "UxGroup"})
    registrar = lab.calls.component("registrar")
    registrar.update_accounts([replace(account, ring_group="UxGroup") if account.username == "transfer_b" else account
                               for account in registrar.accounts.values()])
    for identity in ("casa", "transfer_b"):
        lab.runtime.endpoints.update(identity, ring_group="UxGroup")
    observer = await open_browser_ws(lab, hass_ws_client)
    caller, winner, _other = registered_phones
    try:
        await asyncio.to_thread(caller.command, "/dial UxGroup")
        await asyncio.to_thread(winner.wait_for, "Incoming call", 3)
        async with asyncio.timeout(3):
            while (initial := await browser_snapshot(observer))["state"] != "ringing":
                await asyncio.sleep(.01)
        call_id = initial["call_id"]
        await asyncio.to_thread(winner.command, "/accept")
        await asyncio.to_thread(caller.wait_for, "Call established", 3)
        await asyncio.to_thread(winner.wait_for, "Call established", 3)
        await wait_for(lambda: lab.calls.get_session(call_id).state == "in_call")
        session = lab.calls.get_session(call_id)
        assert "casa" not in session.endpoint_claims
        assert phone_states(lab.hass)["casa"]["state"] == "idle"
        assert snapshot(lab.runtime, "casa") == ()
        current = await browser_snapshot(observer)
        assert current["state"] == "idle"
        (tmp_path / "different-account-loser.json").write_text(json.dumps(current, indent=2))
        assert_no_browser_media_owner(lab)
    finally:
        await observer.close()
        await end_call(lab, caller)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_incoming_account_application_takeover_publishes_no_false_outgoing_state(
    canonical_browser, registered_phones, hass_ws_client, monkeypatch, tmp_path,
):
    from homeassistant.core import ServiceCall
    from custom_components.voip_stack import automation_call, automation_context, peer_snapshot
    from custom_components.voip_stack.const import CALL_EVENT
    from unittest.mock import AsyncMock

    lab = canonical_browser
    monkeypatch.setattr(peer_snapshot, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    events = []
    detach_events = lab.hass.bus.async_listen(CALL_EVENT, lambda event: events.append(dict(event.data)))
    observer = await open_browser_ws(lab, hass_ws_client)
    caller = registered_phones[0]
    binding = None
    try:
        await asyncio.to_thread(caller.command, "/dial Casa")
        async with asyncio.timeout(3):
            while (initial := await browser_snapshot(observer))["state"] != "ringing":
                await asyncio.sleep(.01)
        call_id = initial["call_id"]
        session = lab.calls.get_session(call_id)
        envelope = next(event for event in reversed(events) if event.get("call_id") == call_id
                        and event.get("automation_control") in {"ha_anchored", "routable"})
        execution = automation_context.CallExecution(session.token, envelope,
            ringing_guard=(session.event_context.sequence, session.callee))
        binding = automation_context.current_execution.set(execution)
        call = automation_context.bind_call_action(ServiceCall(lab.hass, "voip_stack", "tts_say", {}))
        app, call = await automation_call.resolve_application_action(call)
        app.claim(call)
        current = await browser_snapshot(observer)
        assert current["state"] in {"idle", "cancelled"}
        assert not current["can_answer"]
        await app.answer()
        await asyncio.to_thread(caller.wait_for, "Call established", 3)
        after_answer = await browser_snapshot(observer)
        assert after_answer["state"] in {"idle", "cancelled"}
        model = Path(__file__).resolve().parents[1] / "custom_components/voip_stack/frontend/voip-stack-card-model.js"
        subprocess.run(["node", "--input-type=module", "-e",
            f"import {{phoneCallPresentation}} from {model.as_uri()!r};let input='';for await(const chunk of process.stdin)input+=chunk;"
            "for(const snapshot of JSON.parse(input)){const p=phoneCallPresentation(snapshot);"
            "if(p.outgoing||p.state==='connecting')throw Error('Incoming application view became outgoing');}"],
            input=json.dumps([initial, current, after_answer]), text=True, check=True)
        (tmp_path / "incoming-application-payloads.json").write_text(json.dumps([initial, current, after_answer], indent=2))
    finally:
        if binding is not None:
            automation_context.current_execution.reset(binding)
        detach_events()
        await observer.close()
        await end_call(lab, caller)


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
