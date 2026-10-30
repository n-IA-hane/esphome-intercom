"""Ordinary HA scripts originate announcement calls over real SIP/RTP sockets."""

import asyncio
import re
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.script import Script, async_validate_actions_config
from homeassistant.helpers import config_validation as cv
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.voip_stack.core import rtp, sip

pytestmark = pytest.mark.ha


class FixtureRoster(list):
    """Commit synthetic contact edits at their source, as the HA owner does."""

    def __init__(self, entries, runtime):
        super().__init__(entries)
        self.runtime = runtime

    def _publish(self):
        if self.runtime.directory is not None:
            self.runtime.directory.replace(self)

    def append(self, entry):
        super().append(entry)
        self._publish()

    def __setitem__(self, index, value):
        super().__setitem__(index, value)
        self._publish()


class Phone(asyncio.DatagramProtocol):
    def __init__(self, rtp_socket):
        self.rtp_socket = rtp_socket
        self.transport = None
        self.invites = []
        self.requests = []
        self.invalid_datagrams = []
        self.invited = asyncio.Event()
        self.bye = asyncio.Event()
        self.answered = asyncio.Event()
        self.auto_answer = True
        self.final_status = 200
        self.answer_on_cancel = False
        self.direction = "sendrecv"
        self.last_request = None
        self.remote = None
        self.frames = []
        self.audio = asyncio.Queue()
        self.bye_at = None

    def connection_made(self, transport):
        self.transport = transport

    def respond(self, request, address, status, body=b""):
        headers = [(name, request.header(name)) for name in ("Via", "From", "Call-ID", "CSeq")]
        to = request.header("To")
        headers.append(("To", to if ";tag=" in to else to + ";tag=bedroom"))
        if body:
            port = self.transport.get_extra_info("sockname")[1]
            headers += [("Content-Type", "application/sdp"), ("Contact", f"<sip:bedroom@127.0.0.1:{port}>")]
        reason = {180: "Ringing", 200: "OK", 486: "Busy Here", 487: "Request Terminated"}[status]
        self.transport.sendto(sip.build_response(status, reason, headers, body), address)

    def answer(self):
        request = self.last_request
        if self.final_status != 200:
            self.respond(request, self.remote, self.final_status)
            return
        match = re.search(rb"a=rtpmap:(\d+) L16/16000(?:/1)?\r", request.body)
        assert match, request.body
        self.payload_type = int(match[1])
        ptime = int(re.search(rb"a=ptime:(\d+)", request.body)[1])
        body = (
            "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Phone\r\n"
            "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
            f"m=audio {self.rtp_socket.getsockname()[1]} RTP/AVP {self.payload_type}\r\n"
            f"a=rtpmap:{self.payload_type} L16/16000\r\na=ptime:{ptime}\r\na={self.direction}\r\n"
        ).encode()
        self.respond(request, self.remote, 200, body)
        self.answered.set()

    def datagram_received(self, raw, address):
        try:
            request = sip.parse_message(raw)
        except sip.SipError:
            self.invalid_datagrams.append((address, len(raw), raw[:20].hex()))
            return
        self.requests.append(request)
        if request.method == "INVITE":
            self.invites.append(request)
            self.last_request, self.remote = request, address
            self.respond(request, address, 180)
            self.invited.set()
            if self.auto_answer:
                self.answer()
        elif request.method == "CANCEL":
            self.respond(request, address, 200)
            if self.answer_on_cancel:
                self.answer()
            else:
                self.respond(self.last_request, address, 487)
        elif request.method == "BYE":
            self.bye_at = asyncio.get_running_loop().time()
            self.respond(request, address, 200)
            self.bye.set()

    def remote_hangup(self, *, cseq=2):
        request = self.last_request
        port = self.transport.get_extra_info("sockname")[1]
        target = str(sip.parse_sip_uri(request.header("Contact")))
        headers = [
            ("Via", f"SIP/2.0/UDP 127.0.0.1:{port};branch=z9hG4bK-remote-bye"),
            ("From", request.header("To") + ";tag=bedroom"),
            ("To", request.header("From")), ("Call-ID", request.header("Call-ID")),
            ("CSeq", f"{cseq} BYE"),
        ]
        self.transport.sendto(sip.build_request("BYE", target, headers), self.remote)


