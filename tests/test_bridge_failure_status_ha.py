"""Remote bridge failures retain wire status without exporting auth/redirect context.

Local account 302 forwarding is covered separately by test_local_redirect_ha.
"""

import asyncio
import socket

import pytest

from custom_components.voip_stack.config import normalized_trunk_config
from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.sip_client import SipCallClient
from custom_components.voip_stack.sip_trunk import SipTrunkClient
from custom_components.voip_stack.trunk_runtime import trunk_client_config

from .test_browser_logical_availability_ha import resources
from .test_trunk_calls_ha import lab as lab, trunks as trunks
from custom_components.voip_stack.sip_udp_io import SipDatagramQueueProtocol

pytestmark = pytest.mark.ha


class ObservedCallerProtocol(SipDatagramQueueProtocol):
    """Observe source-side INVITE responses independently of client state."""

    def __init__(self, queue):
        super().__init__(queue)
        self.invite_responses = []

    def datagram_received(self, raw, address):
        message = sip.parse_message(raw)
        if message.is_response and message.header("CSeq").endswith(" INVITE"):
            self.invite_responses.append(message)
        super().datagram_received(raw, address)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("provisional", [False, True], ids=["immediate", "after-ringing"])
@pytest.mark.parametrize(("remote_status", "remote_reason", "expected_status"), [
    (300, "Multiple Choices", 480),
    (302, "Moved Temporarily", 480),
    (380, "Alternative Service", 480),
    (401, "Unauthorized", 480),
    (407, "Proxy Authentication Required", 480),
    (403, "Forbidden", 403),
    (404, "Not Found", 404),
    (486, "Busy Here", 486),
    (488, "Not Acceptable Here", 488),
    (500, "Server Internal Error", 500),
    (503, "Service Unavailable", 503),
    (504, "Server Time-out", 504),
    (603, "Decline", 603),
])
async def test_bridge_failure_status_on_caller_wire(
    trunks, monkeypatch, provisional, remote_status, remote_reason, expected_status
):
    """Exercise both owners: returned failure and failure after a deferred 180."""
    await _failure_on_caller_wire(
        trunks, monkeypatch, provisional, remote_status, remote_reason, expected_status
    )


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize(("remote_status", "remote_reason", "expected_status"), [
    (302, "Moved Temporarily", 480),
    (401, "Unauthorized", 480),
    (407, "Proxy Authentication Required", 480),
    (486, "Busy Here", 486),
    (503, "Service Unavailable", 480),
])
async def test_direct_trunk_group_keeps_aggregate_failure_on_caller_wire(
    trunks, monkeypatch, remote_status, remote_reason, expected_status
):
    """The existing group reducer must not export its branches' 3xx/auth statuses."""
    await _failure_on_caller_wire(
        trunks, monkeypatch, True, remote_status, remote_reason, expected_status,
        group=True,
    )


