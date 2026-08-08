#!/usr/bin/env python3
"""Inbound and outbound route selection contracts."""

from __future__ import annotations

from .voip_phase1_support import (
    PKG_DIR,
    roster,
    router,
    unittest,
)


class RouterContractTest(unittest.TestCase):
    def test_secure_sip_uri_stays_on_the_direct_tls_route(self) -> None:
        target = "sips:door@pbx.example:5061"
        decision = router.resolve_ha_router(target, [], trunk_ready=True)
        self.assertEqual(decision.action, router.RouteAction.DIRECT)
        self.assertEqual(decision.sip_uri, target)
        self.assertEqual(decision.reason, router.RouteReason.DIRECT_URI)

    def test_trusted_inbound_automation_can_select_a_direct_sip_uri(self) -> None:
        target = "sip:callee@127.0.0.1:20020"
        context = router.CallContext(call_id="direct-automation", direction="inbound", origin="trunk", route_hint=target)
        decision = router.route_inbound_trunk(context, [])
        self.assertEqual(decision.action, router.RouteAction.DIRECT)
        self.assertEqual(decision.sip_uri, target)

    def test_sip_video_offer_is_not_gated_by_target_roster_capabilities(self) -> None:
        source = (PKG_DIR / "softphone_originate.py").read_text()
        self.assertIn(
            "use_trunk or not native_audio_endpoint or esphome_sip_endpoint",
            source,
        )
        self.assertNotIn("target_video_enabled", source)
        self.assertNotIn('target_endpoint.supports("video")', source)

    def _matrix_entries(self):
        return roster.parse_roster_json(
            [
                {
                    "id": "Casa",
                    "name": "Casa",
                    "address": "192.168.1.10",
                    "metadata": {"local_ha": True, "sip_transport": "tcp", "sip_port": 5060},
                },
                {
                    "id": "Spotpear",
                    "name": "Spotpear",
                    "address": "192.168.1.31",
                    "extension": "101",
                    "metadata": {"sip_transport": "udp", "sip_port": 5060},
                },
                {
                    "id": "WS3",
                    "name": "WS3",
                    "address": "192.168.1.47",
                    "extension": "102",
                    "metadata": {"sip_transport": "udp", "sip_port": 5060},
                },
                {
                    "id": "Zoiper",
                    "name": "Zoiper",
                    "sip_uri": "sip:Zoiper@192.168.1.17:57029;transport=tcp",
                    "extension": "201",
                    "metadata": {"registered": True, "sip_transport": "tcp"},
                },
                {"id": "Daniele", "name": "Daniele", "number": "3510000000"},
            ]
        )

    def test_dialplan_matrix_core_routes(self) -> None:
        entries = self._matrix_entries()
        ha_uri = "sip:Casa@192.168.1.10:5060;transport=tcp"

        cases = [
            (
                "ESP calls HA by name",
                router.resolve_esp_origin("Casa", entries, ha_uri),
                router.RouteAction.DIRECT,
                "sip:Casa@192.168.1.10;transport=tcp",
                "Casa",
            ),
            (
                "ESP calls external contact by name through HA",
                router.resolve_esp_origin("Daniele", entries, ha_uri),
                router.RouteAction.BRIDGE,
                "sip:Daniele@192.168.1.10;transport=tcp",
                "Daniele",
            ),
            (
                "ESP dials internal extension through HA",
                router.resolve_esp_origin("101", entries, ha_uri),
                router.RouteAction.BRIDGE,
                "sip:101@192.168.1.10;transport=tcp",
                "101",
            ),
            (
                "ESP calls another ESP by name direct",
                router.resolve_esp_origin("WS3", entries, ha_uri),
                router.RouteAction.DIRECT,
                "sip:WS3@192.168.1.47;transport=udp",
                "WS3",
            ),
            (
                "HA calls ESP by name",
                router.resolve_ha_router("Spotpear", entries, trunk_ready=True),
                router.RouteAction.FORWARD,
                "sip:Spotpear@192.168.1.31;transport=udp",
                "Spotpear",
            ),
            (
                "HA calls ESP by extension",
                router.resolve_ha_router("101", entries, trunk_ready=True),
                router.RouteAction.FORWARD,
                "sip:Spotpear@192.168.1.31;transport=udp",
                "Spotpear",
            ),
            (
                "HA calls registered softphone",
                router.resolve_ha_router("Zoiper", entries, trunk_ready=True),
                router.RouteAction.FORWARD,
                "sip:Zoiper@192.168.1.17:57029;transport=tcp",
                "Zoiper",
            ),
            (
                "HA calls external contact number via trunk",
                router.resolve_ha_router("Daniele", entries, trunk_ready=True),
                router.RouteAction.TRUNK,
                "",
                "3510000000",
            ),
            (
                "HA calls raw external number via trunk",
                router.resolve_ha_router("3510000000", entries, trunk_ready=True),
                router.RouteAction.TRUNK,
                "",
                "3510000000",
            ),
        ]
        for label, decision, action, sip_uri, target in cases:
            with self.subTest(label):
                self.assertEqual(decision.action, action)
                self.assertEqual(decision.target, target)
                if sip_uri:
                    self.assertEqual(decision.sip_uri, sip_uri)

    def test_dialplan_matrix_inbound_trunk_and_failures(self) -> None:
        entries = self._matrix_entries()
        no_hint = router.route_inbound_trunk(
            router.CallContext(call_id="in-1", direction="inbound", origin="trunk"),
            entries,
            trunk_ready=True,
        )
        self.assertEqual(no_hint.action, router.RouteAction.ANSWER_HA)

        extension_hint = router.route_inbound_trunk(
            router.CallContext(
                call_id="in-2",
                direction="inbound",
                origin="trunk",
                route_hint="101",
            ),
            entries,
            trunk_ready=True,
        )
        self.assertEqual(extension_hint.action, router.RouteAction.FORWARD)
        self.assertEqual(extension_hint.target, "Spotpear")

        external_number_hint = router.route_inbound_trunk(
            router.CallContext(
                call_id="in-3",
                direction="inbound",
                origin="trunk",
                route_hint="3510000000",
            ),
            entries,
            trunk_ready=True,
        )
        self.assertEqual(external_number_hint.action, router.RouteAction.REJECT)
        self.assertEqual(external_number_hint.reason, router.RouteReason.ROUTE_NOT_FOUND)

        missing_trunk = router.resolve_ha_router("Daniele", entries, trunk_ready=False)
        self.assertEqual(missing_trunk.action, router.RouteAction.REJECT)
        self.assertEqual(missing_trunk.reason, router.RouteReason.TRUNK_UNAVAILABLE)

    def test_esp_numeric_target_always_bridges_to_ha(self) -> None:
        entries = roster.parse_roster_json(
            [
                {"id": "HA", "address": "192.168.1.10"},
                {"id": "200", "address": "192.168.1.20", "metadata": {"sip_transport": "udp"}},
            ]
        )
        decision = router.resolve_esp_origin("200", entries, "sip:200@192.168.1.10;transport=tcp")
        self.assertEqual(decision.action, router.RouteAction.BRIDGE)
        self.assertEqual(decision.reason, router.RouteReason.NUMBER_VIA_HA)
        self.assertEqual(decision.sip_uri, "sip:200@192.168.1.10;transport=tcp")

    def test_ha_router_extension_forwards_to_esp(self) -> None:
        entries = roster.parse_roster_json(
            [{"id": "200", "name": "WS3", "address": "192.168.1.47", "metadata": {"sip_transport": "udp"}}]
        )
        decision = router.resolve_ha_router("200", entries, trunk_ready=False)
        self.assertEqual(decision.action, router.RouteAction.FORWARD)
        self.assertEqual(decision.target, "200")
        self.assertEqual(decision.sip_uri, "sip:200@192.168.1.47;transport=udp")

    def test_ha_router_extension_alias_forwards_to_esp(self) -> None:
        entries = roster.parse_roster_json(
            [
                {
                    "id": "Spotpear",
                    "name": "Spotpear Ball v2",
                    "address": "192.168.1.31",
                    "extension": "200",
                    "metadata": {"sip_transport": "udp"},
                }
            ]
        )
        decision = router.resolve_ha_router("200", entries, trunk_ready=False)
        self.assertEqual(decision.action, router.RouteAction.FORWARD)
        self.assertEqual(decision.target, "Spotpear")
        self.assertEqual(decision.sip_uri, "sip:Spotpear@192.168.1.31;transport=udp")

    def test_manual_phonebook_number_overrides_discovered_endpoint_without_duplicate(self) -> None:
        discovered = roster.parse_roster_json(
            [
                {
                    "id": "Spotpear",
                    "name": "Spotpear Ball v2",
                    "address": "192.168.1.31",
                    "metadata": {"sip_transport": "udp", "sip_port": 5060},
                }
            ]
        )
        manual = roster.parse_roster_json(
            [{"id": "Spotpear", "name": "Spotpear Ball v2", "number": "200"}]
        )
        merged = roster.merge_roster_overrides(discovered, manual)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].address, "192.168.1.31")
        self.assertEqual(merged[0].number, "200")
        self.assertEqual(merged[0].metadata["sip_transport"], "udp")

    def test_ha_router_decodes_sip_uri_user_for_phonebook_lookup(self) -> None:
        entries = roster.parse_roster_json(
            [
                {
                    "id": "Waveshare S3 Audio",
                    "address": "192.168.1.47",
                    "metadata": {"sip_transport": "udp"},
                }
            ]
        )
        decision = router.resolve_ha_router("Waveshare%20S3%20Audio", entries, trunk_ready=False)
        self.assertEqual(decision.action, router.RouteAction.FORWARD)
        self.assertEqual(decision.target, "Waveshare S3 Audio")
        self.assertEqual(decision.sip_uri, "sip:Waveshare_S3_Audio@192.168.1.47;transport=udp")

    def test_ha_router_explicit_sip_uri_routes_without_phonebook_entry(self) -> None:
        decision = router.resolve_ha_router("sip:LabPhone@192.168.1.60:5062;transport=tcp", [], trunk_ready=False)
        self.assertEqual(decision.action, router.RouteAction.DIRECT)
        self.assertEqual(decision.target, "sip:LabPhone@192.168.1.60:5062;transport=tcp")
        self.assertEqual(decision.sip_uri, "sip:LabPhone@192.168.1.60:5062;transport=tcp")
        self.assertIsNone(decision.entry)

    def test_ha_router_address_port_transport_contract_builds_direct_uri(self) -> None:
        entries = roster.parse_roster_json(
            [
                {
                    "id": "Desk",
                    "name": "Desk",
                    "address": "192.168.1.55",
                    "port": 5070,
                    "metadata": {"transport": "tcp"},
                }
            ]
        )
        decision = router.resolve_ha_router("Desk", entries, trunk_ready=False)
        self.assertEqual(decision.action, router.RouteAction.FORWARD)
        self.assertEqual(decision.target, "Desk")
        self.assertEqual(decision.sip_uri, "sip:Desk@192.168.1.55:5070;transport=tcp")

    def test_ha_router_public_number_requires_ready_trunk(self) -> None:
        unavailable = router.resolve_ha_router("0551234567", [], trunk_ready=False)
        self.assertEqual(unavailable.action, router.RouteAction.REJECT)
        self.assertEqual(unavailable.status, 503)
        self.assertEqual(unavailable.reason, router.RouteReason.TRUNK_UNAVAILABLE)
        ready = router.resolve_ha_router("0551234567", [], trunk_ready=True)
        self.assertEqual(ready.action, router.RouteAction.TRUNK)

    def test_trunk_service_code_preserves_prefix_and_bypasses_local_extension(self) -> None:
        entries = roster.parse_roster_json(
            [
                {
                    "id": "Fritz App",
                    "name": "Fritz App",
                    "address": "192.0.2.61",
                    "extension": "621",
                }
            ]
        )

        ready = router.resolve_ha_router("**621", entries, trunk_ready=True)
        self.assertEqual(ready.action, router.RouteAction.TRUNK)
        self.assertEqual(ready.target, "**621")
        self.assertEqual(ready.source, "trunk")

        unavailable = router.resolve_ha_router(
            "**621", entries, trunk_ready=False
        )
        self.assertEqual(unavailable.action, router.RouteAction.REJECT)
        self.assertEqual(unavailable.target, "**621")
        self.assertEqual(unavailable.status, 503)
        self.assertEqual(
            unavailable.reason,
            router.RouteReason.TRUNK_UNAVAILABLE,
        )

    def test_roster_contact_fields_are_data_driven(self) -> None:
        entries = roster.parse_roster_json(
            [
                {"name": "Daniele", "number": "3510000000"},
                {"name": "Spotpear", "address": "192.168.1.31", "extension": "101"},
                {"name": "Desk", "address": "192.168.1.55", "metadata": {"sip_transport": "udp"}},
                {"name": "Logical HA Target"},
            ]
        )
        self.assertEqual(entries[0].number, "3510000000")
        self.assertEqual(entries[1].extension, "101")
        self.assertEqual(entries[2].address, "192.168.1.55")
        self.assertEqual(entries[3].id, "Logical HA Target")

    def test_ha_router_name_with_number_and_no_endpoint_uses_trunk(self) -> None:
        entries = roster.parse_roster_json(
            [{"id": "Daniele", "name": "Daniele", "number": "3510000000"}]
        )
        unavailable = router.resolve_ha_router("Daniele", entries, trunk_ready=False)
        self.assertEqual(unavailable.action, router.RouteAction.REJECT)
        self.assertEqual(unavailable.target, "3510000000")
        self.assertEqual(unavailable.reason, router.RouteReason.TRUNK_UNAVAILABLE)
        ready = router.resolve_ha_router("Daniele", entries, trunk_ready=True)
        self.assertEqual(ready.action, router.RouteAction.TRUNK)
        self.assertEqual(ready.target, "3510000000")

    def test_ha_router_extension_resolves_internal_endpoint_not_trunk_number(self) -> None:
        entries = roster.parse_roster_json(
            [
                {
                    "id": "Spotpear",
                    "name": "Spotpear Ball v2",
                    "address": "192.168.1.31",
                    "extension": "101",
                    "metadata": {"sip_transport": "udp"},
                },
                {"id": "Daniele", "name": "Daniele", "number": "101"},
            ]
        )
        internal = router.resolve_ha_router("101", entries, trunk_ready=True)
        self.assertEqual(internal.action, router.RouteAction.FORWARD)
        self.assertEqual(internal.target, "Spotpear")
        self.assertIn("192.168.1.31", internal.sip_uri)

        external = router.resolve_ha_router("Daniele", entries, trunk_ready=True)
        self.assertEqual(external.action, router.RouteAction.TRUNK)
        self.assertEqual(external.target, "101")

    def test_ha_router_name_only_contact_answers_ha(self) -> None:
        entries = roster.parse_roster_json([{"id": "Casa Logica", "name": "Casa Logica"}])
        decision = router.resolve_ha_router("Casa Logica", entries, trunk_ready=True)
        self.assertEqual(decision.action, router.RouteAction.ANSWER_HA)
        self.assertEqual(decision.reason, router.RouteReason.NAME_VIA_HA)

    def test_trunk_inbound_no_hint_answers_ha(self) -> None:
        ctx = router.CallContext(call_id="trunk-1", direction="inbound", origin="trunk")
        decision = router.route_inbound_trunk(ctx, [], trunk_ready=False)
        self.assertEqual(decision.action, router.RouteAction.ANSWER_HA)

    def test_trunk_inbound_unknown_hint_rejects_route_not_found(self) -> None:
        ctx = router.CallContext(
            call_id="trunk-2",
            direction="inbound",
            origin="trunk",
            route_hint="999",
        )
        decision = router.route_inbound_trunk(ctx, [], trunk_ready=False)
        self.assertEqual(decision.action, router.RouteAction.REJECT)
        self.assertEqual(decision.reason, router.RouteReason.ROUTE_NOT_FOUND)
        self.assertEqual(decision.status, 404)

    def test_trunk_inbound_hint_resolves_phonebook_extension_alias(self) -> None:
        entries = roster.parse_roster_json(
            [
                {
                    "id": "Spotpear",
                    "name": "Spotpear Ball v2",
                    "address": "192.168.1.31",
                    "extension": "200",
                    "metadata": {"sip_transport": "udp"},
                }
            ]
        )
        ctx = router.CallContext(
            call_id="trunk-3",
            direction="inbound",
            origin="trunk",
            route_hint="200",
        )
        decision = router.route_inbound_trunk(ctx, entries, trunk_ready=False)
        self.assertEqual(decision.action, router.RouteAction.FORWARD)
        self.assertEqual(decision.target, "Spotpear")

    def test_disabled_entry_rejects(self) -> None:
        entries = roster.parse_roster_json(
            [{"id": "WS3", "address": "192.168.1.47", "enabled": False}]
        )
        decision = router.resolve_ha_router("WS3", entries, trunk_ready=False)
        self.assertEqual(decision.action, router.RouteAction.REJECT)
        self.assertEqual(decision.reason, router.RouteReason.TARGET_DISABLED)
