"""REFER auth follows the committed leg principal on a shared registered flow."""

import asyncio
from dataclasses import replace
import hashlib
import re
import time
from types import SimpleNamespace

# Load real HA and integration packages before the pure protocol helper loaders.
import homeassistant.config_entries  # noqa: F401
import homeassistant.core  # noqa: F401
import custom_components.voip_stack  # noqa: F401

from .voip_phase1_support import (
    audio_format,
    sip,
    sip_listener,
    sip_registrar,
    sip_transfer,
)
from .test_refer_correlation import Peer
from custom_components.voip_stack.sip_application import SipApplicationMethods
from custom_components.voip_stack.runtime_data import VoipStackRuntime

import pytest

from custom_components.voip_stack.endpoint_registry import EndpointRegistry
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.endpoint_session import TerminationIntent

pytestmark = pytest.mark.ha
PASSWORDS = {"Alice": "loopback-only-A", "Bob": "loopback-only-B"}


def params(value):
    return {
        k.lower(): q or token
        for k, q, token in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', value)
    }


def md5(value):
    return hashlib.md5(value.encode()).hexdigest()


def digest_header(challenge, user, password, method, uri, cnonce):
    p = params(challenge)
    assert p.get("algorithm", "MD5").upper() == "MD5"
    assert "auth" in p.get("qop", "auth").split(",")
    response = md5(
        md5(user + ":" + p["realm"] + ":" + password)
        + ":"
        + p["nonce"]
        + ":00000001:"
        + cnonce
        + ":auth:"
        + md5(method + ":" + uri)
    )
    return (
        'Digest username="'
        + user
        + '", realm="'
        + p["realm"]
        + '", nonce="'
        + p["nonce"]
        + '", uri="'
        + uri
        + '", response="'
        + response
        + '", algorithm=MD5, qop=auth, nc=00000001, cnonce="'
        + cnonce
        + '"'
    )


def verify_independent(message, user, nonce):
    p = params(message.header("Authorization"))
    assert (
        p["username"] == user and p["nonce"] == nonce and p["realm"] == "loopback-peer"
    )
    expected = md5(
        md5(user + ":loopback-peer:" + PASSWORDS[user])
        + ":"
        + nonce
        + ":"
        + p["nc"]
        + ":"
        + p["cnonce"]
        + ":auth:"
        + md5(message.method + ":" + message.uri)
    )
    assert p["response"] == expected and p["uri"] == message.uri
    return True


def summary(message, direction):
    return {
        "direction": direction,
        "method": message.method,
        "status": message.status_code,
        "call_id": message.header("Call-ID"),
        "CSeq": message.header("CSeq"),
        "Via": message.header("Via"),
        "Event": message.header("Event"),
        "Subscription-State": message.header("Subscription-State"),
        "Authorization_present": bool(message.header("Authorization")),
        "Authorization_username": params(message.header("Authorization")).get(
            "username"
        ),
        "GRUU_target": ";gr=" in str(message.uri),
    }


