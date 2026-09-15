"""Browser source handoff uses a real, supported RTP contract."""

from types import SimpleNamespace

import pytest

from custom_components.voip_stack import local_softphone_runtime as local
from custom_components.voip_stack.phone_endpoint import EndpointKind, PhoneEndpoint

pytestmark = pytest.mark.ha


def test_pending_browser_source_offers_mono_rtp_without_lowering_sample_rate(monkeypatch):
    phones = {
        name: PhoneEndpoint(endpoint_id=name, name=name, kind=EndpointKind.BROWSER)
        for name in ("caller", "callee")
    }
    media = {"local_bridge": True}
    registry = SimpleNamespace(
        get_session=lambda _id: SimpleNamespace(token=object()),
        resource_for=lambda *_args: media,
    )
    monkeypatch.setattr(local, "call_registry", lambda _hass: registry)
    monkeypatch.setattr(local, "local_softphone_bridge", lambda _hass: object())
    monkeypatch.setattr(local, "_endpoint", lambda _hass, name: phones[name])
    source = local.PendingLocalSource(
        object(),
        SimpleNamespace(call_id="local-call", caller_endpoint_id="caller",
                        callee_endpoint_id="callee", callee_state=local.LocalCallState.RINGING),
        "127.0.0.1", 15060,
    )
    assert source.invite.call_id == "local-call"
    for format in (source.invite.send_format, source.invite.recv_format):
        assert format.audio_format.sample_rate == 48000
        assert format.audio_format.channels == 1
        assert format.audio_format.nominal_frame_bytes <= 1200
    assert media == {"local_bridge": True}
