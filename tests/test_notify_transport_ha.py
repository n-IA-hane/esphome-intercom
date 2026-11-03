"""A UDP subscriber observes application notifications on real UDP/TCP peers."""

import asyncio
import time
from xml.etree import ElementTree

import pytest

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.core.sip_auth import verify_digest_authorization
from custom_components.voip_stack.call_projection import publish_bridge_projection
from custom_components.voip_stack.sip_application import SipApplicationMethods
from custom_components.voip_stack.sip_endpoint import SipEndpointManager
from custom_components.voip_stack.sip_registrar import SipAccount, SipRegistrar, SipRegistration
from .test_automation_originate_ha import lab as lab

pytestmark = pytest.mark.ha
NS = {"d": "urn:ietf:params:xml:ns:dialog-info"}


async def read_record(reader):
    head = await reader.readuntil(b"\r\n\r\n")
    length = next(int(line.split(b":", 1)[1]) for line in head.split(b"\r\n")
                  if line.lower().startswith(b"content-length:"))
    return head + await reader.readexactly(length)


@pytest.mark.parametrize("challenge,close_before_200", [
    pytest.param(401, False, id="401"), pytest.param(407, False, id="407"),
    pytest.param(401, True, id="401-close-before-200"),
])
async def test_udp_subscription_large_dialog_notify_uses_owned_tcp_and_keeps_target(lab, challenge, close_before_200):
    import pytest_socket
    pytest_socket.socket_allow_hosts(["127.0.0.1", "127.0.0.3"])
    loop = asyncio.get_running_loop()
    observed, responses = asyncio.Queue(), asyncio.Queue()
    writers, handlers = [], set()
    challenged = False
    dropped = []
    watcher = "watcher"

    def response(request, transport, *, status=200, contact=False):
        headers = [(key, request.header(key)) for key in ("Via", "From", "To", "Call-ID", "CSeq")]
        if status in (401, 407):
            headers.append(("WWW-Authenticate" if status == 401 else "Proxy-Authenticate",
                            'Digest realm="watcher", nonce="independent", qop="auth"'))
        if contact:
            headers.append(("Contact", f"<sip:{watcher}@127.0.0.3:{port};transport=tcp>"))
        return sip.build_response(status, "OK" if status == 200 else "Authentication required", headers)

    async def stream(reader, writer):
        nonlocal challenged
        handlers.add(asyncio.current_task()); writers.append(writer)
        try:
            while True:
                raw = await read_record(reader)
                request = sip.parse_message(raw)
                if b"confirmed" in request.body and not challenged:
                    challenged = True
                    writer.write(response(request, "TCP", status=challenge))
                else:
                    if b"confirmed" in request.body:
                        verify_digest_authorization(
                            authorization_header=request.header("Authorization" if challenge == 401 else "Proxy-Authorization"),
                            username=watcher, password="test-only", method="NOTIFY",
                            uri=request.uri, realm="watcher", nonce="independent")
                        if close_before_200 and not dropped:
                            dropped.append(raw)
                            await observed.put((request, "TCP_CLOSED_BEFORE_200", raw))
                            return
                    writer.write(response(request, "TCP", contact=True))
                await writer.drain()
                await observed.put((request, "TCP", raw))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            handlers.discard(asyncio.current_task())
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(stream, "127.0.0.3", 0)
    port = server.sockets[0].getsockname()[1]

    class Datagram(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport

        def datagram_received(self, raw, address):
            message = sip.parse_message(raw)
            if message.is_response:
                responses.put_nowait(message)
            else:
                self.transport.sendto(response(message, "UDP"), address)
                observed.put_nowait((message, "UDP", raw))

    udp, peer = await loop.create_datagram_endpoint(Datagram, local_addr=("127.0.0.3", port))
    registrar = SipRegistrar(enabled=True, accounts=[SipAccount(watcher, "Watcher", "test-only")],
                             local_ip="127.0.0.1", local_sip_port=5060)
    registrar.registrations[watcher] = SipRegistration(username=watcher,
        contact_uri=f"sip:{watcher}@127.0.0.3:{port};transport=udp", source_host="127.0.0.3",
        source_port=port, transport="UDP", expires_at=time.time() + 300)
    methods = SipApplicationMethods(lab.hass, registrar)
    lab.calls.bind_endpoint_registry(lab.runtime.endpoints)
    lab.runtime.endpoints.update("sip:bedroom0", extension="101")
    manager = SipEndpointManager(host="127.0.0.1", port=0, local_ip="127.0.0.1",
        local_rtp_port=41000, supported_formats=[AudioFormat(16000)], on_invite=lambda _: None,
        on_request=methods.handle, request_router=methods)
    assert await manager.start()
    owned_tcp = manager.tcp_server
    target = manager.udp_server.transport.get_extra_info("sockname")
    headers = [("Via", f"SIP/2.0/UDP 127.0.0.3:{port};branch=z9hG4bK-subscribe"),
        ("From", f"<sip:{watcher}@127.0.0.3>;tag=watcher"), ("To", "<sip:101@127.0.0.1>"),
        ("Contact", f"<sip:{watcher}@127.0.0.3:{port};transport=udp>"),
        ("Call-ID", "notify-transport-subscription"), ("CSeq", "1 SUBSCRIBE"),
        ("Event", "dialog"), ("Expires", "300"), ("Accept", "application/dialog-info+xml")]

    async def next_notify():
        return await asyncio.wait_for(observed.get(), 2)

    try:
        udp.sendto(sip.build_request("SUBSCRIBE", "sip:101@127.0.0.1", headers), target)
        accepted = await asyncio.wait_for(responses.get(), 2)
        assert accepted.status_code == 200
        initial, initial_transport, _ = await next_notify()
        assert initial_transport == "UDP"
        assert not ElementTree.fromstring(initial.body).findall("d:dialog", NS)
        session = lab.calls.upsert("caller-" + "x" * 180 + "@independent.test", state="connecting",
            owner="router", route_kind="sip", source_endpoint_id="sip:bedroom0")
        lab.calls.claim_endpoint(session.call_id, "sip:bedroom0", role="source")
        publish_bridge_projection(lab.hass, session)
        trying, trying_transport, trying_raw = await next_notify()
        assert ElementTree.fromstring(trying.body).find("d:dialog/d:state", NS).text == "proceeding"
        assert len(trying_raw) > 1300
        assert trying_transport == "TCP"
        lab.calls.transition(session.call_id, state="in_call")
        publish_bridge_projection(lab.hass, session)
        challenged_request, challenged_transport, _ = await next_notify()
        confirmed, confirmed_transport, _ = await next_notify()
        if close_before_200:
            assert confirmed_transport == "TCP_CLOSED_BEFORE_200"
            confirmed, confirmed_transport, retried_raw = await next_notify()
            assert retried_raw == dropped[0]
            assert len(writers) == 2
        assert challenged_transport == confirmed_transport == "TCP"
        assert ElementTree.fromstring(confirmed.body).find("d:dialog/d:state", NS).text == "confirmed"
        assert confirmed.uri == challenged_request.uri
        assert sip.parse_cseq(confirmed.header("CSeq")).number > sip.parse_cseq(challenged_request.header("CSeq")).number
        assert confirmed.header("Authorization" if challenge == 401 else "Proxy-Authorization")
        assert "SIP/2.0/TCP" in confirmed.header("Via")
        assert "transport=tcp" in confirmed.header("Contact")
        await lab.calls.terminate_call_wait(session.call_id, reason="remote_hangup")
        publish_bridge_projection(lab.hass, session, intent=session.termination_intent)
        idle, idle_transport, idle_raw = await next_notify()
        assert len(idle_raw) < 1300
        assert idle_transport == "TCP"
        assert not ElementTree.fromstring(idle.body).findall("d:dialog", NS)
        assert [int(ElementTree.fromstring(message.body).attrib["version"])
                for message in (initial, trying, challenged_request, confirmed, idle)] == [0, 1, 2, 2, 3]
        assert len(methods.subscriptions) == 1
        await asyncio.wait_for(asyncio.gather(*(s.notify_task for s in methods.notifiers.values())), 1)
        assert not methods.notifiers
        for cseq, expires in ((2, 300), (3, 0)):
            refresh = [(key, value) for key, value in headers if key not in {"Via", "To", "CSeq", "Expires"}]
            refresh += [("Via", f"SIP/2.0/UDP 127.0.0.3:{port};branch=z9hG4bK-refresh{cseq}"),
                        ("To", accepted.header("To")), ("CSeq", f"{cseq} SUBSCRIBE"), ("Expires", str(expires))]
            udp.sendto(sip.build_request("SUBSCRIBE", "sip:101@127.0.0.1", refresh), target)
            refreshed = await asyncio.wait_for(responses.get(), 2)
            assert refreshed.status_code == 200 and refreshed.header("To") == accepted.header("To")
            notification, _, _ = await next_notify()
            assert notification.header("Subscription-State").startswith("active" if expires else "terminated")
            assert int(ElementTree.fromstring(notification.body).attrib["version"]) == cseq + 2
            await asyncio.wait_for(asyncio.gather(*(s.notify_task for s in methods.notifiers.values())), 1)
            assert len(methods.subscriptions) == bool(expires)
        assert not methods.notifiers
    finally:
        await methods.stop()
        await manager.stop()
        udp.close()
        for writer in writers:
            writer.close()
        await asyncio.gather(*handlers, return_exceptions=True)
        server.close()
        await server.wait_closed()
    assert not methods.tasks and not methods.subscriptions and not methods.notifiers
    assert not manager.servers
    assert not owned_tcp._client_tasks and not owned_tcp._writers and not owned_tcp._tcp_writers