@pytest.fixture
async def lab(hass, monkeypatch, socket_enabled):
    from custom_components import voip_stack
    from custom_components.voip_stack import automation_originate as module, services
    from custom_components.voip_stack import automation_call
    from custom_components.voip_stack import voicemail, mailbox, mailbox_actions
    from custom_components.voip_stack.endpoint_registry import EndpointRegistry
    from custom_components.voip_stack.phone_control import PhoneAdapterRegistry
    from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
    from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
    from custom_components.voip_stack.roster import RosterEntry
    from custom_components.voip_stack.runtime_data import VoipStackRuntime

    sockets, transports, phones, collectors = [], [], [], []
    for _ in range(2):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        sockets.append(sock)
        transport, phone = await hass.loop.create_datagram_endpoint(lambda: Phone(sock), local_addr=("127.0.0.1", 0))
        transports.append(transport)
        phones.append(phone)
        async def collect(receiver=phone):
            while True:
                raw = await hass.loop.sock_recv(receiver.rtp_socket, 2048)
                await receiver.audio.put((raw, hass.loop.time()))
        collectors.append(asyncio.create_task(collect()))
    endpoints = EndpointRegistry()
    roster = [RosterEntry("Wakeup-Caller", metadata={"virtual_endpoint": "automation", "automation_timeout": 30})]
    for index, transport in enumerate(transports):
        name = f"bedroom{index}"
        endpoints.register(PhoneEndpoint(
            endpoint_id=f"sip:{name}", name=name, kind=EndpointKind.SIP_ACCOUNT,
            username=name, device_id=f"device-{name}",
        ))
        roster.append(RosterEntry(name, sip_uri=f"sip:{name}@127.0.0.1:{transport.get_extra_info('sockname')[1]}",
                                  metadata={"endpoint_id": f"sip:{name}", "endpoint_kind": "sip_account", "registered": True}))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        base_rtp_port = probe.getsockname()[1]
    entry = MockConfigEntry(domain="voip_stack", data={})
    entry.add_to_hass(hass)
    from homeassistant.helpers import entity_registry as er
    er.async_get(hass).async_get_or_create(
        "sensor", "voip_stack", "voip_stack_phonebook", config_entry=entry,
        suggested_object_id="voip_phonebook",
    )
    er.async_get(hass).async_get_or_create(
        "event", "voip_stack", "voip_stack_call_event", config_entry=entry,
        suggested_object_id="voip_stack_call",
    )
    calls = SipEndpointRuntime(allow_dark_sessions=True)
    calls.activate()
    runtime = VoipStackRuntime(
        transport_config={"sip_port": 5099, "rtp_port": base_rtp_port, "advertise_host": "127.0.0.1"},
        assist_config={}, trunk_config={}, endpoints=endpoints,
        phones=PhoneAdapterRegistry(hass, endpoints), sip=calls,
    )
    entry.runtime_data = runtime
    roster = FixtureRoster(roster, runtime)
    monkeypatch.setattr(module, "async_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(module, "async_build_peer_snapshot", AsyncMock(return_value=[]))
    monkeypatch.setattr(module, "roster_from_peers", lambda *_args: roster)
    from custom_components.voip_stack.parking import async_park_call
    from custom_components.voip_stack.call_recording import async_start_recording, async_stop_recording, async_get_recording, async_delete_recording
    from custom_components.voip_stack.call_pickup import async_pickup_call
    from custom_components.voip_stack.phone_state import async_get_phone_state
    from custom_components.voip_stack.external_call_access import async_set_external_call_access
    await services.async_register_services(hass, {
        "call": voip_stack._handle_sip_call_target_service,
        "answer": voip_stack._handle_sip_answer_service,
        "decline": voip_stack._handle_sip_decline_service,
        "forward": voip_stack._handle_sip_forward_service,
        "transfer": voip_stack._handle_sip_transfer_service,
        "park": async_park_call,
        "pickup": async_pickup_call,
        "get_phone_state": async_get_phone_state,
        "set_external_call_access": async_set_external_call_access,
        "start_recording": async_start_recording,
        "stop_recording": async_stop_recording,
        "get_recording": async_get_recording,
        "delete_recording": async_delete_recording,
        "tts_say": automation_call.async_tts_say,
        "play_media": automation_call.async_play_media,
        "hangup": voip_stack._handle_sip_hangup_service,
        "send_to_voicemail": voicemail.async_send_to_voicemail,
        "set_voicemail_settings": mailbox.async_set_voicemail_settings,
        "set_phone_settings": voip_stack._handle_set_phone_settings_service,
        "transcribe_voicemail": mailbox_actions.async_transcribe_voicemail,
        "get_voicemail": mailbox_actions.async_get_voicemail,
    })
    obj = SimpleNamespace(hass=hass, phones=phones, calls=calls, runtime=runtime, roster=roster)
    yield obj
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    for session in list(calls.sessions.values()):
        await calls.request_termination(session.call_id, TerminationIntent("test_done"))
    for transport in transports:
        transport.close()
    for task in collectors:
        task.cancel()
    await asyncio.gather(*collectors, return_exceptions=True)
    for sock in sockets:
        sock.close()
    assert not [item for phone in phones for item in phone.invalid_datagrams]


def sequence(destination="bedroom0"):
    return [
        {"action": "voip_stack.call", "data": {"source_automation": "Wakeup-Caller", "destination": destination, "answer_timeout": 5}, "response_variable": "dialed"},
        {"action": "voip_stack.tts_say", "data": {"call_id": "{{ dialed.call_id }}", "expected_generation": "{{ dialed.generation }}", "tts_entity_id": "tts.test", "message": "Wake up"}},
        {"action": "voip_stack.hangup", "data": {"call_id": "{{ dialed.call_id }}", "expected_generation": "{{ dialed.generation }}"}},
    ]


def tts_fixture(monkeypatch, lab, *, block=False):
    from homeassistant.components import tts

    calls = []
    started = asyncio.Event()
    cancelled = asyncio.Event()
    pcm = b"\x80\x01" * 960

    class Stream:
        def async_set_message(self, message):
            assert message == "Wake up"

        async def async_stream_result(self):
            started.set()
            try:
                if block:
                    await asyncio.Event().wait()
                for chunk in (pcm[:13], pcm[13:777], pcm[777:]):
                    yield chunk
            finally:
                cancelled.set()

    def create(*args):
        calls.append(args)
        return Stream()

    monkeypatch.setattr(tts, "async_create_stream", create)
    return SimpleNamespace(calls=calls, started=started, cancelled=cancelled, pcm=pcm)


@pytest.mark.parametrize("delayed", [False, True])
@pytest.mark.parametrize("forwarded", [False, True])
async def test_ordinary_script_calls_then_speaks_and_hangs_up(lab, monkeypatch, delayed, forwarded):
    stream = tts_fixture(monkeypatch, lab)
    if forwarded:
        from custom_components.voip_stack.phone_policy import PhonePolicy
        lab.runtime.endpoints.update("sip:bedroom0", policy=PhonePolicy.from_data({
            "always": {"kind": "contact", "target": "sip:bedroom1"},
        }))
    phone = lab.phones[int(forwarded)]
    phone.auto_answer = not delayed
    script = Script(lab.hass, await async_validate_actions_config(lab.hass, cv.SCRIPT_SCHEMA(sequence())), "Wakeup", "automation")
    task = asyncio.create_task(script.async_run(context=Context()))
    try:
        await asyncio.wait_for(phone.invited.wait(), 2)
        if delayed:
            assert stream.calls == []
            assert not task.done()
            phone.answer()
        await asyncio.wait_for(task, 5)
        await asyncio.wait_for(phone.bye.wait(), 2)
        received = bytearray()
        last_audio_at = 0
        async with asyncio.timeout(2):
            while len(received) < len(stream.pcm):
                raw, arrived_at = await phone.audio.get()
                packet = rtp.parse_packet(raw)
                assert packet.payload_type == phone.payload_type
                if any(packet.payload):
                    converted = bytearray(len(packet.payload))
                    converted[::2], converted[1::2] = packet.payload[1::2], packet.payload[::2]
                    received.extend(converted)
                    last_audio_at = arrived_at
        assert bytes(received) == stream.pcm
        assert phone.bye_at - last_audio_at >= 0.18
        assert len(stream.calls) == 1
        if forwarded:
            assert not lab.phones[0].invites
        assert not any(session.live for session in lab.calls.sessions.values())
        assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)
        assert not lab.runtime.softphones
    finally:
        await script.async_stop()
        await asyncio.gather(task, return_exceptions=True)


