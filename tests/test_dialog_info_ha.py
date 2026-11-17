"""BLF uses canonical call ownership and the existing SIP subscription owner."""

import asyncio
import time
from xml.etree import ElementTree

import pytest

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.call_projection import publish_bridge_projection
from custom_components.voip_stack.sip_application import SipApplicationMethods
from custom_components.voip_stack.sip_registrar import SipRegistrar, SipAccount, SipRegistration
from .test_automation_originate_ha import lab as lab
from .test_trunk_calls_ha import trunks as trunks
from .test_sip_phone_transfers_ha import registered_phones as registered_phones

pytestmark = pytest.mark.ha
NS = {"d": "urn:ietf:params:xml:ns:dialog-info"}


def subscribe(*, tag="", event="dialog", expires=300, accept="application/dialog-info+xml", cseq=1, call_id="watch-101", target="101"):
    return sip.parse_message(sip.build_request("SUBSCRIBE", f"sip:{target}@127.0.0.1", [
        ("From", "<sip:watcher@127.0.0.1>;tag=watcher"),
        ("To", f"<sip:{target}@127.0.0.1>" + (f";tag={tag}" if tag else "")),
        ("Contact", "<sip:watcher@127.0.0.3:5060>"),
        ("Call-ID", call_id), ("CSeq", f"{cseq} SUBSCRIBE"),
        ("Event", event), ("Expires", str(expires)), ("Accept", accept),
    ]))


@pytest.fixture
async def blf(lab):
    lab.calls.bind_endpoint_registry(lab.runtime.endpoints)
    lab.runtime.endpoints.update("sip:bedroom0", extension="101")
    registrar = SipRegistrar(enabled=True, accounts=[SipAccount("watcher", "Watcher", "test-only")],
                             local_ip="127.0.0.1", local_sip_port=5060)
    registrar.registrations["watcher"] = SipRegistration(
        username="watcher", contact_uri="sip:watcher@127.0.0.3:5060", source_host="127.0.0.3",
        source_port=5060, transport="UDP", expires_at=time.time() + 300,
    )
    methods, notifications = SipApplicationMethods(lab.hass, registrar), asyncio.Queue()
    async def send(_request, _address, result):
        await notifications.put(result.follow_up)
        return sip.SipMessage(status_code=200, reason="OK")
    yield methods, notifications, send
    await methods.stop()
    assert not methods.tasks and methods.dialog_unsubscribe is None and methods.endpoint_unsubscribe is None


async def test_blf_tracks_ringing_answer_multiple_calls_and_final_idle(lab, blf):
    methods, notifications, send = blf
    initial = await methods.handle(subscribe(), ("127.0.0.3", 5060), "UDP", send)
    assert initial.status == 200
    initial.follow_up_scheduler()
    root = ElementTree.fromstring((await asyncio.wait_for(notifications.get(), 1)).body)
    assert root.attrib == {"entity": "sip:101@127.0.0.1", "version": "0", "state": "full"}
    assert not root.findall("d:dialog", NS)
    first = lab.calls.upsert("first", state="ringing", owner="router", route_kind="sip",
                             source_endpoint_id="other", dest_endpoint_id="sip:bedroom0")
    lab.calls.claim_endpoint(first.call_id, "sip:bedroom0", role="destination")
    publish_bridge_projection(lab.hass, first)
    ringing = ElementTree.fromstring((await asyncio.wait_for(notifications.get(), 1)).body)
    assert ringing.find("d:dialog/d:state", NS).text == "early"
    lab.calls.transition(first.call_id, state="in_call")
    publish_bridge_projection(lab.hass, first)
    answered = ElementTree.fromstring((await asyncio.wait_for(notifications.get(), 1)).body)
    assert answered.find("d:dialog/d:state", NS).text == "confirmed"
    publish_bridge_projection(lab.hass, first)
    await lab.hass.async_block_till_done()
    assert notifications.empty()
    second = lab.calls.upsert("second", state="connecting", owner="router", route_kind="sip",
                              source_endpoint_id="sip:bedroom0")
    lab.calls.claim_endpoint(second.call_id, "sip:bedroom0", role="source")
    publish_bridge_projection(lab.hass, second)
    concurrent = ElementTree.fromstring((await asyncio.wait_for(notifications.get(), 1)).body)
    assert len(concurrent.findall("d:dialog", NS)) == 2
    await lab.calls.terminate_call_wait(first.call_id, reason="remote_hangup")
    publish_bridge_projection(lab.hass, first, intent=first.termination_intent)
    remaining = ElementTree.fromstring((await asyncio.wait_for(notifications.get(), 1)).body)
    assert len(remaining.findall("d:dialog", NS)) == 1
    await lab.calls.terminate_call_wait(second.call_id, reason="remote_hangup")
    publish_bridge_projection(lab.hass, second, intent=second.termination_intent)
    idle = ElementTree.fromstring((await asyncio.wait_for(notifications.get(), 1)).body)
    assert not idle.findall("d:dialog", NS)
    assert [int(item.attrib["version"]) for item in (root, ringing, answered, concurrent, remaining, idle)] == list(range(6))


