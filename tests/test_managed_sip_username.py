"""Register managed SIP names on loopback and check when their contacts are callable."""

import asyncio
from contextlib import ExitStack
from dataclasses import replace
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest

from .voip_phase1_support import _reserved_udp_ports, audio_format, sip, sip_auth, sip_listener, sip_registrar


@pytest.fixture
async def registrar_wire():
    host = "127.0.0.1"
    with _reserved_udp_ports(2) as (sip_port, rtp_port):
        registrar = sip_registrar.SipRegistrar(
            enabled=True, accounts=[], local_ip=host, local_sip_port=sip_port,
        )
    server = sip_listener.SipUdpServer(
        host=host, port=sip_port, local_ip=host, local_rtp_port=rtp_port,
        supported_formats=[audio_format.AudioFormat(16000, frame_ms=20)],
        on_invite=AsyncMock(), on_register=registrar.handle_register,
    )
    assert await server.start()
    try:
        with ExitStack() as clients:
            async def register(account):
                client = clients.enter_context(socket.socket(socket.AF_INET, socket.SOCK_DGRAM))
                client.setblocking(False)
                client.bind((host, 0))
                address = client.getsockname()
                port = address[1]
                encoded = quote(account.username, safe="")
                uri = f"sip:{host}:{sip_port}"
                contact = f"sip:{encoded}@{host}:{port}"
                instance = f"urn:uuid:12345678-1234-5678-1234-{port:012d}"
                loop = asyncio.get_running_loop()

                async def exchange(cseq, authorization=""):
                    headers = [
                        ("Via", f"SIP/2.0/UDP {host}:{port};branch=z9hG4bK-{port}-{cseq};rport"),
                        ("From", f"<sip:{encoded}@{host}>;tag=peer"),
                        ("To", f"<sip:{encoded}@{host}>"),
                        ("Call-ID", f"managed-phone-{port}"),
                        ("CSeq", f"{cseq} REGISTER"),
                        ("Contact", f'<{contact}>;expires=120;+sip.instance="<{instance}>"'),
                        ("Supported", "gruu"),
                    ]
                    if authorization:
                        headers.append(("Authorization", authorization))
                    await loop.sock_sendto(client, sip.build_request("REGISTER", uri, headers), (host, sip_port))
                    raw = await asyncio.wait_for(loop.sock_recv(client, 8192), 2)
                    return sip.parse_message(raw)

                challenge = await exchange(1)
                assert challenge.status_code == 401
                authorization = sip_auth.build_digest_authorization(
                    challenge_header=challenge.header("WWW-Authenticate"), username=account.username,
                    password=account.password, method="REGISTER", uri=uri,
                )
                # Digest quotes the original name; SIP URI userinfo is percent-encoded.
                quoted_name = account.username.replace("\\", "\\\\").replace('"', '\\"')
                assert f'username="{quoted_name}"' in authorization
                response = await exchange(2, authorization)
                assert response.status_code == 200
                assert contact in {str(sip.parse_sip_uri(header)) for header in response.header_values("Contact")}
                return address, contact

            yield registrar, register
    finally:
        await server.stop()