async def test_stopping_script_while_ringing_cancels_call(lab, monkeypatch):
    stream = tts_fixture(monkeypatch, lab)
    phone = lab.phones[0]
    phone.auto_answer = False
    script = Script(lab.hass, await async_validate_actions_config(lab.hass, cv.SCRIPT_SCHEMA(sequence())), "Wakeup", "automation")
    task = asyncio.create_task(script.async_run(context=Context()))
    await asyncio.wait_for(phone.invited.wait(), 2)
    await script.async_stop()
    await asyncio.gather(task, return_exceptions=True)
    assert stream.calls == []
    assert any(request.method == "CANCEL" for request in phone.requests)
    assert not any(session.live for session in lab.calls.sessions.values())


async def test_phone_deadline_cancels_real_sip_invite_before_action_deadline(lab, monkeypatch):
    from custom_components.voip_stack.phone_policy import PhonePolicy
    from custom_components.voip_stack.phone_policy_runtime import setup_phone_policy_events
    from custom_components.voip_stack.store import config_entry

    setup_phone_policy_events(lab.hass, config_entry(lab.hass))
    lab.runtime.endpoints.update("sip:bedroom0", policy=PhonePolicy.from_data({
        "ringing_timeout": 1, "no_answer": {"kind": "hangup"},
    }))
    stream = tts_fixture(monkeypatch, lab)
    phone = lab.phones[0]
    phone.auto_answer = False
    script = Script(lab.hass, await async_validate_actions_config(lab.hass, cv.SCRIPT_SCHEMA(sequence())), "Wakeup", "automation")
    async with asyncio.timeout(3):
        with pytest.raises(ServiceValidationError):
            await script.async_run(context=Context())
    assert stream.calls == []
    assert any(request.method == "CANCEL" for request in phone.requests)
    assert not any(session.live for session in lab.calls.sessions.values())
    assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)


