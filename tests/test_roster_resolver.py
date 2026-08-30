#!/usr/bin/env python3
"""Roster destination resolution contracts."""

from __future__ import annotations

from .voip_phase1_support import (
    roster,
    router,
    unittest,
)


class RosterResolverTest(unittest.TestCase):
    def test_number_contact_routes_through_ha_trunk(self) -> None:
        entries = roster.parse_roster_json(
            {
                "contacts": [
                    {"id": "Nonna", "number": "+12025550100"},
                ]
            }
        )
        phone_from_ha = router.resolve_ha_router("Nonna", entries, trunk_ready=True)
        self.assertEqual(phone_from_ha.action, router.RouteAction.TRUNK)
        self.assertEqual(phone_from_ha.target, "+12025550100")
