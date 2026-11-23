"""Authenticated line-scoped feature sync shares canonical DND and event owners."""

import time
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import pytest

from .test_managed_phones_ha import (
    custom_integration_path as custom_integration_path,
    phone_lab as phone_lab,
)
from .test_udp_outbound_flow_ha import (
    lab as lab, register_phone, trunks as trunks, wire_lab as wire_lab,
)

pytestmark = pytest.mark.ha
NS = "http://www.ecma-international.org/standards/ecma-323/csta/ed3"


def request(*, target="alice", device="alice", device_ref=None, nested=False, enabled=None, cseq=1, tag="", expires=300):
    from custom_components.voip_stack.core import sip
    reference = escape(device_ref if device_ref is not None else f"sip:{device}@localhost")
    if nested:
        reference = "<deviceIdentifier>" + reference + "</deviceIdentifier>"
    body = (f'<SetDoNotDisturb xmlns="{NS}"><device>{reference}</device>'
            f'<doNotDisturbOn>{str(enabled).lower()}</doNotDisturbOn></SetDoNotDisturb>').encode() if enabled is not None else b""
    return sip.parse_message(sip.build_request("SUBSCRIBE", f"sip:{target}@localhost", [
        ("From", "<sip:alice@localhost>;tag=subscriber"),
        ("To", f"<sip:{target}@localhost>" + (f";tag={tag}" if tag else "")),
        ("Contact", "<sip:alice@127.0.0.2:5060>"), ("Call-ID", "dnd-subscription"),
        ("CSeq", f"{cseq} SUBSCRIBE"), ("Event", "as-feature-event"),
        ("Accept", "application/x-as-feature-event+xml"), ("Expires", str(expires)),
        ("Content-Type", "application/x-as-feature-event+xml"),
    ], body))


@pytest.fixture
async def features(phone_lab):
    from custom_components.voip_stack.phone_config import _add_phone_subentry, sip_account_phone_data, sync_registry_from_entry
    from custom_components.voip_stack.sip_application import SipApplicationMethods
    from custom_components.voip_stack.sip_registrar import SipAccount, SipRegistration

    lab = phone_lab
    data = sip_account_phone_data({"username": "alice", "password": "fixture-password", "display_name": "Alice", "extension": "301"}, endpoint_id="alice")
    _add_phone_subentry(lab.hass, lab.entry, data=data, title="Alice")
    sync_registry_from_entry(lab.hass, lab.entry)
    lab.registrar.update_accounts([SipAccount("alice", "Alice", "fixture-password"), SipAccount("bob", "Bob", "fixture-bob")])
    lab.registrar.registrations["alice"] = SipRegistration(username="alice", contact_uri="sip:alice@127.0.0.2:5060",
        source_host="127.0.0.2", source_port=5060, transport="UDP", expires_at=time.time() + 300)
    app = SipApplicationMethods(lab.hass, lab.registrar)
    sent = []
    async def sender(_request, _addr, result):
        from custom_components.voip_stack.core import sip
        sent.append(result)
        return sip.parse_message(b"SIP/2.0 200 OK\r\nContent-Length: 0\r\n\r\n")
    yield lab, app, sender, sent
    await app.stop()


@pytest.mark.parametrize("target", ["alice", "301"])
async def test_phone_and_ha_dnd_share_one_state_and_subscription(features, target):
    from custom_components.voip_stack.switch import async_set_endpoint_dnd
    from custom_components.voip_stack.phone_config import phone_subentry_by_endpoint_id

    lab, app, sender, sent = features
    result = await app.handle(request(target=target), ("127.0.0.2", 5060), "UDP", sender)
    assert result.status == 200
    result.follow_up_scheduler()
    await lab.hass.async_block_till_done()
    assert ET.fromstring(sent[-1].follow_up.body).findtext(f"{{{NS}}}doNotDisturbOn") == "false"
    result = await app.handle(request(target=target, enabled=True, cseq=2, tag=result.to_tag), ("127.0.0.2", 5060), "UDP", sender)
    assert result.status == 200
    result.follow_up_scheduler()
    await lab.hass.async_block_till_done()
    assert lab.runtime.endpoints.get("alice").dnd is True
    assert phone_subentry_by_endpoint_id(lab.entry, "alice").data["dnd"] is True
    assert ET.fromstring(sent[-1].follow_up.body).findtext(f"{{{NS}}}doNotDisturbOn") == "true"
    await async_set_endpoint_dnd(lab.hass, "alice", False)
    await lab.hass.async_block_till_done()
    assert ET.fromstring(sent[-1].follow_up.body).findtext(f"{{{NS}}}doNotDisturbOn") == "false"
    count = len(sent)
    await async_set_endpoint_dnd(lab.hass, "alice", False)
    await lab.hass.async_block_till_done()
    assert len(sent) == count
    assert len(app.subscriptions) == 1