@pytest.mark.slow
@pytest.mark.timeout(75)
async def test_default_final_wait_accepts_real_answer_after_old_sixty_second_limit(lab):
    from custom_components.voip_stack.core.audio_format import AudioFormat
    from custom_components.voip_stack.sip_client import SipCallClient

    phone = lab.phones[0]
    phone.auto_answer = False
    client = SipCallClient(
        local_ip="127.0.0.1", local_name="Caller", local_sip_port=0, local_rtp_port=42000,
        supported_formats=[AudioFormat(16000, "s16le", 1, 20)],
    )
    answer = None
    try:
        result = await client.invite(
            target="bedroom0", remote_host="127.0.0.1",
            remote_sip_port=phone.transport.get_extra_info("sockname")[1], timeout=2,
        )
        assert result == "ringing"
        started = lab.hass.loop.time()
        answer = lab.hass.loop.call_later(61, phone.answer)
        async with asyncio.timeout(65):
            assert await client.wait_for_final() == "in_call"
        assert lab.hass.loop.time() - started >= 60
        await client.terminate()
        await asyncio.wait_for(phone.bye.wait(), 2)
    finally:
        if answer is not None:
            answer.cancel()
        await client.close()


@pytest.mark.parametrize("remote_hangup", [False, True])
async def test_final_sip_answer_wins_timeout_while_media_is_starting(lab, monkeypatch, remote_hangup):
    from homeassistant.core import callback
    from custom_components.voip_stack.local_call_media import LocalCallMedia
    from custom_components.voip_stack.phone_policy import PhonePolicy
    from custom_components.voip_stack.phone_policy_runtime import setup_phone_policy_events
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.websocket_api import CALL_EVENT

    setup_phone_policy_events(lab.hass, config_entry(lab.hass))
    lab.runtime.endpoints.update("sip:bedroom0", policy=PhonePolicy.from_data({
        "ringing_timeout": 1, "no_answer": {"kind": "hangup"},
    }))
    ringing, starting, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_start = LocalCallMedia.start

    async def gated_start(media):
        starting.set()
        try:
            await release.wait()
            await original_start(media)
        finally:
            stopped.set()

    monkeypatch.setattr(LocalCallMedia, "start", gated_start)
    @callback
    def observe_ringing(event):
        if event.data.get("state") == "remote_ringing":
            ringing.set()

    unsubscribe = lab.hass.bus.async_listen(CALL_EVENT, observe_ringing)
    stream = tts_fixture(monkeypatch, lab)
    phone = lab.phones[0]
    phone.auto_answer = False
    script = Script(lab.hass, await async_validate_actions_config(lab.hass, cv.SCRIPT_SCHEMA(sequence())), "Wakeup", "automation")
    task = asyncio.create_task(script.async_run(context=Context()))
    try:
        await asyncio.wait_for(ringing.wait(), 2)
        call_id = phone.last_request.header("Call-ID")
        deadline = lab.calls.resource_for(call_id, "deadline:phone_policy")
        assert deadline is not None
        phone.answer()
        await asyncio.wait_for(starting.wait(), 2)
        if remote_hangup:
            phone.remote_hangup()
            await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)
            assert stopped.is_set()
            assert not stream.calls
            assert not any(session.live for session in lab.calls.sessions.values())
            return
        call_id = phone.last_request.header("Call-ID")
        session = lab.calls.get_session(call_id)
        # A committed final answer may now cancel this owned deadline
        # automatically. Its already-queued callback must also remain harmless.
        deadline._expired()
        fallback = next((item for item in session.tasks if item.get_name() == "phone-policy-fallback"), None)
        if fallback is not None:
            await fallback
        assert session.live
        assert session.termination_intent is None
        release.set()
        await asyncio.wait_for(task, 3)
        await asyncio.wait_for(phone.bye.wait(), 2)
    finally:
        unsubscribe()
        release.set()
        await script.async_stop()
        await asyncio.gather(task, return_exceptions=True)


async def test_stopping_script_during_tts_terminates_call(lab, monkeypatch):
    stream = tts_fixture(monkeypatch, lab, block=True)
    script = Script(lab.hass, await async_validate_actions_config(lab.hass, cv.SCRIPT_SCHEMA(sequence())), "Wakeup", "automation")
    task = asyncio.create_task(script.async_run(context=Context()))
    await asyncio.wait_for(stream.started.wait(), 2)
    await script.async_stop()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.wait_for(lab.phones[0].bye.wait(), 2)
    assert stream.cancelled.is_set()
    assert not any(session.live for session in lab.calls.sessions.values())


