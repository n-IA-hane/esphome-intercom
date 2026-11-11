"""Assist forwards its real SIP call, retaining its consumer on no answer."""

import asyncio
import struct
import socket
from types import SimpleNamespace

import pytest

from custom_components.voip_stack import assist_runtime
from custom_components.voip_stack.router import RouteAction, RouteDecision
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.core import rtp, sdp
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from .test_queue_call_ha import queue_lab as queue_lab, lab as lab, trunks as trunks
from .test_sip_capabilities_ha import wait_until

pytestmark = pytest.mark.ha


async def enter_assist(lab, monkeypatch):
    started = asyncio.Event()
    async def consume(conversation):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(assist_runtime.AssistConversation, "_pipeline_loop", consume)
    lab.runtime.assist_config.update(assist_name="Assistant", assist_pipeline="preferred")
    lab.roster.append(RosterEntry("Assistant", extension="403", metadata={"virtual_endpoint": "assist_pipeline"}))
    client, audio = await lab.caller()
    result = await client.invite(target="403", remote_host="127.0.0.1", remote_sip_port=lab.runtime.transport_config["sip_port"], timeout=3)
    if result == "ringing":
        result = await client.wait_for_final(timeout=3)
    assert result == "in_call"
    await asyncio.wait_for(started.wait(), 2)
    return client, audio


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_assist_noanswer_retains_same_consumer_context_media_and_call(queue_lab, monkeypatch):
    lab = queue_lab
    client, _audio = await enter_assist(lab, monkeypatch)
    registry = lab.calls
    call_id = client.dialog_ids.call_id
    session = registry.get_session(call_id)
    application = registry.resource_for(call_id, "assist_application")
    media, conversation = application.media, application.conversation
    consumer, context = media._consumer_task, conversation.context
    lab.phones[0].auto_answer = False
    entry = RosterEntry("attempt", name="Reception", ha_bridge=True,
                        metadata={"group_type": "ring", "members": ["bedroom0"]})
    await registry.forward_call(call_id=call_id, destination="Reception", on_failure="resume",
                                _resolved_decision=RouteDecision(RouteAction.GROUP, target="Reception", entry=entry), _ring_timeout=0.2)
    task = registry.task_for(call_id, "forward")
    if task is not None:
        await asyncio.shield(task)
    assert session.live and session.owner == "assist"
    assert registry.resource_for(call_id, "assist_application") is application
    assert application.media is media and application.conversation is conversation
    assert conversation.context is context and not conversation.detached
    assert media._consumer_task is consumer and not consumer.done() and not media.closed.is_set()
    assert any(request.method == "CANCEL" for request in lab.phones[0].requests)
    await client.terminate()
    await wait_until(lambda: registry.get_session(call_id) is None)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_assist_successful_forward_retires_only_old_consumer_and_commits_bridge(queue_lab, monkeypatch):
    lab = queue_lab
    client, audio = await enter_assist(lab, monkeypatch)
    registry, call_id = lab.calls, client.dialog_ids.call_id
    application = registry.resource_for(call_id, "assist_application")
    media, consumer = application.media, application.media._consumer_task
    context = application.conversation.context
    from homeassistant.helpers.llm import LLMContext, ToolInput
    from custom_components.voip_stack.assist_call_tools import current_call_tools
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    lab.runtime.directory = PhoneDirectory(lab.roster)
    llm_context = LLMContext(platform="conversation", context=context, language="en", assistant="conversation", device_id=None)
    transfer = next(tool for tool in current_call_tools(lab.hass, context) if tool.action == "transfer")
    result = await transfer.async_call(lab.hass, ToolInput(transfer.name, {"destination": "bedroom0"}), llm_context)
    assert result == {"status": "connected"}
    session = registry.get_session(call_id)
    assert session.live and session.owner == "bridge"
    assert session.metadata["dest_endpoint_id"] == "sip:bedroom0"
    assert registry.resource_for(call_id, "assist_application") is None
    assert consumer.done() and media.closed.is_set()
    assert application.conversation.context is context and application.conversation.detached
    assert registry.resource_for(call_id, "relay") is not media
    assert len(lab.phones[0].invites) == 1
    dialog = client.dialog
    sample_count = dialog.send_format.audio_format.nominal_frame_bytes // 2
    for index in range(5):
        raw = rtp.build_packet(rtp.RtpPacket(dialog.send_format.payload_type, index,
            index * dialog.send_format.rtp_timestamp_step, 889, struct.pack(f"!{sample_count}h", *([3210] * sample_count))))
        audio.sendto(raw, (dialog.remote_rtp_host, dialog.remote_rtp_port))
        await asyncio.sleep(float(dialog.send_format.frame_ms) / 1000)
    async with asyncio.timeout(2):
        while True:
            raw, _ = await lab.phones[0].audio.get()
            payload = rtp.parse_packet(raw).payload
            if any(payload):
                assert set(struct.unpack(f"!{len(payload) // 2}h", payload)) == {3210}
                break
    target_offer = sdp.parse_sdp(lab.phones[0].invites[0].body)
    for index in range(5):
        packet = rtp.RtpPacket(lab.phones[0].payload_type, index, index * 160, 890, struct.pack("!160h", *([-1230] * 160)))
        lab.phones[0].rtp_socket.sendto(rtp.build_packet(packet), (target_offer["connection_ip"], target_offer["media_port"]))
        await asyncio.sleep(0.01)
    async with asyncio.timeout(2):
        while True:
            raw = await lab.hass.loop.sock_recv(audio, 4096)
            payload = rtp.parse_packet(raw).payload
            if any(payload):
                assert set(struct.unpack(f"!{len(payload) // 2}h", payload)) == {-1230}
                break
    await client.terminate()
    await wait_until(lambda: registry.get_session(call_id) is None)
    assert not lab.phones[1].invites


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_assist_transfer_tool_reports_busy_and_keeps_trusted_conversation(queue_lab, monkeypatch):
    from homeassistant.helpers.llm import LLMContext, ToolInput
    from custom_components.voip_stack.assist_call_tools import current_call_tools
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    lab = queue_lab
    client, _audio = await enter_assist(lab, monkeypatch)
    application = lab.calls.resource_for(client.dialog_ids.call_id, "assist_application")
    media, context = application.media, application.conversation.context
    lab.runtime.directory = PhoneDirectory(lab.roster)
    lab.phones[0].final_status = 486
    transfer = next(tool for tool in current_call_tools(lab.hass, context) if tool.action == "transfer")
    llm_context = LLMContext(platform="conversation", context=context, language="en", assistant="conversation", device_id=None)
    result = await transfer.async_call(lab.hass, ToolInput(transfer.name, {"destination": "bedroom0"}), llm_context)
    assert result == {"status": "not_answered", "resumed": True}
    assert application.session.live and application.session.owner == "assist"
    assert application.media is media and not media.closed.is_set()
    assert application.conversation.context is context and not application.conversation.detached
    await client.terminate()
    await wait_until(lambda: lab.calls.get_session(client.dialog_ids.call_id) is None)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_assist_resume_tool_cancels_ringing_and_restores_same_conversation(queue_lab, monkeypatch):
    from homeassistant.helpers.llm import LLMContext, ToolInput
    from custom_components.voip_stack.assist_call_tools import current_call_tools
    lab = queue_lab
    client, _audio = await enter_assist(lab, monkeypatch)
    call_id = client.dialog_ids.call_id
    application = lab.calls.resource_for(call_id, "assist_application")
    media, context, consumer = application.media, application.conversation.context, application.media._consumer_task
    lab.phones[0].auto_answer = False
    await lab.calls.forward_call(call_id=call_id, destination="bedroom0", on_failure="resume")
    await asyncio.wait_for(lab.phones[0].invited.wait(), 2)
    resume = next(tool for tool in current_call_tools(lab.hass, context) if tool.action == "resume")
    llm_context = LLMContext(platform="conversation", context=context, language="en", assistant="conversation", device_id=None)
    assert await resume.async_call(lab.hass, ToolInput(resume.name, {}), llm_context) == {"status": "resumed"}
    assert application.session.owner == "assist" and application.media is media
    assert application.conversation.context is context and not application.conversation.detached
    assert media._consumer_task is consumer and not consumer.done()
    assert any(request.method == "CANCEL" for request in lab.phones[0].requests)
    await client.terminate()
    await wait_until(lambda: lab.calls.get_session(call_id) is None)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_assist_transfer_tool_can_enter_queue_and_keep_the_original_call(queue_lab, monkeypatch):
    from homeassistant.helpers.llm import LLMContext, ToolInput
    from custom_components.voip_stack.assist_call_tools import current_call_tools
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    from .test_queue_call_ha import wait_for_bridge
    lab = queue_lab
    client, _audio = await enter_assist(lab, monkeypatch)
    call_id = client.dialog_ids.call_id
    application = lab.calls.resource_for(call_id, "assist_application")
    media, context = application.media, application.conversation.context
    lab.runtime.directory = PhoneDirectory(lab.roster)
    lab.manager.agent_states[lab.identity]["sip:bedroom1"] = "paused"
    transfer = next(tool for tool in current_call_tools(lab.hass, context) if tool.action == "transfer")
    llm_context = LLMContext(platform="conversation", context=context, language="en", assistant="conversation", device_id=None)
    result = await transfer.async_call(lab.hass, ToolInput(transfer.name, {"destination": "Reception"}), llm_context)
    assert result["status"] == "connected"
    await wait_for_bridge(lab, call_id)
    assert lab.calls.get_session(call_id).metadata["dest_endpoint_id"] == "sip:bedroom0"
    assert application.conversation.detached and media.closed.is_set()
    assert not lab.manager.waiting and not lab.manager.reservations
    assert len(lab.phones[0].invites) == 1
    await client.terminate()
    await wait_until(lambda: lab.calls.get_session(call_id) is None)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_assist_browser_decline_reports_failure_and_keeps_live_source(queue_lab, monkeypatch):
    from homeassistant.core import Context
    from homeassistant.helpers.llm import LLMContext, ToolInput
    from custom_components.voip_stack.assist_call_tools import current_call_tools
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    lab = queue_lab
    browser = PhoneEndpoint(endpoint_id="browser-target", name="Browser", username="browser", device_id="browser-target-device",
                            kind=EndpointKind.BROWSER, availability="available")
    lab.runtime.endpoints.register(browser)
    lab.runtime.softphone_presence[browser.endpoint_id] = 1
    lab.roster.append(RosterEntry("Browser", metadata={"local_ha": True, "endpoint_id": browser.endpoint_id, "endpoint_kind": "browser"}))
    client, _audio = await enter_assist(lab, monkeypatch)
    session = lab.calls.get_session(client.dialog_ids.call_id)
    application = lab.calls.resource_for(session.call_id, "assist_application")
    media, context, consumer = application.media, application.conversation.context, application.media._consumer_task
    lab.runtime.directory = PhoneDirectory(lab.roster)
    transfer = next(tool for tool in current_call_tools(lab.hass, context) if tool.action == "transfer")
    llm_context = LLMContext(platform="conversation", context=context, language="en", assistant="conversation", device_id=None)
    task = asyncio.create_task(transfer.async_call(lab.hass, ToolInput(transfer.name, {"destination": "Browser"}), llm_context))
    await wait_until(lambda: lab.runtime.softphones.get(browser.endpoint_id, {}).get("state") == "ringing")
    assert not task.done()
    assert application.media is media and not media.closed.is_set()
    await lab.hass.services.async_call("voip_stack", "decline", {"device_id": browser.device_id, "call_id": session.call_id}, blocking=True, context=Context())
    assert await asyncio.wait_for(task, 2) == {"status": "not_answered", "resumed": True}
    assert session.live and session.owner == "assist"
    assert application.media is media and application.conversation.context is context
    assert media._consumer_task is consumer and not consumer.done()
    await client.terminate()
    await asyncio.wait_for(session.terminated.wait(), 2)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("fail_first", [False, True])