async def test_named_phone_contact_becomes_callable_on_promotion(registrar_wire):
    registrar, register = registrar_wire
    name = 'Caffè "Nord" #2'
    pending = sip_registrar.SipAccount(name, name, "fixture-password", registration_only=True)
    registrar.update_accounts([pending])
    address, contact = await register(pending)
    binding, = registrar.registered_contacts(name)
    assert binding.contact_uri == contact
    assert sip.parse_sip_uri(contact).user == name
    gruus = [sip.parse_sip_uri(uri) for uri in (binding.public_gruu, binding.temporary_gruu)]
    assert gruus[0].user == name
    assert registrar.registered_roster_entries() == []
    assert registrar.account_for_source(address, "UDP", username=name) is None
    assert registrar.account_for_source(address, "UDP") is None
    assert not registrar.registration_matches_source(name, *address, "UDP")
    assert registrar.select_outbound_flow(name, sip.parse_sip_uri(contact), allow_ordinary=True) is None
    assert registrar.authentication_generation(name) is None
    for uri in gruus:
        assert registrar.resolve_gruu(uri) == ()
        assert registrar.local_gruu_target(uri) is None

    active = replace(pending, registration_only=False)
    registrar.update_accounts([active])
    assert registrar.registered_contacts(name) == (binding,)
    assert registrar.authentication_generation(name)
    assert registrar.account_for_source(address, "UDP") == active
    assert registrar.registration_matches_source(name, *address, "UDP")
    assert registrar.select_outbound_flow(name, sip.parse_sip_uri(contact), allow_ordinary=True)
    entry, = registrar.registered_roster_entries()
    assert (entry.id, entry.name, entry.sip_uri) == (name, name, contact)
    assert entry.metadata["sip_contacts"][0]["uri"] == contact
    for uri in gruus:
        assert registrar.resolve_gruu(uri) == (binding,)
        assert registrar.local_gruu_target(uri) == sip.parse_sip_uri(contact)


async def test_number_sign_in_account_name_keeps_its_own_contacts(registrar_wire):
    registrar, register = registrar_wire
    desk = sip_registrar.SipAccount("Desk", "Desk", "first-fixture-password")
    numbered = sip_registrar.SipAccount("Desk#2", "Desk#2", "second-fixture-password")
    registrar.update_accounts([desk, numbered])
    first, _ = await register(desk)
    second, _ = await register(desk)
    numbered_address, _ = await register(numbered)

    # Reload also rebuilds the internal binding index.
    registrar.update_accounts([desk, numbered])
    assert {b.source_port for b in registrar.registered_contacts(desk.username)} == {first[1], second[1]}
    numbered_binding, = registrar.registered_contacts(numbered.username)
    assert numbered_binding.source_port == numbered_address[1]
    assert registrar.account_for_source(second, "UDP") == desk
    assert registrar.account_for_source(numbered_address, "UDP") == numbered
    registrar.remove_registration(numbered.username)
    assert registrar.registered_contacts(numbered.username) == ()
    assert {b.source_port for b in registrar.registered_contacts(desk.username)} == {first[1], second[1]}


@pytest.mark.parametrize("function", ["blf", "speed_dial"])
def test_handset_key_uses_escaped_sip_name_or_existing_extension(function):
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
    from custom_components.voip_stack.phone_profile import resolve_phone_keys

    name = 'Caffè "Nord" #2'
    expected = "Caff%C3%A8%20%22Nord%22%20%232"
    runtime = SimpleNamespace(endpoints=EndpointRegistry())
    target = runtime.endpoints.register(PhoneEndpoint(
        "esp:desk", name, EndpointKind.ESPHOME, username=name, device_id="desk-device",
    ))
    key_slot = {"slot": "1", "functions": [function], "max_value_bytes": len(expected)}
    profile = {"capabilities": {"key_slots": [key_slot]}}
    phone = {
        "lines": [{"slot": "1", "endpoint_id": "own-line"}],
        "keys": [{"slot": "1", "line": "1", "function": function, "target": target.endpoint_id}],
    }
    key, = resolve_phone_keys(runtime, phone, profile)
    assert key["label"] == name
    assert key["dial_target"] == expected
    assert key["subscription_target"] == (expected if function == "blf" else "")

    key_slot["max_value_bytes"] -= 1
    with pytest.raises(ValueError, match="phone_key_target_too_long"):
        resolve_phone_keys(runtime, phone, profile)

    runtime.endpoints.update(target.endpoint_id, extension="*42#")
    key, = resolve_phone_keys(runtime, phone, profile)
    assert key["dial_target"] == "*42#"
    assert key["subscription_target"] == ("*42#" if function == "blf" else "")