async def test_parallel_script_runs_keep_separate_calls(lab, monkeypatch):
    stream = tts_fixture(monkeypatch, lab)
    script = Script(lab.hass, await async_validate_actions_config(lab.hass, cv.SCRIPT_SCHEMA(sequence("{{ bedroom }}"))), "Wakeup", "automation", script_mode="parallel")
    await asyncio.wait_for(asyncio.gather(
        script.async_run({"bedroom": "bedroom0"}, Context()),
        script.async_run({"bedroom": "bedroom1"}, Context()),
    ), 5)
    assert len(stream.calls) == 2
    assert lab.phones[0].invites[0].header("Call-ID") != lab.phones[1].invites[0].header("Call-ID")
    assert all(phone.bye.is_set() for phone in lab.phones)
    assert not any(session.live for session in lab.calls.sessions.values())


async def dial(lab, context, **extra):
    return await lab.hass.services.async_call("voip_stack", "call", {
        "source_automation": "Wakeup-Caller", "destination": "bedroom0",
        "answer_timeout": 1, **extra,
    }, blocking=True, return_response=True, context=context)


@pytest.mark.parametrize("status", [486, None])
async def test_busy_or_unanswered_peer_releases_call_without_tts(lab, monkeypatch, status):
    stream = tts_fixture(monkeypatch, lab)
    phone = lab.phones[0]
    phone.auto_answer = status is not None
    phone.final_status = status
    with pytest.raises(ServiceValidationError):
        await dial(lab, Context())
    assert stream.calls == []
    assert not any(session.live for session in lab.calls.sessions.values())
    assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)


@pytest.mark.parametrize("late_answer", [False, True])
async def test_no_answer_policy_retargets_outgoing_announcement_without_replacing_session(lab, monkeypatch, late_answer):
    from functools import partial
    from custom_components.voip_stack.call_forwarder import async_forward_existing_call
    from custom_components.voip_stack.phone_policy import PhonePolicy
    from custom_components.voip_stack.phone_policy_runtime import setup_phone_policy_events
    from custom_components.voip_stack.store import config_entry

    setup_phone_policy_events(lab.hass, config_entry(lab.hass))
    lab.runtime.endpoints.update("sip:bedroom0", policy=PhonePolicy.from_data({
        "ringing_timeout": 1,
        "no_answer": {"kind": "contact", "target": "sip:bedroom1"},
    }))
    lab.calls.forward_call = partial(async_forward_existing_call, SimpleNamespace(
        hass=lab.hass, config=lab.runtime.transport_config, local_ip="127.0.0.1",
    ))
    first, second = lab.phones
    first.auto_answer = False
    first.answer_on_cancel = late_answer
    stream = tts_fixture(monkeypatch, lab)
    script = Script(lab.hass, await async_validate_actions_config(lab.hass, cv.SCRIPT_SCHEMA(sequence())), "Wakeup", "automation")
    result = await asyncio.wait_for(script.async_run(context=Context()), 8)
    assert result.variables["dialed"]["call_id"] == first.invites[0].header("Call-ID")
    assert second.invites[0].header("Call-ID") != result.variables["dialed"]["call_id"]
    assert len(first.invites) == len(second.invites) == 1
    assert any(request.method == "CANCEL" for request in first.requests)
    if late_answer:
        assert first.bye.is_set()
    assert second.bye.is_set()
    assert len(stream.calls) == 1
    voiced = []
    while not second.audio.empty():
        raw, _ = second.audio.get_nowait()
        payload = rtp.parse_packet(raw).payload
        if any(payload):
            voiced.append(payload)
    assert b"".join(voiced) == b"\x01\x80" * 960
    assert first.audio.empty()
    assert not lab.calls.sessions
    assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)


@pytest.mark.parametrize("outcome", ["busy", "timeout", "cancel"])
async def test_retarget_failure_or_cancellation_releases_both_attempts(lab, monkeypatch, outcome):
    from functools import partial
    from custom_components.voip_stack.call_forwarder import async_forward_existing_call
    from custom_components.voip_stack.phone_policy import PhonePolicy
    from custom_components.voip_stack.phone_policy_runtime import setup_phone_policy_events
    from custom_components.voip_stack.store import config_entry

    setup_phone_policy_events(lab.hass, config_entry(lab.hass))
    lab.runtime.endpoints.update("sip:bedroom0", policy=PhonePolicy.from_data({
        "ringing_timeout": 1, "no_answer": {"kind": "contact", "target": "sip:bedroom1"},
    }))
    lab.calls.forward_call = partial(async_forward_existing_call, SimpleNamespace(
        hass=lab.hass, config=lab.runtime.transport_config, local_ip="127.0.0.1",
    ))
    first, second = lab.phones
    first.auto_answer = False
    second.auto_answer = outcome == "busy"
    second.final_status = 486 if outcome == "busy" else 200
    stream = tts_fixture(monkeypatch, lab)
    task = asyncio.create_task(dial(lab, Context(), answer_timeout=2))
    try:
        async with asyncio.timeout(4):
            await second.invited.wait()
            if outcome == "cancel":
                task.cancel()
            with pytest.raises((ServiceValidationError, asyncio.CancelledError)):
                await task
        assert not stream.calls
        assert not lab.calls.sessions
        assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)
        assert any(request.method == "CANCEL" for request in first.requests)
        if outcome != "busy":
            assert any(request.method == "CANCEL" for request in second.requests)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("destination", ["bedroom1", "missing-phone"])
