"""Canonical phone availability reaches SIP PIDF through the existing owner."""
import asyncio
import socket
import time
from dataclasses import replace
from xml.etree import ElementTree

import pytest

from custom_components.voip_stack.phone_endpoint import (
    EndpointAvailability, EndpointKind, PhoneEndpoint,
)
from .test_dialog_info_ha import blf as blf, lab as lab, subscribe
from .test_trunk_calls_ha import trunks as trunks
from .test_ha_sip_application import _publish
from custom_components.voip_stack.core import sip
from custom_components.voip_stack.sip_registrar import SipAccount, SipRegistration

pytestmark = pytest.mark.ha
PIDF = {"p": "urn:ietf:params:xml:ns:pidf"}


def basic(notification):
    return ElementTree.fromstring(notification.body).find("p:tuple/p:status/p:basic", PIDF).text


async def test_logical_presence_initial_notify_uses_canonical_availability_without_card(lab, blf):
    methods, notifications, send = blf
    lab.runtime.endpoints.register(PhoneEndpoint(
        endpoint_id="browser:Casa", name="Casa", username="Casa", extension="667",
        kind=EndpointKind.BROWSER, availability=EndpointAvailability.AVAILABLE,
    ))
    assert not lab.runtime.softphone_presence
    dialog = await methods.handle(subscribe(target="Casa", call_id="logical-dialog"),
                                  ("127.0.0.3", 5060), "UDP", send)
    assert dialog.status == 200
    dialog.follow_up_scheduler()
    assert ElementTree.fromstring((await asyncio.wait_for(notifications.get(), 1)).body).tag.endswith("dialog-info")
    response = await methods.handle(subscribe(target="Casa", event="presence",
        accept="application/pidf+xml", call_id="logical-presence"), ("127.0.0.3", 5060), "UDP", send)
    assert response.status == 200
    assert basic(response.follow_up) == "open"
    response.follow_up_scheduler()
    assert basic(await asyncio.wait_for(notifications.get(), 1)) == "open"


@pytest.mark.parametrize("kind", [EndpointKind.BROWSER, EndpointKind.SIP_ACCOUNT, EndpointKind.ESPHOME])
async def test_canonical_availability_notifies_closed_open_and_releases_owner(lab, blf, kind):
    methods, notifications, send = blf
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="subject", name="Phone", username="phone",
        kind=kind, availability=EndpointAvailability.AVAILABLE))
    result = await methods.handle(subscribe(target="phone", event="presence", accept="application/pidf+xml"),
                                  ("127.0.0.3", 5060), "UDP", send)
    assert result.status == 200
    result.follow_up_scheduler()
    assert basic(await asyncio.wait_for(notifications.get(), 1)) == "open"
    owner = lab.calls.component("phone_state")
    assert len(owner.listeners) == 1
    lab.runtime.endpoints.update("subject", availability=EndpointAvailability.OFFLINE)
    assert basic(await asyncio.wait_for(notifications.get(), 1)) == "closed"
    lab.runtime.endpoints.update("subject", availability=EndpointAvailability.AVAILABLE)
    assert basic(await asyncio.wait_for(notifications.get(), 1)) == "open"
    lab.runtime.endpoints.remove("subject")
    terminal = await asyncio.wait_for(notifications.get(), 1)
    assert dict(terminal.headers)["Subscription-State"] == "terminated;reason=noresource"
    assert basic(terminal) == "closed"
    assert not methods.subscriptions and methods.presence_unsubscribe is None
    assert not owner.listeners


async def test_extension_uses_canonical_publish_updates_and_expiry_falls_back(lab, blf):
    methods, notifications, send = blf
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="sip:alice", name="Alice", username="alice",
        extension="667", kind=EndpointKind.SIP_ACCOUNT, availability=EndpointAvailability.AVAILABLE))
    methods.registrar.update_accounts([*methods.registrar.accounts.values(), SipAccount("alice", "Alice", "test-only")])
    methods.registrar.registrations["alice"] = SipRegistration(username="alice", contact_uri="sip:alice@127.0.0.4:5060",
        source_host="127.0.0.4", source_port=5060, transport="UDP", expires_at=time.time()+300)
    def document(value):
        return ('<presence xmlns="urn:ietf:params:xml:ns:pidf" entity="sip:alice@localhost">'
                f'<tuple id="phone"><status><basic>{value}</basic></status></tuple></presence>').encode()
    published = await methods.handle(_publish(body=document("closed")), ("127.0.0.4", 5060), "UDP", send)
    assert published.status == 200
    subscribed = await methods.handle(subscribe(target="667", event="presence", accept="application/pidf+xml"),
                                      ("127.0.0.3", 5060), "UDP", send)
    assert subscribed.status == 200 and basic(subscribed.follow_up) == "closed"
    subscribed.follow_up_scheduler()
    assert basic(await asyncio.wait_for(notifications.get(), 1)) == "closed"
    updated = await methods.handle(_publish(body=document("open"), entity_tag=dict(published.headers)["SIP-ETag"]),
                                  ("127.0.0.4", 5060), "UDP", send)
    assert updated.status == 200
    assert basic(await asyncio.wait_for(notifications.get(), 1)) == "open"
    # Q compositor policy closes delivery while retaining the unchanged publication lease.
    leases = dict(methods.publications)
    published_document = methods._publication("alice").body
    lab.runtime.endpoints.update("sip:alice", availability=EndpointAvailability.OFFLINE)
    assert basic(await asyncio.wait_for(notifications.get(), 1)) == "closed"
    assert methods.publications == leases
    assert all(methods.publications[tag] is lease for tag, lease in leases.items())
    lab.runtime.endpoints.update("sip:alice", availability=EndpointAvailability.AVAILABLE)
    restored = await asyncio.wait_for(notifications.get(), 1)
    assert basic(restored) == "open" and restored.body == published_document
    assert methods.publications == leases
    for tag, publication in tuple(methods.publications.items()):
        methods.publications[tag] = replace(publication, expires_at=time.time()-1)
    methods._notify_subscribers("alice")
    fallback = await asyncio.wait_for(notifications.get(), 1)
    assert basic(fallback) == "open"
    assert ElementTree.fromstring(fallback.body).attrib["entity"] == "sip:667@127.0.0.1"
    assert not methods.publications and not methods.publication_timers