async def exercise(order, *, revoke=False, logical_leg=False, end_before_final=False):
    result = {
        "registered_order": list(order),
        "authenticated_dialog_principal": "Alice",
        "wire": [],
    }
    loop = asyncio.get_running_loop()
    remote, peer = await loop.create_datagram_endpoint(
        Peer, local_addr=("127.0.0.1", 0)
    )
    peer.gruu = True
    peer_addr = remote.get_extra_info("sockname")
    registrar = sip_registrar.SipRegistrar(
        enabled=True,
        accounts=[sip_registrar.SipAccount(u, u, p) for u, p in PASSWORDS.items()],
        local_ip="127.0.0.1",
        local_sip_port=5060,
    )
    endpoints = EndpointRegistry()
    for username in PASSWORDS:
        endpoints.register(
            PhoneEndpoint(
                endpoint_id="account:" + username,
                name=username,
                kind=EndpointKind.SIP_ACCOUNT,
                username=username,
            )
        )
    calls = SipEndpointRuntime(allow_dark_sessions=True)
    calls.activate()
    calls.bind_endpoint_registry(endpoints)
    runtime = VoipStackRuntime(
        transport_config={},
        assist_config={},
        trunk_config={},
        endpoints=endpoints,
        phones=None,
        sip=calls,
    )
    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_entries=lambda _: [SimpleNamespace(runtime_data=runtime)]
        ),
    )
    application = SipApplicationMethods(hass, registrar)
    actions = []
    release = asyncio.Event()
    terminated = asyncio.Event()

    async def accepted(invite):
        # The trusted principal is independently established by successful
        # Digest REGISTER and validated against its live binding, not selected
        # merely from an arbitrary incoming From header.
        assert registrar.registration_matches_source(
            "Alice", invite.source_host, invite.source_port, "UDP"
        )
        result["INVITE_principal_binding_validated"] = True
        calls.upsert(
            invite.call_id, state="in_call", source_endpoint_id="account:Alice"
        )
        calls.add_leg(
            invite.call_id,
            "logical-source" if logical_leg else invite.call_id,
            role="caller",
            state="in_call",
            sip_call_id=invite.call_id,
            endpoint_id="account:Alice",
        )
        return sip_listener.SipInviteResult(
            200,
            "OK",
            answer_sdp=(
                "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=loopback\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                "m=audio 41000 RTP/AVP 96\r\na=rtpmap:96 L16/16000/1\r\na=ptime:20\r\na=sendrecv\r\n"
            ),
        )

    async def referred(cid, target):
        await release.wait()
        actions.append(cid)
        return 603 if end_before_final else 200

    async def ended(call_id, _reason):
        await calls.get_session(call_id).terminate(TerminationIntent("remote_hangup"))
        terminated.set()

    endpoint = sip_listener.SipUdpEndpoint(
        local_ip="127.0.0.1",
        local_sip_port=0,
        local_rtp_port=41000,
        supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
        on_invite=accepted,
        on_register=registrar.handle_register,
        on_refer=referred,
        on_terminated=ended,
        request_router=application,
    )
    local, _ = await loop.create_datagram_endpoint(
        lambda: endpoint, local_addr=("127.0.0.1", 0)
    )
    owner_addr = local.get_extra_info("sockname")
    endpoint.local_sip_port = owner_addr[1]
    registrar.local_sip_port = owner_addr[1]
    uri = f"sip:127.0.0.1:{owner_addr[1]}"

    async def receive():
        message, address = await peer.receive()
        result["wire"].append(summary(message, "owner->peer"))
        return message, address

    def send(method, request_uri, headers, body=b""):
        raw = sip.build_request(method, request_uri, headers, body)
        result["wire"].append(summary(sip.parse_message(raw), "peer->owner"))
        remote.sendto(raw, owner_addr)

    async def registration(user, expires=120, first_cseq=1):
        challenge = ""
        for index in range(2):
            cseq = first_cseq + index
            headers = [
                (
                    "Via",
                    f"SIP/2.0/UDP 127.0.0.1:{peer_addr[1]};branch=z9hG4bKreg{user}{cseq};rport",
                ),
                ("From", f"<sip:{user}@127.0.0.1>;tag=reg{user}"),
                ("To", f"<sip:{user}@127.0.0.1>"),
                ("Call-ID", "register-" + user),
                ("CSeq", f"{cseq} REGISTER"),
                ("Contact", f"<sip:{user}@127.0.0.1:{peer_addr[1]}>;expires={expires}"),
                ("Expires", str(expires)),
            ]
            if index:
                headers.append(
                    (
                        "Authorization",
                        digest_header(
                            challenge,
                            user,
                            PASSWORDS[user],
                            "REGISTER",
                            uri,
                            "refer-dialog-" + user + str(cseq),
                        ),
                    )
                )
            send("REGISTER", uri, headers)
            response, _ = await receive()
            assert response.status_code == (401 if index == 0 else 200)
            if index == 0:
                challenge = response.header("WWW-Authenticate")
        return {"username": user, "Expires": expires, "authenticated_200": True}

    call_id = "loopback-refer-" + ("-".join(order))
    try:
        result["registrations"] = [await registration(user) for user in order]
        result["binding_sources"] = {
            user: [
                {"host": x.source_host, "port": x.source_port, "transport": x.transport}
                for x in registrar.registered_contacts(user)
            ]
            for user in order
        }
        assert all(
            registrar.registration_matches_source(user, *peer_addr, "UDP")
            for user in order
        )
        specific = registrar.account_for_source(peer_addr, "UDP", username="Alice")
        assert specific is not None and specific.username == "Alice"
        ambiguous = registrar.account_for_source(peer_addr, "UDP")
        result["unqualified_account_for_source"] = (
            ambiguous.username if ambiguous else None
        )
        headers = [
            ("Via", f"SIP/2.0/UDP 127.0.0.1:{peer_addr[1]};branch=z9hG4bKinvite;rport"),
            ("From", "<sip:Alice@127.0.0.1>;tag=alice-dialog"),
            ("To", "<sip:HA@127.0.0.1>"),
            ("Call-ID", call_id),
            ("CSeq", "1 INVITE"),
            ("Contact", f"<sip:Alice@127.0.0.1:{peer_addr[1]};gr=fixture>"),
            ("Content-Type", "application/sdp"),
        ]
        body = b"v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=loopback\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 45000 RTP/AVP 96\r\na=rtpmap:96 L16/16000/1\r\na=ptime:20\r\na=sendrecv\r\n"
        send("INVITE", uri, headers, body)
        while True:
            answer, _ = await receive()
            if int(answer.status_code or 0) >= 200:
                break
        assert answer.status_code == 200
        dialog_headers = [
            ("From", headers[1][1]),
            ("To", answer.header("To")),
            ("Call-ID", call_id),
        ]
        send(
            "ACK",
            uri,
            [
                ("Via", f"SIP/2.0/UDP 127.0.0.1:{peer_addr[1]};branch=z9hG4bKack"),
                *dialog_headers,
                ("CSeq", "1 ACK"),
            ],
        )
        credentials = application.dialog_credentials(call_id, peer_addr, "UDP")
        result["dialog_credentials_username"] = (
            credentials.username if credentials else None
        )
        send(
            "REFER",
            uri,
            [
                ("Via", f"SIP/2.0/UDP 127.0.0.1:{peer_addr[1]};branch=z9hG4bKrefer"),
                *dialog_headers,
                ("CSeq", "2 REFER"),
                ("Contact", f"<sip:Alice@127.0.0.1:{peer_addr[1]};gr=fixture>"),
                ("Refer-To", "<sip:next@127.0.0.1>"),
            ],
        )
        first = None
        refer_response = False
        while first is None or not refer_response:
            message, address = await receive()
            if message.method == "NOTIFY":
                first, address_first = message, address
            elif (
                message.status_code == 200
                and sip.parse_cseq(message.header("CSeq")).method == "REFER"
            ):
                refer_response = True
        dialog = endpoint.active_dialogs[call_id]
        task = dialog.refer_task
        assert task is not None
        successful = []

        async def challenge_notify(message, address, index, expect_retry=True):
            nonce = "fixture-notify-" + str(index)
            response_headers = [
                (k, message.header(k)) for k in ("Via", "From", "To", "Call-ID", "CSeq")
            ]
            response_headers.append(
                (
                    "WWW-Authenticate",
                    'Digest realm="loopback-peer", nonce="' + nonce + '", qop="auth"',
                )
            )
            raw = sip.build_response(401, "Unauthorized", response_headers)
            result["wire"].append(summary(sip.parse_message(raw), "peer->owner"))
            remote.sendto(raw, address)
            if credentials is None or not expect_retry:
                return
            retry, address = await receive()
            assert retry.method == "NOTIFY"
            assert (
                sip.parse_cseq(retry.header("CSeq")).number
                > sip.parse_cseq(message.header("CSeq")).number
            )
            assert retry.header("Via") != message.header("Via")
            assert verify_independent(retry, "Alice", nonce)
            successful.append(
                {
                    "Event": retry.header("Event"),
                    "Subscription-State": retry.header("Subscription-State"),
                    "verified_Digest_username": "Alice",
                    "body": retry.body.decode(),
                    "GRUU_target": ";gr=" in retry.uri,
                }
            )
            peer.answer(retry, address, 200)

        await challenge_notify(first, address_first, 1)
        if end_before_final:
            send("BYE", uri, [
                ("Via", f"SIP/2.0/UDP 127.0.0.1:{peer_addr[1]};branch=z9hG4bKearlybye"),
                *dialog_headers, ("CSeq", "3 BYE"),
            ])
            response, _ = await receive()
            assert response.status_code == 200
            await asyncio.wait_for(terminated.wait(), 2)
            assert call_id not in endpoint.active_dialogs
            assert call_id in endpoint._event_dialogs
            assert calls.get_session(call_id) is None
            result["BYE_before_terminal"] = True
        if revoke and credentials is not None:
            result["unregister_Alice"] = await registration(
                "Alice", expires=0, first_cseq=3
            )
            assert application.dialog_credentials(call_id, peer_addr, "UDP") is None
        release.set()
        if credentials is not None:
            final, address = await receive()
            assert final.method == "NOTIFY"
            await challenge_notify(final, address, 2, expect_retry=not revoke)
        await asyncio.wait_for(task, 2)
        result["transfer_action_completed"] = actions == [call_id]
        result["authenticated_NOTIFIES"] = successful
        result["terminal_authenticated"] = any(
            x["Subscription-State"].startswith("terminated") for x in successful
        )
        if credentials is None or revoke:
            try:
                async with asyncio.timeout(0.15):
                    extra, _ = await peer.receive()
            except TimeoutError:
                extra = None
            result["extra_message_after_rejected_progress"] = (
                summary(extra, "owner->peer") if extra else None
            )
            assert extra is None
        if "unregister_Alice" not in result:
            result["unregister_Alice"] = await registration(
                "Alice", expires=0, first_cseq=3
            )
        assert not registrar.registered_contacts("Alice")
        revoked = application.dialog_credentials(call_id, peer_addr, "UDP")
        result["credentials_after_principal_binding_revoked"] = (
            revoked.username if revoked else None
        )
        result["remaining_Bob_binding"] = bool(registrar.registered_contacts("Bob"))
        if not end_before_final:
            send(
                "BYE",
                uri,
                [
                    ("Via", f"SIP/2.0/UDP 127.0.0.1:{peer_addr[1]};branch=z9hG4bKbye"),
                    *dialog_headers,
                    ("CSeq", "3 BYE"),
                ],
            )
            response, _ = await receive()
            assert response.status_code == 200
            result["BYE_200"] = True
            assert call_id not in endpoint.active_dialogs
        return result
    finally:
        release.set()
        local.close()
        await endpoint.wait_closed()
        remote.close()
        await application.stop()
        await calls.shutdown()