@pytest.mark.parametrize("through_group", [False, True])
async def test_assist_browser_ready_commits_duplex_audio_and_failed_bind_can_retry(queue_lab, monkeypatch, fail_first, through_group):
    from homeassistant.core import Context
    from homeassistant.helpers.llm import LLMContext, ToolInput
    from custom_components.voip_stack.assist_call_tools import current_call_tools
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    from custom_components.voip_stack import audio_ws_view
    from custom_components.voip_stack.audio_ws import encode_audio_frame
    from custom_components.voip_stack.websocket_owner import MediaWebSocketOwner, async_release_media_owner
    from .test_browser_paging_ha import Browser
    class Client(Browser):
        async def close(self, **_kwargs):
            self.force_close()
    lab = queue_lab
    endpoint = PhoneEndpoint(endpoint_id="browser-target", name="Browser", username="browser", device_id="browser-target-device",
                             kind=EndpointKind.BROWSER, availability="available")
    lab.runtime.endpoints.register(endpoint)
    lab.runtime.softphone_presence[endpoint.endpoint_id] = 1
    lab.roster.append(RosterEntry("Browser", metadata={"local_ha": True, "endpoint_id": endpoint.endpoint_id, "endpoint_kind": "browser"}))
    if through_group:
        lab.roster.append(RosterEntry("Browser group", ha_bridge=True, metadata={"group_type": "ring", "members": ["Browser"]}))
    client, audio = await enter_assist(lab, monkeypatch)
    session = lab.calls.get_session(client.dialog_ids.call_id)
    app = lab.calls.resource_for(session.call_id, "assist_application")
    media, context, consumer = app.media, app.conversation.context, app.media._consumer_task
    lab.runtime.directory = PhoneDirectory(lab.roster)
    llm_context = LLMContext(platform="conversation", context=context, language="en", assistant="conversation", device_id=None)
    owner_key = f"{endpoint.endpoint_id}|{session.call_id}"
    workers = []
    try:
        for attempt in range(2 if fail_first else 1):
            transfer = next(tool for tool in current_call_tools(lab.hass, context) if tool.action == "transfer")
            task = asyncio.create_task(transfer.async_call(lab.hass, ToolInput(transfer.name,
                {"destination": "Browser group" if through_group else "Browser"}), llm_context))
            await wait_until(lambda: lab.runtime.softphones.get(endpoint.endpoint_id, {}).get("state") == "ringing")
            assert app.media is media and not media.closed.is_set() and not consumer.done()
            await lab.hass.services.async_call("voip_stack", "answer", {"device_id": endpoint.device_id, "call_id": session.call_id,
                "media_client_id": "target-client"}, blocking=True, context=Context())
            await wait_until(lambda: lab.calls.resource_for(session.call_id, f"softphone_media_endpoint:{endpoint.endpoint_id}") is not None)
            live = audio_ws_view._active_softphone_media_session(lab.hass, endpoint.endpoint_id)
            assert live is not None
            playback = lab.calls.resource_for(session.call_id, "browser_playback")
            browser = Client()
            owner = MediaWebSocketOwner(websocket=browser, client_id="target-client")
            lab.runtime.media.owners_for("audio")[owner_key] = owner
            playback.attach(browser)
            blocker = None
            if fail_first and attempt == 0:
                blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                blocker.bind(("0.0.0.0", live.local_rtp_port))
            async def run_browser():
                try:
                    await audio_ws_view._run_audio_session(lab.hass, browser, live, endpoint_id=endpoint.endpoint_id,
                                                          playback=playback, handoff_requested=owner.handoff_requested)
                finally:
                    playback.detach(browser)
                    await async_release_media_owner(lab.runtime.media.owners_for("audio"), lab.runtime.media.owner_lock_for("audio"), owner_key, owner)
            worker = asyncio.create_task(run_browser())
            workers.append(worker)
            if blocker is not None:
                try:
                    assert await asyncio.wait_for(task, 2) == {"status": "not_answered", "resumed": True}
                finally:
                    blocker.close()
                assert session.owner == "assist" and app.media is media and not media.closed.is_set()
                assert app.conversation.context is context and media._consumer_task is consumer and not consumer.done()
                continue
            await wait_until(lambda: bool(browser.contracts))
            assert not task.done() and app.media is media and not media.closed.is_set()
            browser.ready()
            assert await asyncio.wait_for(task, 2) == {"status": "connected"}
            assert session.owner == "ha_softphone" and media.closed.is_set() and consumer.done()
            assert app.conversation.detached
            # The original source RTP port remains the relay's left socket.
            assert lab.calls.resource_for(session.call_id, "relay").left_port == media.local_rtp_port
            fmt = client.dialog.send_format
            samples = fmt.audio_format.nominal_frame_bytes // 2
            for index in range(8):
                packet = rtp.RtpPacket(fmt.payload_type, index, index * fmt.rtp_timestamp_step, 992,
                                       struct.pack(f"!{samples}h", *([2310] * samples)))
                audio.sendto(rtp.build_packet(packet), (client.dialog.remote_rtp_host, client.dialog.remote_rtp_port))
                await asyncio.sleep(float(fmt.frame_ms) / 1000)
            await wait_until(lambda: any(any(frame) for frame in browser.frames))
            nonzero = next(frame for frame in browser.frames if any(frame))
            assert set(struct.unpack(f"<{len(nonzero)//2}h", nonzero)) == {2310}
            from aiohttp import WSMsgType
            for _ in range(8):
                browser.incoming.put_nowait(SimpleNamespace(type=WSMsgType.BINARY, data=encode_audio_frame(struct.pack("<320h", *([-1230] * 320)))))
                await asyncio.sleep(0.02)
            async with asyncio.timeout(2):
                while True:
                    packet = rtp.parse_packet(await lab.hass.loop.sock_recv(audio, 4096))
                    if any(packet.payload):
                        assert set(struct.unpack(f"!{len(packet.payload)//2}h", packet.payload)) == {-1230}
                        break
        await client.terminate()
        await asyncio.wait_for(session.terminated.wait(), 2)
        assert not lab.calls.sessions and not lab.runtime.media.owners_for("audio")
    finally:
        await client.terminate()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_browser_assist_to_browser_keeps_the_original_socket_and_duplex_audio(queue_lab, monkeypatch):
    from aiohttp import WSMsgType
    from homeassistant.core import Context
    from homeassistant.helpers.llm import LLMContext, ToolInput
    from custom_components.voip_stack import audio_ws_view, local_softphone_runtime
    from custom_components.voip_stack.assist_call_tools import current_call_tools
    from custom_components.voip_stack.audio_ws import encode_audio_frame
    from custom_components.voip_stack.phone_directory import PhoneDirectory
    from custom_components.voip_stack.websocket_owner import MediaWebSocketOwner, async_release_media_owner
    from .test_browser_paging_ha import Browser

    class Client(Browser):
        async def close(self, **_kwargs):
            self.force_close()

    lab = queue_lab
    started = asyncio.Event()
    async def consume(_conversation):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(assist_runtime.AssistConversation, "_pipeline_loop", consume)
    lab.runtime.assist_config.update(assist_name="Assistant", assist_pipeline="preferred")
    lab.roster.append(RosterEntry("Assistant", extension="403", metadata={"virtual_endpoint": "assist_pipeline"}))
    for identity in ("source-browser", "waiting-browser", "target-browser"):
        lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id=identity, name=identity, device_id=f"device-{identity}",
            kind=EndpointKind.BROWSER, availability="available"))
        lab.runtime.softphone_presence[identity] = 1
        lab.roster.append(RosterEntry(identity, metadata={"local_ha": True, "endpoint_id": identity, "endpoint_kind": "browser"}))
    local_softphone_runtime.async_setup_local_softphone_bridge(lab.hass)
    snapshot = local_softphone_runtime.start_local_softphone_call(lab.hass, "source-browser", "waiting-browser",
        context=Context(), caller_owner_id="source-client")
    session = lab.calls.get_session(snapshot.call_id)
    workers, browsers, task = [], {}, None
    async def connect(endpoint_id, client_id):
        live = audio_ws_view._active_softphone_media_session(lab.hass, endpoint_id)
        assert live is not None
        browser = Client()
        browsers[endpoint_id] = browser
        owner_key = f"{endpoint_id}|{session.call_id}"
        owner = MediaWebSocketOwner(websocket=browser, client_id=client_id)
        lab.runtime.media.owners_for("audio")[owner_key] = owner
        playback = lab.calls.resource_for(session.call_id, "browser_playback")
        if playback is not None:
            playback.attach(browser)
        async def run():
            try:
                await audio_ws_view._run_audio_session(lab.hass, browser, live, endpoint_id=endpoint_id,
                    playback=playback, handoff_requested=owner.handoff_requested)
            finally:
                if playback is not None:
                    playback.detach(browser)
                await async_release_media_owner(lab.runtime.media.owners_for("audio"), lab.runtime.media.owner_lock_for("audio"), owner_key, owner)
        worker = asyncio.create_task(run())
        workers.append(worker)
        await wait_until(lambda: bool(browser.contracts) or worker.done())
        assert not worker.done(), worker.exception()
        browser.ready()
        return browser, live, owner
    try:
        initial = await lab.calls.forward_call(call_id=session.call_id, destination="Assistant", on_failure="resume")
        assert (await asyncio.wait_for(asyncio.shield(initial), 2)).status == "connected"
        await asyncio.wait_for(started.wait(), 2)
        source, source_live, source_owner = await connect("source-browser", "source-client")
        application = lab.calls.resource_for(session.call_id, "assist_application")
        media, context = application.media, application.conversation.context
        original_transport = media.transport
        lab.runtime.directory = PhoneDirectory(lab.roster)
        llm_context = LLMContext(platform="conversation", context=context, language="en", assistant="conversation", device_id=None)
        transfer = next(tool for tool in current_call_tools(lab.hass, context) if tool.action == "transfer")
        task = asyncio.create_task(transfer.async_call(lab.hass, ToolInput(transfer.name, {"destination": "target-browser"}), llm_context))
        await wait_until(lambda: lab.runtime.softphones.get("target-browser", {}).get("state") == "ringing")
        await lab.hass.services.async_call("voip_stack", "decline", {"device_id": "device-target-browser", "call_id": session.call_id},
            blocking=True, context=Context())
        assert await asyncio.wait_for(task, 2) == {"status": "not_answered", "resumed": True}
        assert session.owner == "assist" and application.conversation.context is context and not application.conversation.detached
        assert application.media is media and media.transport is original_transport and not source_owner.released.is_set()
        transfer = next(tool for tool in current_call_tools(lab.hass, context) if tool.action == "transfer")
        task = asyncio.create_task(transfer.async_call(lab.hass, ToolInput(transfer.name, {"destination": "target-browser"}), llm_context))
        await wait_until(lambda: lab.runtime.softphones.get("target-browser", {}).get("state") == "ringing")
        assert application.media is media and media.transport is original_transport and not source_owner.released.is_set()
        await lab.hass.services.async_call("voip_stack", "answer", {"device_id": "device-target-browser", "call_id": session.call_id,
            "media_client_id": "target-client"}, blocking=True, context=Context())
        await wait_until(lambda: lab.calls.resource_for(session.call_id, "softphone_media_endpoint:target-browser") is not None)
        target, _target_live, _target_owner = await connect("target-browser", "target-client")
        assert await asyncio.wait_for(task, 2) == {"status": "connected"}
        assert lab.calls.resource_for(session.call_id, "relay").left_transport is original_transport
        assert not source_owner.released.is_set() and browsers["source-browser"] is source
        for sender, receiver, sample in ((source, target, 2310), (target, source, -1230)):
            receiver.frames.clear()
            # The WS contract accepts source frames in its own negotiated quantum.
            live = source_live if sender is source else _target_live
            count = live.recv_format.audio_format.nominal_frame_bytes // 2
            for _ in range(10):
                sender.incoming.put_nowait(SimpleNamespace(type=WSMsgType.BINARY,
                    data=encode_audio_frame(struct.pack(f"<{count}h", *([sample] * count)))))
                await asyncio.sleep(float(live.recv_format.frame_ms) / 1000)
            # The FIR has a finite startup transient; polyphase gain and s16
            # quantization can differ by one LSB after conversion to 48k.
            output = source_live if receiver is source else _target_live
            await wait_until(lambda: any(len(frame) == output.send_format.audio_format.nominal_frame_bytes
                and all(abs(value - sample) <= 1 for value in struct.unpack(f"<{len(frame)//2}h", frame))
                for frame in receiver.frames))
        await lab.calls.terminate_call_wait(session.call_id, reason="local_hangup")
        await asyncio.wait_for(session.terminated.wait(), 2)
        await asyncio.gather(*workers)
        assert not lab.calls.sessions and not lab.runtime.media.owners_for("audio")
    finally:
        if session.live:
            await lab.calls.terminate_call_wait(session.call_id, reason="test_cleanup")
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        for browser in browsers.values():
            browser.force_close()
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
