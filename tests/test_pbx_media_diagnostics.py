"""Optional display reports follow real relay formats and exact SIP/API owners."""

import asyncio
from dataclasses import replace
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aioesphomeapi import UserService, UserServiceArg, UserServiceArgType
from homeassistant.config_entries import ConfigSubentry
import pytest

from custom_components.voip_stack import call_projection, device_resolver
from custom_components.voip_stack import pbx_media_diagnostics as diagnostics
from custom_components.voip_stack.core import rtp, sdp
from custom_components.voip_stack.endpoint_registry import EndpointRegistry
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint
from custom_components.voip_stack.sip_rtp_bridge import RtpPeer, SipRtpRelay
from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay, VideoRtpPeer
from tests.test_managed_phones_ha import (
    account_service_device,
    custom_integration_path as custom_integration_path,
    drain_provisioning,
    managed as managed,
    phone_lab as phone_lab,
    phone_record,
)

pytestmark = pytest.mark.ha


def fmt(rate=16000, *, codec="L16", ms=20, pt=96):
    return sdp.RtpPcmFormat(pt, codec, rate, 1, ms)


def peer(wire, host, port, **kwargs):
    return RtpPeer(host, port, wire.payload_type, wire.audio_format,
                   rtp_format=wire, send_rtp_format=wire, **kwargs)


def relay(left=None, right=None, **kwargs):
    return SipRtpRelay(left=left or peer(fmt(), "192.0.2.1", 40000),
                       right=right or peer(fmt(pt=110), "192.0.2.2", 41000),
                       left_port=42000, right_port=42002, **kwargs)


def service(key=21):
    return UserService(name="set_pbx_media_diagnostics", key=key,
                       args=[UserServiceArg(name=name, type=UserServiceArgType.STRING)
                             for name in ("call_id", "audio", "video")])


def setup(monkeypatch, media=None):
    calls = SipEndpointRuntime()
    calls.activate()
    endpoints = EndpointRegistry()
    for name in ("caller", "callee"):
        endpoints.register(PhoneEndpoint(endpoint_id=name, name=name, kind="sip_account", username=name,
                                         device_id=name, account_device_id=f"account-{name}"))
    calls.bind_endpoint_registry(endpoints)
    data = SimpleNamespace(sip=calls, endpoints=endpoints)
    monkeypatch.setattr(call_projection, "runtime_data", lambda _hass: data)
    monkeypatch.setattr(call_projection, "require_runtime_data", lambda _hass: data)
    origins = {}
    for name in ("caller", "callee"):
        native = SimpleNamespace(available=True, services={21: service()},
                                 client=SimpleNamespace(is_connected=True, execute_service=AsyncMock()))
        origins[name] = SimpleNamespace(domain="esphome", runtime_data=native)
    account_entry = SimpleNamespace(subentries={name: ConfigSubentry(subentry_type="phone", title=name,
        unique_id=f"phone:{name}", data=MappingProxyType({"kind": "sip_account", "managed_esphome": True,
            "device_id": name, "origin_entry_id": name, "username": name})) for name in origins})
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_get_entry=origins.get,
                           async_entries=lambda _domain: [account_entry]))
    discover = AsyncMock(return_value=[{"device_id": name, "origin_entry_id": name} for name in origins])
    monkeypatch.setattr(device_resolver, "get_resolver", lambda _hass: SimpleNamespace(list_devices=discover))
    session = calls.upsert("internal-session", state="in_call",
                           source_endpoint_id="caller", dest_endpoint_id="callee")
    calls.observe_leg(session.call_id, "incoming-wire", role="caller", state="in_call",
                      sip_call_id="incoming-wire", endpoint_id="caller")
    calls.observe_leg(session.call_id, "outgoing-wire", role="callee", state="in_call",
                      sip_call_id="outgoing-wire", endpoint_id="callee",
                      dialog=SimpleNamespace(dialog=SimpleNamespace(call_id="outgoing-wire")))
    media = media or relay()
    calls.attach_relay(session.call_id, media)
    return SimpleNamespace(hass=hass, data=data, session=session, media=media,
                           origins=origins, discover=discover)


