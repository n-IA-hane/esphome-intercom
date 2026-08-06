#!/usr/bin/env python3
"""SIP registrar authentication and lifecycle contracts."""

from __future__ import annotations

from .voip_phase1_support import (
    router,
    sip,
    sip_auth,
    sip_registrar,
    unittest,
)


class SipRegistrarTest(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _authorized_register(
        registrar,
        *,
        username: str,
        password: str,
        call_id: str,
        cseq: int,
        contacts: list[str],
        expires: int | None = 120,
        host: str = "192.0.2.50",
        port: int = 5062,
        transport: str = "UDP",
    ) -> sip.SipMessage:
        request_uri = "sip:192.168.1.10"
        challenge = registrar._challenge()[1]
        authorization = sip_auth.build_digest_authorization(
            challenge_header=challenge,
            username=username,
            password=password,
            method="REGISTER",
            uri=request_uri,
        )
        headers = [
            ("Via", f"SIP/2.0/{transport} {host}:{port};branch=z9hG4bK{call_id};rport"),
            ("From", f"<sip:{username}@192.168.1.10>;tag=a"),
            ("To", f"<sip:{username}@192.168.1.10>"),
            ("Call-ID", call_id),
            ("CSeq", f"{cseq} REGISTER"),
            *(("Contact", contact) for contact in contacts),
            ("Authorization", authorization),
        ]
        if expires is not None:
            headers.append(("Expires", str(expires)))
        return sip.parse_message(
            sip.build_request("REGISTER", request_uri, headers, b"")
        )

    def test_digest_nonce_cache_is_bounded(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        for _ in range(sip_registrar.MAX_ACTIVE_NONCES + 20):
            registrar._challenge()
        self.assertEqual(len(registrar.nonces), sip_registrar.MAX_ACTIVE_NONCES)

    def test_register_aor_comes_from_to_not_request_uri_or_from(self) -> None:
        request = sip.parse_message(
            sip.build_request(
                "REGISTER",
                "sip:wrong@192.168.1.10",
                [
                    ("From", "<sip:also-wrong@192.168.1.10>;tag=a"),
                    ("To", "<sip:correct@192.168.1.10>"),
                ],
            )
        )

        self.assertEqual(
            sip_registrar._extract_register_username(request),
            "correct",
        )

    async def test_register_challenge_hides_accounts_and_reuses_source_nonce(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("Known", "Known", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )

        def request(username: str) -> sip.SipMessage:
            return sip.parse_message(
                sip.build_request(
                    "REGISTER",
                    f"sip:{username}@192.168.1.10",
                    [
                        ("Via", "SIP/2.0/UDP 192.0.2.50:43000;branch=z9hG4bKenum;rport"),
                        ("From", f"<sip:{username}@192.168.1.10>;tag=a"),
                        ("To", f"<sip:{username}@192.168.1.10>"),
                        ("Call-ID", f"reg-{username}"),
                        ("CSeq", "1 REGISTER"),
                        ("Contact", f"<sip:{username}@192.0.2.50:43000>"),
                    ],
                )
            )

        known = await registrar.handle_register(
            request("Known"),
            ("192.0.2.50", 43000),
            "UDP",
        )
        unknown = await registrar.handle_register(
            request("Unknown"),
            ("192.0.2.50", 43000),
            "UDP",
        )

        self.assertEqual((known.status, unknown.status), (401, 401))
        known_nonce = sip_auth.parse_digest_challenge(
            dict(known.headers)["WWW-Authenticate"]
        )["nonce"]
        unknown_nonce = sip_auth.parse_digest_challenge(
            dict(unknown.headers)["WWW-Authenticate"]
        )["nonce"]
        self.assertEqual(known_nonce, unknown_nonce)
        self.assertEqual(len(registrar.nonces), 1)

    def test_register_contacts_reject_non_sip_and_normalize_display_address(self) -> None:
        request = sip.SipMessage(
            method="REGISTER",
            uri="sip:ha@192.168.1.10",
            headers=(
                ("Contact", "https://example.invalid/phone"),
                ("Contact", '"Desk" <sip:desk@192.168.1.50:5090;transport=tcp>;expires=60'),
                ("Expires", "120"),
            ),
        )
        contacts = sip_registrar._register_contacts(request)
        self.assertEqual(
            contacts,
            [
                (
                    "sip:desk@192.168.1.50:5090;transport=tcp",
                    60,
                    '"Desk" <sip:desk@192.168.1.50:5090;transport=tcp>;expires=60',
                )
            ],
        )

    async def test_register_challenge_then_binding_roster_entry(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("SmartphoneDany", "Smartphone Dany", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        base_headers = [
            ("Via", "SIP/2.0/UDP 192.168.1.50:5062;branch=z9hG4bKreg;rport"),
            ("From", "<sip:SmartphoneDany@192.168.1.10>;tag=a"),
            ("To", "<sip:SmartphoneDany@192.168.1.10>"),
            ("Call-ID", "reg-1"),
            ("CSeq", "1 REGISTER"),
            ("Contact", "<sip:SmartphoneDany@192.168.1.50:5062;transport=udp>"),
            ("Expires", "120"),
        ]
        request_uri = "sip:SmartphoneDany@192.168.1.10"
        challenge_req = sip.parse_message(sip.build_request("REGISTER", request_uri, base_headers, b""))
        challenge = await registrar.handle_register(challenge_req, ("192.168.1.50", 5062), "UDP")
        self.assertEqual(challenge.status, 401)
        authenticate = dict(challenge.headers)["WWW-Authenticate"]
        authorization = sip_auth.build_digest_authorization(
            challenge_header=authenticate,
            username="SmartphoneDany",
            password="secret",
            method="REGISTER",
            uri=request_uri,
        )
        ok_req = sip.parse_message(
            sip.build_request("REGISTER", request_uri, base_headers + [("Authorization", authorization)], b"")
        )
        ok = await registrar.handle_register(ok_req, ("192.168.1.50", 5062), "UDP")
        self.assertEqual(ok.status, 200)
        # An identical transaction is a legal retransmission when the 200 OK
        # was lost, but the same digest cannot authorize a different binding.
        self.assertEqual(
            (await registrar.handle_register(ok_req, ("192.168.1.50", 5062), "UDP")).status,
            200,
        )
        replay_headers = [
            (name, "2 REGISTER" if name == "CSeq" else value)
            for name, value in base_headers
        ]
        replay_headers = [
            (
                name,
                "<sip:SmartphoneDany@198.51.100.99:5090;transport=udp>"
                if name == "Contact"
                else value,
            )
            for name, value in replay_headers
        ]
        replay_req = sip.parse_message(
            sip.build_request(
                "REGISTER",
                request_uri,
                replay_headers + [("Authorization", authorization)],
                b"",
            )
        )
        replay = await registrar.handle_register(
            replay_req,
            ("198.51.100.99", 5090),
            "UDP",
        )
        self.assertEqual(replay.status, 401)
        entries = registrar.roster_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].id, "SmartphoneDany")
        self.assertEqual(entries[0].sip_uri, "sip:SmartphoneDany@192.168.1.50:5062;transport=udp")
        self.assertTrue(entries[0].metadata["registered"])

    async def test_dahua_refresh_replay_rotates_nonce_with_stale_and_recovers(
        self,
    ) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("100", "Dahua VTO", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        request_uri = "sip:192.168.1.10"
        source = ("192.0.2.85", 5060)
        base_headers = [
            ("Via", "SIP/2.0/UDP 192.0.2.85:5060;branch=z9hG4bKdahua;rport"),
            ("From", "<sip:100@VDP>;tag=dahua"),
            ("To", "<sip:100@VDP>"),
            ("Call-ID", "dahua-register"),
            ("CSeq", "1 REGISTER"),
            ("Contact", "<sip:100@192.0.2.85:5060>"),
            ("Expires", "60"),
            ("User-Agent", "Dahua UAC/3.0"),
        ]
        first = sip.parse_message(
            sip.build_request("REGISTER", request_uri, base_headers, b"")
        )
        challenge = await registrar.handle_register(first, source, "UDP")
        first_challenge = dict(challenge.headers)["WWW-Authenticate"]
        first_nonce = sip_auth.parse_digest_challenge(first_challenge)["nonce"]
        authorization = sip_auth.build_digest_authorization(
            challenge_header=first_challenge,
            username="100",
            password="secret",
            method="REGISTER",
            uri=request_uri,
        )
        authenticated = sip.parse_message(
            sip.build_request(
                "REGISTER",
                request_uri,
                [*base_headers, ("Authorization", authorization)],
                b"",
            )
        )
        self.assertEqual(
            (await registrar.handle_register(authenticated, source, "UDP")).status,
            200,
        )

        refresh_headers = [
            (name, "2 REGISTER" if name == "CSeq" else value)
            for name, value in base_headers
        ]
        replayed_refresh = sip.parse_message(
            sip.build_request(
                "REGISTER",
                request_uri,
                [*refresh_headers, ("Authorization", authorization)],
                b"",
            )
        )
        stale = await registrar.handle_register(replayed_refresh, source, "UDP")
        stale_challenge = dict(stale.headers)["WWW-Authenticate"]
        stale_params = sip_auth.parse_digest_challenge(stale_challenge)

        self.assertEqual(stale.status, 401)
        self.assertEqual(stale_params["stale"], "true")
        self.assertNotEqual(stale_params["nonce"], first_nonce)
        self.assertEqual(len(registrar.registered_contacts("100")), 1)

        refreshed_authorization = sip_auth.build_digest_authorization(
            challenge_header=stale_challenge,
            username="100",
            password="secret",
            method="REGISTER",
            uri=request_uri,
        )
        recovered = sip.parse_message(
            sip.build_request(
                "REGISTER",
                request_uri,
                [
                    *refresh_headers,
                    ("Authorization", refreshed_authorization),
                ],
                b"",
            )
        )
        self.assertEqual(
            (await registrar.handle_register(recovered, source, "UDP")).status,
            200,
        )
        registration = registrar.registered_contacts("100")[0]
        self.assertEqual(registration.cseq, 2)
        self.assertEqual(registration.user_agent, "Dahua UAC/3.0")
        roster = registrar.roster_entries()
        self.assertEqual(len(roster), 1)
        self.assertEqual(roster[0].metadata["sip_profile"], "dahua")
        self.assertEqual(
            roster[0].metadata["sip_contacts"][0]["sip_profile"],
            "dahua",
        )
        self.assertEqual(
            roster[0].metadata["sip_contacts"][0]["user_agent"],
            "Dahua UAC/3.0",
        )

    async def test_bad_register_password_does_not_claim_stale_nonce(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("100", "Dahua VTO", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        request_uri = "sip:192.168.1.10"
        challenge_header = registrar._challenge("UDP:192.0.2.85")[1]
        bad_authorization = sip_auth.build_digest_authorization(
            challenge_header=challenge_header,
            username="100",
            password="wrong",
            method="REGISTER",
            uri=request_uri,
        )
        request = sip.parse_message(
            sip.build_request(
                "REGISTER",
                request_uri,
                [
                    ("Via", "SIP/2.0/UDP 192.0.2.85:5060;branch=z9hG4bKbad;rport"),
                    ("From", "<sip:100@VDP>;tag=dahua"),
                    ("To", "<sip:100@VDP>"),
                    ("Call-ID", "dahua-bad-password"),
                    ("CSeq", "1 REGISTER"),
                    ("Contact", "<sip:100@192.0.2.85:5060>"),
                    ("Expires", "60"),
                    ("Authorization", bad_authorization),
                ],
                b"",
            )
        )

        result = await registrar.handle_register(
            request,
            ("192.0.2.85", 5060),
            "UDP",
        )

        self.assertEqual(result.status, 401)
        self.assertNotIn("stale=true", dict(result.headers)["WWW-Authenticate"])

    async def test_register_contact_is_pinned_to_authenticated_source_flow(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("Pivot", "Pivot", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        request_uri = "sip:Pivot@192.168.1.10"
        challenge = registrar._challenge()[1]
        authorization = sip_auth.build_digest_authorization(
            challenge_header=challenge,
            username="Pivot",
            password="secret",
            method="REGISTER",
            uri=request_uri,
        )
        request = sip.parse_message(
            sip.build_request(
                "REGISTER",
                request_uri,
                [
                    ("Via", "SIP/2.0/UDP 192.0.2.50:43000;branch=z9hG4bKpivot;rport"),
                    ("From", "<sip:Pivot@192.168.1.10>;tag=a"),
                    ("To", "<sip:Pivot@192.168.1.10>"),
                    ("Call-ID", "reg-pivot"),
                    ("CSeq", "1 REGISTER"),
                    (
                        "Contact",
                        "<sip:Pivot@127.0.0.1:22;transport=tcp;ob;line=abc>",
                    ),
                    ("Expires", "120"),
                    ("Authorization", authorization),
                ],
            )
        )

        result = await registrar.handle_register(
            request,
            ("192.0.2.50", 43000),
            "UDP",
        )

        self.assertEqual(result.status, 200)
        registration = registrar.registrations["Pivot"]
        self.assertEqual(
            registration.advertised_contact_uri,
            "sip:Pivot@127.0.0.1:22;transport=tcp;ob;line=abc",
        )
        self.assertEqual(
            registration.contact_uri,
            "sip:Pivot@192.0.2.50:43000;ob;line=abc;transport=udp",
        )
        self.assertEqual(
            registrar.roster_entries()[0].sip_uri,
            registration.contact_uri,
        )

    async def test_password_rotation_revokes_case_variant_registration(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("DeskPhone", "Desk", "old-secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        registrar.registrations["deskphone"] = sip_registrar.SipRegistration(
            username="deskphone",
            contact_uri="sip:deskphone@192.168.1.50:5062",
            source_host="192.168.1.50",
            source_port=5062,
            transport="UDP",
            expires_at=9999999999,
        )

        registrar.update_accounts(
            [sip_registrar.SipAccount("DeskPhone", "Desk", "new-secret")]
        )

        self.assertEqual(registrar.registrations, {})

    def test_account_title_case_update_retains_binding_without_offline_event(
        self,
    ) -> None:
        changes: list[tuple[str, bool]] = []
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("deskphone", "Desk", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
            on_registration_change=lambda username, registered: changes.append(
                (username, registered)
            ),
        )
        registrar.registrations["deskphone"] = sip_registrar.SipRegistration(
            username="deskphone",
            contact_uri="sip:deskphone@192.168.1.50:5062",
            source_host="192.168.1.50",
            source_port=5062,
            transport="UDP",
            expires_at=9999999999,
        )

        registrar.update_accounts(
            [sip_registrar.SipAccount("DeskPhone", "Desk", "secret")]
        )

        self.assertEqual(tuple(registrar.registrations), ("DeskPhone",))
        self.assertEqual(
            registrar.registrations["DeskPhone"].username,
            "DeskPhone",
        )
        self.assertEqual(changes, [])

    def test_account_removal_emits_one_offline_event(self) -> None:
        changes: list[tuple[str, bool]] = []
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("DeskPhone", "Desk", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
            on_registration_change=lambda username, registered: changes.append(
                (username, registered)
            ),
        )
        registrar.registrations["DeskPhone"] = sip_registrar.SipRegistration(
            username="DeskPhone",
            contact_uri="sip:DeskPhone@192.168.1.50:5062",
            source_host="192.168.1.50",
            source_port=5062,
            transport="UDP",
            expires_at=9999999999,
        )

        registrar.update_accounts([])

        self.assertEqual(registrar.registrations, {})
        self.assertEqual(changes, [("DeskPhone", False)])

    async def test_registration_identity_requires_exact_signaling_flow(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("DeskPhone", "Desk", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        registrar.registrations["DeskPhone"] = sip_registrar.SipRegistration(
            username="DeskPhone",
            contact_uri="sip:DeskPhone@192.168.1.50:5062;transport=tcp",
            source_host="192.168.1.50",
            source_port=5062,
            transport="TCP",
            expires_at=9999999999,
        )

        self.assertTrue(
            registrar.registration_matches_source(
                "deskphone", "192.168.1.50", 5062, "tcp"
            )
        )
        self.assertFalse(
            registrar.registration_matches_source(
                "DeskPhone", "192.168.1.50", 5099, "TCP"
            )
        )
        self.assertFalse(
            registrar.registration_matches_source(
                "DeskPhone", "192.168.1.99", 5062, "TCP"
            )
        )

    def test_digest_client_accepts_auth_int_only_challenge(self) -> None:
        authorization = sip_auth.build_digest_authorization(
            challenge_header='Digest realm="test", nonce="nonce", qop="auth-int"',
            username="desk",
            password="secret",
            method="REGISTER",
            uri="sip:pbx.example",
        )

        self.assertEqual(
            sip_auth.parse_digest_challenge(authorization)["qop"],
            "auth-int",
        )

    async def test_stale_unregister_does_not_remove_active_binding(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("Zoiper", "Zoiper", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        registrar.registrations["Zoiper"] = sip_registrar.SipRegistration(
            username="Zoiper",
            contact_uri="sip:Zoiper@192.168.1.50:5062;transport=tcp",
            source_host="192.168.1.50",
            source_port=5062,
            transport="TCP",
            expires_at=9999999999,
        )
        challenge = registrar._challenge()[1]
        authorization = sip_auth.build_digest_authorization(
            challenge_header=challenge,
            username="Zoiper",
            password="secret",
            method="REGISTER",
            uri="sip:192.168.1.10;transport=tcp",
        )
        stale = sip.parse_message(
            sip.build_request(
                "REGISTER",
                "sip:192.168.1.10;transport=tcp",
                [
                    ("Via", "SIP/2.0/TCP 192.168.1.50:5062;branch=z9hG4bKreg;rport"),
                    ("From", "<sip:Zoiper@192.168.1.10>;tag=a"),
                    ("To", "<sip:Zoiper@192.168.1.10>"),
                    ("Call-ID", "reg-stale"),
                    ("CSeq", "2 REGISTER"),
                    ("Contact", "<sip:Zoiper@192.168.1.50:5060;transport=tcp>;expires=0"),
                    ("Authorization", authorization),
                ],
                b"",
            )
        )
        self.assertEqual((await registrar.handle_register(stale, ("192.168.1.50", 5062), "TCP")).status, 200)

        entries = registrar.registered_roster_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].sip_uri, "sip:Zoiper@192.168.1.50:5062;transport=tcp")

    async def test_registered_softphone_roster_entry_carries_group_membership(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[
                sip_registrar.SipAccount(
                    "Zoiper",
                    "Zoiper",
                    "secret",
                    conference_group="CG Casa",
                    conference_ring=True,
                    ring_group="RG Casa",
                )
            ],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        registrar.registrations["Zoiper"] = sip_registrar.SipRegistration(
            username="Zoiper",
            contact_uri="sip:Zoiper@192.168.1.50:5062;transport=udp",
            source_host="192.168.1.50",
            source_port=5062,
            transport="UDP",
            expires_at=9999999999,
        )

        entries = registrar.registered_roster_entries()

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].metadata["conference_group"], "CG Casa")
        self.assertTrue(entries[0].metadata["conference_ring"])
        self.assertEqual(entries[0].metadata["ring_group"], "RG Casa")

    async def test_register_with_active_and_expired_contacts_keeps_active_binding(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("Zoiper", "Zoiper", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        request_uri = "sip:192.168.1.10;transport=tcp"
        challenge = registrar._challenge()[1]
        authorization = sip_auth.build_digest_authorization(
            challenge_header=challenge,
            username="Zoiper",
            password="secret",
            method="REGISTER",
            uri=request_uri,
        )
        request = sip.parse_message(
            sip.build_request(
                "REGISTER",
                request_uri,
                [
                    ("Via", "SIP/2.0/TCP 192.168.1.50:5062;branch=z9hG4bKreg;rport"),
                    ("From", "<sip:Zoiper@192.168.1.10>;tag=a"),
                    ("To", "<sip:Zoiper@192.168.1.10>"),
                    ("Call-ID", "reg-multi"),
                    ("CSeq", "3 REGISTER"),
                    ("Contact", "<sip:Zoiper@192.168.1.50:5060;transport=tcp>;expires=0"),
                    ("Contact", "<sip:Zoiper@192.168.1.50:5062;transport=tcp>"),
                    ("Expires", "120"),
                    ("Authorization", authorization),
                ],
                b"",
            )
        )

        result = await registrar.handle_register(request, ("192.168.1.50", 5062), "TCP")

        self.assertEqual(result.status, 200)
        entries = registrar.registered_roster_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].sip_uri, "sip:Zoiper@192.168.1.50:5062;transport=tcp")

    def test_sip_account_does_not_publish_as_phonebook_contact_when_not_registered(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("Zoiper", "Zoiper", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )

        self.assertEqual(registrar.roster_entries(), [])
        self.assertEqual(registrar.registered_roster_entries(), [])

    def test_registered_softphone_entry_is_sip_uri_contact(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("Zoiper", "Zoiper", "secret", extension="201")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        registrar.registrations["Zoiper"] = sip_registrar.SipRegistration(
            username="Zoiper",
            contact_uri="sip:Zoiper@192.168.1.50:5062;transport=tcp",
            source_host="192.168.1.50",
            source_port=5062,
            transport="TCP",
            expires_at=9999999999,
        )

        entries = registrar.registered_roster_entries()

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].id, "Zoiper")
        self.assertEqual(entries[0].sip_uri, "sip:Zoiper@192.168.1.50:5062;transport=tcp")
        self.assertEqual(entries[0].extension, "201")
        self.assertTrue(entries[0].metadata["registered"])
        self.assertNotIn("softphone", entries[0].metadata)
        by_name = router.resolve_ha_router("Zoiper", entries, trunk_ready=False)
        by_extension = router.resolve_ha_router("201", entries, trunk_ready=False)
        self.assertEqual(by_name.action, router.RouteAction.FORWARD)
        self.assertEqual(by_extension.action, router.RouteAction.FORWARD)
        self.assertEqual(by_extension.target, "Zoiper")
        self.assertEqual(by_extension.sip_uri, "sip:Zoiper@192.168.1.50:5062;transport=tcp")

    def test_account_without_registration_is_not_a_callable_roster_entry(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("Zoiper", "Zoiper", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )

        entries = registrar.registered_roster_entries()
        decision = router.resolve_ha_router("Zoiper", entries, trunk_ready=False)

        self.assertEqual(entries, [])
        self.assertEqual(decision.action, router.RouteAction.REJECT)
        self.assertEqual(decision.status, 404)
        self.assertEqual(decision.reason, router.RouteReason.ROUTE_NOT_FOUND)

    def test_sip_uri_parser_accepts_name_addr_with_header_params(self) -> None:
        parsed = sip.parse_sip_uri("<sip:Zoiper@192.168.1.10:41171;transport=udp>;tag=7bc04a5b")

        self.assertEqual(parsed.user, "Zoiper")
        self.assertEqual(parsed.host, "192.168.1.10")
        self.assertEqual(parsed.port, 41171)
        self.assertEqual(dict(parsed.params)["transport"], "udp")

    async def test_register_accepts_host_only_request_uri_from_baresip(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("SmartphoneDany", "Smartphone Dany", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        request_uri = "sip:192.168.1.10;transport=tcp"
        base_headers = [
            ("Via", "SIP/2.0/TCP 192.168.1.50:49258;branch=z9hG4bKreg;rport"),
            ("From", '"Smartphone Dany" <sip:SmartphoneDany@192.168.1.10>;tag=a'),
            ("To", "<sip:SmartphoneDany@192.168.1.10>"),
            ("Call-ID", "reg-host-only"),
            ("CSeq", "1 REGISTER"),
            ("Contact", "<sip:SmartphoneDany@192.168.1.50:49258;transport=tcp>"),
            ("Expires", "120"),
        ]
        challenge_req = sip.parse_message(sip.build_request("REGISTER", request_uri, base_headers, b""))
        challenge = await registrar.handle_register(challenge_req, ("192.168.1.50", 49258), "TCP")
        self.assertEqual(challenge.status, 401)
        authorization = sip_auth.build_digest_authorization(
            challenge_header=dict(challenge.headers)["WWW-Authenticate"],
            username="SmartphoneDany",
            password="secret",
            method="REGISTER",
            uri=request_uri,
        )
        ok_req = sip.parse_message(
            sip.build_request("REGISTER", request_uri, base_headers + [("Authorization", authorization)], b"")
        )
        ok = await registrar.handle_register(ok_req, ("192.168.1.50", 49258), "TCP")
        self.assertEqual(ok.status, 200)
        self.assertEqual(
            registrar.roster_entries()[0].sip_uri,
            "sip:SmartphoneDany@192.168.1.50:49258;transport=tcp",
        )

    async def test_multiple_contacts_are_independent_and_all_returned(self) -> None:
        changes: list[tuple[str, bool]] = []
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("Multi", "Multi", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
            on_registration_change=lambda username, registered: changes.append(
                (username, registered)
            ),
        )
        first = self._authorized_register(
            registrar,
            username="Multi",
            password="secret",
            call_id="device-a",
            cseq=1,
            contacts=["<sip:Multi@192.0.2.51:5062>;q=0.2"],
            host="192.0.2.51",
        )
        second = self._authorized_register(
            registrar,
            username="Multi",
            password="secret",
            call_id="device-b",
            cseq=1,
            contacts=["<sip:Multi@192.0.2.52:5064>;q=0.9"],
            host="192.0.2.52",
            port=5064,
        )

        self.assertEqual(
            (await registrar.handle_register(first, ("192.0.2.51", 5062), "UDP")).status,
            200,
        )
        result = await registrar.handle_register(
            second, ("192.0.2.52", 5064), "UDP"
        )

        self.assertEqual(result.status, 200)
        self.assertEqual(len(registrar.registered_contacts("Multi")), 2)
        self.assertEqual(
            len([value for name, value in result.headers if name == "Contact"]),
            2,
        )
        roster = registrar.registered_roster_entries()
        self.assertEqual(len(roster), 1)
        self.assertEqual(len(roster[0].metadata["sip_contacts"]), 2)
        self.assertEqual(roster[0].sip_uri, "sip:Multi@192.0.2.52:5064")
        self.assertTrue(
            registrar.registration_matches_source(
                "Multi", "192.0.2.51", 5062, "UDP"
            )
        )
        self.assertTrue(
            registrar.registration_matches_source(
                "Multi", "192.0.2.52", 5064, "UDP"
            )
        )
        self.assertEqual(changes, [("Multi", True)])

    async def test_unregister_one_contact_preserves_other_and_presence(self) -> None:
        changes: list[tuple[str, bool]] = []
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("Multi", "Multi", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
            on_registration_change=lambda username, registered: changes.append(
                (username, registered)
            ),
        )
        for suffix in ("a", "b"):
            port = 5061 + ord(suffix) - ord("a")
            request = self._authorized_register(
                registrar,
                username="Multi",
                password="secret",
                call_id=f"device-{suffix}",
                cseq=1,
                contacts=[f"<sip:Multi@192.0.2.50:{port}>"],
                port=port,
            )
            await registrar.handle_register(request, ("192.0.2.50", port), "UDP")

        unregister = self._authorized_register(
            registrar,
            username="Multi",
            password="secret",
            call_id="device-a",
            cseq=2,
            contacts=["<sip:Multi@192.0.2.50:5061>;expires=0"],
            port=5061,
        )
        result = await registrar.handle_register(
            unregister, ("192.0.2.50", 5061), "UDP"
        )

        self.assertEqual(result.status, 200)
        self.assertEqual(len(registrar.registered_contacts("Multi")), 1)
        self.assertEqual(changes, [("Multi", True)])

    async def test_register_query_and_wildcard_use_complete_binding_set(self) -> None:
        changes: list[tuple[str, bool]] = []
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("Multi", "Multi", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
            on_registration_change=lambda username, registered: changes.append(
                (username, registered)
            ),
        )
        create = self._authorized_register(
            registrar,
            username="Multi",
            password="secret",
            call_id="device-a",
            cseq=1,
            contacts=["<sip:Multi@192.0.2.50:5062>"],
        )
        await registrar.handle_register(create, ("192.0.2.50", 5062), "UDP")
        query = self._authorized_register(
            registrar,
            username="Multi",
            password="secret",
            call_id="query",
            cseq=1,
            contacts=[],
            expires=None,
        )

        query_result = await registrar.handle_register(
            query, ("192.0.2.50", 5062), "UDP"
        )
        self.assertEqual(query_result.status, 200)
        self.assertEqual(
            len([1 for name, _value in query_result.headers if name == "Contact"]),
            1,
        )

        invalid_wildcard = self._authorized_register(
            registrar,
            username="Multi",
            password="secret",
            call_id="wild-invalid",
            cseq=1,
            contacts=["*"],
            expires=120,
        )
        self.assertEqual(
            (
                await registrar.handle_register(
                    invalid_wildcard, ("192.0.2.50", 5062), "UDP"
                )
            ).status,
            400,
        )
        wildcard = self._authorized_register(
            registrar,
            username="Multi",
            password="secret",
            call_id="wild",
            cseq=1,
            contacts=["*"],
            expires=0,
        )
        wildcard_result = await registrar.handle_register(
            wildcard, ("192.0.2.50", 5062), "UDP"
        )
        self.assertEqual(wildcard_result.status, 200)
        self.assertEqual(wildcard_result.headers, ())
        self.assertEqual(changes, [("Multi", True), ("Multi", False)])

    async def test_lower_cseq_and_invalid_batch_do_not_mutate_bindings(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("Multi", "Multi", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        create = self._authorized_register(
            registrar,
            username="Multi",
            password="secret",
            call_id="device-a",
            cseq=10,
            contacts=["<sip:Multi@192.0.2.50:5062>"],
        )
        await registrar.handle_register(create, ("192.0.2.50", 5062), "UDP")
        before = [binding.snapshot() for binding in registrar.registrations.values()]
        stale = self._authorized_register(
            registrar,
            username="Multi",
            password="secret",
            call_id="device-a",
            cseq=9,
            contacts=["<sip:Multi@192.0.2.50:5062>"],
        )
        stale_result = await registrar.handle_register(
            stale, ("192.0.2.50", 5062), "UDP"
        )
        self.assertEqual(stale_result.status, 500)
        self.assertEqual(
            [binding.snapshot() for binding in registrar.registrations.values()],
            before,
        )

        invalid_batch = self._authorized_register(
            registrar,
            username="Multi",
            password="secret",
            call_id="device-b",
            cseq=1,
            contacts=[
                "<sip:Multi@192.0.2.50:5064>",
                "https://example.invalid/contact",
            ],
        )
        invalid_result = await registrar.handle_register(
            invalid_batch, ("192.0.2.50", 5064), "UDP"
        )
        self.assertEqual(invalid_result.status, 400)
        self.assertEqual(
            [binding.snapshot() for binding in registrar.registrations.values()],
            before,
        )