async def test_blf_refresh_keeps_dialog_version_and_removal_terminates_subscription(lab, blf):
    methods, notifications, send = blf
    first = await methods.handle(subscribe(expires=7200), ("127.0.0.3", 5060), "UDP", send)
    assert dict(first.headers)["Expires"] == "3600"
    first.follow_up_scheduler()
    await asyncio.wait_for(notifications.get(), 1)
    refreshed = await methods.handle(subscribe(tag=first.to_tag, event="DIALOG", cseq=2), ("127.0.0.3", 5060), "UDP", send)
    assert refreshed.status == 200 and refreshed.to_tag == first.to_tag
    assert len(methods.subscriptions) == 1
    refreshed.follow_up_scheduler()
    refreshed_notify = await asyncio.wait_for(notifications.get(), 1)
    assert ElementTree.fromstring(refreshed_notify.body).attrib["version"] == "1"
    lab.runtime.endpoints.remove("sip:bedroom0")
    final = await asyncio.wait_for(notifications.get(), 1)
    assert dict(final.headers)["Subscription-State"] == "terminated;reason=noresource"
    assert not methods.subscriptions
    assert methods.dialog_unsubscribe is None and methods.endpoint_unsubscribe is None


@pytest.mark.parametrize("changes,status", [
    ({"tag": "unknown"}, 481), ({"accept": "application/pidf+xml"}, 406),
    ({"event": "dialog;from-tag=alone"}, 400), ({"expires": -1}, 400),
])
async def test_invalid_blf_subscription_does_not_create_state(blf, changes, status):
    methods, _notifications, send = blf
    result = await methods.handle(subscribe(**changes), ("127.0.0.3", 5060), "UDP", send)
    assert result.status == status
    assert not methods.subscriptions and methods.dialog_unsubscribe is None