async def _failure_on_caller_wire(
    lab, monkeypatch, provisional, remote_status, remote_reason, expected_status,
    *, group=False,
):
    targets = lab.phones if group else lab.phones[:1]
    if group:
        lab.roster.append(RosterEntry("Reception", metadata={
            "group_type": "ring", "members": ["bedroom0", "bedroom1"],
            "ring_timeout": 5, "step_timeout": .1,
        }))
        lab.runtime.directory.replace(lab.roster)

    def reject(target):
        request = target.last_request
        headers = [(key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
        headers.append(("To", request.header("To") + ";tag=bedroom"))
        if 300 <= remote_status < 400:
            headers.append(("Contact", "<sip:other@192.0.2.42>"))
        elif remote_status in {401, 407}:
            header = "WWW-Authenticate" if remote_status == 401 else "Proxy-Authenticate"
            headers.append((header, 'Digest realm="destination", nonce="fixture-only", algorithm=MD5'))
        target.transport.sendto(
            sip.build_response(remote_status, remote_reason, headers), target.remote
        )

    for target in targets:
        original_receive = target.datagram_received

        def receive(raw, address, target=target, original_receive=original_receive):
            request = sip.parse_message(raw)
            if request.method != "INVITE":
                original_receive(raw, address)
                return
            target.requests.append(request)
            target.invites.append(request)
            target.last_request, target.remote = request, address
            target.invited.set()
            if provisional:
                target.respond(request, address, 180)
            else:
                reject(target)

        monkeypatch.setattr(target, "datagram_received", receive)
    listener = ("127.0.0.1", lab.runtime.transport_config["sip_port"])
    remote_uri = f"sip:failure-target@127.0.0.1:{lab.phones[0].transport.get_extra_info('sockname')[1]}"
    observed_routes = []
    from custom_components.voip_stack import invite_router
    original_bridge = invite_router.route_sip_bridge

    async def observed_bridge(**kwargs):
        observed_routes.append((kwargs["target_endpoint"], kwargs["bridge_to_trunk"], kwargs["decision"]))
        return await original_bridge(**kwargs)

    monkeypatch.setattr(invite_router, "route_sip_bridge", observed_bridge)
    config = normalized_trunk_config({
        "trunk_enabled": True, "trunk_register": False, "trunk_options_interval": 30,
        "trunk_server": "127.0.0.42", "trunk_allowed_ips": ["127.0.0.42/32"],
        "trunk_inbound_default_target": "Reception" if group else remote_uri,
    })
    provider = SipTrunkClient(
        config=trunk_client_config(config), local_ip=listener[0],
        local_sip_port=listener[1], trunk_id="failure-provider",
    )
    lab.runtime.trunk_configs["failure-provider"] = config
    lab.calls.adopt_component("trunk:failure-provider", provider, closer=provider.stop)
    media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    media.bind(("127.0.0.42", 0))
    client = SipCallClient(
        local_ip="127.0.0.42", local_name="Independent caller", local_uri_user="caller",
        local_sip_port=0, local_rtp_port=media.getsockname()[1],
        supported_formats=[AudioFormat(16000, frame_ms=20)],
    )
    try:
        client.transport, client.protocol = await lab.hass.loop.create_datagram_endpoint(
            lambda: ObservedCallerProtocol(client.queue), local_addr=("127.0.0.42", 0)
        )
        client.local_sip_port = client.transport.get_extra_info("sockname")[1]
        result = await client.invite(
            target="450", remote_host=listener[0], remote_sip_port=listener[1], timeout=3
        )
        assert all(target.invites for target in targets), (result, [(message.status_code, message.reason) for message in client.protocol.invite_responses])
        await asyncio.wait_for(asyncio.gather(*(target.invited.wait() for target in targets)), 3)
        if provisional:
            assert result == "ringing"
            assert not any(message.status_code >= 200 for message in client.protocol.invite_responses)
            for target in targets:
                reject(target)
            result = await client.wait_for_final(timeout=3)
        assert result not in {"ringing", "in_call", "timeout"}
        responses = client.protocol.invite_responses
        assert any(message.status_code == 180 for message in responses) == provisional
        finals = [message for message in responses if message.status_code >= 200]
        assert {message.status_code for message in finals} == {expected_status}
        if not group:
            assert len(observed_routes) == 1
            target_endpoint, bridge_to_trunk, decision = observed_routes[0]
            assert target_endpoint is None and bridge_to_trunk is False
            assert decision.sip_uri == remote_uri and decision.entry is None
        assert client.last_sip_status_code == expected_status
        if expected_status == 480:
            assert {message.reason for message in finals} == {"Temporarily Unavailable"}
        elif not group:
            assert {message.reason for message in finals} == {remote_reason}
        assert all(not message.header("WWW-Authenticate") for message in finals)
        assert all(not message.header("Proxy-Authenticate") for message in finals)
        assert all("192.0.2.42" not in message.header("Contact") for message in finals)
        if not group:
            assert not lab.phones[1].invites
        async with asyncio.timeout(3):
            while not all(any(request.method == "ACK" for request in target.requests) for target in targets):
                await asyncio.sleep(0)
            while lab.calls.sessions:
                await asyncio.sleep(0)
        assert resources(lab)["call_scoped_quiescent"]
        assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)
    finally:
        await client.close()
        media.close()