@pytest.mark.parametrize("failure_policy", ["resume", "busy", "terminate"])
async def test_announcement_forward_can_resume_original_destination(lab, monkeypatch, destination, failure_policy):
    from custom_components.voip_stack.call_forwarder import async_forward_existing_call

    first, second = lab.phones
    first.auto_answer = False
    second.final_status = 486
    context = Context()
    stream = tts_fixture(monkeypatch, lab)
    task = asyncio.create_task(dial(lab, context, answer_timeout=5))
    try:
        await asyncio.wait_for(first.invited.wait(), 2)
        original_id = first.invites[0].header("Call-ID")
        original_session = lab.calls.get_session(original_id)
        first.auto_answer = True
        await async_forward_existing_call(SimpleNamespace(
            hass=lab.hass, config=lab.runtime.transport_config, local_ip="127.0.0.1",
        ), call_id=original_id, destination=destination, on_failure=failure_policy)
        if failure_policy != "resume":
            with pytest.raises(ServiceValidationError):
                await asyncio.wait_for(task, 5)
            assert len(first.invites) == 1 and not stream.calls
            assert not lab.calls.sessions
            assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)
            if failure_policy == "busy":
                assert original_session.termination_intent.response_status == 486
                assert original_session.termination_intent.public_state == "busy"
            return
        result = await asyncio.wait_for(task, 5)
        assert result["call_id"] == original_id
        assert len(first.invites) == 2
        assert first.invites[1].header("Call-ID") != original_id
        assert len(second.invites) == (1 if destination == "bedroom1" else 0)
        assert any(request.method == "CANCEL" for request in first.requests)
        await lab.hass.services.async_call("voip_stack", "tts_say", {
            "tts_entity_id": "tts.test", "message": "Wake up",
        }, blocking=True, context=context)
        await lab.hass.services.async_call("voip_stack", "hangup", {}, blocking=True, context=context)
        assert len(stream.calls) == 1 and first.bye.is_set()
        assert not lab.calls.sessions
        assert all(not endpoint.active_call_id for endpoint in lab.runtime.endpoints.endpoints)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stale_generation_and_different_controller_cannot_touch_call(lab, monkeypatch):
    stream = tts_fixture(monkeypatch, lab)
    context = Context()
    result = await dial(lab, context)
    for controller, generation in ((Context(), result["generation"]), (context, result["generation"] + 1)):
        for action in ("tts_say", "hangup"):
            data = {"call_id": result["call_id"], "expected_generation": generation}
            if action == "tts_say":
                data.update(tts_entity_id="tts.test", message="Wake up")
            with pytest.raises(ServiceValidationError):
                await lab.hass.services.async_call("voip_stack", action, data,
                                                   blocking=True, context=controller)
            assert lab.calls.get_session(result["call_id"]).live
    assert stream.calls == []
    await lab.hass.services.async_call("voip_stack", "hangup", {
        "call_id": result["call_id"], "expected_generation": result["generation"],
    }, blocking=True, context=context)
    assert lab.calls.get_session(result["call_id"]) is None


async def test_second_run_cannot_seize_busy_destination(lab):
    context = Context()
    result = await dial(lab, context)
    with pytest.raises(ServiceValidationError):
        await dial(lab, Context())
    assert len(lab.phones[0].invites) == 1
    assert lab.calls.get_session(result["call_id"]).live


async def test_remote_bye_during_tts_cancels_stream_and_releases_call(lab, monkeypatch):
    stream = tts_fixture(monkeypatch, lab, block=True)
    script = Script(lab.hass, await async_validate_actions_config(lab.hass, cv.SCRIPT_SCHEMA(sequence())), "Wakeup", "automation")
    task = asyncio.create_task(script.async_run(context=Context()))
    try:
        await asyncio.wait_for(stream.started.wait(), 2)
        lab.phones[0].remote_hangup()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
        assert stream.cancelled.is_set()
        assert not any(session.live for session in lab.calls.sessions.values())
    finally:
        await script.async_stop()
        await asyncio.gather(task, return_exceptions=True)