async def test_dialog_filter_uses_monitored_phone_tags_and_does_not_mix_dialogs(lab, blf):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    methods, _notifications, send = blf
    lab.calls.upsert("parent", state="in_call", owner="bridge", source_endpoint_id="other",
                     dest_endpoint_id="sip:bedroom0")
    lab.calls.add_leg("parent", "wire", role="callee", state="in_call", endpoint_id="sip:bedroom0")
    client = SimpleNamespace(dialog=object(), dialog_ids=sip.SipDialogIds("wire", "ha-tag", "phone-tag"),
                             on_refer=None, terminate=AsyncMock(), close=AsyncMock())
    lab.calls.attach_sip_client("parent", "wire", client, state="in_call")
    lab.calls.claim_endpoint("parent", "sip:bedroom0", role="destination")
    for index, remote in enumerate(("ha-tag", "wrong")):
        result = await methods.handle(subscribe(
            call_id=f"filtered-{index}",
            event=f'dialog;call-id="wire";to-tag=phone-tag;from-tag={remote}',
        ), ("127.0.0.3", 5060), "UDP", send)
        assert result.status == 200
        root = ElementTree.fromstring(result.follow_up.body)
        dialogs = root.findall("d:dialog", NS)
        if index == 0:
            assert len(dialogs) == 1
            assert dialogs[0].attrib["local-tag"] == "phone-tag"
            assert dialogs[0].attrib["remote-tag"] == "ha-tag"
        else:
            assert dialogs == []


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_c"}], indirect=True)
async def test_sipp_observes_baresip_ring_answer_hangup_and_resource_removal(registered_phones, trunks, tmp_path):
    import shutil
    import socket
    from pathlib import Path

    executable = shutil.which("sipp")
    if executable is None:
        pytest.skip("SIPp is not installed")
    lab = trunks
    registrar = lab.calls.component("registrar")
    registrar.update_accounts([*registrar.accounts.values(), SipAccount("blfwatch", "BLF watcher", "isolated-test-only")])
    lab.runtime.endpoints.update("transfer_c", extension="101", device_id="blf-phone-device")
    methods = lab.calls.component("sip_application")
    from homeassistant.helpers.trigger import TriggerConfig
    from custom_components.voip_stack.trigger import PhoneStateTrigger
    from custom_components.voip_stack.automation_context import current_execution
    native, native_tasks = [], []
    subject = PhoneStateTrigger(lab.hass, TriggerConfig("voip_stack.phone_state_changed",
        options={"phone": "blf-phone-device", "to": "held"}))
    def run(payload, _description, _context):
        async def observe():
            native.append((current_execution.get().token, payload["phone"]["state"]))
        task = asyncio.create_task(observe())
        native_tasks.append(task)
        return task
    detach = await subject.async_attach_runner(run)
    scenario = Path(__file__).parent / "fixtures/sipp/dialog-subscription.xml"
    messages = tmp_path / "dialog-messages.log"
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    process = await asyncio.create_subprocess_exec(
        executable, f'127.0.0.1:{lab.runtime.transport_config["sip_port"]}',
        "-sf", str(scenario), "-i", "127.0.0.1", "-p", str(port), "-m", "1",
        "-trace_msg", "-message_file", str(messages), "-trace_err", "-error_file", str(tmp_path / "dialog-errors.log"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=tmp_path,
    )
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                if process.returncode is not None:
                    raise AssertionError((await process.communicate())[0].decode())
                await asyncio.sleep(0.01)
    def captured():
        return messages.read_text() if messages.exists() else ""
    try:
        await wait_for(lambda: bool(methods.subscriptions))
        caller, _operator, destination = registered_phones
        await asyncio.to_thread(caller.dial, "transfer_c", wait_for="180 Ringing")
        await wait_for(lambda: "early</state>" in captured())
        await asyncio.to_thread(destination.command, "/accept")
        await asyncio.to_thread(caller.wait_for, "Call established", 3)
        await wait_for(lambda: "confirmed</state>" in captured())
        before_hold=len(captured())
        await asyncio.to_thread(destination.command, "/hold")
        await wait_for(lambda: 'pname="+sip.rendering" pval="no"' in captured()[before_hold:])
        from custom_components.voip_stack.phone_state import phone_states
        assert phone_states(lab.hass)["transfer_c"]["state"] == "held"
        await asyncio.gather(*native_tasks)
        assert native == [(next(iter(lab.calls.sessions.values())).token, "held")]
        before_resume=len(captured())
        await asyncio.to_thread(destination.command, "/resume")
        await wait_for(lambda: "confirmed</state>" in captured()[before_resume:])
        assert 'pname="+sip.rendering" pval="no"' not in captured()[before_resume:]
        assert phone_states(lab.hass)["transfer_c"]["state"] == "in_call"
        await asyncio.to_thread(caller.hangup)
        await wait_for(lambda: not lab.calls.sessions)
        lab.runtime.endpoints.remove("transfer_c")
        output, _ = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, output.decode()
        documents = [ElementTree.fromstring(line.strip()) for line in captured().splitlines()
                     if line.strip().startswith("<dialog-info")]
        assert len(documents) >= 4
        versions = [int(item.attrib["version"]) for item in documents]
        assert versions == list(range(len(documents)))
        assert not documents[0].findall("d:dialog", NS) and not documents[-1].findall("d:dialog", NS)
        assert not methods.subscriptions
    finally:
        detach()
        if process.returncode is None:
            process.terminate()
            await process.communicate()


async def test_parking_slot_blf_tracks_reservation_without_a_fake_phone(lab, blf):
    from custom_components.voip_stack.store import config_entry
    from custom_components.voip_stack.parking import get_parking_lot

    entry=config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry,data={**entry.data,'parking':{'enabled':True}})
    methods, notifications, send=blf
    count=len(lab.runtime.endpoints.endpoints)
    result=await methods.handle(subscribe(target='701'),('127.0.0.3',5060),'UDP',send)
    assert result.status==200
    assert not ElementTree.fromstring(result.follow_up.body).findall('d:dialog',NS)
    lot=get_parking_lot(lab.hass)
    session=lab.calls.upsert('parked-call',state='in_call',owner='parking')
    session.claim_answer(session.token,lambda:True)
    parked=lot.reserve(session)
    busy=ElementTree.fromstring((await asyncio.wait_for(notifications.get(),1)).body)
    assert busy.find('d:dialog/d:state',NS).text=='confirmed'
    lot.release(parked)
    idle=ElementTree.fromstring((await asyncio.wait_for(notifications.get(),1)).body)
    assert not idle.findall('d:dialog',NS)
    assert len(lab.runtime.endpoints.endpoints)==count
    result=await methods.handle(subscribe(target='701',tag=result.to_tag,cseq=2,expires=0),('127.0.0.3',5060),'UDP',send)
    assert result.status==200
    assert methods.parking_unsubscribe is None and methods.dialog_unsubscribe is None
    await lab.calls.terminate_call_wait(session.call_id,reason='test_complete')


