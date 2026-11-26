"""The same trunk domain and owned network hop survive every call ingress."""

import pytest

from custom_components.voip_stack.core.sip import parse_sip_uri
from .test_automation_originate_ha import lab
from .test_trunk_calls_ha import (
    trunks,
    test_automation_announcement_uses_selected_trunk_and_preserves_registration as _automation,
    test_pending_announcement_forward_uses_explicit_trunk as _forward,
    test_browser_phone_preference_and_explicit_override_dial_the_selected_line as _browser,
    test_sip_phone_uses_its_line_preference_through_real_ha_listener_and_duplex_rtp as _sip,
)

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("path", ["automation", "forward", "browser", "sip"])
async def test_trunk_logical_domain_remains_independent_of_transport_for_each_path(trunks, monkeypatch, path):
    lab = trunks
    for identity, config in tuple(lab.runtime.trunk_configs.items()):
        lab.runtime.trunk_configs[identity] = {**config, "trunk_domain": f"{identity}.voice.example.invalid"}
    lab.runtime.trunk_config = lab.runtime.trunk_configs["home"]
    if path == "automation":
        await _automation(lab, monkeypatch, "home", 0)
    elif path == "forward":
        await _forward(lab, monkeypatch)
    elif path == "browser":
        await _browser(lab, monkeypatch, "", 1)
    else:
        await _sip(lab, False)
    observed = []
    for index, identity in enumerate(("home", "work")):
        for invite in lab.phones[index].invites:
            target = parse_sip_uri(invite.uri)
            assert target.host == f"{identity}.voice.example.invalid"
            observed.append(target)
    assert observed