async def test_normal_phone_name_cannot_be_used_as_automation_source(lab):
    with pytest.raises(ServiceValidationError):
        await dial(lab, Context(), source_automation="bedroom0")
    assert not lab.phones[0].invites


@pytest.mark.parametrize("failure", ["synthesis", "timeout", "receive_only"])
async def test_tts_failure_ends_outgoing_application_call(lab, monkeypatch, failure):
    from homeassistant.components import tts

    if failure == "receive_only":
        lab.phones[0].direction = "sendonly"
    tts_fixture(monkeypatch, lab, block=failure == "timeout")
    if failure == "synthesis":
        def broken_tts(*_args):
            raise RuntimeError("synthetic TTS failure")
        monkeypatch.setattr(tts, "async_create_stream", broken_tts)
    actions = sequence()
    actions[1]["data"]["timeout"] = 1
    script = Script(lab.hass, await async_validate_actions_config(lab.hass, cv.SCRIPT_SCHEMA(actions)), "Wakeup", "automation")
    with pytest.raises(ServiceValidationError):
        await asyncio.wait_for(script.async_run(context=Context()), 4)
    await asyncio.wait_for(lab.phones[0].bye.wait(), 2)
    assert not any(session.live for session in lab.calls.sessions.values())


async def test_cancel_racing_with_final_answer_closes_accepted_dialog(lab, monkeypatch):
    stream = tts_fixture(monkeypatch, lab)
    phone = lab.phones[0]
    phone.auto_answer = False
    phone.answer_on_cancel = True
    script = Script(lab.hass, await async_validate_actions_config(lab.hass, cv.SCRIPT_SCHEMA(sequence())), "Wakeup", "automation")
    task = asyncio.create_task(script.async_run(context=Context()))
    await asyncio.wait_for(phone.invited.wait(), 2)
    await script.async_stop()
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.wait_for(phone.bye.wait(), 2)
    assert any(request.method == "ACK" for request in phone.requests)
    assert stream.calls == []
    assert not any(session.live for session in lab.calls.sessions.values())


async def test_response_variable_is_optional(lab):
    context = Context()
    await lab.hass.services.async_call("voip_stack", "call", {
        "source_automation": "Wakeup-Caller", "destination": "bedroom0",
    }, blocking=True, context=context)
    assert len(lab.phones[0].invites) == 1
    await lab.hass.services.async_call("voip_stack", "hangup", {}, blocking=True, context=context)
    assert not lab.calls.sessions


async def test_nested_voip_trigger_cannot_start_an_unbound_second_call(lab):
    from custom_components.voip_stack.automation_context import CallExecution, current_execution
    from custom_components.voip_stack.endpoint_session import CallToken

    token = current_execution.set(CallExecution(CallToken("incoming-call", 1), {}))
    try:
        with pytest.raises(ServiceValidationError):
            await dial(lab, Context())
    finally:
        current_execution.reset(token)
    assert not lab.phones[0].invites


async def test_ordinary_browser_session_cannot_be_converted_by_tts(lab, monkeypatch):
    stream = tts_fixture(monkeypatch, lab)
    session = lab.calls.upsert("browser-existing", state="in_call", owner="browser")
    with pytest.raises(ServiceValidationError):
        await lab.hass.services.async_call("voip_stack", "tts_say", {
            "call_id": session.call_id, "expected_generation": session.generation,
            "tts_entity_id": "tts.test", "message": "Wake up",
        }, blocking=True, context=Context())
    assert session.live
    assert stream.calls == []


async def test_script_stop_between_actions_is_bounded_by_application_deadline(lab):
    lab.roster[0].metadata["automation_timeout"] = 1
    checkpoint = asyncio.Event()

    async def reached(_call):
        checkpoint.set()

    lab.hass.services.async_register("test", "checkpoint", reached)
    actions = [sequence()[0], {"action": "test.checkpoint"}, {"delay": 10}, sequence()[2]]
    script = Script(lab.hass, await async_validate_actions_config(lab.hass, cv.SCRIPT_SCHEMA(actions)), "Wakeup", "automation")
    task = asyncio.create_task(script.async_run(context=Context()))
    await asyncio.wait_for(checkpoint.wait(), 2)
    await script.async_stop()
    await asyncio.gather(task, return_exceptions=True)
    # HA cannot notify a completed service that a later Delay was stopped.
    # The call's documented idle deadline owns this remaining cleanup.
    assert any(session.live for session in lab.calls.sessions.values())
    await asyncio.wait_for(lab.phones[0].bye.wait(), 2)
    await lab.hass.async_block_till_done()
    assert not any(session.live for session in lab.calls.sessions.values())


