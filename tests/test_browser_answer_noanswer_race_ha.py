"""Public two-browser Answer arbitration at the phone's voicemail deadline."""

import asyncio
import socket
import struct

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError

from custom_components.voip_stack.call_scope import call_belongs_to_endpoint
from custom_components.voip_stack.core import rtp
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint
from custom_components.voip_stack.phone_policy import PhonePolicy
from custom_components.voip_stack.phone_policy_runtime import setup_phone_policy_events
from custom_components.voip_stack.runtime_diagnostics import runtime_resource_snapshot
from custom_components.voip_stack.store import config_entry
from custom_components.voip_stack.websocket_api import CALL_EVENT, _ha_softphone_store

from .test_automation_originate_ha import lab as lab
from .test_browser_phone_policy_ha import browser_lab as browser_lab

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("winner", ["answer", "voicemail"])
async def test_two_browser_answer_arbitrates_with_committed_voicemail(
    browser_lab, tmp_path, record_property, winner,
):
    """A retired callee cannot answer or disturb the caller's new application."""
    current = browser_lab
    current.hass.config.config_dir = str(tmp_path)
    current.hass.config.media_dirs = {"local": str(tmp_path / "media")}
    entry = config_entry(current.hass)
    current.hass.config_entries.async_add_subentry(entry, ConfigSubentry(
        subentry_type="phone", title="Kitchen", unique_id="phone:kitchen",
        data={"endpoint_id": "kitchen", "name": "kitchen", "kind": "browser",
              "voicemail": {"enabled": True, "max_seconds": 60}},
    ))
    current.runtime.endpoints.update("kitchen", policy=PhonePolicy.from_data({
        "ringing_timeout": 10,
        "no_answer": {"kind": "voicemail", "target": "kitchen"},
    }))
    current.runtime.endpoints.register(PhoneEndpoint(
        endpoint_id="third", name="Third phone", device_id="device-third",
        kind=EndpointKind.BROWSER, availability="available",
        capabilities=frozenset({"audio", "dtmf"}),
    ))
    setup_phone_policy_events(current.hass, entry)
    ringing = asyncio.Event()
    recording = asyncio.Event()
    projections = []

    @callback
    def observe(event):
        projections.append(dict(event.data))
        if event.data.get("endpoint_id") == "kitchen" and event.data.get("state") == "ringing":
            ringing.set()
        if event.data.get("event_type") == "answered":
            application = current.calls.resource_for(event.data["call_id"], "voicemail")
            if application is not None and application.playback is not None:
                application.playback.attach(recording)
                application.playback.ready(recording)
        if event.data.get("event_type") == "voicemail_recording":
            recording.set()

    unsubscribe = current.hass.bus.async_listen(CALL_EVENT, observe)
    call_id = ""
    source_socket = None
    try:
        await current.hass.services.async_call("voip_stack", "call", {
            "device_id": "device-browser", "destination": "kitchen",
            "media_client_id": "caller-browser-A",
        }, blocking=True)
        await asyncio.wait_for(ringing.wait(), 2)
        call_id = _ha_softphone_store(current.hass, "kitchen")["call_id"]
        session = current.calls.get_session(call_id)
        token = session.token
        deadline = current.calls.resource_for(call_id, "deadline:phone_policy")
        assert call_id.startswith("local-") and deadline is not None
        assert call_belongs_to_endpoint(current.calls, call_id, "browser")
        assert call_belongs_to_endpoint(current.calls, call_id, "kitchen")
        answer = {"device_id": "device-kitchen", "call_id": call_id,
                  "media_client_id": "callee-browser-B"}

        # A third phone is never an eligible participant, before either winner.
        with pytest.raises(ServiceValidationError):
            await current.hass.services.async_call("voip_stack", "answer", {
                **answer, "device_id": "device-third",
            }, blocking=True)
        assert current.calls.get_session(call_id) is session
        assert _ha_softphone_store(current.hass, "kitchen")["state"] == "ringing"

        if winner == "answer":
            await current.hass.services.async_call(
                "voip_stack", "answer", answer, blocking=True,
            )
            deadline._expired()  # Deterministic late timer, never a wall-clock sleep.
            assert session.token == token and session.owner == "local_bridge"
            assert session.state == "in_call"
            assert current.calls.resource_for(call_id, "voicemail") is None
            caller = current.bridge.acquire_media(call_id, "browser", "caller-browser-A")
            callee = current.bridge.acquire_media(call_id, "kitchen", "callee-browser-B")
            for sender, lease, receiver, peer, sample in (
                ("browser", caller, "kitchen", callee, 2000),
                ("kitchen", callee, "browser", caller, -3000),
            ):
                pcm = struct.pack("<320h", *([sample] * 320))
                current.bridge.send_audio(call_id, sender, lease.token, pcm)
                assert current.bridge.receive_audio_nowait(call_id, receiver, peer.token) == pcm
        else:
            deadline._expired()  # Commit the actual fallback before delivering Answer.
            await asyncio.wait_for(recording.wait(), 3)
            application = current.calls.resource_for(call_id, "voicemail")
            media = current.calls.resource_for(call_id, "relay")
            assert application is not None and media is application.media
            assert session.token == token and session.owner == "voicemail"
            assert _ha_softphone_store(current.hass, "kitchen")["state"] == "idle"
            assert not call_belongs_to_endpoint(current.calls, call_id, "kitchen")
            assert call_belongs_to_endpoint(current.calls, call_id, "browser")
            assert not current.runtime.endpoints.require("kitchen").active_call_id
            assert current.runtime.endpoints.require("browser").active_call_id == call_id
            with pytest.raises(ServiceValidationError) as stale_answer:
                await current.hass.services.async_call(
                    "voip_stack", "answer", answer, blocking=True,
                )
            assert stale_answer.value.translation_key == "call_not_ringing"
            assert stale_answer.value.translation_placeholders == {"call_id": call_id}
            assert "no longer ringing" in str(stale_answer.value)
            record_property("late_answer_error", str(stale_answer.value))
            # Reject a genuine foreign phone too; neither rejected action owns media.
            with pytest.raises(ServiceValidationError, match="another phone endpoint"):
                await current.hass.services.async_call("voip_stack", "answer", {
                    **answer, "device_id": "device-third",
                }, blocking=True)
            assert session.token == token and session.owner == "voicemail"
            assert current.calls.resource_for(call_id, "voicemail") is application
            assert current.calls.resource_for(call_id, "relay") is media
            assert _ha_softphone_store(current.hass, "browser")["state"] == "in_call"
            assert _ha_softphone_store(current.hass, "kitchen")["state"] == "idle"
            assert current.bridge.get_call(call_id) is None
            assert not current.runtime.media.owners_for("audio")
            assert not current.runtime.media.owners_for("video")

            # Independently decode caller PCM after both rejections; the fallback lives.
            received = asyncio.Event()
            decoded = []
            detach = media.decoder.add_pcm_observer(
                lambda pcm, *_args: (decoded.append(pcm), received.set())
            )
            try:
                source_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                source_socket.setblocking(False)
                source_socket.bind(("127.0.0.1", application.invite.remote_rtp_port))
                fmt = application.invite.recv_format
                samples = fmt.sample_rate * fmt.frame_ms // 1000 * fmt.channels
                payload = struct.pack(f"!{samples}h", *([1800] * samples))
                packet = rtp.build_packet(rtp.RtpPacket(fmt.payload_type, 0, 0, 567, payload))
                await current.hass.loop.sock_sendto(
                    source_socket, packet, ("127.0.0.1", media.local_rtp_port),
                )
                await asyncio.wait_for(received.wait(), 2)
                assert decoded == [struct.pack(f"<{samples}h", *([1800] * samples))]
            finally:
                detach()
        assert not any(peer.invites for peer in current.phones)
        assert not current.runtime.endpoints.require("third").active_call_id
        record_property("winner", winner)
        record_property("same_call_id", call_id)
    finally:
        if source_socket is not None:
            source_socket.close()
        if call_id and current.calls.get_session(call_id) is not None:
            await current.hass.services.async_call("voip_stack", "hangup", {
                "device_id": "device-browser", "call_id": call_id,
            }, blocking=True)
            barrier = current.calls.request_termination(call_id, TerminationIntent("test_done"))
            if barrier is not None:
                await barrier
        unsubscribe()
    resources = runtime_resource_snapshot(
        current.calls, detailed=True, rtp_port_pool=current.runtime.rtp_port_pool,
        browser_media=current.runtime.media,
    )
    assert resources["call_scoped_quiescent"], resources
    assert all(value == 0 for value in resources["resource_counts"].values()), resources
    assert not current.calls.sessions
    assert current.bridge.get_call(call_id) is None
    assert all(not endpoint.active_call_id for endpoint in current.runtime.endpoints.endpoints)