@pytest.mark.parametrize("order", [("Alice",), ("Alice", "Bob"), ("Bob", "Alice")])
def test_refer_notify_authenticates_original_principal_on_shared_flow(
    order, socket_enabled
):
    result = asyncio.run(exercise(order, logical_leg=len(order) > 1))
    assert result["dialog_credentials_username"] == "Alice"
    assert result["terminal_authenticated"]
    assert len(result["authenticated_NOTIFIES"]) == 2
    assert result["credentials_after_principal_binding_revoked"] is None


@pytest.mark.parametrize("order", [("Alice", "Bob"), ("Bob", "Alice")])
def test_revoked_binding_cannot_authenticate_final_notify_as_remaining_account(
    order, socket_enabled
):
    result = asyncio.run(exercise(order, revoke=True))
    assert not result["terminal_authenticated"]
    assert len(result["authenticated_NOTIFIES"]) == 1
    assert result["credentials_after_principal_binding_revoked"] is None
    assert result["remaining_Bob_binding"]


async def state_scene():
    """Actual typed owners, with explicit registered-flow fixture state."""
    endpoints = EndpointRegistry()
    for username in PASSWORDS:
        endpoints.register(
            PhoneEndpoint(
                endpoint_id="account:" + username,
                name=username,
                kind=EndpointKind.SIP_ACCOUNT,
                username=username,
            )
        )
    calls = SipEndpointRuntime(allow_dark_sessions=True)
    runtime = VoipStackRuntime(
        transport_config={},
        assist_config={},
        trunk_config={},
        endpoints=endpoints,
        phones=None,
        sip=calls,
    )
    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_entries=lambda _: [SimpleNamespace(runtime_data=runtime)]
        ),
    )
    registrar = sip_registrar.SipRegistrar(
        enabled=True,
        accounts=[sip_registrar.SipAccount(u, u, p) for u, p in PASSWORDS.items()],
        local_ip="127.0.0.1",
        local_sip_port=5060,
    )
    address = ("127.0.0.1", 50000)
    for username in PASSWORDS:
        registrar.registrations[username.casefold()] = sip_registrar.SipRegistration(
            username=username,
            contact_uri=f"sip:{username}@127.0.0.1:50000",
            source_host=address[0],
            source_port=address[1],
            transport="UDP",
            expires_at=time.time() + 120,
        )
    session = calls.upsert(
        "wire-owned",
        state="in_call",
        source_endpoint_id="account:Alice",
        inbound_trunk_id="T",
    )
    leg = calls.add_leg(
        "wire-owned",
        "logical-source",
        role="caller",
        state="in_call",
        sip_call_id="wire-owned",
        endpoint_id="account:Alice",
    )
    app = SipApplicationMethods(hass, registrar)
    return SimpleNamespace(
        app=app,
        registrar=registrar,
        endpoints=endpoints,
        calls=calls,
        runtime=runtime,
        session=session,
        leg=leg,
        address=address,
    )