async def settle(case):
    await asyncio.gather(*tuple(case.session.tasks))


async def close(case):
    await case.session.terminate(TerminationIntent("test_done"))


@pytest.mark.parametrize("source,target,operation", [
    (fmt(), fmt(pt=110), "forwarded"),
    (fmt(ms=10), fmt(ms=20), "repacketized"),
    (fmt(), fmt(48000), "resampled"),
    (fmt(8000, codec="PCMA", pt=8), fmt(8000, codec="PCMU", pt=0), "codec_converted"),
    (fmt(8000, codec="PCMA", pt=8), fmt(), "resampled_codec_converted"),
    (fmt(8000, codec="G722", pt=9), fmt(), "codec_converted"),
])
async def test_operations_come_from_observed_wire_and_current_encoder(source, target, operation):
    media = relay(left=peer(source, "192.0.2.1", 40000), right=peer(target, "192.0.2.2", 41000))
    try:
        assert media.audio_operations == ("unknown", "unknown")
        media.left_to_right_clock.observe_format(source)
        assert media.audio_operations == (operation, "unknown")
        media.right.can_receive = False
        assert media.audio_operations[0] == "inactive"
        media.right.can_receive = True
        media.right.connection_held = True
        assert media.audio_operations[0] == "inactive"
    finally:
        await media.stop()


async def test_debug_decoder_does_not_make_identical_wire_a_conversion(monkeypatch, tmp_path):
    from custom_components.voip_stack import sip_rtp_bridge

    monkeypatch.setattr(sip_rtp_bridge, "DEBUG_CAPTURE_DIR", tmp_path)
    media = relay(debug_capture=True)
    try:
        assert media.left_decoder is not None and media.right_encoder is not None
        media.left_to_right_clock.observe_format(media.left.inbound_rtp_format)
        assert media.audio_operations == ("forwarded", "unknown")
    finally:
        await media.stop()


async def test_actual_alias_first_packet_changes_report_without_aggregate_route_change(monkeypatch):
    preferred, alias = fmt(48000, ms=10), fmt(16000, ms=10, pt=97)
    target = fmt(48000, ms=10, pt=110)
    media = relay(left=peer(preferred, "192.0.2.1", 40000, inbound_rtp_formats=(preferred, alias)),
                  right=peer(target, "192.0.2.2", 41000))
    case = setup(monkeypatch, media)
    media.left_transport = Mock()
    media.right_transport = Mock()
    route_events = Mock(wraps=call_projection.publish_media_route)
    monkeypatch.setattr(call_projection, "publish_media_route", route_events)
    try:
        call_projection.publish_media_route(case.hass, case.session.call_id, media.media_route)
        await settle(case)
        before = media.media_route
        sink = case.origins["caller"].runtime_data.client.execute_service
        assert sink.call_args.args[1] == {"call_id": "incoming-wire", "audio": "unknown,unknown", "video": "inactive,inactive"}
        media.handle_packet("left", rtp.build_packet(rtp.RtpPacket(alias.payload_type,
                            1, 0, 123, b"\0" * 320)), ("192.0.2.1", 40000))
        await settle(case)
        assert media.left_rx_packets == 1
        assert media.media_route == before == "ha_transcoding"
        assert route_events.call_count == 1
        assert sink.call_args.args[1]["audio"] == "resampled,unknown"
        assert case.origins["callee"].runtime_data.client.execute_service.call_args.args[1]["audio"] == "unknown,resampled"
        count = sink.await_count
        media.handle_packet("left", rtp.build_packet(rtp.RtpPacket(alias.payload_type,
                            2, 160, 123, b"\0" * 320)), ("192.0.2.1", 40000))
        await settle(case)
        assert sink.await_count == count
        assert route_events.call_count == 1
        assert media.right_decoder is None
    finally:
        await close(case)