async def test_activity_projection_tracks_dialogs_and_occupied_slots(lab, blf):
    from custom_components.voip_stack.dialog_info import activity_snapshot
    from custom_components.voip_stack.parking_lot import ParkingLot

    assert activity_snapshot(lab.runtime) == {"parked": [], "phones": []}
    session = lab.calls.upsert("activity", state="ringing", owner="router", route_kind="sip",
                               source_endpoint_id="other", dest_endpoint_id="sip:bedroom0")
    lab.calls.claim_endpoint(session.call_id, "sip:bedroom0", role="destination")
    assert activity_snapshot(lab.runtime)["phones"][0]["state"] == "ringing"
    assert activity_snapshot(lab.runtime)["phones"][0]["call_ids"] == [session.call_id]
    lab.calls.transition(session.call_id, state="in_call")
    assert activity_snapshot(lab.runtime)["phones"][0]["state"] == "busy"
    lot = ParkingLot(("701", "702"))
    lab.calls.adopt_component("parking", lot, closer=lot.close)
    parked = lot.reserve(session)
    assert activity_snapshot(lab.runtime)["parked"] == ["701"]
    lot.release(parked)
    assert activity_snapshot(lab.runtime)["parked"] == []
    await lab.calls.terminate_call_wait(session.call_id, reason="remote_hangup")
    assert activity_snapshot(lab.runtime) == {"parked": [], "phones": []}


async def test_activity_subscription_updates_and_unsubscribes(lab, blf, monkeypatch, hass_admin_user):
    from types import SimpleNamespace
    from custom_components.voip_stack import websocket_api
    from custom_components.voip_stack.parking import PARKING_CHANGED
    from custom_components.voip_stack.parking_lot import ParkingLot

    events = []
    connection = SimpleNamespace(user=hass_admin_user, subscriptions={}, send_result=lambda *args: None,
                                 send_event=lambda identity, value: events.append(value))
    monkeypatch.setattr(websocket_api, "require_websocket_read", lambda *args: None)
    monkeypatch.setattr(websocket_api, "require_websocket_endpoint_read", lambda *args: None)
    websocket_api.websocket_subscribe_call_events(lab.hass, connection, {"id": 77, "include_activity": True})
    assert events[-1] == {"event_type": "voip_stack.activity", "data": {"parked": [], "phones": []}}
    lot = ParkingLot(("701",), lambda: lab.hass.bus.async_fire(PARKING_CHANGED))
    lab.calls.adopt_component("parking", lot, closer=lot.close)
    session = lab.calls.upsert("activity-stream", state="in_call", owner="router", route_kind="sip")
    lab.calls.claim_endpoint(session.call_id, "sip:bedroom0", role="destination")
    parked = lot.reserve(session)
    await lab.hass.async_block_till_done()
    activity = [event["data"] for event in events if event["event_type"] == "voip_stack.activity"][-1]
    assert activity["parked"] == ["701"] and activity["phones"][0]["state"] == "busy"
    lot.release(parked)
    await lab.calls.terminate_call_wait(session.call_id, reason="remote_hangup")
    publish_bridge_projection(lab.hass, session, intent=session.termination_intent)
    await lab.hass.async_block_till_done()
    assert [event["data"] for event in events if event["event_type"] == "voip_stack.activity"][-1] == {"parked": [], "phones": []}
    connection.subscriptions[77]()
    count = len(events)
    lab.hass.bus.async_fire(PARKING_CHANGED)
    await lab.hass.async_block_till_done()
    assert len(events) == count

    websocket_api.websocket_subscribe_call_events(lab.hass, connection, {"id": 78})
    assert len(events) == count
    lab.hass.bus.async_fire(PARKING_CHANGED)
    await lab.hass.async_block_till_done()
    assert len(events) == count
    lab.hass.bus.async_fire(websocket_api.CALL_EVENT, {"type": "example"})
    await lab.hass.async_block_till_done()
    assert events[-1] == {"event_type": websocket_api.CALL_EVENT, "data": {"type": "example"}}
    connection.subscriptions[78]()