async def close_scene(scene):
    await scene.app.stop()
    await scene.calls.shutdown()


@pytest.mark.parametrize(
    "invalid",
    [
        "unknown_call_id",
        "foreign_explicit_wire_id",
        "duplicate_wire_id",
        "endpoint_removed",
        "account_disabled",
        "account_removed",
        "binding_revoked",
        "wrong_source_port",
    ],
)
def test_dialog_credentials_fail_closed_without_borrowing_another_principal(invalid):
    async def run():
        scene = await state_scene()
        try:
            call_id = "wire-owned"
            address = scene.address
            if invalid in {
                "unknown_call_id",
                "foreign_explicit_wire_id",
                "duplicate_wire_id",
                "endpoint_removed",
            }:
                scene.registrar.remove_registration("Bob")
            if invalid == "unknown_call_id":
                call_id = "foreign-dialog"
            elif invalid == "foreign_explicit_wire_id":
                scene.leg.sip_call_id = "foreign-dialog"
            elif invalid == "duplicate_wire_id":
                scene.calls.add_leg(
                    "wire-owned",
                    "another-leg",
                    role="callee",
                    state="in_call",
                    sip_call_id="wire-owned",
                    endpoint_id="account:Bob",
                )
            elif invalid == "endpoint_removed":
                scene.endpoints.remove("account:Alice", force=True)
            elif invalid == "account_disabled":
                scene.registrar.accounts["alice"] = replace(
                    scene.registrar.accounts["alice"], enabled=False
                )
            elif invalid == "account_removed":
                scene.registrar.accounts.pop("alice")
            elif invalid == "binding_revoked":
                scene.registrar.remove_registration("Alice")
            elif invalid == "wrong_source_port":
                address = (address[0], address[1] + 1)
            assert scene.app.dialog_credentials(call_id, address, "UDP") is None
        finally:
            await close_scene(scene)

    asyncio.run(run())