async def test_presence_rejects_unknown_or_forged_and_target_disable_ends_alias_usage(lab, blf):
    methods, notifications, send = blf
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="sip:alice", name="Alice", username="alice",
        extension="667", kind=EndpointKind.SIP_ACCOUNT, availability=EndpointAvailability.AVAILABLE))
    methods.registrar.update_accounts([*methods.registrar.accounts.values(), SipAccount("alice", "Alice", "test-only")])
    request = subscribe(target="667", event="presence", accept="application/pidf+xml")
    assert (await methods.handle(request, ("127.0.0.9", 5060), "UDP", send)).status == 403
    assert (await methods.handle(subscribe(target="missing", event="presence", accept="application/pidf+xml"),
                                 ("127.0.0.3", 5060), "UDP", send)).status == 404
    result = await methods.handle(request, ("127.0.0.3", 5060), "UDP", send)
    assert result.status == 200
    result.follow_up_scheduler()
    assert basic(await asyncio.wait_for(notifications.get(), 1)) == "open"
    methods.registrar.update_accounts([SipAccount("watcher", "Watcher", "test-only"),
                                      SipAccount("alice", "Alice", "test-only", enabled=False)])
    terminal = await asyncio.wait_for(notifications.get(), 1)
    assert dict(terminal.headers)["Subscription-State"] == "terminated;reason=noresource"
    assert basic(terminal) == "closed" and not methods.subscriptions
    assert (await methods.handle(request, ("127.0.0.3", 5060), "UDP", send)).status == 404


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_real_udp_subscribe_and_notify_for_logical_phones_without_cards(trunks):
    lab = trunks
    registrar = lab.calls.component("registrar")
    registrar.enabled = True
    registrar.update_accounts([*registrar.accounts.values(), SipAccount("watcher", "Watcher", "test-only")])
    for name in ("Casa", "Test"):
        lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="browser:"+name, name=name, username=name,
            kind=EndpointKind.BROWSER, availability=EndpointAvailability.AVAILABLE))
    assert not lab.runtime.softphone_presence
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.bind(("127.0.0.1", 0))
        client.setblocking(False)
        port = client.getsockname()[1]
        registrar.registrations["watcher"] = SipRegistration(username="watcher",
            contact_uri=f"sip:watcher@127.0.0.1:{port}", source_host="127.0.0.1", source_port=port,
            transport="UDP", expires_at=time.time()+300)
        server = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
        for name in ("Casa", "Test"):
            request = sip.build_request("SUBSCRIBE", f"sip:{name}@127.0.0.1", [
                ("Via", f"SIP/2.0/UDP 127.0.0.1:{port};branch=z9hG4bK-{name};rport"),
                ("From", f"<sip:watcher@127.0.0.1>;tag=watcher-{name}"),
                ("To", f"<sip:{name}@127.0.0.1>"), ("Contact", f"<sip:watcher@127.0.0.1:{port}>"),
                ("Call-ID", "presence-wire-"+name), ("CSeq", "1 SUBSCRIBE"),
                ("Event", "presence"), ("Accept", "application/pidf+xml"), ("Expires", "300")])
            await lab.hass.loop.sock_sendto(client, request, server)
            subscribed = notified = False
            async with asyncio.timeout(3):
                while not (subscribed and notified):
                    raw, addr = await lab.hass.loop.sock_recvfrom(client, 65535)
                    message = sip.parse_message(raw)
                    assert message.header("Call-ID") == "presence-wire-"+name
                    if message.method == "NOTIFY":
                        assert message.header("Event") == "presence"
                        assert message.header("Content-Type") == "application/pidf+xml"
                        assert basic(message) == "open"
                        assert ElementTree.fromstring(message.body).attrib["entity"] == f"sip:{name}@127.0.0.1"
                        response = sip.build_response(200, "OK", [(key, message.header(key))
                            for key in ("Via", "From", "To", "Call-ID", "CSeq")])
                        await lab.hass.loop.sock_sendto(client, response, addr)
                        notified = True
                    else:
                        assert message.status_code == 200 and message.header("CSeq") == "1 SUBSCRIBE"
                        subscribed = True
