"""DID policy follows the configured SIP field after trunk trust is established."""

import asyncio
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from .voip_phase1_support import audio_format, sdp, sip, sip_listener
from custom_components.voip_stack.inbound_identity import (
    apply_inbound_identity, identity_fields, normalized_did_config,
)


def wire_invite(*, call_id="did-wire", to="001234", custom="009999", body=True):
    sdp = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=DID fixture\r\n"
           "c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 35000 RTP/AVP 0\r\n"
           "a=rtpmap:0 PCMU/8000\r\na=ptime:20\r\na=sendrecv\r\n").encode() if body else b""
    return sip.build_request("INVITE", "sip:registration-account@127.0.0.1", [
        ("Via", "SIP/2.0/UDP 127.0.0.1:5090;branch=z9hG4bK-did"),
        ("From", '"External Caller" <sip:001111@caller.example>;tag=caller'),
        ("To", f'"Public DID" <sip:{to}@provider.example>'),
        ("P-Called-Party-ID", "<tel:+004444>"),
        ("X-Called-Number", custom),
        ("History-Info", "<sip:old-target@provider.example>;index=1"),
        ("Call-ID", call_id), ("CSeq", "1 INVITE"),
        ("Contact", "<sip:caller@127.0.0.1:5090>"),
        *(([("Content-Type", "application/sdp")]) if body else []),
    ], sdp)


def endpoint(config=None, *, trusted=True, on_invite=None, on_offerless_invite=None):
    def identity(request, initial):
        return apply_inbound_identity(initial, config or {}, request)

    return sip_listener.SipUdpEndpoint(
        local_ip="127.0.0.1", local_rtp_port=44000,
        supported_formats=[audio_format.AudioFormat(8000, frame_ms=20)],
        on_invite=on_invite or (lambda _: None), on_offerless_invite=on_offerless_invite,
        trusted_trunk=trusted, trusted_trunk_id="provider-a",
        invite_identity_resolver=identity,
    )


@pytest.mark.parametrize("source,expected,label", [
    ("request_uri", "registration-account", "request_uri"),
    ("to", "001234", "to"),
    ("p_called_party_id", "+004444", "p_called_party_id"),
    ("custom_header", "009999", "header:X-Called-Number"),
    ("fixed", "000123", "fixed"),
])
@pytest.mark.parametrize("offered", [False, True])
def test_each_explicit_source_and_offerless_use_same_parser(source, expected, label, offered):
    owner = endpoint({"trunk_did_source": source, "trunk_did_header": "X-Called-Number", "trunk_did_fixed": "000123"})
    request = sip.parse_message(wire_invite(body=offered))
    invite = (owner._parse_invite if offered else owner._parse_initial_invite)(request, ("127.0.0.1", 5090))
    assert invite is not None
    assert (invite.called_number, invite.called_number_raw, invite.called_number_source) == (expected, expected, label)
    assert (invite.caller_number, invite.caller_name, invite.inbound_trunk_id) == ("001111", "External Caller", "provider-a")
    assert invite.routing_target == "registration-account" and invite.target == "Public DID"
    with pytest.raises(FrozenInstanceError):
        invite.called_number = "changed"


def test_default_retains_request_uri_compatibility_without_guessing_from_to_or_history():
    invite = endpoint()._parse_initial_invite(sip.parse_message(wire_invite()), ("127.0.0.1", 5090))
    assert invite.called_number == "registration-account"
    assert normalized_did_config({})["trunk_did_fallbacks"] == []


def test_fallback_order_and_first_prefix_rule_preserve_zeroes_and_raw_number():
    config = {"trunk_did_source": "custom_header", "trunk_did_header": "X-Missing",
              "trunk_did_fallbacks": ["to", "p_called_party_id"],
              "trunk_did_prefix_rules": [{"prefix": "001", "replacement": "000"},
                                         {"prefix": "00", "replacement": "+"}]}
    invite = endpoint(config)._parse_initial_invite(sip.parse_message(wire_invite()), ("127.0.0.1", 5090))
    assert (invite.called_number, invite.called_number_raw, invite.called_number_source) == ("000234", "001234", "to")


@pytest.mark.parametrize("value", ["not a number", "<sip:a@host>, <sip:b@host>", "<sip:broken@>", ""])
def test_missing_or_invalid_selected_source_has_no_implicit_fallback(value):
    invite = endpoint({"trunk_did_source": "custom_header", "trunk_did_header": "X-Called-Number"})._parse_initial_invite(
        sip.parse_message(wire_invite(custom=value)), ("127.0.0.1", 5090))
    assert invite is not None and invite.called_number == invite.called_number_raw == ""


def test_duplicate_selected_identity_headers_are_ambiguous_and_only_explicit_fallback_is_used():
    request = sip.parse_message(wire_invite())
    request = replace(request, headers=(*request.headers, ("p-called-party-id", "<sip:other@provider.example>")))
    no_fallback = endpoint({"trunk_did_source": "p_called_party_id"})._parse_initial_invite(request, ("127.0.0.1", 5090))
    fallback = endpoint({"trunk_did_source": "p_called_party_id", "trunk_did_fallbacks": ["to"]})._parse_initial_invite(request, ("127.0.0.1", 5090))
    assert no_fallback.called_number == ""
    assert (fallback.called_number, fallback.called_number_source) == ("001234", "to")