@pytest.mark.parametrize(
    "role,explicit,ready,expected",
    [
        ("caller", False, True, "trunk-user"),
        ("callee", True, True, "trunk-user"),
        ("callee", False, True, None),
        ("caller", False, False, None),
    ],
)
def test_trunk_credentials_require_the_matching_owned_leg(
    role, explicit, ready, expected
):
    async def run():
        scene = await state_scene()
        try:
            scene.leg.endpoint_id = ""
            scene.leg.role = role
            scene.leg.trunk_id = "T" if explicit else ""
            trunk = SimpleNamespace(
                ready=ready,
                config=SimpleNamespace(
                    username="trunk-user",
                    password="loopback-trunk-only",
                    auth_username="proxy-user",
                ),
            )
            scene.runtime.trunk_configs = {"T": {}}
            scene.calls.attach_component("trunk:T", trunk)
            credentials = scene.app.dialog_credentials(
                "wire-owned", scene.address, "UDP"
            )
            assert (credentials.username if credentials else None) == expected
            if credentials:
                assert credentials.auth_username == "proxy-user"
            # A local principal never falls through to bridge trunk metadata.
            scene.leg.endpoint_id = "account:Alice"
            scene.leg.trunk_id = ""
            scene.registrar.remove_registration("Alice")
            assert (
                scene.app.dialog_credentials("wire-owned", scene.address, "UDP") is None
            )
        finally:
            await close_scene(scene)

    asyncio.run(run())