async def test_browser_dialog_tags_use_the_observed_local_phone_perspective(lab):
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    from .test_ha_call_transfer import _established
    from custom_components.voip_stack.dialog_info import snapshot
    phone=PhoneEndpoint(endpoint_id='browser-blf',name='Browser',kind='browser',device_id='browser-device')
    lab.runtime.endpoints.register(phone)
    client=_established('browser-dialog','peer-tag','desk')
    session=lab.calls.upsert('browser-dialog',state='in_call',owner='ha_softphone',source_endpoint_id=phone.endpoint_id)
    lab.calls.observe_leg(session.call_id,session.call_id,role='ha_softphone',state='in_call',endpoint_id=phone.endpoint_id,dialog=client)
    observed=snapshot(lab.runtime,phone.endpoint_id)[0]
    assert (observed.local_tag,observed.remote_tag)==(client.dialog_ids.local_tag,'peer-tag')
    assert observed.peer_endpoint_id == ""


async def test_connected_destination_precedes_requested_target_in_peer_projection(lab):
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    from custom_components.voip_stack.dialog_info import snapshot
    from .test_ha_call_transfer import _established

    phone = PhoneEndpoint(endpoint_id="projection-browser", name="Browser", kind="browser")
    lab.runtime.endpoints.register(phone)
    client = _established("projection-priority", "peer-tag", "desk")
    session = lab.calls.upsert(
        "projection-priority", state="in_call", owner="ha_softphone",
        source_endpoint_id=phone.endpoint_id, target_endpoint_id="old-requested-target",
        dest_endpoint_id="sip:bedroom0",
    )
    lab.calls.observe_leg(session.call_id, session.call_id, role="ha_softphone",
        state="in_call", endpoint_id=phone.endpoint_id, dialog=client)
    assert snapshot(lab.runtime, phone.endpoint_id)[0].peer_endpoint_id == "sip:bedroom0"


async def test_blf_hold_uses_negotiated_media_and_contact_with_rfc_parameter_names(lab):
    from .test_ha_call_transfer import _established
    from custom_components.voip_stack.dialog_info import snapshot,document
    client=_established('held-dialog','phone-tag','desk')
    client.dialog.remote_audio_direction='sendonly'
    client.dialog.local_audio_direction='recvonly'
    client.dialog.remote_target_uri='sip:desk@127.0.0.4:5090'
    session=lab.calls.upsert('held-dialog',state='in_call',owner='bridge',dest_endpoint_id='sip:bedroom0')
    lab.calls.observe_leg(session.call_id,session.call_id,role='callee',state='in_call',endpoint_id='sip:bedroom0',dialog=client)
    states=snapshot(lab.runtime,'sip:bedroom0')
    assert len(states)==1 and states[0].held
    root=ElementTree.fromstring(document('sip:101@127.0.0.1',1,states))
    target=root.find('d:dialog/d:local/d:target',NS)
    assert target.attrib['uri']==client.dialog.remote_target_uri
    assert target.find('d:param',NS).attrib=={'pname':'+sip.rendering','pval':'no'}