def test_untrusted_source_never_reads_provider_identity_or_uses_fixed_did():
    initial = endpoint(trusted=False)._parse_initial_invite(sip.parse_message(wire_invite()), ("127.0.0.1", 5090))
    class ForbiddenHeaders:
        def header_values(self, _name):
            raise AssertionError("Untrusted provider header was consulted")
    assert apply_inbound_identity(initial, {"trunk_did_source": "fixed", "trunk_did_fixed": "0009"}, ForbiddenHeaders()) is initial
    assert not initial.called_number and not initial.inbound_trunk_id


def test_reinvite_keeps_initial_identity_even_when_contact_target_and_provider_headers_change():
    owner = endpoint({"trunk_did_source": "to"})
    request = sip.parse_message(wire_invite())
    first = owner._parse_invite(request, ("127.0.0.1", 5090))
    owner.active_dialogs[first.call_id] = SimpleNamespace(invite=first, codec_policy=first.codec_policy)
    changed = sip.parse_message(wire_invite(to="000777"))
    changed = replace(changed, uri="sip:retargeted-contact@127.0.0.1")
    updated = owner._parse_invite(changed, ("127.0.0.1", 5090))
    assert updated.request_uri.user == "retargeted-contact"
    assert identity_fields(updated) == identity_fields(first)


@pytest.mark.parametrize("config", [
    {"trunk_did_source": "history_info"},
    {"trunk_did_source": "custom_header"},
    {"trunk_did_source": "fixed", "trunk_did_fixed": 123},
    {"trunk_did_fallbacks": "to"},
    {"trunk_did_fallbacks": ["request_uri"]},
    {"trunk_did_fallbacks": ["to", "to"]},
    {"trunk_did_header": "X-Called\r\nAuthorization"},
    {"trunk_did_header": "Authorization"},
    {"trunk_did_prefix_rules": [{"prefix": 0, "replacement": ""}]},
    {"trunk_did_prefix_rules": [{"prefix": "", "replacement": "+"}]},
])
def test_invalid_configuration_is_rejected_before_runtime(config):
    with pytest.raises(ValueError):
        normalized_did_config(config)


async def test_real_udp_invite_exposes_distinct_to_did_and_quiesces_after_error_ack():
    import socket
    seen = asyncio.Queue()
    async def received(invite):
        await seen.put(invite)
        return sip_listener.SipInviteResult(486, "Busy Here")
    owner = endpoint({"trunk_did_source": "to"}, on_invite=received)
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: owner, local_addr=("127.0.0.1", 0))
    source = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    source.bind(("127.0.0.1", 0))
    source.setblocking(False)
    request = sip.parse_message(wire_invite())
    address = transport.get_extra_info("sockname")
    try:
        await loop.sock_sendto(source, wire_invite(), address)
        invite = await asyncio.wait_for(seen.get(), 2)
        assert (invite.request_uri.user, invite.called_number) == ("registration-account", "001234")
        while True:
            raw, _ = await asyncio.wait_for(loop.sock_recvfrom(source, 8192), 2)
            response = sip.parse_message(raw)
            if response.status_code == 486:
                break
        headers = [(key, response.header(key)) for key in ("Via", "From", "To", "Call-ID")]
        headers.append(("CSeq", "1 ACK"))
        await loop.sock_sendto(source, sip.build_request("ACK", request.uri, headers), address)
        async with asyncio.timeout(2):
            while owner.pending_invites or owner.completed_invites:
                await asyncio.sleep(.001)
        assert not owner.active_dialogs
    finally:
        source.close()
        transport.close()
        await owner.wait_closed()


async def test_delayed_ack_keeps_initial_custom_did_with_contact_ruri_and_changed_header():
    sent, initial_invites, accepted = [], [], []
    pcm = audio_format.AudioFormat(8000, frame_ms=20)
    local_offer = sdp.build_offer_directional("127.0.0.1", "127.0.0.1", 44000, [pcm], [pcm])

    async def accept_answer(invite):
        accepted.append(invite)
        return sip_listener.SipInviteResult(200, "OK")

    async def offerless(initial):
        initial_invites.append(initial)
        return sip_listener.UasDelayedOfferPlan(local_offer, accept_answer)

    owner = endpoint({"trunk_did_source": "custom_header", "trunk_did_header": "X-Called-Number"},
                     on_offerless_invite=offerless)
    owner.send_override = lambda raw, _: not sent.append(raw)
    try:
        request = sip.parse_message(wire_invite(body=False))
        await owner._handle_datagram(wire_invite(body=False), ("127.0.0.1", 5090))
        final = next(response for raw in sent if (response := sip.parse_message(raw)).status_code == 200)
        selected = sdp.audio_format_to_rtp(pcm, 96)
        answer = sdp.build_answer_directional("127.0.0.1", "127.0.0.1", 35000, selected, selected,
                                              remote_sdp=final.body).encode()
        headers = [(key, final.header(key)) for key in ("From", "To", "Call-ID")]
        headers += [("Via", "SIP/2.0/UDP 127.0.0.1:5090;branch=z9hG4bK-did-ack"),
                    ("CSeq", "1 ACK"), ("Content-Type", "application/sdp"),
                    ("X-Called-Number", "000changed")]
        await owner._handle_datagram(sip.build_request("ACK", "sip:retargeted-contact@127.0.0.1", headers, answer),
                                     ("127.0.0.1", 5090))
        assert len(initial_invites) == len(accepted) == 1
        assert accepted[0].called_number == "009999"
        assert identity_fields(accepted[0]) == identity_fields(initial_invites[0])
        assert owner.active_dialogs[request.header("Call-ID")].invite.called_number == "009999"
    finally:
        await owner.async_send_bye("did-wire", timeout=.001)
        owner.cancel_request_tasks()
