from __future__ import annotations

import pytest

from custom_components.voip_stack.inbound_routing.automation import AutomationRoute

pytestmark = pytest.mark.ha


def test_answer_ha_route_preserves_selected_endpoint() -> None:
    route = AutomationRoute.from_payload(
        {
            "action": "answer_ha",
            "endpoint_id": "browser:casa",
        }
    )

    assert route.action == "answer_ha"
    assert route.endpoint_id == "browser:casa"