def simple_sequence(destination="bedroom0"):
    actions = sequence(destination)
    actions[0].pop("response_variable")
    for action in actions[1:]:
        action["data"].pop("call_id")
        action["data"].pop("expected_generation")
    actions.insert(1, {"delay": {"milliseconds": 10}})
    return actions


async def test_two_scripts_speak_and_hang_up_their_own_call_without_ids(lab, monkeypatch):
    stream = tts_fixture(monkeypatch, lab)
    scripts = [Script(lab.hass, await async_validate_actions_config(lab.hass, cv.SCRIPT_SCHEMA(simple_sequence(f"bedroom{i}"))), f"Wakeup {i}", "automation") for i in range(2)]
    await asyncio.gather(*(script.async_run(context=Context()) for script in scripts))
    assert len(stream.calls) == 2
    for phone in lab.phones:
        assert phone.bye.is_set()
        assert len(phone.invites) == 1
        voiced = []
        while not phone.audio.empty():
            raw, _ = phone.audio.get_nowait()
            payload = rtp.parse_packet(raw).payload
            if any(payload):
                voiced.append(payload)
        assert b''.join(voiced) == b'\x01\x80' * 960
    assert not lab.calls.sessions
    assert not lab.runtime.softphones


async def test_implicit_action_never_falls_back_to_another_execution(lab, monkeypatch):
    tts_fixture(monkeypatch, lab)
    first, second = Context(), Context()
    one = await dial(lab, first)
    two = await dial(lab, second, destination="bedroom1")
    await lab.hass.services.async_call("voip_stack", "hangup", {}, blocking=True, context=first)
    for action, data in (("hangup", {}), ("tts_say", {"tts_entity_id":"tts.test", "message":"Wake up"})):
        with pytest.raises(ServiceValidationError, match="no outgoing Automation call"):
            await lab.hass.services.async_call("voip_stack", action, data, blocking=True, context=first)
    assert lab.calls.get_session(one["call_id"]) is None
    assert lab.calls.get_session(two["call_id"]).live
    await lab.hass.services.async_call("voip_stack", "hangup", {}, blocking=True, context=second)


async def test_multiple_calls_in_same_execution_require_explicit_selection(lab):
    context = Context()
    one = await dial(lab, context)
    two = await dial(lab, context, destination="bedroom1")
    with pytest.raises(ServiceValidationError, match="multiple calls"):
        await lab.hass.services.async_call("voip_stack", "hangup", {}, blocking=True, context=context)
    assert len(lab.calls.sessions) == 2
    for result in (one, two):
        await lab.hass.services.async_call("voip_stack", "hangup", {
            "call_id":result["call_id"], "expected_generation":result["generation"],
        }, blocking=True, context=context)
    assert not lab.calls.sessions


async def test_outgoing_announcement_owns_rtp_port_before_invite_and_reuses_it(lab, monkeypatch):
    """An independent peer cannot take the advertised port during signaling."""
    phone = lab.phones[0]
    answer = phone.answer
    advertised = []
    exposed = []

    def observe_answer():
        port = int(re.search(rb"m=audio (\d+)", phone.last_request.body)[1])
        advertised.append(port)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as contender:
            try:
                contender.bind(("0.0.0.0", port))
            except OSError:
                pass
            else:
                exposed.append(port)
        answer()

    monkeypatch.setattr(phone, "answer", observe_answer)
    for _ in range(2):
        result = await dial(lab, Context())
        assert not exposed, "The INVITE advertised an RTP port not yet bound by its owner"
        media = lab.calls.resource_for(result["call_id"], "relay")
        assert media.transport.get_extra_info("sockname")[1] == advertised[-1]
        await lab.calls.terminate_call_wait(result["call_id"], reason="test_done")
        assert not lab.calls.sessions
        assert not lab.runtime.rtp_port_pool["used"]
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as released:
            released.bind(("0.0.0.0", advertised[-1]))


async def test_outgoing_rtp_bind_failure_releases_call_and_allows_retry(lab, monkeypatch):
    from custom_components.voip_stack import automation_originate

    def unavailable(_port):
        raise OSError("RTP socket unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(automation_originate, "bind_sip_rtp_socket", unavailable)
        with pytest.raises(ServiceValidationError):
            await dial(lab, Context())
    assert not lab.phones[0].invites
    assert not lab.calls.sessions
    assert not lab.runtime.rtp_port_pool["used"]
    assert all(not phone.active_call_id for phone in lab.runtime.endpoints.endpoints)
    result = await dial(lab, Context())
    media = lab.calls.resource_for(result["call_id"], "relay")
    port = media.local_rtp_port
    await lab.calls.terminate_call_wait(result["call_id"], reason="test_done")
    assert not lab.calls.sessions
    assert not lab.runtime.rtp_port_pool["used"]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as released:
        released.bind(("0.0.0.0", port))
