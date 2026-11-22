"""Local video receive intent remains independent of camera transmission."""

import pytest

from custom_components.voip_stack.endpoint_registry import EndpointRegistry
from custom_components.voip_stack.local_softphone_bridge import LocalSoftphoneBridge
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
from custom_components.voip_stack.core import sdp
from custom_components.voip_stack.core.media.capability import resolve_peer_media_profile

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("receive,send,caller_direction,callee_direction", [
    (False, False, "inactive", "inactive"), (True, False, "recvonly", "sendonly"),
    (False, True, "sendonly", "recvonly"), (True, True, "sendrecv", "sendrecv")])
def test_local_answer_uses_explicit_directional_intent(receive, send, caller_direction, callee_direction):
    registry = EndpointRegistry()
    for username in ("caller", "callee"):
        registry.register(PhoneEndpoint(endpoint_id=username, name=username, kind=EndpointKind.BROWSER,
            availability="available", capabilities=frozenset({"audio", "video"})))
    bridge = LocalSoftphoneBridge(registry)
    started = bridge.start_call("caller", "callee", request_video=receive, enable_caller_video_send=send)
    answered = bridge.answer(started.call_id, "callee", enable_video_send=True, accept_video=True)
    assert answered.call.video_direction_for("caller") == caller_direction
    assert answered.call.video_direction_for("callee") == callee_direction
    assert answered.call.caller_video_receive is receive


def test_video_engine_does_not_add_a_stream_from_peer_capability_alone():
    peer = resolve_peer_media_profile({"sip_profile": "dahua"})
    assert sdp.plan_browser_video_offer("H264", peer_profile=peer) == ()
    receiving = sdp.plan_browser_video_offer("H264", receive=True, peer_profile=peer)
    assert len(receiving) == 1 and receiving[0].packetization_mode == 0
    sending = sdp.plan_browser_video_offer("H264", camera_send=True, receive=False, peer_profile=peer)
    assert sending and all(fmt.packetization_mode == 1 for fmt in sending)