@pytest.mark.parametrize("gruu", [False, True])
async def test_direct_browser_notify_has_real_early_dialog_identity_and_caller_target(lab, blf, monkeypatch, gruu):
    from dataclasses import replace
    from unittest.mock import AsyncMock
    from custom_components.voip_stack import softphone_originate
    from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
    from custom_components.voip_stack.roster import dump_roster_json

    methods, _notifications, send = blf
    source = PhoneEndpoint(endpoint_id="browser-casa", name="Casa", username="Casa",
        device_id="browser-casa-device", kind="browser", availability="available")
    lab.runtime.endpoints.register(source)
    lab.runtime.endpoints.update("sip:bedroom0", availability="available")
    lab.hass.states.async_set("sensor.voip_phonebook", "ready", {"roster_json": dump_roster_json(lab.roster)})
    monkeypatch.setattr(softphone_originate, "_ha_advertise_host", AsyncMock(return_value="127.0.0.1"))
    monkeypatch.setattr(softphone_originate, "_resolve_target_device", AsyncMock(return_value=None))
    phone = lab.phones[0]
    phone.auto_answer = False
    initial = await methods.handle(subscribe(), ("127.0.0.3", 5060), "UDP", send)
    assert initial.status == 200
    call_id = ""
    try:
        await lab.hass.services.async_call("voip_stack", "call", {
            "device_id": source.device_id, "destination": "bedroom0",
        }, blocking=True)
        call_id = lab.runtime.endpoints.get(source.endpoint_id).active_call_id
        client = lab.calls.sip_client_for(call_id)
        assert client is not None and client.dialog is None and len(client.early_dialogs) == 1
        request = subscribe(tag=initial.to_tag, cseq=2)
        if gruu:
            request = replace(request, uri="sip:101@127.0.0.1:5060;transport=udp;gr=watched-instance")
        refreshed = await methods.handle(request, ("127.0.0.3", 5060), "UDP", send)
        assert refreshed.status == 200
        assert dict(refreshed.follow_up.headers)["Event"] == "dialog"
        root = ElementTree.fromstring(refreshed.follow_up.body)
        assert root.attrib["entity"] == request.uri
        dialog = root.find("d:dialog", NS)
        assert dialog.find("d:state", NS).text == "early"
        wire = phone.invites[-1]
        assert dialog.attrib["call-id"] == wire.header("Call-ID")
        assert dialog.attrib["local-tag"] == "bedroom"
        assert dialog.attrib["remote-tag"] == sip.extract_tag(wire.header("From"))
        assert dialog.find("d:remote/d:identity", NS).text == sip.name_addr_uri(wire.header("From"))
        assert dialog.find("d:remote/d:target", NS).attrib["uri"] == sip.name_addr_uri(wire.header("Contact"))
    finally:
        if call_id:
            session = lab.calls.get_session(call_id)
            await lab.hass.services.async_call("voip_stack", "hangup", {
                "device_id": source.device_id, "call_id": call_id,
            }, blocking=True)
            await asyncio.wait_for(session.terminated.wait(), 2)
    assert not lab.calls.sessions and not lab.runtime.rtp_port_pool.get("used")


def test_dialog_fallback_peer_aor_does_not_inherit_watched_gruu():
    from custom_components.voip_stack.dialog_info import DialogState, document

    entity = "sip:430@127.0.0.1:5060;transport=udp;gr=watched-instance"
    local = "sip:430@127.0.0.4:5060;gr=actual-phone-instance"
    remote = "sip:Casa@127.0.0.1:5060;gr=actual-caller-instance"
    state = DialogState("unwired", "early", "recipient", peer_user="Casa",
                        local_target=local, remote_target=remote)
    root = ElementTree.fromstring(document(entity, 0, (state,)))
    assert root.attrib["entity"] == entity
    peer = sip.parse_sip_uri(root.find("d:dialog/d:remote/d:identity", NS).text)
    assert peer.user == "Casa" and "gr" not in dict(peer.params)
    assert dict(peer.params)["transport"] == "udp"
    assert root.find("d:dialog/d:local/d:target", NS).attrib["uri"] == local
    assert root.find("d:dialog/d:remote/d:target", NS).attrib["uri"] == remote
