#!/usr/bin/env python3
"""SIP URI, message and debug capture contracts."""

from __future__ import annotations

from .voip_phase1_support import (
    Path,
    audio_format,
    debug_capture,
    os,
    sip,
    tempfile,
    unittest,
)


class SipUriTest(unittest.TestCase):
    def test_uri_user_is_percent_encoded_and_line_breaks_are_rejected(self) -> None:
        self.assertEqual(
            str(sip.SipUri("Home Assistant", "192.168.1.10", 5060)),
            "sip:Home%20Assistant@192.168.1.10:5060",
        )
        with self.assertRaises(sip.SipError):
            sip.parse_sip_uri("sip:test@192.168.1.10\r\nX-Injected: yes")

    def test_endpoint_identity_includes_signaling_port(self) -> None:
        self.assertTrue(sip.sip_endpoints_equal("192.0.2.10", 5060, "192.0.2.10", 5060))
        self.assertFalse(sip.sip_endpoints_equal("192.0.2.10", 5062, "192.0.2.10", 5060))
        self.assertTrue(sip.sip_endpoints_equal("[2001:db8::1]", 5060, "2001:db8::1", 5060))

    def test_local_listener_match_requires_host_and_port(self) -> None:
        local = sip.parse_sip_uri("sip:HA@127.0.0.1:15060")
        sibling = sip.parse_sip_uri("sip:Phone@127.0.0.1:15102")
        kwargs = {
            "listener_hosts": ("localhost", "127.0.0.1", "::1"),
            "listener_port": 15060,
        }
        self.assertTrue(sip.sip_uri_targets_listener(local, **kwargs))
        self.assertFalse(sip.sip_uri_targets_listener(sibling, **kwargs))

    def test_message_builder_rejects_header_injection(self) -> None:
        with self.assertRaises(sip.SipError):
            sip.build_request(
                "OPTIONS",
                "sip:test@192.168.1.10",
                [("Call-ID", "safe\r\nX-Injected: yes")],
            )

    def test_debug_capture_names_are_path_safe_and_collision_resistant(self) -> None:
        hostile = debug_capture.safe_capture_name("../../etc/passwd")
        absolute = debug_capture.safe_capture_name("/tmp/escape")
        self.assertNotIn("/", hostile)
        self.assertNotIn("..", hostile)
        self.assertNotEqual(hostile, absolute)

    def test_debug_capture_session_names_are_unique_and_path_safe(self) -> None:
        first = debug_capture.capture_session_name("../../same-call")
        second = debug_capture.capture_session_name("../../same-call")

        self.assertNotEqual(first, second)
        self.assertNotIn("/", first)
        self.assertNotIn("..", first)

    def test_debug_wav_packs_right_aligned_24_bit_containers(self) -> None:
        fmt = audio_format.AudioFormat(16000, "s24le_in_s32", 1, 20)
        width, payload = debug_capture.wav_pcm_payload(
            fmt,
            bytes((0x56, 0x34, 0x12, 0x00, 0xAA, 0xCB, 0xED, 0xFF)),
        )

        self.assertEqual(width, 3)
        self.assertEqual(payload, bytes((0x56, 0x34, 0x12, 0xAA, 0xCB, 0xED)))

    def test_debug_capture_retention_bounds_files_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            for index in range(5):
                (directory / f"capture-{index}.wav").write_bytes(b"x" * 10)
            untouched = directory / "notes.txt"
            untouched.write_text("keep", encoding="utf-8")
            debug_capture.prune_debug_captures(directory, max_files=3, max_bytes=25)
            self.assertLessEqual(len(list(directory.glob("*.wav"))), 2)
            self.assertTrue(untouched.exists())

    def test_debug_capture_retention_never_splits_a_capture_group(self) -> None:
        suffixes = (
            "_ha_ws_rtp_to_browser.wav",
            "_ha_ws_browser_to_rtp.wav",
            "_ha_ws_timing.json",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            for generation, stem in enumerate(("old_session", "new_session"), 1):
                for suffix in suffixes:
                    path = directory / f"{stem}{suffix}"
                    path.write_bytes(b"x" * 10)
                    stamp = generation * 1_000_000_000
                    os.utime(path, ns=(stamp, stamp))

            debug_capture.prune_debug_captures(
                directory,
                max_files=4,
                max_bytes=100,
            )

            self.assertEqual(
                {path.name for path in directory.iterdir()},
                {f"new_session{suffix}" for suffix in suffixes},
            )

    def test_debug_capture_retention_reaps_interrupted_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            interrupted = directory / ".capture.wav.deadbeef.tmp"
            interrupted.write_bytes(b"partial")

            debug_capture.prune_debug_captures(directory)

            self.assertFalse(interrupted.exists())

    def test_debug_capture_directory_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "capture"
            debug_capture.ensure_debug_capture_dir(directory)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_debug_capture_commit_is_atomic_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "capture"
            destination = directory / "sample.json"
            with debug_capture.debug_capture_transaction(directory):
                temporary = debug_capture.capture_temp_path(destination)
                temporary.write_text('{"ok": true}', encoding="utf-8")
                debug_capture.commit_capture_file(temporary, destination)

            self.assertEqual(destination.read_text(encoding="utf-8"), '{"ok": true}')
            self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(directory.glob("*.tmp")), [])

    def test_parse_host_only_uri_used_by_standard_register_routes(self) -> None:
        uri = sip.parse_sip_uri("sip:192.168.1.10;transport=tcp")
        self.assertEqual(uri.user, "")
        self.assertEqual(uri.host, "192.168.1.10")
        self.assertEqual(uri.params, (("transport", "tcp"),))
        self.assertEqual(str(uri), "sip:192.168.1.10;transport=tcp")

    def test_extract_tag_ignores_quoted_display_name_parameters(self) -> None:
        self.assertEqual(sip.extract_tag("<sip:a@b>;tag=abc;x=y"), "abc")
        self.assertEqual(sip.extract_tag("<sip:a@b>;TAG=ABC;x=y"), "ABC")
        self.assertEqual(sip.extract_tag(""), "")
        self.assertEqual(sip.extract_tag('"not;tag=quoted" <sip:a@b>;tag=real;x=y'), "real")

    def test_record_route_set_preserves_list_values_and_uac_reverses_order(self) -> None:
        response = sip.parse_message(
            sip.build_response(
                200,
                "OK",
                [
                    ("From", "<sip:a@example.test>;tag=a"),
                    ("To", "<sip:b@example.test>;tag=b"),
                    ("Call-ID", "route-set"),
                    ("CSeq", "1 INVITE"),
                    (
                        "Record-Route",
                        '"Core, proxy" <sip:core@192.0.2.10:5070;lr>, '
                        "<sip:edge@192.0.2.11:5080;lr>",
                    ),
                ],
            )
        )

        self.assertEqual(
            sip.record_route_set(response, reverse=True),
            (
                "<sip:edge@192.0.2.11:5080;lr>",
                '"Core, proxy" <sip:core@192.0.2.10:5070;lr>',
            ),
        )

    def test_dialog_request_routing_supports_loose_and_strict_routes(self) -> None:
        target = "sip:desk@192.0.2.20:5090"
        loose = sip.dialog_request_routing(
            target,
            (
                "<sip:edge@192.0.2.11:5080;lr>",
                "<sip:core@192.0.2.10:5070;lr>",
            ),
        )
        self.assertEqual(loose.request_uri, target)
        self.assertEqual(
            loose.route_headers,
            (
                "<sip:edge@192.0.2.11:5080;lr>",
                "<sip:core@192.0.2.10:5070;lr>",
            ),
        )
        self.assertEqual(loose.next_hop_uri, "sip:edge@192.0.2.11:5080;lr")

        strict = sip.dialog_request_routing(
            target,
            (
                "<sip:strict@192.0.2.12:5065>",
                "<sip:edge@192.0.2.11:5080;lr>",
            ),
        )
        self.assertEqual(strict.request_uri, "sip:strict@192.0.2.12:5065")
        self.assertEqual(
            strict.route_headers,
            (
                "<sip:edge@192.0.2.11:5080;lr>",
                f"<{target}>",
            ),
        )
        self.assertEqual(strict.next_hop_uri, "sip:strict@192.0.2.12:5065")

    def test_dialog_list_headers_reject_unbalanced_name_address_brackets(self) -> None:
        response = sip.SipMessage(headers=(("Contact", "<sip:a@example.test>>"),))
        with self.assertRaises(sip.SipError):
            sip.contact_target_uri(response)

        routed = sip.SipMessage(
            headers=(("Record-Route", "<sip:proxy@example.test;lr"),)
        )
        with self.assertRaises(sip.SipError):
            sip.record_route_set(routed)