@pytest.mark.parametrize("target,device,source", [("bob", "alice", "127.0.0.2"),
    ("alice", "bob", "127.0.0.2"), ("alice", "alice", "127.0.0.9")])
async def test_foreign_line_device_or_source_cannot_set_dnd(features, target, device, source):
    lab, app, sender, sent = features
    result = await app.handle(request(target=target, device=device, enabled=True), (source, 5060), "UDP", sender)
    assert result.status == 403
    assert lab.runtime.endpoints.get("alice").dnd is False
    assert not app.subscriptions and not sent


@pytest.mark.parametrize("device_ref,nested", [("0c383e68475c", False), ("Desk phone 1", True)])
async def test_opaque_device_hint_uses_authenticated_account_without_inventory(features, device_ref, nested):
    from custom_components.voip_stack.switch import async_set_endpoint_dnd
    from custom_components.voip_stack.phone_config import phone_subentry_by_endpoint_id

    lab, app, sender, sent = features
    assert not any(row.subentry_type == "physical_phone" for row in lab.entry.subentries.values())
    result = await app.handle(request(target="301", device_ref=device_ref, nested=nested, enabled=True),
        ("127.0.0.2", 5060), "UDP", sender)
    assert result.status == 200
    result.follow_up_scheduler()
    await lab.hass.async_block_till_done()
    assert lab.runtime.endpoints.get("alice").dnd is True
    assert phone_subentry_by_endpoint_id(lab.entry, "alice").data["dnd"] is True
    # The body hint neither chooses another owner nor changes notification identity.
    notification = ET.fromstring(sent[-1].follow_up.body)
    assert notification.findtext(f"{{{NS}}}device") == "sip:301@localhost"
    await async_set_endpoint_dnd(lab.hass, "alice", False)
    await lab.hass.async_block_till_done()
    assert lab.runtime.endpoints.get("alice").dnd is False
    assert ET.fromstring(sent[-1].follow_up.body).findtext(f"{{{NS}}}doNotDisturbOn") == "false"


@pytest.mark.parametrize("target,source", [("bob", "127.0.0.2"), ("alice", "127.0.0.9")])
async def test_opaque_hint_never_authorizes_a_foreign_target_or_source(features, target, source):
    lab, app, sender, sent = features
    result = await app.handle(request(target=target, device_ref="0c383e68475c", enabled=True),
        (source, 5060), "UDP", sender)
    assert result.status == 403
    assert lab.runtime.endpoints.get("alice").dnd is False
    assert not app.subscriptions and not sent


@pytest.mark.parametrize("device_ref", ["sips:bob@localhost", "bob@localhost"])
async def test_explicit_foreign_SIP_device_reference_stays_forbidden(features, device_ref):
    lab, app, sender, sent = features
    result = await app.handle(request(device_ref=device_ref, enabled=True), ("127.0.0.2", 5060), "UDP", sender)
    assert result.status == 403
    assert lab.runtime.endpoints.get("alice").dnd is False
    assert not app.subscriptions and not sent


async def test_stale_cseq_cannot_change_dnd_and_credential_revoke_retires_owner(features):
    from custom_components.voip_stack.sip_registrar import SipAccount

    lab, app, sender, sent = features
    result = await app.handle(request(enabled=True), ("127.0.0.2", 5060), "UDP", sender)
    assert result.status == 200
    result.follow_up_scheduler()
    await lab.hass.async_block_till_done()
    stale = await app.handle(request(enabled=False, tag=result.to_tag), ("127.0.0.2", 5060), "UDP", sender)
    assert stale.status != 200 and lab.runtime.endpoints.get("alice").dnd is True
    lab.registrar.update_accounts([SipAccount("alice", "Alice", "new-fixture-password")])
    await lab.hass.async_block_till_done()
    assert not app.subscriptions


@pytest.mark.parametrize("body", [b"<broken", b"<!DOCTYPE a><SetDoNotDisturb/>",
    b"<SetDoNotDisturb><doNotDisturbOn>true</doNotDisturbOn></SetDoNotDisturb>",
    f'<SetDoNotDisturb xmlns="{NS}"><doNotDisturbOn>toggle</doNotDisturbOn></SetDoNotDisturb>'.encode(),
    *[f'<SetDoNotDisturb xmlns="{NS}"><device>{value}</device><doNotDisturbOn>true</doNotDisturbOn></SetDoNotDisturb>'.encode()
      for value in ("", "Desk\nphone", "x" * 257, "é" * 129)],
    f'<SetDoNotDisturb xmlns="{NS}"><device>one</device><device>two</device><doNotDisturbOn>true</doNotDisturbOn></SetDoNotDisturb>'.encode(),
    f'<SetDoNotDisturb xmlns="{NS}"><device><deviceIdentifier>one</deviceIdentifier><deviceIdentifier>two</deviceIdentifier></device><doNotDisturbOn>true</doNotDisturbOn></SetDoNotDisturb>'.encode(),
    f'<SetDoNotDisturb xmlns="{NS}"><doNotDisturbOn>true</doNotDisturbOn><doNotDisturbOn>false</doNotDisturbOn></SetDoNotDisturb>'.encode()])
