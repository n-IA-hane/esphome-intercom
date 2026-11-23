"""TCP subscriber ownership, flow loss, lease expiry and fresh full snapshots."""

import asyncio
import time
from xml.etree import ElementTree

import pytest

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.call_projection import publish_bridge_projection
from custom_components.voip_stack.sip_application import SipApplicationMethods
from custom_components.voip_stack.sip_endpoint import SipEndpointManager
from custom_components.voip_stack.sip_registrar import SipAccount, SipRegistrar, SipRegistration
from .test_automation_originate_ha import lab as lab
from .test_notify_transport_ha import NS, read_record

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("outbound", [False, True], ids=["ordinary-contact", "authenticated-outbound"])
async def test_closed_tcp_subscription_recovers_only_by_its_route_and_renews_after_expiry(lab, outbound):
    import pytest_socket
    pytest_socket.socket_allow_hosts(["127.0.0.1", "127.0.0.3"])
    observed, responses = asyncio.Queue(), asyncio.Queue()
    contact_writers, subscriber_writers, tasks = [], [], set()
    registrar = SipRegistrar(enabled=True, accounts=[SipAccount("watcher", "Watcher", "test-only")],
                             local_ip="127.0.0.1", local_sip_port=5060)
    methods = SipApplicationMethods(lab.hass, registrar)
    lab.calls.bind_endpoint_registry(lab.runtime.endpoints)
    lab.runtime.endpoints.update("bedroom0", extension="101")

    async def receive(reader, writer, origin):
        tasks.add(asyncio.current_task())
        try:
            while True:
                raw = await read_record(reader)
                message = sip.parse_message(raw)
                if message.is_response:
                    responses.put_nowait(message)
                    continue
                assert message.method == "NOTIFY"
                headers = [(key, message.header(key)) for key in ("Via", "From", "To", "Call-ID", "CSeq")]
                headers.append(("Contact", contact_uri))
                writer.write(sip.build_response(200, "OK", headers))
                await writer.drain()
                observed.put_nowait((message, origin))
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            tasks.discard(asyncio.current_task())
            writer.close()
            await writer.wait_closed()

    async def contact(reader, writer):
        contact_writers.append(writer)
        await receive(reader, writer, "contact")

    server = await asyncio.start_server(contact, "127.0.0.3", 0)
    contact_port = server.sockets[0].getsockname()[1]
    contact_uri = f"<sip:watcher@127.0.0.3:{contact_port};transport=tcp>"
    manager = SipEndpointManager(host="127.0.0.1", port=0, local_ip="127.0.0.1", local_rtp_port=41000,
        supported_formats=[AudioFormat(16000)], on_invite=lambda _: None, on_request=methods.handle, request_router=methods)
    assert await manager.start()
    target = manager.tcp_server.server.sockets[0].getsockname()
    current_writer = None

    async def open_subscriber():
        nonlocal current_writer
        reader, current_writer = await asyncio.open_connection(*target, local_addr=("127.0.0.3", 0))
        subscriber_writers.append(current_writer)
        source = current_writer.get_extra_info("sockname")
        registrar.registrations["watcher"] = SipRegistration(username="watcher",
            contact_uri=f"sip:watcher@127.0.0.3:{source[1]};transport=tcp",
            advertised_contact_uri=contact_uri[1:-1], source_host=source[0], source_port=source[1],
            transport="TCP", expires_at=time.time() + 300, outbound=outbound,
            instance_id="urn:uuid:10000000-0000-4000-8000-000000000002" if outbound else "", reg_id=1 if outbound else 0)
        task = asyncio.create_task(receive(reader, current_writer, "registered-flow"))
        tasks.add(task)
        return source

    async def subscribe(call_id, *, tag="", cseq=1, expires=1):
        source = current_writer.get_extra_info("sockname")
        request = sip.build_request("SUBSCRIBE", "sip:101@127.0.0.1", [
            ("Via", f"SIP/2.0/TCP 127.0.0.3:{source[1]};branch=z9hG4bK-{call_id}-{cseq}"),
            ("From", f"<sip:watcher@127.0.0.3>;tag={call_id}"),
            ("To", "<sip:101@127.0.0.1>" + (f";tag={tag}" if tag else "")),
            ("Contact", contact_uri), ("Call-ID", call_id), ("CSeq", f"{cseq} SUBSCRIBE"),
            ("Event", "dialog"), ("Accept", "application/dialog-info+xml"), ("Expires", str(expires))])
        current_writer.write(request)
        await current_writer.drain()
        accepted = await asyncio.wait_for(responses.get(), 2)
        assert accepted.status_code == 200
        return accepted

    async def notification():
        return await asyncio.wait_for(observed.get(), 2)

    async def drained():
        async with asyncio.timeout(2):
            while methods.notifiers:
                await asyncio.sleep(.001)

    try:
        source = await open_subscriber()
        initial_accepted = await subscribe("initial", expires=10)
        initial, origin = await notification()
        assert origin == ("registered-flow" if outbound else "contact")
        assert not ElementTree.fromstring(initial.body).findall("d:dialog", NS)
        assert len(contact_writers) == int(not outbound)
        # Close both the receiving path and the original SUBSCRIBE connection.
        for writer in (*contact_writers, current_writer):
            writer.close()
            await writer.wait_closed()
        async with asyncio.timeout(2):
            while source in manager.tcp_server._writers or ("127.0.0.3", contact_port) in manager.tcp_server._writers:
                await asyncio.sleep(.001)
        call = lab.calls.upsert("tcp-observed-call", state="connecting", owner="router", route_kind="sip",
            source_endpoint_id="bedroom0", dest_endpoint_id="bedroom1")
        lab.calls.claim_endpoint(call.call_id, "bedroom0", role="source")
        publish_bridge_projection(lab.hass, call)
        if outbound:
            await drained()
            assert observed.empty() and not contact_writers
            assert not methods.subscriptions and not methods.subscription_timers
            assert not methods.task_failures
        else:
            active, origin = await notification()
            assert origin == "contact" and len(contact_writers) == 2
            assert ElementTree.fromstring(active.body).find("d:dialog/d:state", NS).text == "proceeding"
        await open_subscriber()
        # An expired/lost flow creates a new usage with the current full state.
        accepted = await subscribe("recovered")
        full, origin = await notification()
        root = ElementTree.fromstring(full.body)
        assert root.attrib["state"] == "full" and root.attrib["version"] == "0"
        assert root.find("d:dialog/d:state", NS).text == "proceeding"
        if not outbound:
            # Retire the first ordinary usage through its original dialog.
            await subscribe("initial", tag=sip.extract_tag(initial_accepted.header("To")), cseq=2, expires=0)
            retired, _ = await notification()
            assert retired.header("Subscription-State").startswith("terminated")
        await lab.calls.terminate_call_wait(call.call_id, reason="remote_hangup")
        publish_bridge_projection(lab.hass, call, intent=call.termination_intent)
        idle, _ = await notification()
        assert not ElementTree.fromstring(idle.body).findall("d:dialog", NS)
        expired, _ = await notification()
        assert expired.header("Subscription-State").startswith("terminated;reason=timeout")
        await drained()
        assert not methods.subscriptions and not methods.subscription_timers
        accepted = await subscribe("after-expiry", expires=10)
        after_expiry, _ = await notification()
        root = ElementTree.fromstring(after_expiry.body)
        assert root.attrib["state"] == "full" and root.attrib["version"] == "0" and not root.findall("d:dialog", NS)
        await subscribe("after-expiry", tag=sip.extract_tag(accepted.header("To")), cseq=2, expires=0)
        terminal, _ = await notification()
        assert terminal.header("Subscription-State").startswith("terminated")
        await drained()
        assert not methods.subscriptions and not methods.notifiers and not methods.subscription_timers
    finally:
        await methods.stop()
        await manager.stop()
        for writer in (*subscriber_writers, *contact_writers):
            writer.close()
        await asyncio.gather(*tasks, return_exceptions=True)
        server.close()
        await server.wait_closed()
    assert not manager.tcp_server and not methods.tasks