async def test_video_uses_committed_transcoder_direction_and_hold(monkeypatch):
    case = setup(monkeypatch)
    video_fmt = sdp.RtpVideoFormat(payload_type=98, encoding="JPEG")
    video = SipVideoRtpRelay(left=VideoRtpPeer("192.0.2.1", 40004, 40005, video_fmt),
                             right=VideoRtpPeer("192.0.2.2", 41004, 41005, video_fmt),
                             left_port=42004, right_port=42006)
    case.media.video_relay = video
    try:
        # This is the active generation, including same-codec JPEG geometry work.
        video._transcode.directions.add("left")
        video._transcode.transcoders["left"] = SimpleNamespace(
            ready=True, async_close=AsyncMock())
        assert diagnostics._video_operations(case.media) == ("converted", "forwarded")
        video.right.video_format = replace(video_fmt, direction="sendonly")
        assert diagnostics._video_operations(case.media) == ("inactive", "forwarded")
        video.right.video_format = video_fmt
        video.left.connection_held = True
        assert diagnostics._video_operations(case.media) == ("converted", "inactive")
    finally:
        await close(case)


async def test_video_processing_updates_use_current_owner_without_route_events(monkeypatch):
    case = setup(monkeypatch)
    video_fmt = sdp.RtpVideoFormat(payload_type=26, encoding="JPEG")
    video = SipVideoRtpRelay(
        left=VideoRtpPeer("192.0.2.1", 40004, 40005, video_fmt),
        right=VideoRtpPeer("192.0.2.2", 41004, 41005, video_fmt),
        left_port=42004, right_port=42006,
    )
    known = {"left": True, "right": True}
    monkeypatch.setattr(video, "processing_known_from", lambda side: known[side])
    case.media.attach_video_relay(video)
    publish = Mock(wraps=call_projection.publish_media_route)
    monkeypatch.setattr(call_projection, "publish_media_route", publish)
    try:
        call_projection.publish_media_route(case.hass, case.session.call_id, case.media.media_route)
        await settle(case)
        sink = case.origins["caller"].runtime_data.client.execute_service
        assert sink.call_args.args[1]["video"] == "forwarded,forwarded"
        assert publish.call_count == 1

        known["left"] = False
        video.on_media_processing_changed()
        await settle(case)
        assert sink.call_args.args[1]["video"] == "unknown,forwarded"

        known["left"] = True
        video._transcode.directions.add("left")
        video.on_media_processing_changed()
        await settle(case)
        assert sink.call_args.args[1]["video"] == "converted,forwarded"
        assert publish.call_count == 1

        # A rebinding and a media handoff retire the old observer independently.
        stale = video.on_media_processing_changed
        case.media.bind_video_processing_observer()
        count = sink.await_count
        stale()
        await settle(case)
        assert sink.await_count == count
        stale = video.on_media_processing_changed
        case.media.video_relay = None
        stale()
        await settle(case)
        assert sink.await_count == count
        assert publish.call_count == 1
    finally:
        await close(case)
        await video.stop()


async def test_exact_leg_ids_native_inventory_and_delivery_dedup(monkeypatch):
    case = setup(monkeypatch)
    try:
        case.media.left_to_right_clock.observe_format(case.media.left.inbound_rtp_format)
        diagnostics.publish_pbx_media_diagnostics(case.hass, case.data, case.session)
        await settle(case)
        for name, cid, audio in (("caller", "incoming-wire", "forwarded,unknown"),
                                  ("callee", "outgoing-wire", "unknown,forwarded")):
            sink = case.origins[name].runtime_data.client.execute_service
            assert sink.call_args.args == (case.origins[name].runtime_data.services[21],
                                           {"call_id": cid, "audio": audio, "video": "inactive,inactive"})
        diagnostics.publish_pbx_media_diagnostics(case.hass, case.data, case.session)
        await settle(case)
        assert case.origins["caller"].runtime_data.client.execute_service.await_count == 1
        native = case.origins["caller"].runtime_data
        native.services = {99: service(99)}
        diagnostics.publish_pbx_media_diagnostics(case.hass, case.data, case.session)
        await settle(case)
        assert native.client.execute_service.call_args.args[0].key == 99
    finally:
        await close(case)