async def test_malformed_or_unsupported_feature_never_creates_owner_or_changes_state(features, body):
    from dataclasses import replace
    lab, app, sender, sent = features
    result = await app.handle(replace(request(), body=body), ("127.0.0.2", 5060), "UDP", sender)
    assert result.status == 400
    assert lab.runtime.endpoints.get("alice").dnd is False
    assert not app.subscriptions and not sent


async def test_unsubscribe_releases_feature_observer_and_owned_timer(features):
    lab, app, sender, sent = features
    created = await app.handle(request(), ("127.0.0.2", 5060), "UDP", sender)
    assert app.feature_unsubscribe is not None
    result = await app.handle(request(cseq=2, tag=created.to_tag, expires=0), ("127.0.0.2", 5060), "UDP", sender)
    assert result.status == 200 and not app.subscriptions
    assert app.feature_unsubscribe is None and not app.subscription_timers
    result.follow_up_scheduler()
    await lab.hass.async_block_till_done()
    assert dict(sent[-1].follow_up.headers)["Subscription-State"].startswith("terminated")


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("target_user", ["outbound-phone", "301"])
@pytest.mark.parametrize("device_reference", ["sip:outbound-phone@127.0.0.1", "0c383e68475c", "Desk phone 1"])
async def test_digest_registered_udp_phone_syncs_dnd_in_both_directions_on_real_listener(wire_lab, target_user, device_reference):
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.phone_config import _add_phone_subentry, sip_account_phone_data, sync_registry_from_entry
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.switch import async_set_endpoint_dnd

    wire = wire_lab
    entry = config_entry(wire.lab.hass)
    assert not any(row.subentry_type == "physical_phone" for row in entry.subentries.values())
    data = sip_account_phone_data({"username": "outbound-phone", "password": "fixture-only-password",
        "display_name": "Feature phone", "extension": "301"}, endpoint_id="outbound-phone")
    _add_phone_subentry(wire.lab.hass, entry, data=data, title="Feature phone")
    sync_registry_from_entry(wire.lab.hass, entry)
    await register_phone(wire, outbound=False, discover=False)
    phone, user, call_id = wire.a, "outbound-phone", "feature-sync-wire"
    target = f"sip:{target_user}@127.0.0.1:{wire.listener[1]}"
    def send(sequence, *, tag="", value=None, expires=300):
        headers = [("Via", f"SIP/2.0/UDP {phone.address[0]}:{phone.address[1]};branch=z9hG4bKfeature{sequence};rport"),
            ("From", f"<sip:{user}@127.0.0.1>;tag=feature-phone"),
            ("To", f"<{target}>" + (f";tag={tag}" if tag else "")),
            ("Contact", f"<sip:{user}@{phone.address[0]}:{phone.address[1]}>"),
            ("Call-ID", call_id), ("CSeq", f"{sequence} SUBSCRIBE"),
            ("Event", "as-feature-event"), ("Expires", str(expires)),
            ("Content-Type", "application/x-as-feature-event+xml")]
        body = (f'<SetDoNotDisturb xmlns="{NS}"><device>{device_reference}</device>'
                f'<doNotDisturbOn>{str(value).lower()}</doNotDisturbOn></SetDoNotDisturb>').encode() if value is not None else b""
        phone.transport.sendto(sip.build_request("SUBSCRIBE", target, headers, body), wire.listener)
    async def transaction(expected):
        import asyncio
        response, notified = None, False
        async with asyncio.timeout(3):
            while response is None or not notified:
                message, address = await phone.messages.get()
                if message.header("Call-ID") != call_id:
                    continue
                if message.method == "NOTIFY":
                    assert message.header("Content-Type") == "application/x-as-feature-event+xml"
                    assert ET.fromstring(message.body).findtext(f"{{{NS}}}doNotDisturbOn") == expected
                    phone.respond(message, address)
                    notified = True
                elif not message.is_request:
                    assert message.status_code == 200
                    response = message
        return response
    send(1)
    accepted = await transaction("false")
    send(2, tag=sip.extract_tag(accepted.header("To")), value=True)
    await transaction("true")
    assert wire.lab.runtime.endpoints.get("outbound-phone").dnd is True
    await async_set_endpoint_dnd(wire.lab.hass, "outbound-phone", False)
    notification, address = await phone.receive("NOTIFY")
    assert ET.fromstring(notification.body).findtext(f"{{{NS}}}doNotDisturbOn") == "false"
    phone.respond(notification, address)
    assert wire.lab.runtime.endpoints.get("outbound-phone").dnd is False
    send(3, tag=sip.extract_tag(accepted.header("To")), value=False)
    await transaction("false")
    assert wire.lab.runtime.endpoints.get("outbound-phone").dnd is False
    send(4, tag=sip.extract_tag(accepted.header("To")), expires=0)
    await transaction("false")
