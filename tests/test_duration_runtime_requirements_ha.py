"""Application leaves require their stored duration instead of inventing one."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.voip_stack.call_ivr import CallIVR
from custom_components.voip_stack import parking

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("settings", [{}, {"timeout": 47}])
async def test_parking_never_arms_a_default_residence_when_policy_is_missing(monkeypatch, settings):
    registry = SimpleNamespace(request_termination=Mock())
    monkeypatch.setattr(parking, "call_registry", lambda _hass: registry)
    monkeypatch.setattr(parking, "publish_bridge_projection", Mock())
    subject = SimpleNamespace(hass=object(), settings=settings, answer=AsyncMock(),
        lot=SimpleNamespace(arm_return=Mock()), parked=SimpleNamespace(slot="701"), _return=AsyncMock(),
        session=SimpleNamespace(call_id="parked", generation=3))
    await parking.ParkingCall.run(subject)
    if settings:
        subject.lot.arm_return.assert_called_once_with(subject.parked, 47, subject._return)
        registry.request_termination.assert_not_called()
    else:
        subject.lot.arm_return.assert_not_called()
        intent = registry.request_termination.call_args.args[1]
        assert intent.reason == "parking_failed" and intent.response_status == 503


@pytest.mark.parametrize("method", ["next_digit", "collect"])
async def test_keypad_wait_requires_an_explicit_resolved_duration(method):
    ivr = CallIVR(SimpleNamespace(discard_output=Mock()), None)
    # Queue a valid reply so a hidden default cannot turn this witness into a
    # long wall-clock wait. The call must refuse an absent policy nevertheless.
    ivr.feed("#")
    with pytest.raises(TypeError, match="timeout"):
        await getattr(ivr, method)()