@pytest.mark.parametrize("revoked", [False, True])
def test_gruu_consumer_resolves_original_owned_dialog_not_new_usage(revoked):
    """Exercise the real consumer at its sender boundary, without a GRUU call claim."""

    async def run():
        from custom_components.voip_stack.core.sip_dialog import (
            DialogKey,
            SubscriptionDialog,
        )

        scene = await state_scene()
        try:
            if revoked:
                scene.registrar.remove_registration("Alice")
            lookups = []

            def credentials(call_id, address, transport):
                lookups.append(call_id)
                return scene.app.dialog_credentials(call_id, address, transport)

            request = sip.parse_message(
                sip.build_request(
                    "INVITE",
                    "sip:HA@127.0.0.1",
                    [
                        ("From", "<sip:untrusted-display@127.0.0.1>;tag=peer"),
                        ("To", "<sip:HA@127.0.0.1>"),
                        ("Call-ID", "wire-owned"),
                        ("CSeq", "1 INVITE"),
                        ("Contact", "<sip:Alice@127.0.0.1:50000;gr=fixture>"),
                    ],
                )
            )
            dialog = sip_listener._ActiveDialog(
                request=request,
                addr=scene.address,
                to_tag="ha",
                cseq=2,
                transport="UDP",
                remote_uri="sip:Alice@127.0.0.1",
            )
            endpoint = sip_listener.SipUdpEndpoint(
                local_ip="127.0.0.1",
                local_rtp_port=41000,
                supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
                on_invite=lambda _: None,
                request_router=SimpleNamespace(dialog_credentials=credentials),
            )
            observed = []

            async def sender(_request, _address, result):
                observed.append(result)
                return sip.SipMessage(status_code=401, reason="Unauthorized")

            endpoint._send_application_follow_up_unlocked = sender
            usage = SubscriptionDialog(
                key=DialogKey("new-gruu-refer", "ha", ""),
                local_uri="sip:HA@127.0.0.1",
                remote_uri="sip:Alice@127.0.0.1",
                remote_target_uri="sip:Alice@127.0.0.1:50000;gr=fixture",
                remote_cseq=0,
            )
            subscription = sip_transfer.ReferSubscription()
            result = await endpoint._await_refer_outcome(
                "wire-owned",
                dialog,
                sip_transfer.SipReferTarget("sip:next@127.0.0.1"),
                subscription,
                usage,
                1,
            )
            assert not result.accepted and result.status == 401
            assert lookups == ["wire-owned"]
            actual = observed[0].follow_up_credentials
            assert (actual.username if actual else None) == (
                None if revoked else "Alice"
            )
            assert observed[0].follow_up_dialog.key.call_id == "new-gruu-refer"
            subscription.close()
        finally:
            await close_scene(scene)

    asyncio.run(run())


@pytest.mark.parametrize("order", [("Alice",), ("Alice", "Bob"), ("Bob", "Alice")])
@pytest.mark.parametrize("revoke", [False, True])
def test_refer_terminal_digest_survives_bye_but_not_binding_revocation(order, revoke, socket_enabled):
    result = asyncio.run(exercise(order, revoke=revoke, end_before_final=True))
    assert result["BYE_before_terminal"]
    assert result["terminal_authenticated"] is (not revoke)
    assert len(result["authenticated_NOTIFIES"]) == (1 if revoke else 2)
    if not revoke:
        assert "603" in result["authenticated_NOTIFIES"][-1]["body"]


def test_verified_trunk_ingress_ignores_a_local_directory_name_collision():
    async def run():
        scene = await state_scene()
        try:
            scene.session.update_metadata(ingress="trunk")
            trunk = SimpleNamespace(ready=True, config=SimpleNamespace(
                username="trunk-user", password="loopback-trunk-only", auth_username="proxy-user"))
            scene.runtime.trunk_configs = {"T": {}}
            scene.calls.attach_component("trunk:T", trunk)
            # The identically named directory endpoint remains on the caller leg.
            # Its account is registered elsewhere, just as on the audited route.
            scene.address = ("127.0.0.2", 5060)
            bound = scene.app.bind_dialog_credentials("wire-owned", scene.address, "UDP")
            assert bound().username == "trunk-user"
            await scene.session.terminate(TerminationIntent("remote_hangup"))
            assert bound().username == "trunk-user"
            trunk.ready = False
            assert bound() is None
        finally:
            await close_scene(scene)
    asyncio.run(run())