async def test_delivery_reads_latest_formats_after_discovery_and_client_replacement(monkeypatch):
    case = setup(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    devices = case.discover.return_value

    async def paused():
        entered.set()
        await release.wait()
        return devices

    case.discover.side_effect = paused
    diagnostics.publish_pbx_media_diagnostics(case.hass, case.data, case.session)
    await entered.wait()
    case.media.left_to_right_clock.observe_format(case.media.left.inbound_rtp_format)
    previous = case.origins["caller"].runtime_data.client
    current = SimpleNamespace(is_connected=True, execute_service=AsyncMock())
    case.origins["caller"].runtime_data.client = current
    release.set()
    try:
        await settle(case)
        previous.execute_service.assert_not_awaited()
        assert current.execute_service.call_args.args[1]["audio"] == "forwarded,unknown"
        assert len(case.session.resources) == 1
    finally:
        await close(case)


async def test_changed_output_encoder_format_updates_reverse_operation(monkeypatch):
    wire, alias = fmt(48000, ms=10), fmt(16000, ms=10, pt=97)
    media = relay(left=peer(wire, "192.0.2.1", 40000, outbound_rtp_formats=(wire, alias),
                            match_received_codec=True),
                  right=peer(wire, "192.0.2.2", 41000))
    try:
        media.right_to_left_clock.observe_format(wire)
        assert media.audio_operations[1] == "forwarded"
        assert media.left_encoder.follow_received(alias)
        assert media.left_encoder.fmt == alias
        assert media.audio_operations[1] == "resampled"
    finally:
        await media.stop()


@pytest.mark.parametrize("reason", ["unsupported", "offline", "error", "missing-leg", "wrong-dialog"])
async def test_optional_channel_failure_never_changes_sip_or_uses_session_id(monkeypatch, reason):
    case = setup(monkeypatch)
    native = case.origins["caller"].runtime_data
    if reason == "unsupported":
        native.services = {}
    elif reason == "offline":
        native.available = False
    elif reason == "error":
        native.client.execute_service.side_effect = RuntimeError("Information channel failed")
    elif reason == "missing-leg":
        case.session.legs.pop("incoming-wire")
    else:
        case.session.legs["incoming-wire"].dialog = SimpleNamespace(call_id="foreign-call")
    try:
        diagnostics.publish_pbx_media_diagnostics(case.hass, case.data, case.session)
        await settle(case)
        assert case.session.live and case.session.state == "in_call"
        assert case.session.call_id == "internal-session"
        assert native.client.execute_service.await_count == (1 if reason == "error" else 0)
        assert case.origins["callee"].runtime_data.client.execute_service.await_count == 1
    finally:
        await close(case)


@pytest.mark.parametrize("change", ["relay", "leg", "state", "generation"])
async def test_queued_delivery_rechecks_call_resource_and_leg_identity(monkeypatch, change):
    case = setup(monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    devices = case.discover.return_value

    async def paused():
        entered.set()
        await release.wait()
        return devices

    case.discover.side_effect = paused
    diagnostics.publish_pbx_media_diagnostics(case.hass, case.data, case.session)
    await entered.wait()
    if change == "relay":
        case.session.release_resource(f"relay:{case.session.call_id}", value=case.media)
    elif change == "leg":
        case.session.legs["incoming-wire"].sip_call_id = "different-wire"
    elif change == "state":
        case.data.sip.upsert(case.session.call_id, state="terminating")
    else:
        case.data.sip.calls.pop(case.session.call_id)
    release.set()
    try:
        await settle(case)
        assert all(origin.runtime_data.client.execute_service.await_count == 0 for origin in case.origins.values())
    finally:
        await close(case)
        await case.media.stop()


async def test_termination_cancels_pending_optional_delivery(monkeypatch):
    case = setup(monkeypatch)
    entered = asyncio.Event()

    async def paused():
        entered.set()
        await asyncio.Event().wait()

    case.discover.side_effect = paused
    diagnostics.publish_pbx_media_diagnostics(case.hass, case.data, case.session)
    await entered.wait()
    await close(case)
    assert not case.session.tasks and not case.session.named_tasks
    assert all(origin.runtime_data.client.execute_service.await_count == 0 for origin in case.origins.values())


async def test_canonical_managed_sip_account_uses_native_hardware_binding(managed):
    from custom_components.voip_stack.esphome_native import ProvisioningActions

    lab = managed
    native = lab.native.runtime_data
    native.services = {**native.services, 21: service()}
    client = native.client
    sent = AsyncMock(wraps=client.execute_service)
    client.execute_service = sent
    await lab.controller.async_sync()
    await drain_provisioning(lab, lab.controller)
    phone = phone_record(lab)
    endpoint = lab.runtime.endpoints.get(phone.endpoint_id)
    assert endpoint.kind.value == "sip_account"
    assert endpoint.username == phone.username
    assert endpoint.device_id == lab.device.id
    assert endpoint.account_device_id == account_service_device(lab).id
    assert endpoint.account_device_id != endpoint.device_id
    lab.sent.clear()
    sent.reset_mock()
    api_owner = ProvisioningActions.capture(native)
    calls = lab.runtime.sip
    session = calls.upsert("internal-managed-session", state="in_call", source_endpoint_id=phone.endpoint_id)
    calls.observe_leg(session.call_id, "actual-incoming-sip-id", role="caller", state="in_call",
                      endpoint_id=phone.endpoint_id, sip_call_id="actual-incoming-sip-id")
    media = relay()
    calls.attach_relay(session.call_id, media)
    try:
        diagnostics.publish_pbx_media_diagnostics(lab.hass, lab.runtime, session)
        await asyncio.gather(*tuple(session.tasks))
        payload = {"call_id": "actual-incoming-sip-id", "audio": "unknown,unknown", "video": "inactive,inactive"}
        assert lab.sent == [("set_pbx_media_diagnostics", payload)]
        sent.assert_awaited_once_with(native.services[21], payload)
        assert native.client is client
        assert session.metadata["pbx_media_diagnostics"]["delivered"][phone.endpoint_id][0].same_owner(api_owner)
        assert not lab.events
    finally:
        await session.terminate(TerminationIntent("test_done"))


@pytest.mark.parametrize("field,value", [
    ("managed_esphome", False), ("origin_entry_id", "foreign-origin"), ("username", "Foreign account"),
])
async def test_hardware_delivery_requires_the_current_account_association(monkeypatch, field, value):
    case = setup(monkeypatch)
    case.session.metadata["dest_endpoint_id"] = ""
    entry = case.hass.config_entries.async_entries("voip_stack")[0]
    row = entry.subentries["caller"]
    entry.subentries["caller"] = ConfigSubentry(subentry_type=row.subentry_type, title=row.title,
        unique_id=row.unique_id, data=MappingProxyType({**row.data, field: value}))
    peers = (case.media.left, case.media.right)
    legs = tuple(case.session.legs.items())
    try:
        diagnostics.publish_pbx_media_diagnostics(case.hass, case.data, case.session)
        await settle(case)
        assert all(origin.runtime_data.client.execute_service.await_count == 0 for origin in case.origins.values())
        assert case.session.live and case.session.state == "in_call"
        assert tuple(case.session.legs.items()) == legs
        assert (case.media.left, case.media.right) == peers
        assert case.media.audio_operations == ("unknown", "unknown")
        assert case.media.left_rx_packets == case.media.right_rx_packets == case.media.forwarded == 0
    finally:
        await close(case)
