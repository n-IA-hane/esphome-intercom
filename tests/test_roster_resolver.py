#!/usr/bin/env python3
"""Roster destination resolution contracts."""

from __future__ import annotations

from .voip_phase1_support import (
    roster,
    router,
    unittest,
)


class RosterResolverTest(unittest.TestCase):
    def test_route_decisions(self) -> None:
        entries = roster.parse_roster_json(
            {
                "contacts": [
                    {"id": "HA", "address": "192.168.1.10"},
                    {"id": "Cucina", "address": "192.168.1.30"},
                    {
                        "id": "Studio",
                        "address": "192.168.1.31",
                        "metadata": {"sip_transport": "tcp"},
                    },
                    {"id": "Corridoio"},
                    {"id": "Nonna", "number": "0574863562"},
                ]
            }
        )
        ha_uri = "sip:Home@192.168.1.10;transport=tcp"
        cucina = router.resolve_esp_origin("Cucina", entries, ha_uri)
        self.assertEqual(cucina.action, router.RouteAction.DIRECT)
        self.assertEqual(cucina.sip_uri, "sip:Cucina@192.168.1.30")

        studio = router.resolve_esp_origin("Studio", entries, ha_uri)
        self.assertEqual(studio.action, router.RouteAction.DIRECT)
        self.assertEqual(studio.sip_uri, "sip:Studio@192.168.1.31;transport=tcp")

        corridoio = router.resolve_esp_origin("Corridoio", entries, ha_uri)
        self.assertEqual(corridoio.action, router.RouteAction.BRIDGE)
        self.assertEqual(corridoio.sip_uri, "sip:Corridoio@192.168.1.10;transport=tcp")

        phone_from_esp = router.resolve_esp_origin("Nonna", entries, ha_uri)
        self.assertEqual(phone_from_esp.action, router.RouteAction.BRIDGE)
        self.assertEqual(phone_from_esp.target, "Nonna")

        phone_from_ha = router.resolve_ha_router("Nonna", entries, trunk_ready=True)
        self.assertEqual(phone_from_ha.action, router.RouteAction.TRUNK)
        self.assertEqual(phone_from_ha.target, "0574863562")

    def test_explicit_sip_uri_and_name_at_ip(self) -> None:
        entries = roster.parse_roster_json([{"id": "HA", "address": "192.168.1.10"}])
        self.assertEqual(
            router.resolve_esp_origin("sip:Cucina@192.168.1.30", entries, "sip:Home@192.168.1.10").sip_uri,
            "sip:Cucina@192.168.1.30",
        )
        self.assertEqual(
            router.resolve_esp_origin("Cucina@192.168.1.30", entries, "sip:Home@192.168.1.10").sip_uri,
            "sip:Cucina@192.168.1.30",
        )

    def test_sip_transport_is_separate_from_endpoint_transport(self) -> None:
        entries = roster.parse_roster_json(
            {
                "contacts": [
                    {
                        "id": "Casa",
                        "address": "192.168.1.10",
                        "metadata": {"sip_transport": "tcp", "sip_port": 5060},
                    },
                    {
                        "id": "Cucina",
                        "address": "192.168.1.30",
                        "metadata": {"sip_transport": "tcp", "sip_port": 5060},
                    },
                    {
                        "id": "Salotto",
                        "address": "192.168.1.31",
                        "metadata": {"sip_transport": "udp", "sip_port": 5060},
                    },
                ]
            }
        )
        self.assertEqual(
            router.resolve_esp_origin("Cucina", entries, "sip:Casa@192.168.1.10;transport=tcp").sip_uri,
            "sip:Cucina@192.168.1.30;transport=tcp",
        )
        self.assertEqual(
            router.resolve_esp_origin("Salotto", entries, "sip:Casa@192.168.1.10;transport=tcp").sip_uri,
            "sip:Salotto@192.168.1.31;transport=udp",
        )
        bridged_entries = [
            entries[0],
            entries[1],
            roster.RosterEntry(
                id="Salotto",
                address="192.168.1.31",
                ha_bridge=True,
                metadata={"sip_transport": "udp", "sip_port": 5060},
            ),
        ]
        self.assertEqual(
            router.resolve_esp_origin(
                "Salotto",
                bridged_entries,
                "sip:Casa@192.168.1.10;transport=tcp",
            ).sip_uri,
            "sip:Salotto@192.168.1.10;transport=tcp",
        )
