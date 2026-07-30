#!/usr/bin/env python3
"""Golden tests for the phase-1 SIP/SDP/RTP PCM profile."""

from __future__ import annotations

import importlib.util
import asyncio
import contextlib
import os
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _load_intercom_module(name: str):
    if "custom_components" not in sys.modules:
        root_pkg = types.ModuleType("custom_components")
        root_pkg.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root_pkg
    if PKG_NAME not in sys.modules:
        pkg = types.ModuleType(PKG_NAME)
        pkg.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = pkg

    full_name = f"{PKG_NAME}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


audio_format = _load_intercom_module("audio_format")
audio_pcm = _load_intercom_module("audio_pcm")
sip = _load_intercom_module("sip")
sdp = _load_intercom_module("sdp")
codec_capabilities = _load_intercom_module("codec_capabilities")
g722_codec = _load_intercom_module("g722_codec")
rtp = _load_intercom_module("rtp")
roster = _load_intercom_module("roster")
router = _load_intercom_module("router")
debug_capture = _load_intercom_module("debug_capture")
sip_client = _load_intercom_module("sip_client")
sip_tcp_io = _load_intercom_module("sip_tcp_io")
sip_listener = _load_intercom_module("sip_listener")
sip_registrar = _load_intercom_module("sip_registrar")
sip_auth = _load_intercom_module("sip_auth")
sip_runtime = _load_intercom_module("sip_runtime")
sip_rtp_bridge = _load_intercom_module("sip_rtp_bridge")
sip_bridge = _load_intercom_module("sip_bridge")
sip_trunk = _load_intercom_module("sip_trunk")
sip_endpoint = _load_intercom_module("sip_endpoint")
dtmf = _load_intercom_module("dtmf")


def _load_audio_ws_runtime_module():
    """Load audio_ws_view with minimal HA adapters and real media primitives."""

    package_name = "voip_stack_audio_runtime_test"
    module_name = f"{package_name}.audio_ws_view"
    if module_name in sys.modules:
        return sys.modules[module_name]

    package = types.ModuleType(package_name)
    package.__path__ = [str(PKG_DIR)]
    sys.modules[package_name] = package
    dependencies = {
        "rtp": rtp,
        "audio_ws": _load_intercom_module("audio_ws"),
        "call_registry": _load_intercom_module("call_registry"),
        "const": _load_intercom_module("const"),
        "debug_capture": debug_capture,
        "dtmf": dtmf,
        "media_debug": _load_intercom_module("media_debug"),
        "queue_utils": _load_intercom_module("queue_utils"),
        "sip_client": sip_client,
        "websocket_owner": _load_intercom_module("websocket_owner"),
    }
    for name, module in dependencies.items():
        sys.modules[f"{package_name}.{name}"] = module

    websocket_api = types.ModuleType(f"{package_name}.websocket_api")
    websocket_api.CALL_EVENT = "voip_stack_call_event"
    websocket_api._ha_softphone_store = (
        lambda hass, _endpoint_id="default": hass.store
    )
    websocket_api._publish_ha_softphone_state = (
        lambda _hass, endpoint_id="default": None  # noqa: ARG005
    )
    sys.modules[websocket_api.__name__] = websocket_api

    homeassistant = sys.modules.setdefault("homeassistant", types.ModuleType("homeassistant"))
    if not hasattr(homeassistant, "__path__"):
        homeassistant.__path__ = []
    components = sys.modules.setdefault(
        "homeassistant.components", types.ModuleType("homeassistant.components")
    )
    if not hasattr(components, "__path__"):
        components.__path__ = []
    http = sys.modules.setdefault(
        "homeassistant.components.http", types.ModuleType("homeassistant.components.http")
    )
    http.HomeAssistantView = getattr(http, "HomeAssistantView", type("HomeAssistantView", (), {}))
    core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    exceptions = sys.modules.setdefault(
        "homeassistant.exceptions", types.ModuleType("homeassistant.exceptions")
    )
    exceptions.Unauthorized = getattr(
        exceptions, "Unauthorized", type("Unauthorized", (Exception,), {})
    )
    exceptions.UnknownUser = getattr(
        exceptions, "UnknownUser", type("UnknownUser", (Exception,), {})
    )

    spec = importlib.util.spec_from_file_location(module_name, PKG_DIR / "audio_ws_view.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        # A failed dynamic import must not poison later tests with a partially
        # initialized module from ``sys.modules``.
        sys.modules.pop(module_name, None)
        raise
    return module


def _load_video_ws_runtime_module():
    """Load video_ws_view with minimal HA adapters and real video primitives."""

    # Reuse the deterministic Home Assistant module stubs installed by the
    # audio runtime loader; the media views share the same HA surface.
    _load_audio_ws_runtime_module()
    core = sys.modules["homeassistant.core"]
    core.callback = getattr(core, "callback", lambda target: target)
    package_name = "voip_stack_video_runtime_test"
    module_name = f"{package_name}.video_ws_view"
    if module_name in sys.modules:
        return sys.modules[module_name]

    package = types.ModuleType(package_name)
    package.__path__ = [str(PKG_DIR)]
    sys.modules[package_name] = package
    websocket_api = types.ModuleType(f"{package_name}.websocket_api")
    websocket_api.CALL_EVENT = "voip_stack_call_event"
    websocket_api._ha_softphone_store = (
        lambda hass, _endpoint_id="default": hass.store
    )
    websocket_api._publish_ha_softphone_state = (
        lambda _hass, endpoint_id="default": None  # noqa: ARG005
    )
    sys.modules[websocket_api.__name__] = websocket_api

    spec = importlib.util.spec_from_file_location(
        module_name, PKG_DIR / "video_ws_view.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module


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


def _load_sip_transport_with_homeassistant_stubs():
    if "homeassistant" not in sys.modules:
        ha_pkg = types.ModuleType("homeassistant")
        ha_pkg.__path__ = []
        sys.modules["homeassistant"] = ha_pkg
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    sys.modules["homeassistant.core"] = core
    components = types.ModuleType("homeassistant.components")
    components.__path__ = []
    sys.modules["homeassistant.components"] = components
    network = types.ModuleType("homeassistant.components.network")

    async def async_get_announce_addresses(_hass):
        return ["127.0.0.1"]

    network.async_get_announce_addresses = async_get_announce_addresses
    sys.modules["homeassistant.components.network"] = network
    return _load_intercom_module("fsm")


@contextlib.contextmanager
def _reserved_udp_ports(count: int):
    sockets = []
    try:
        ports = []
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
            ports.append(sock.getsockname()[1])
        yield ports
    finally:
        for sock in sockets:
            sock.close()


class SipProfileTest(unittest.TestCase):
    def test_explicit_empty_client_profile_never_falls_back_to_defaults(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=40000,
            supported_send_formats=[],
            supported_recv_formats=[],
        )

        self.assertEqual(client.supported_send_formats, [])
        self.assertEqual(client.supported_recv_formats, [])
        with self.assertRaises(sdp.SdpError):
            sdp.build_offer_directional(
                client.local_ip,
                client.local_ip,
                client.local_rtp_port,
                client.supported_send_formats,
                client.supported_recv_formats,
            )

    def test_build_and_parse_invite_with_l16_sdp(self) -> None:
        body = sdp.build_offer(
            "192.168.1.20",
            "192.168.1.20",
            40000,
            [audio_format.AudioFormat(48000, "s16le", 1, 10)],
        ).encode()
        msg = sip.build_request(
            "INVITE",
            "sip:Cucina@192.168.1.30",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.20:5060;branch=z9hG4bKtest"),
                ("Max-Forwards", "70"),
                ("From", "<sip:Spotpear@192.168.1.20>;tag=abc"),
                ("To", "<sip:Cucina@192.168.1.30>"),
                ("Call-ID", "call-1"),
                ("CSeq", "1 INVITE"),
                ("Contact", "<sip:Spotpear@192.168.1.20:5060>"),
                ("Content-Type", "application/sdp"),
            ],
            body,
        )
        parsed = sip.parse_message(msg)
        self.assertEqual(parsed.method, "INVITE")
        self.assertEqual(parsed.uri, "sip:Cucina@192.168.1.30")
        self.assertEqual(parsed.header("Call-ID"), "call-1")
        self.assertEqual(parsed.body, body)

    def test_standard_offer_does_not_include_trunk_codecs_by_default(self) -> None:
        body = sdp.build_offer_directional(
            "192.168.1.20",
            "192.168.1.20",
            40000,
            [
                audio_format.AudioFormat(48000, "s16le", 2, 20),
                audio_format.AudioFormat(8000, "s16le", 1, 20),
            ],
            [
                audio_format.AudioFormat(48000, "s16le", 2, 20),
                audio_format.AudioFormat(8000, "s16le", 1, 20),
            ],
        )
        self.assertNotIn("OPUS/48000/2", body)
        self.assertNotIn("PCMA/8000", body)
        self.assertNotIn("PCMU/8000", body)

    def test_trunk_offer_includes_available_common_codec_fallbacks(self) -> None:
        with patch.object(
            sdp,
            "common_sip_codecs",
            return_value=frozenset({"OPUS", "G722"}),
        ):
            body = sdp.build_offer_directional(
                "192.168.1.20",
                "192.168.1.20",
                40000,
                list(audio_format.HA_TRUNK_AUDIO_FORMATS),
                list(audio_format.HA_TRUNK_AUDIO_FORMATS),
                include_common_codecs=True,
            )
        self.assertIn("m=audio 40000 RTP/AVP 98 9 8 0", body)
        self.assertIn("a=rtpmap:98 OPUS/48000/2", body)
        self.assertIn("a=rtpmap:9 G722/8000/1", body)
        self.assertIn("a=rtpmap:8 PCMA/8000/1", body)
        self.assertIn("a=rtpmap:0 PCMU/8000/1", body)
        self.assertIn("a=ptime:20", body)

    def test_trunk_offer_does_not_advertise_unavailable_optional_codecs(self) -> None:
        with patch.object(
            sdp,
            "common_sip_codecs",
            return_value=frozenset(),
        ):
            body = sdp.build_offer_directional(
                "192.168.1.20",
                "192.168.1.20",
                40000,
                list(audio_format.HA_TRUNK_AUDIO_FORMATS),
                list(audio_format.HA_TRUNK_AUDIO_FORMATS),
                include_common_codecs=True,
            )
        self.assertNotIn(" OPUS/", body)
        self.assertNotIn(" G722/", body)
        self.assertIn("a=rtpmap:8 PCMA/8000/1", body)
        self.assertIn("a=rtpmap:0 PCMU/8000/1", body)

    def test_g722_uses_16khz_pcm_with_the_rfc3551_8khz_rtp_clock(self) -> None:
        fmt = sdp.RtpPcmFormat(9, "G722", 8000, 1, 20)

        self.assertEqual(fmt.audio_format.sample_rate, 16000)
        self.assertEqual(fmt.rtp_clock_rate, 8000)
        self.assertEqual(fmt.pcm_sample_rate, 16000)
        self.assertEqual(fmt.rtp_timestamp_step, 160)
        self.assertEqual(fmt.audio_format.nominal_frame_samples, 320)

    def test_g722_static_payload_negotiates_against_16khz_pcm(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.30\r\n"
            "s=-\r\n"
            "c=IN IP4 192.168.1.30\r\n"
            "t=0 0\r\n"
            "m=audio 41000 RTP/AVP 9\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )
        selected = sdp.negotiate_directional(
            offer,
            [audio_format.AudioFormat(16000, "s16le", 1, 20)],
            [audio_format.AudioFormat(16000, "s16le", 1, 20)],
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send.wire_token(), "pt=9:G722/8000/1/20ms")
        self.assertEqual(selected.recv.audio_format.sample_rate, 16000)

    def test_optional_codec_capabilities_require_both_encoder_and_decoder(self) -> None:
        class CodecContext:
            @staticmethod
            def create(name: str, mode: str):
                if (name, mode) == ("libopus", "w"):
                    raise RuntimeError("encoder unavailable")
                return object()

        fake_av = types.SimpleNamespace(CodecContext=CodecContext)
        codec_capabilities.common_sip_codecs.cache_clear()
        try:
            with patch.dict(sys.modules, {"av": fake_av}):
                self.assertEqual(
                    codec_capabilities.common_sip_codecs(),
                    frozenset({"G722"}),
                )
        finally:
            codec_capabilities.common_sip_codecs.cache_clear()

    def test_g722_pyav_adapter_keeps_codec_state_and_pcm_shape(self) -> None:
        contexts: list[object] = []

        class Frame:
            def __init__(self, samples=None) -> None:
                self.samples = samples
                self.sample_rate = 0

            def to_ndarray(self):
                return g722_codec.np.arange(320, dtype="<i2").reshape(1, -1)

        class Context:
            def __init__(self, mode: str) -> None:
                self.mode = mode
                self.sample_rate = 0
                self.layout = ""
                self.format = ""
                self.bit_rate = 0
                self.opened = False
                contexts.append(self)

            def open(self) -> None:
                self.opened = True

            def decode(self, _packet):
                return [Frame()]

            def encode(self, frame):
                self.encoded_frame = frame
                return [bytes(range(160))]

        class CodecContext:
            @staticmethod
            def create(name: str, mode: str):
                self.assertEqual(name, "g722")
                return Context(mode)

        class AudioFrame:
            @staticmethod
            def from_ndarray(samples, *, format: str, layout: str):
                self.assertEqual(samples.shape, (1, 320))
                self.assertEqual((format, layout), ("s16", "mono"))
                return Frame(samples)

        fake_av = types.SimpleNamespace(
            CodecContext=CodecContext,
            AudioFrame=AudioFrame,
            Packet=lambda payload: payload,
        )
        with patch.dict(sys.modules, {"av": fake_av}):
            encoder = g722_codec.G722Encoder()
            decoder = g722_codec.G722Decoder()
            encoded = encoder.encode(bytes(640))
            decoded = decoder.decode(encoded)

        self.assertEqual(len(contexts), 2)
        self.assertTrue(contexts[0].opened)
        self.assertEqual(encoded, bytes(range(160)))
        self.assertEqual(len(decoded), 640)
        self.assertEqual(encoder.audio_format.sample_rate, 16000)
        self.assertEqual(decoder.audio_format.frame_ms, 20)

    def test_g722_relay_advances_rtp_clock_by_160_not_pcm_samples(self) -> None:
        class PassthroughCodec:
            def __init__(self, _fmt) -> None:
                pass

            def decode(self, payload: bytes) -> bytes:
                return payload

            def encode(self, _pcm: bytes) -> bytes:
                return bytes(160)

        class Transport:
            def __init__(self) -> None:
                self.sent: list[bytes] = []

            def sendto(self, packet: bytes, _addr) -> None:
                self.sent.append(packet)

        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        l16 = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
        g722 = sdp.RtpPcmFormat(9, "G722", 8000, 1, 20)
        with (
            patch.object(sip_rtp_bridge, "RtpPayloadDecoder", PassthroughCodec),
            patch.object(sip_rtp_bridge, "RtpPayloadEncoder", PassthroughCodec),
        ):
            relay = sip_rtp_bridge.SipRtpRelay(
                left=sip_rtp_bridge.RtpPeer(
                    "127.0.0.1",
                    40000,
                    96,
                    pcm,
                    rtp_format=l16,
                    send_rtp_format=l16,
                ),
                right=sip_rtp_bridge.RtpPeer(
                    "127.0.0.2",
                    40002,
                    9,
                    pcm,
                    rtp_format=g722,
                    send_rtp_format=g722,
                    timestamp=1000,
                ),
                left_port=41000,
                right_port=41002,
            )
        transport = Transport()
        relay.right_transport = transport
        packet = rtp.build_packet(
            rtp.RtpPacket(
                payload_type=96,
                sequence=1,
                timestamp=0,
                ssrc=7,
                payload=bytes(pcm.nominal_frame_bytes),
            )
        )

        relay.handle_packet("left", packet, ("127.0.0.1", 40000))

        self.assertEqual(len(transport.sent), 1)
        self.assertEqual(rtp.parse_packet(transport.sent[0]).timestamp, 1000)
        self.assertEqual(relay.right.timestamp, 1160)

    def test_trunk_opus_answer_negotiates_48k_stereo_20ms(self) -> None:
        answer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.30\r\n"
            "s=-\r\n"
            "c=IN IP4 192.168.1.30\r\n"
            "t=0 0\r\n"
            "m=audio 41000 RTP/AVP 98\r\n"
            "a=rtpmap:98 opus/48000/2\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )
        selected = sdp.negotiate_answer_directional(
            answer,
            list(audio_format.HA_TRUNK_AUDIO_FORMATS),
            list(audio_format.HA_TRUNK_AUDIO_FORMATS),
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send.wire_token(), "pt=98:OPUS/48000/2/20ms")
        self.assertEqual(selected.recv.wire_token(), "pt=98:OPUS/48000/2/20ms")

    def test_sdp_parser_scopes_connection_and_payloads_to_selected_audio(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.20\r\n"
            "s=-\r\n"
            "c=IN IP4 192.168.1.21\r\n"
            "t=0 0\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "c=IN IP4 192.168.1.22\r\n"
            "a=rtpmap:96 L16/16000/1\r\n"
            "a=ptime:20\r\n"
            "m=video 42000 RTP/AVP 97\r\n"
            "c=IN IP4 192.168.1.99\r\n"
            "a=rtpmap:97 H264/90000\r\n"
            "m=audio 43000 RTP/AVP 98\r\n"
            "c=IN IP4 192.168.1.98\r\n"
            "a=rtpmap:98 L16/48000/1\r\n"
        )

        parsed = sdp.parse_sdp(offer)
        self.assertEqual(parsed["connection_ip"], "192.168.1.22")
        self.assertEqual(parsed["media_port"], 41000)
        self.assertEqual(parsed["payload_order"], [96])
        self.assertEqual(parsed["rtpmap"], {96: ("L16", 16000, 1)})

    def test_sdp_parser_skips_rejected_audio_and_validates_transport_ranges(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.20\r\n"
            "s=-\r\n"
            "c=IN IP4 192.168.1.20\r\n"
            "t=0 0\r\n"
            "m=audio 0 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\n"
            "m=audio 41000 RTP/AVP 97\r\n"
            "a=rtpmap:97 L16/48000/1\r\n"
        )
        parsed = sdp.parse_sdp(offer)
        self.assertEqual(parsed["media_port"], 41000)
        self.assertEqual(parsed["payload_order"], [97])

        with self.assertRaises(sdp.SdpError):
            sdp.parse_sdp(offer.replace("41000", "70000"))
        with self.assertRaises(sdp.SdpError):
            sdp.parse_sdp(offer.replace("RTP/AVP 97", "RTP/AVP 128"))

    def test_parser_preserves_unsupported_method_for_sip_response(self) -> None:
        raw = (
            b"REGISTER sip:ha@192.168.1.10 SIP/2.0\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        parsed = sip.parse_message(raw)
        self.assertEqual(parsed.method, "REGISTER")

    def test_ignores_datagram_bytes_after_content_length(self) -> None:
        raw = (
            b"OPTIONS sip:ha@192.168.1.10 SIP/2.0\r\n"
            b"Content-Length: 0\r\n\r\nx"
        )
        parsed = sip.parse_message(raw)
        self.assertEqual(parsed.method, "OPTIONS")
        self.assertEqual(parsed.body, b"")

    def test_parser_unfolds_bounded_sip_header_continuations(self) -> None:
        raw = (
            b"OPTIONS sip:ha@192.168.1.10 SIP/2.0\r\n"
            b"Subject: standard SIP\r\n"
            b"  continuation\r\n"
            b"Content-Length: 0\r\n\r\n"
        )

        parsed = sip.parse_message(raw)

        self.assertEqual(parsed.header("Subject"), "standard SIP continuation")

    def test_compact_sip_headers_are_canonicalized(self) -> None:
        raw = (
            b"OPTIONS sip:ha@192.168.1.10 SIP/2.0\r\n"
            b"v: SIP/2.0/UDP 192.168.1.20:5060;branch=z9hG4bKcompact\r\n"
            b"f: <sip:test@192.168.1.20>;tag=remote\r\n"
            b"t: <sip:ha@192.168.1.10>\r\n"
            b"i: compact-call\r\n"
            b"CSeq: 1 OPTIONS\r\n"
            b"l: 0\r\n\r\n"
        )
        parsed = sip.parse_message(raw)
        self.assertEqual(parsed.header("Via"), "SIP/2.0/UDP 192.168.1.20:5060;branch=z9hG4bKcompact")
        self.assertEqual(parsed.header("Call-ID"), "compact-call")
        self.assertEqual(parsed.header("Content-Length"), "0")

    def test_sip_uri_parser_accepts_display_name_address(self) -> None:
        parsed = sip.parse_sip_uri('"Kitchen phone" <sip:kitchen@192.0.2.20:5090;transport=tcp>')
        self.assertEqual(str(parsed), "sip:kitchen@192.0.2.20:5090;transport=tcp")

    def test_parser_rejects_canonical_and_compact_content_length_together(self) -> None:
        raw = b"OPTIONS sip:ha@192.168.1.10 SIP/2.0\r\nContent-Length: 0\r\nl: 0\r\n\r\n"
        with self.assertRaises(sip.SipError):
            sip.parse_message(raw)

    def test_parser_rejects_duplicate_dialog_identity_headers(self) -> None:
        raw = (
            b"OPTIONS sip:ha@192.168.1.10 SIP/2.0\r\n"
            b"Call-ID: first\r\n"
            b"i: second\r\n"
            b"CSeq: 1 OPTIONS\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        with self.assertRaises(sip.SipError):
            sip.parse_message(raw)

    def test_ack_reuses_invite_cseq_with_fresh_branch(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        client.transport = FakeTransport()  # type: ignore[assignment]
        client.dialog_ids = sip.SipDialogIds(
            call_id="call-ack",
            local_tag="local",
            remote_tag="remote",
            cseq=7,
            branch="z9hG4bKinvite",
        )
        client._invite_cseq = 7
        client._send_ack(
            "192.168.1.30",
            5060,
            "sip:Cucina@192.168.1.30",
            "sip:HA@192.168.1.10:5060",
            "sip:Cucina@192.168.1.30:5060",
        )
        raw, addr = client.transport.sent[0]  # type: ignore[union-attr]
        parsed = sip.parse_message(raw)
        self.assertEqual(addr, ("192.168.1.30", 5060))
        self.assertEqual(parsed.method, "ACK")
        self.assertEqual(parsed.header("CSeq"), "7 ACK")
        self.assertNotIn("z9hG4bKinvite", parsed.header("Via"))
        self.assertIn("SIP/2.0/UDP 192.168.1.10:5060", parsed.header("Via"))
        self.assertIn(";rport", parsed.header("Via"))

    def test_cancel_reuses_invite_transaction(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        client.transport = FakeTransport()  # type: ignore[assignment]
        client.dialog_ids = sip.SipDialogIds(
            call_id="call-cancel",
            local_tag="local",
            cseq=9,
            branch="z9hG4bKinvite",
        )
        client._invite_cseq = 9
        client._pending_request_uri = "sip:Cucina@192.168.1.30:5060"
        client._pending_local_uri = "sip:HA@192.168.1.10:5060"
        client._pending_remote_uri = "sip:Cucina@192.168.1.30:5060"
        client._pending_remote_host = "192.168.1.30"
        client._pending_remote_sip_port = 5060
        client._invite_transaction_active = True
        client._received_provisional = True

        client.cancel()

        raw, addr = client.transport.sent[0]  # type: ignore[union-attr]
        parsed = sip.parse_message(raw)
        self.assertEqual(addr, ("192.168.1.30", 5060))
        self.assertEqual(parsed.method, "CANCEL")
        self.assertEqual(parsed.header("CSeq"), "9 CANCEL")
        self.assertIn("z9hG4bKinvite", parsed.header("Via"))

    def test_tcp_ack_and_bye_use_stream_writer(self) -> None:
        sent = bytearray()
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="Casa",
            local_sip_port=43123,
            local_rtp_port=41000,
            signaling_transport="TCP",
        )
        client.use_reused_tcp_connection(send=sent.extend, responses=asyncio.Queue(), close=lambda: None)
        client.dialog_ids = sip.SipDialogIds(
            call_id="call-tcp-dialog",
            local_tag="local",
            remote_tag="remote",
            cseq=3,
            branch="z9hG4bKinvite",
        )
        client._invite_cseq = 3
        client.dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="192.168.1.30",
            remote_sip_port=5060,
            remote_rtp_host="192.168.1.30",
            remote_rtp_port=40000,
            local_rtp_port=41000,
            call_id="call-tcp-dialog",
            local_uri="sip:Casa@192.168.1.10:43123",
            remote_uri="sip:ESP@192.168.1.30:5060",
            send_format=sdp.RtpPcmFormat(96, "L16", 16000, 1, 32),
            recv_format=sdp.RtpPcmFormat(96, "L16", 16000, 1, 32),
        )

        client._send_ack(
            "192.168.1.30",
            5060,
            "sip:ESP@192.168.1.30:5060",
            "sip:Casa@192.168.1.10:43123",
            "sip:ESP@192.168.1.30:5060",
        )
        ack = sip.parse_message(bytes(sent))
        self.assertEqual(ack.method, "ACK")
        self.assertIn("SIP/2.0/TCP 192.168.1.10:43123", ack.header("Via"))

        sent.clear()
        client.bye()
        bye = sip.parse_message(bytes(sent))
        self.assertEqual(bye.method, "BYE")
        self.assertIn("SIP/2.0/TCP 192.168.1.10:43123", bye.header("Via"))

    def test_invite_carries_intercom_display_identity_headers(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="Casa",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        client.transport = FakeTransport()  # type: ignore[assignment]
        asyncio.run(client.invite(target="Cucina", remote_host="192.168.1.30", remote_sip_port=5060, timeout=0))

        raw, _ = client.transport.sent[0]  # type: ignore[union-attr]
        parsed = sip.parse_message(raw)
        self.assertEqual(parsed.header("X-Voip-Stack-Caller-Name"), "Casa")
        self.assertEqual(parsed.header("X-Voip-Stack-Caller-Route"), "Casa")
        self.assertEqual(parsed.header("X-Voip-Stack-Dest-Name"), "Cucina")

    def test_invite_preserves_registered_contact_request_uri_params(self) -> None:
        sent: list[bytes] = []
        contact_uri = "sip:Zoiper@192.168.1.50:5062;transport=tcp;ob;line=abc123"
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="Casa",
            local_sip_port=5060,
            local_rtp_port=41000,
            signaling_transport="TCP",
        )
        client.use_reused_tcp_connection(
            send=sent.append,
            responses=asyncio.Queue(),
            close=lambda: None,
        )

        asyncio.run(
            client.invite(
                target="Zoiper",
                remote_host="192.168.1.50",
                remote_sip_port=5062,
                request_uri=contact_uri,
                timeout=0,
            )
        )

        self.assertTrue(sent)
        raw = sent[0].decode()
        self.assertTrue(raw.startswith(f"INVITE {contact_uri} SIP/2.0\r\n"))
        parsed = sip.parse_message(sent[0])
        self.assertEqual(parsed.header("To"), f"<{contact_uri}>")

    def test_tcp_invite_connection_refused_returns_transport_unreachable(self) -> None:
        async def run() -> str:
            server = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            server.close()
            await server.wait_closed()
            client = sip_client.SipCallClient(
                local_ip="127.0.0.1",
                local_name="Casa",
                local_sip_port=5060,
                local_rtp_port=41000,
                signaling_transport="TCP",
            )
            return await client.invite(
                target="TestBaresip",
                remote_host="127.0.0.1",
                remote_sip_port=port,
                timeout=0.1,
            )

        result = asyncio.run(run())

        self.assertEqual(result, "transport_unreachable")

    def test_dialog_headers_preserve_transport_port_and_rport(self) -> None:
        headers = sip.dialog_headers(
            request_uri="sip:Cucina@192.168.1.30:5070",
            local_uri="sip:Casa@192.168.1.10:43123",
            remote_uri="sip:Cucina@192.168.1.30:5070",
            dialog=sip.SipDialogIds(call_id="call-via", local_tag="local"),
            method="INVITE",
            contact_uri="sip:Casa@192.168.1.10:43123",
            transport="TCP",
        )
        via = dict(headers)["Via"]
        self.assertEqual(via.split(";", 1)[0], "SIP/2.0/TCP 192.168.1.10:43123")
        self.assertIn(";rport", via)

    def test_parse_via_and_cseq_for_transaction_matching(self) -> None:
        via = sip.parse_via("SIP/2.0/TCP 192.168.1.10:43123;branch=z9hG4bKabc;rport=43123;received=192.168.1.10")
        self.assertEqual(via.transport, "TCP")
        self.assertEqual(via.host, "192.168.1.10")
        self.assertEqual(via.port, 43123)
        self.assertEqual(via.branch, "z9hG4bKabc")
        self.assertEqual(via.rport, 43123)
        self.assertEqual(via.received, "192.168.1.10")

        cseq = sip.parse_cseq("42 INVITE")
        self.assertEqual(cseq.number, 42)
        self.assertEqual(cseq.method, "INVITE")

    def test_auth_challenge_failures_have_explicit_reasons(self) -> None:
        self.assertEqual(sip.sip_failure_reason(401), "auth_required_unsupported")
        self.assertEqual(sip.sip_failure_reason(407), "proxy_auth_required_unsupported")
        self.assertEqual(sip.sip_failure_reason(488), "media_incompatible")

    def test_sip_transport_classifies_terminal_response_reasons(self) -> None:
        sip_transport = _load_sip_transport_with_homeassistant_stubs()
        self.assertEqual(sip_transport.sip_terminal_status("busy"), ("decline", 0, "busy"))
        self.assertEqual(sip_transport.sip_terminal_status("declined"), ("decline", 0, "declined"))
        self.assertEqual(sip_transport.sip_terminal_status("cancelled"), ("decline", 0, "cancelled"))
        self.assertEqual(
            sip_transport.sip_terminal_status("media_incompatible"),
            ("error", 488, "media_incompatible"),
        )
        self.assertEqual(
            sip_transport.sip_terminal_status("auth_required_unsupported"),
            ("error", 401, "auth_required_unsupported"),
        )
        self.assertEqual(
            sip_transport.sip_terminal_status("proxy_auth_required_unsupported"),
            ("error", 407, "proxy_auth_required_unsupported"),
        )
        self.assertEqual(sip_transport.sip_terminal_status("timeout"), ("error", 408, "timeout"))
        self.assertEqual(sip_transport.sip_terminal_status("sip_500"), ("error", 500, "sip_500"))
        self.assertEqual(sip_transport.sip_public_state("sip_500"), "transport_unreachable")
        self.assertEqual(sip_transport.sip_terminal_reason("sip_500"), "sip_500")
        self.assertEqual(
            sip_transport.sip_failure_response("sip_500"),
            (480, "Temporarily Unavailable", "sip_500", "transport_unreachable"),
        )
        self.assertEqual(
            sip_transport.sip_failure_response("dnd"),
            (486, "Busy Here", "dnd", "declined"),
        )


class SipClientSocketTest(unittest.IsolatedAsyncioTestCase):
    async def test_local_audio_websocket_relays_pcm_without_rtp_and_isolates_bad_frames(
        self,
    ) -> None:
        audio_ws_view = _load_audio_ws_runtime_module()
        audio_ws = _load_intercom_module("audio_ws")
        const = _load_intercom_module("const")
        from aiohttp import WSMsgType

        audio_contract = audio_format.HA_SIP_PCM_FORMATS[0]
        expected = int(audio_contract.nominal_frame_bytes)
        peer_pcm = bytes((index % 251 for index in range(expected)))
        browser_pcm = bytes((250 - (index % 251) for index in range(expected)))

        class Bridge:
            def __init__(self) -> None:
                self.sent: list[bytes] = []
                self.peer_delivered = False
                self.closed = asyncio.Event()

            async def receive_audio(
                self,
                _call_id: str,
                _endpoint_id: str,
                _token: str,
            ) -> bytes:
                if not self.peer_delivered:
                    self.peer_delivered = True
                    return peer_pcm
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            def send_audio(
                self,
                _call_id: str,
                _endpoint_id: str,
                _token: str,
                pcm: bytes,
            ) -> bool:
                self.sent.append(bytes(pcm))
                return False

            async def wait_closed(self, _call_id: str) -> None:
                await self.closed.wait()

        class WebSocket:
            def __init__(self) -> None:
                self.json: list[dict] = []
                self.binary: list[bytes] = []
                self.messages = [
                    types.SimpleNamespace(
                        type=WSMsgType.BINARY,
                        data=audio_ws.encode_audio_frame(bytes(expected + 1)),
                    ),
                    types.SimpleNamespace(
                        type=WSMsgType.BINARY,
                        data=audio_ws.encode_audio_frame(browser_pcm),
                    ),
                ]
                self.peer_frame_sent = asyncio.Event()
                self.forced_closed = False

            async def send_json(self, payload: dict) -> None:
                self.json.append(dict(payload))

            async def send_bytes(self, payload: bytes) -> None:
                self.binary.append(bytes(payload))
                self.peer_frame_sent.set()

            def force_close(self) -> None:
                self.forced_closed = True

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.messages:
                    return self.messages.pop(0)
                await self.peer_frame_sent.wait()
                raise StopAsyncIteration

        lease = types.SimpleNamespace(
            call_id="local-audio",
            endpoint_id="kitchen",
            token="lease-token",
        )
        hass = types.SimpleNamespace(
            data={const.DOMAIN: {const.CONF_DEBUG_MODE: False}},
            store={"call_id": lease.call_id, "state": "in_call"},
        )
        bridge = Bridge()
        ws = WebSocket()

        await asyncio.wait_for(
            audio_ws_view._run_local_audio_session(hass, ws, bridge, lease),
            timeout=1,
        )

        self.assertEqual(bridge.sent, [browser_pcm])
        self.assertEqual(
            [audio_ws.decode_audio_frame(frame) for frame in ws.binary],
            [peer_pcm],
        )
        self.assertEqual(ws.json[0]["media_transport"], "local_websocket")
        self.assertEqual(ws.json[0]["audio_direction"], "sendrecv")
        self.assertEqual(hass.store["drop_payload_size"], 1)
        self.assertEqual(hass.store["ws_rx"], 1)
        self.assertEqual(hass.store["ws_tx"], 1)

    async def test_local_video_websocket_relays_access_units_and_keyframe_control(
        self,
    ) -> None:
        video_ws_view = _load_video_ws_runtime_module()
        const = _load_intercom_module("const")
        from aiohttp import WSMsgType

        peer_frame = video_ws_view._VIDEO_HEADER.pack(
            video_ws_view._VIDEO_ACCESS_UNIT,
            0,
            9000,
        ) + b"peer-vp8"
        browser_frame = video_ws_view._VIDEO_HEADER.pack(
            video_ws_view._VIDEO_ACCESS_UNIT,
            0,
            18000,
        ) + b"browser-vp8"

        class Snapshot:
            @staticmethod
            def video_direction_for(_endpoint_id: str) -> str:
                return "sendrecv"

        class Bridge:
            def __init__(self) -> None:
                self.sent: list[bytes] = []
                self.controls: list[str] = []
                self.peer_delivered = False
                self.control_delivered = False
                self.closed = asyncio.Event()

            @staticmethod
            def require_call(_call_id: str) -> Snapshot:
                return Snapshot()

            async def receive_video(
                self,
                _call_id: str,
                _endpoint_id: str,
                _token: str,
            ) -> bytes:
                if not self.peer_delivered:
                    self.peer_delivered = True
                    return peer_frame
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            async def receive_video_control(
                self,
                _call_id: str,
                _endpoint_id: str,
                _token: str,
            ) -> str:
                if not self.control_delivered:
                    self.control_delivered = True
                    return "force_key_frame"
                await asyncio.Event().wait()
                raise AssertionError("unreachable")

            def send_video(
                self,
                _call_id: str,
                _endpoint_id: str,
                _token: str,
                frame: bytes,
            ) -> bool:
                self.sent.append(bytes(frame))
                return False

            def send_video_control(
                self,
                _call_id: str,
                _endpoint_id: str,
                _token: str,
                control: str,
            ) -> bool:
                self.controls.append(control)
                return False

            async def wait_closed(self, _call_id: str) -> None:
                await self.closed.wait()

        class WebSocket:
            def __init__(self) -> None:
                self.json: list[dict] = []
                self.binary: list[bytes] = []
                self.messages = [
                    types.SimpleNamespace(type=WSMsgType.BINARY, data=b"bad"),
                    types.SimpleNamespace(
                        type=WSMsgType.BINARY,
                        data=browser_frame,
                    ),
                    types.SimpleNamespace(
                        type=WSMsgType.TEXT,
                        data='{"type":"request_key_frame"}',
                    ),
                ]
                self.peer_frame_sent = asyncio.Event()
                self.control_sent = asyncio.Event()
                self.forced_closed = False

            async def send_json(self, payload: dict) -> None:
                copied = dict(payload)
                self.json.append(copied)
                if copied.get("type") == "force_key_frame":
                    self.control_sent.set()

            async def send_bytes(self, payload: bytes) -> None:
                self.binary.append(bytes(payload))
                self.peer_frame_sent.set()

            def force_close(self) -> None:
                self.forced_closed = True

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.messages:
                    return self.messages.pop(0)
                await asyncio.gather(
                    self.peer_frame_sent.wait(),
                    self.control_sent.wait(),
                )
                raise StopAsyncIteration

        lease = types.SimpleNamespace(
            call_id="local-video",
            endpoint_id="kitchen",
            token="lease-token",
        )
        hass = types.SimpleNamespace(
            data={const.DOMAIN: {const.CONF_DEBUG_MODE: False}},
            store={"call_id": lease.call_id, "state": "in_call"},
        )
        bridge = Bridge()
        ws = WebSocket()

        await asyncio.wait_for(
            video_ws_view._run_local_video_session(hass, ws, bridge, lease),
            timeout=1,
        )

        self.assertEqual(bridge.sent, [browser_frame])
        self.assertEqual(bridge.controls, ["force_key_frame"])
        self.assertEqual(ws.binary, [peer_frame])
        self.assertEqual(ws.json[0]["media_transport"], "local_websocket")
        self.assertEqual(ws.json[0]["direction"], "sendrecv")
        self.assertTrue(
            any(item.get("type") == "force_key_frame" for item in ws.json)
        )
        self.assertEqual(hass.store["video_drop_error"], 1)
        self.assertEqual(hass.store["video_access_units_tx"], 1)
        self.assertEqual(hass.store["video_access_units_rx"], 1)

    def test_video_udp_ingress_is_bounded_by_source_size_count_and_bytes(
        self,
    ) -> None:
        video_ws_view = _load_video_ws_runtime_module()
        queue = video_ws_view._ByteBudgetQueue(
            maxsize=2,
            max_bytes=6,
            item_size=lambda item: len(item[0]),
        )
        protocol = video_ws_view._RtpVideoProtocol(
            queue,
            source_allowed=lambda host: host == "192.0.2.10",
            min_datagram_bytes=2,
            max_datagram_bytes=4,
        )

        protocol.datagram_received(b"bad", ("192.0.2.11", 4000))
        protocol.datagram_received(b"12345", ("192.0.2.10", 4000))
        protocol.datagram_received(b"abc", ("192.0.2.10", 4000))
        protocol.datagram_received(b"def", ("192.0.2.10", 4000))
        protocol.datagram_received(b"gh", ("192.0.2.10", 4000))

        self.assertEqual(protocol.dropped_packets, 3)
        self.assertEqual(protocol.take_drop_counts(), (1, 1, 1))
        self.assertEqual(protocol.take_drop_counts(), (0, 0, 0))
        self.assertEqual(queue.queued_bytes, 5)
        self.assertEqual(queue.get_nowait()[0], b"def")
        self.assertEqual(queue.queued_bytes, 2)
        self.assertEqual(queue.get_nowait()[0], b"gh")
        self.assertEqual(queue.queued_bytes, 0)

    def test_video_access_unit_queue_tracks_a_byte_budget(self) -> None:
        video_ws_view = _load_video_ws_runtime_module()
        queue = video_ws_view._ByteBudgetQueue(
            maxsize=3,
            max_bytes=5,
            item_size=lambda item: len(item.data),
        )
        first = types.SimpleNamespace(data=b"abc")
        second = types.SimpleNamespace(data=b"de")
        queue.put_nowait(first)
        queue.put_nowait(second)

        self.assertEqual(queue.queued_bytes, 5)
        self.assertFalse(queue.can_fit(types.SimpleNamespace(data=b"f")))
        self.assertIs(queue.get_nowait(), first)
        self.assertEqual(queue.queued_bytes, 2)
        self.assertTrue(queue.can_fit(types.SimpleNamespace(data=b"fgh")))
        with self.assertRaises(asyncio.QueueFull):
            queue.put_nowait(types.SimpleNamespace(data=b"toolarge"))
        self.assertEqual(queue.queued_bytes, 2)

    async def test_video_packetization_executor_preserves_call_control_deadline(
        self,
    ) -> None:
        video_ws_view = _load_video_ws_runtime_module()
        call_control_ran = asyncio.Event()

        def slow_packetizer(*_args, **_kwargs):
            time.sleep(0.08)
            return [(b"rtp", 3)]

        async def call_control() -> None:
            await asyncio.sleep(0.005)
            call_control_ran.set()

        with patch.object(
            video_ws_view,
            "_packetize_browser_access_unit",
            side_effect=slow_packetizer,
        ):
            packetize_task = asyncio.create_task(
                video_ws_view._async_packetize_browser_access_unit(
                    b"frame",
                    encoding="VP8",
                    payload_type=103,
                    sequence=1,
                    timestamp=9000,
                    ssrc=7,
                )
            )
            control_task = asyncio.create_task(call_control())
            try:
                await asyncio.wait_for(call_control_ran.wait(), timeout=0.04)
                self.assertFalse(packetize_task.done())
                self.assertEqual(await packetize_task, [(b"rtp", 3)])
            finally:
                await asyncio.gather(
                    packetize_task,
                    control_task,
                    return_exceptions=True,
                )

    async def test_video_packetization_has_one_global_cpu_slot_and_skips_stale_work(
        self,
    ) -> None:
        video_ws_view = _load_video_ws_runtime_module()
        entered = threading.Event()
        release = threading.Event()
        calls = 0

        def blocking_packetizer(*_args, **_kwargs):
            nonlocal calls
            calls += 1
            entered.set()
            release.wait(timeout=1)
            return [(b"rtp", 3)]

        current = True
        with patch.object(
            video_ws_view,
            "_packetize_browser_access_unit",
            side_effect=blocking_packetizer,
        ):
            first = asyncio.create_task(
                video_ws_view._async_packetize_browser_access_unit(
                    b"first",
                    encoding="VP8",
                    payload_type=103,
                    sequence=1,
                    timestamp=9000,
                    ssrc=7,
                )
            )
            await asyncio.to_thread(entered.wait, 1)
            second = asyncio.create_task(
                video_ws_view._async_packetize_browser_access_unit(
                    b"stale",
                    encoding="VP8",
                    payload_type=103,
                    sequence=2,
                    timestamp=12000,
                    ssrc=7,
                    should_continue=lambda: current,
                )
            )
            current = False
            release.set()
            self.assertEqual(await first, [(b"rtp", 3)])
            self.assertIsNone(await second)
        self.assertEqual(calls, 1)

    async def test_video_rtp_send_burst_yields_to_call_end(self) -> None:
        video_ws_view = _load_video_ws_runtime_module()

        class Transport:
            def __init__(self) -> None:
                self.sent: list[bytes] = []

            def sendto(self, raw: bytes, _target: tuple[str, int]) -> None:
                self.sent.append(raw)

        transport = Transport()
        call_ended = asyncio.Event()
        asyncio.get_running_loop().call_soon(call_ended.set)
        sent: list[tuple[int, int]] = []
        datagrams = [(bytes(12), 4)] * (
            video_ws_view._VIDEO_RTP_SEND_BURST_PACKETS + 3
        )

        complete = await video_ws_view._send_packetized_datagrams(
            transport,
            datagrams,
            ("192.0.2.10", 4000),
            should_continue=lambda: not call_ended.is_set(),
            on_sent=lambda raw_bytes, payload_bytes: sent.append(
                (raw_bytes, payload_bytes)
            ),
        )

        self.assertFalse(complete)
        self.assertEqual(
            len(transport.sent),
            video_ws_view._VIDEO_RTP_SEND_BURST_PACKETS,
        )
        self.assertEqual(len(sent), len(transport.sent))

    async def test_debug_capture_tracks_home_assistant_executor_future(self) -> None:
        audio_ws_view = _load_audio_ws_runtime_module()
        const = _load_intercom_module("const")
        written = asyncio.Event()

        class Capture:
            capture_name = "future-contract"
            call_id = "debug-call"

            @staticmethod
            def write(counters: dict[str, int]) -> None:
                self.assertEqual(counters, {"rtp_rx": 7})

        class Hass:
            def __init__(self) -> None:
                self.data = {const.DOMAIN: {}}

            def async_add_executor_job(self, target, *args):
                async def run():
                    target(*args)
                    written.set()

                # HA exposes executor jobs as an asyncio Future. A resolved
                # Task has the same Future contract without needing threads in
                # this deterministic regression test.
                return asyncio.create_task(run())

        hass = Hass()
        audio_ws_view._schedule_debug_capture_write(
            hass,
            Capture(),
            {"rtp_rx": 7},
        )
        tasks = hass.data[const.DOMAIN]["debug_capture_tasks"]
        self.assertEqual(len(tasks), 1)
        await asyncio.wait_for(written.wait(), timeout=1)
        await asyncio.sleep(0)
        self.assertFalse(tasks)

    async def test_debug_capture_executor_queue_is_bounded(self) -> None:
        audio_ws_view = _load_audio_ws_runtime_module()
        const = _load_intercom_module("const")
        capture_limits = _load_intercom_module("debug_capture")

        class Capture:
            capture_name = "bounded-contract"

            def __init__(self, call_id: str) -> None:
                self.call_id = call_id

            def write(self, _counters: dict[str, int]) -> None:
                raise AssertionError("pending executor job must not run")

        class Hass:
            def __init__(self) -> None:
                self.data = {const.DOMAIN: {}}
                self.scheduled = 0

            def async_add_executor_job(self, _target, *_args):
                self.scheduled += 1
                return asyncio.get_running_loop().create_future()

        hass = Hass()
        for index in range(capture_limits.DEBUG_CAPTURE_MAX_PENDING_WRITES + 3):
            audio_ws_view._schedule_debug_capture_write(
                hass,
                Capture(f"debug-{index}"),
                {},
            )

        tasks = hass.data[const.DOMAIN]["debug_capture_tasks"]
        self.assertEqual(
            len(tasks),
            capture_limits.DEBUG_CAPTURE_MAX_PENDING_WRITES,
        )
        self.assertEqual(
            hass.scheduled,
            capture_limits.DEBUG_CAPTURE_MAX_PENDING_WRITES,
        )
        for task in tasks:
            task.cancel()

    def test_debug_capture_write_slots_are_globally_bounded(self) -> None:
        capture_limits = _load_intercom_module("debug_capture")
        reserved = 0
        try:
            for _index in range(
                capture_limits.DEBUG_CAPTURE_MAX_PENDING_WRITES
            ):
                self.assertTrue(capture_limits.try_reserve_debug_capture_write())
                reserved += 1
            self.assertFalse(capture_limits.try_reserve_debug_capture_write())
        finally:
            for _index in range(reserved):
                capture_limits.release_debug_capture_write()

    def test_debug_capture_write_slots_report_global_occupancy(self) -> None:
        capture_limits = _load_intercom_module("debug_capture")
        self.assertEqual(capture_limits.debug_capture_pending_writes(), 0)
        reserved = 0
        try:
            for expected in (1, 2):
                self.assertTrue(capture_limits.try_reserve_debug_capture_write())
                reserved += 1
                self.assertEqual(
                    capture_limits.debug_capture_pending_writes(),
                    expected,
                )
        finally:
            for _index in range(reserved):
                capture_limits.release_debug_capture_write()
        self.assertEqual(capture_limits.debug_capture_pending_writes(), 0)

    def test_audio_debug_scheduler_reports_global_writer_saturation(self) -> None:
        audio_ws_view = _load_audio_ws_runtime_module()
        const = _load_intercom_module("const")
        capture_limits = _load_intercom_module("debug_capture")
        reserved = 0

        class Capture:
            call_id = "saturated-debug"

            @staticmethod
            def write(_counters: dict[str, int]) -> None:
                raise AssertionError("saturated writer must not be scheduled")

        class Hass:
            def __init__(self) -> None:
                self.data = {const.DOMAIN: {}}
                self.scheduled = 0

            def async_add_executor_job(self, _target, *_args):
                self.scheduled += 1
                raise AssertionError("saturated writer must not reach executor")

        try:
            for _index in range(
                capture_limits.DEBUG_CAPTURE_MAX_PENDING_WRITES
            ):
                self.assertTrue(capture_limits.try_reserve_debug_capture_write())
                reserved += 1
            hass = Hass()
            audio_ws_view._schedule_debug_capture_write(hass, Capture(), {})
            self.assertEqual(hass.scheduled, 0)
            self.assertEqual(
                hass.data[const.DOMAIN]["debug_capture_dropped_writes"],
                1,
            )
        finally:
            for _index in range(reserved):
                capture_limits.release_debug_capture_write()

    def test_audio_debug_capture_rolls_back_partially_published_group(self) -> None:
        audio_ws_view = _load_audio_ws_runtime_module()
        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        rtp_format = sdp.audio_format_to_rtp(pcm, 96)
        capture = audio_ws_view._DebugAudioCapture(
            "atomic-debug",
            rx_format=rtp_format,
            tx_format=rtp_format,
        )
        capture.note_rtp_rx(1.0, bytes(pcm.nominal_frame_bytes))
        capture.note_ws_rx(1.0, bytes(pcm.nominal_frame_bytes))
        real_commit = audio_ws_view.commit_capture_file
        commits = 0

        def fail_second_commit(temporary: Path, destination: Path) -> None:
            nonlocal commits
            commits += 1
            if commits == 2:
                raise OSError("diagnostic rename failed")
            real_commit(temporary, destination)

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(audio_ws_view, "DEBUG_CAPTURE_DIR", Path(temp_dir)),
            patch.object(
                audio_ws_view,
                "debug_capture_transaction",
                side_effect=lambda: contextlib.nullcontext(),
            ),
            patch.object(
                audio_ws_view,
                "commit_capture_file",
                side_effect=fail_second_commit,
            ),
        ):
            with self.assertRaisesRegex(OSError, "rename failed"):
                capture.write({})
            self.assertEqual(list(Path(temp_dir).iterdir()), [])

    async def test_conference_audio_lifetime_ends_with_matching_call_event(self) -> None:
        audio_ws_view = _load_audio_ws_runtime_module()

        class Bus:
            def __init__(self) -> None:
                self.listener = None
                self.removed = False

            def async_listen(self, _event_type, callback):
                self.listener = callback

                def remove() -> None:
                    self.removed = True

                return remove

        bus = Bus()
        hass = types.SimpleNamespace(
            bus=bus,
            store={"call_id": "conference:Ops", "state": "in_call"},
        )
        ended, remove = audio_ws_view._listen_for_call_end(hass, "conference:Ops")
        self.assertFalse(ended.is_set())

        bus.listener(types.SimpleNamespace(data={"call_id": "other", "state": "idle"}))
        self.assertFalse(ended.is_set())
        bus.listener(
            types.SimpleNamespace(
                data={"call_id": "conference:Ops", "state": "idle"}
            )
        )

        await asyncio.wait_for(ended.wait(), timeout=1)
        remove()
        self.assertTrue(bus.removed)

    async def test_conference_audio_drops_oversized_pcm_before_mixer(self) -> None:
        audio_ws_view = _load_audio_ws_runtime_module()
        audio_ws = _load_intercom_module("audio_ws")
        const = _load_intercom_module("const")
        from aiohttp import WSMsgType

        class Bus:
            def async_listen(self, _event_type, _callback):
                return lambda: None

        class Manager:
            def __init__(self) -> None:
                self.frames: list[tuple[str, bytes]] = []

            def push_ha_audio(self, room: str, pcm: bytes) -> None:
                self.frames.append((room, bytes(pcm)))

        class WebSocket:
            def __init__(self, messages) -> None:
                self.messages = list(messages)
                self.json: list[dict] = []

            async def send_json(self, payload: dict) -> None:
                self.json.append(dict(payload))

            async def send_bytes(self, _payload: bytes) -> None:
                return None

            def force_close(self) -> None:
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.messages:
                    raise StopAsyncIteration
                return self.messages.pop(0)

        frame = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
        expected = int(frame.audio_format.nominal_frame_bytes)
        invalid = audio_ws.encode_audio_frame(bytes(expected + 1))
        valid = audio_ws.encode_audio_frame(bytes(expected))
        ws = WebSocket(
            [
                types.SimpleNamespace(type=WSMsgType.BINARY, data=invalid),
                types.SimpleNamespace(type=WSMsgType.BINARY, data=valid),
            ]
        )
        manager = Manager()
        hass = types.SimpleNamespace(
            data={const.DOMAIN: {"conference_manager": manager}},
            store={"call_id": "conference:Ops", "state": "in_call"},
            bus=Bus(),
        )
        session = audio_ws_view._SoftphoneMediaSession(
            call_id="conference:Ops",
            local_rtp_port=0,
            remote_rtp_host="",
            remote_rtp_port=0,
            send_format=frame,
            recv_format=frame,
            conference_room="Ops",
            conference_queue=asyncio.Queue(),
        )

        await audio_ws_view._run_conference_audio_session(hass, ws, session)

        self.assertEqual(manager.frames, [("conference:Ops", bytes(expected))])
        self.assertEqual(hass.store["tx_error"], 1)
        self.assertLessEqual(audio_ws_view._MAX_BROWSER_AUDIO_MESSAGE_BYTES, 4096)

    async def test_audio_websocket_reinvite_rebuilds_live_encoder_and_decoder(self) -> None:
        audio_ws_view = _load_audio_ws_runtime_module()
        audio_ws = _load_intercom_module("audio_ws")
        const = _load_intercom_module("const")
        from aiohttp import WSMsgType

        class Bus:
            def __init__(self) -> None:
                self.listeners: list = []

            def async_listen(self, _event_type, callback):
                self.listeners.append(callback)

                def remove() -> None:
                    self.listeners.remove(callback)

                return remove

        class Hass:
            def __init__(self) -> None:
                self.data = {const.DOMAIN: {const.CONF_DEBUG_MODE: False}}
                self.store = {"call_id": "audio-reinvite", "state": "in_call"}
                self.bus = Bus()

        class WebSocket:
            def __init__(self) -> None:
                self.json: list[dict] = []
                self.binary: list[bytes] = []
                self.messages: asyncio.Queue = asyncio.Queue()
                self.changed = asyncio.Event()

            async def send_json(self, payload: dict) -> None:
                self.json.append(dict(payload))
                self.changed.set()

            async def send_bytes(self, payload: bytes) -> None:
                self.binary.append(bytes(payload))
                self.changed.set()

            def force_close(self) -> None:
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                item = await self.messages.get()
                if item is None:
                    raise StopAsyncIteration
                return item

        async def wait_until(predicate, timeout: float = 1.0) -> None:
            deadline = asyncio.get_running_loop().time() + timeout
            while not predicate():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TimeoutError("condition not reached")
                ws.changed.clear()
                await asyncio.wait_for(ws.changed.wait(), timeout=remaining)

        loop = asyncio.get_running_loop()
        remote = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        remote.bind(("127.0.0.1", 0))
        remote.setblocking(False)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        local_port = int(probe.getsockname()[1])
        probe.close()

        pcma = sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20)
        l16 = sdp.RtpPcmFormat(96, "L16", 16000, 1, 10)
        session = audio_ws_view._SoftphoneMediaSession(
            call_id="audio-reinvite",
            local_rtp_port=local_port,
            remote_rtp_host="127.0.0.1",
            remote_rtp_port=int(remote.getsockname()[1]),
            send_format=pcma,
            recv_format=pcma,
            signaling_host="127.0.0.1",
        )
        hass = Hass()
        ws = WebSocket()
        runtime = asyncio.create_task(
            audio_ws_view._run_audio_session(hass, ws, session)
        )
        try:
            await wait_until(lambda: bool(ws.json))
            first_pcm = bytes(
                (index % 251 for index in range(pcma.audio_format.nominal_frame_bytes))
            )
            first_payload = sip_client.RtpPayloadEncoder(pcma).encode(first_pcm)
            oversized_packet = rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=pcma.payload_type,
                    sequence=0,
                    timestamp=1,
                    ssrc=7,
                    payload=first_payload + b"\x00",
                )
            )
            first_packet = rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=pcma.payload_type,
                    sequence=1,
                    timestamp=1,
                    ssrc=7,
                    payload=first_payload,
                )
            )
            await loop.sock_sendto(remote, oversized_packet, ("127.0.0.1", local_port))
            await loop.sock_sendto(remote, first_packet, ("127.0.0.1", local_port))
            await wait_until(lambda: bool(ws.binary))
            expected_first = sip_client.RtpPayloadDecoder(pcma).decode(first_payload)
            self.assertEqual(len(ws.binary), 1)
            self.assertEqual(audio_ws.decode_audio_frame(ws.binary[-1]), expected_first)

            session.send_format = l16
            session.recv_format = l16
            session.media_generation += 1
            session.update_event.set()
            await wait_until(lambda: any(item.get("type") == "media_update" for item in ws.json))

            second_pcm = bytes(
                (index * 3) % 251 for index in range(l16.audio_format.nominal_frame_bytes)
            )
            second_packet = rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=l16.payload_type,
                    sequence=2,
                    timestamp=321,
                    ssrc=8,
                    payload=sip_client.RtpPayloadEncoder(l16).encode(second_pcm),
                )
            )
            await loop.sock_sendto(remote, second_packet, ("127.0.0.1", local_port))
            await wait_until(lambda: len(ws.binary) >= 2)
            self.assertEqual(audio_ws.decode_audio_frame(ws.binary[-1]), second_pcm)

            await ws.messages.put(
                types.SimpleNamespace(
                    type=WSMsgType.BINARY,
                    data=audio_ws.encode_audio_frame(second_pcm),
                )
            )
            await asyncio.sleep(0.05)
            if runtime.done():
                self.fail(f"audio runtime ended during re-INVITE: {runtime.exception()!r}")
            decoded_tx = None
            deadline = loop.time() + 1.0
            decoder = sip_client.RtpPayloadDecoder(l16)
            while loop.time() < deadline and decoded_tx != second_pcm:
                data = await asyncio.wait_for(
                    loop.sock_recv(remote, 65535),
                    timeout=max(0.01, deadline - loop.time()),
                )
                packet = rtp.parse_packet(data)
                if packet.payload_type == l16.payload_type:
                    decoded_tx = decoder.decode(packet.payload)
            self.assertEqual(decoded_tx, second_pcm)
        finally:
            await ws.messages.put(None)
            await asyncio.wait_for(runtime, timeout=1)
            remote.close()

    async def test_audio_websocket_projects_negotiated_rfc4733_once(self) -> None:
        audio_ws_view = _load_audio_ws_runtime_module()
        const = _load_intercom_module("const")

        class Bus:
            def async_listen(self, _event_type, _callback):
                return lambda: None

        class Hass:
            def __init__(self) -> None:
                self.data = {const.DOMAIN: {const.CONF_DEBUG_MODE: False}}
                self.store = {"call_id": "audio-dtmf", "state": "in_call"}
                self.bus = Bus()

        class WebSocket:
            def __init__(self) -> None:
                self.json: list[dict] = []
                self.binary: list[bytes] = []
                self.messages: asyncio.Queue = asyncio.Queue()

            async def send_json(self, payload: dict) -> None:
                self.json.append(dict(payload))

            async def send_bytes(self, payload: bytes) -> None:
                self.binary.append(bytes(payload))

            def force_close(self) -> None:
                return None

            def __aiter__(self):
                return self

            async def __anext__(self):
                item = await self.messages.get()
                if item is None:
                    raise StopAsyncIteration
                return item

        loop = asyncio.get_running_loop()
        remote = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        remote.bind(("127.0.0.1", 0))
        remote.setblocking(False)
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", 0))
        local_port = int(probe.getsockname()[1])
        probe.close()
        pcma = sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20)
        digits: list[str] = []
        session = audio_ws_view._SoftphoneMediaSession(
            call_id="audio-dtmf",
            local_rtp_port=local_port,
            remote_rtp_host="127.0.0.1",
            remote_rtp_port=int(remote.getsockname()[1]),
            send_format=pcma,
            recv_format=pcma,
            signaling_host="127.0.0.1",
            dtmf_payload_type=101,
            dtmf_events=frozenset(range(16)),
            on_dtmf=digits.append,
        )
        hass = Hass()
        ws = WebSocket()
        runtime = asyncio.create_task(
            audio_ws_view._run_audio_session(hass, ws, session)
        )
        try:
            deadline = loop.time() + 1.0
            while not ws.json and loop.time() < deadline:
                await asyncio.sleep(0.01)
            self.assertTrue(ws.json)
            for sequence, end in ((1, False), (2, True), (3, True)):
                packet = rtp.build_packet(
                    rtp.RtpPacket(
                        payload_type=101,
                        sequence=sequence,
                        timestamp=1234,
                        ssrc=9,
                        payload=dtmf.build_telephone_event_payload(
                            "6", duration=160, end=end
                        ),
                    )
                )
                await loop.sock_sendto(
                    remote, packet, ("127.0.0.1", local_port)
                )
            deadline = loop.time() + 1.0
            while digits != ["6"] and loop.time() < deadline:
                await asyncio.sleep(0.01)
            self.assertEqual(digits, ["6"])

            pcm = bytes(pcma.audio_format.nominal_frame_bytes)
            audio_packet = rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=pcma.payload_type,
                    sequence=4,
                    timestamp=1394,
                    ssrc=7,
                    payload=sip_client.RtpPayloadEncoder(pcma).encode(pcm),
                )
            )
            await loop.sock_sendto(
                remote, audio_packet, ("127.0.0.1", local_port)
            )
            deadline = loop.time() + 1.0
            while not ws.binary and loop.time() < deadline:
                await asyncio.sleep(0.01)
            self.assertTrue(ws.binary)
        finally:
            await ws.messages.put(None)
            await asyncio.wait_for(runtime, timeout=1)
            remote.close()

    async def test_cancelled_tcp_close_releases_media_before_writer_drain(self) -> None:
        class Reservation:
            def __init__(self) -> None:
                self.releases = 0

            def release(self) -> None:
                self.releases += 1

        class BlockingTcpWriter:
            def __init__(self) -> None:
                self.entered = asyncio.Event()
                self.release = asyncio.Event()
                self.calls = 0

            async def close(self) -> None:
                self.calls += 1
                self.entered.set()
                await self.release.wait()

        class StreamWriter:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                return None

        reservation = Reservation()
        rtp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtcp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            media_reservation=reservation,
            video_rtp_socket=rtp_socket,
            video_rtcp_socket=rtcp_socket,
        )
        tcp_writer = BlockingTcpWriter()
        stream_writer = StreamWriter()
        client._tcp_writer = tcp_writer
        client.writer = stream_writer

        close_task = asyncio.create_task(client.close())
        await asyncio.wait_for(tcp_writer.entered.wait(), timeout=1)
        close_task.cancel()
        await asyncio.sleep(0)
        close_task.cancel()
        await asyncio.sleep(0)
        self.assertFalse(close_task.done())
        self.assertEqual(reservation.releases, 1)
        self.assertEqual(rtp_socket.fileno(), -1)
        self.assertEqual(rtcp_socket.fileno(), -1)
        self.assertFalse(stream_writer.closed)
        tcp_writer.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await close_task

        self.assertEqual(reservation.releases, 1)
        self.assertEqual(rtp_socket.fileno(), -1)
        self.assertEqual(rtcp_socket.fileno(), -1)
        self.assertTrue(stream_writer.closed)
        self.assertIsNone(client.media_reservation)
        self.assertIsNone(client._tcp_writer)
        self.assertIsNone(client.writer)
        self.assertTrue(client._closed)

    async def test_concurrent_tcp_close_waiters_share_one_completion_barrier(self) -> None:
        class BlockingTcpWriter:
            def __init__(self) -> None:
                self.entered = asyncio.Event()
                self.release = asyncio.Event()
                self.calls = 0

            async def close(self) -> None:
                self.calls += 1
                self.entered.set()
                await self.release.wait()

        class StreamWriter:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                return None

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        tcp_writer = BlockingTcpWriter()
        stream_writer = StreamWriter()
        client._tcp_writer = tcp_writer
        client.writer = stream_writer

        first = asyncio.create_task(client.close())
        await asyncio.wait_for(tcp_writer.entered.wait(), timeout=1)
        second = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        self.assertFalse(first.done())
        self.assertFalse(second.done())
        self.assertEqual(tcp_writer.calls, 1)

        tcp_writer.release.set()
        await asyncio.gather(first, second)
        self.assertTrue(stream_writer.closed)
        self.assertTrue(client._closed)

    async def test_close_completes_deferred_cancel_before_closing_transport(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []
                self.closed = False

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

            def close(self) -> None:
                self.closed = True

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        responses: asyncio.Queue[tuple[int, str, str]] = asyncio.Queue()

        async def read_response(_timeout: float):
            status, reason, method = await responses.get()
            return (
                sip.parse_message(
                    sip.build_response(
                        status,
                        reason,
                        [
                            (
                                "Via",
                                "SIP/2.0/UDP 127.0.0.1:5060;branch="
                                f"{client.dialog_ids.branch}",
                            ),
                            (
                                "From",
                                "<sip:HA@127.0.0.1>;tag="
                                f"{client.dialog_ids.local_tag}",
                            ),
                            ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                            ("Call-ID", client.dialog_ids.call_id),
                            ("CSeq", f"{client._invite_cseq} {method}"),
                        ],
                    )
                ),
                ("127.0.0.2", 5060),
            )

        client._read_response = read_response  # type: ignore[method-assign]
        owner = asyncio.create_task(
            client.invite(
                target="ESP",
                remote_host="127.0.0.2",
                remote_sip_port=5060,
            )
        )
        while not transport.sent:
            await asyncio.sleep(0)
        close_task = asyncio.create_task(client.close())
        await asyncio.sleep(0)
        self.assertFalse(close_task.done())
        self.assertEqual(
            [sip.parse_message(raw).method for raw, _addr in transport.sent],
            ["INVITE"],
        )

        responses.put_nowait((100, "Trying", "INVITE"))
        while not any(
            sip.parse_message(raw).method == "CANCEL"
            for raw, _addr in transport.sent
        ):
            await asyncio.sleep(0)
        responses.put_nowait((200, "OK", "CANCEL"))
        responses.put_nowait((487, "Request Terminated", "INVITE"))

        self.assertEqual(await asyncio.wait_for(owner, timeout=1), "cancelled")
        await asyncio.wait_for(close_task, timeout=1)

        self.assertIsNone(client.dialog)
        self.assertIsNone(client.early_dialog)
        self.assertTrue(client._closed)
        self.assertTrue(transport.closed)
        self.assertEqual(
            [sip.parse_message(raw).method for raw, _addr in transport.sent],
            ["INVITE", "CANCEL", "ACK"],
        )

    async def test_close_owns_final_waiter_and_acks_late_200_with_bye(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []
                self.closed = False

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

            def close(self) -> None:
                self.closed = True

        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        negotiated = sdp.audio_format_to_rtp(pcm, 96)
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        responses: asyncio.Queue[
            tuple[sip.SipMessage, tuple[str, int]]
        ] = asyncio.Queue()

        async def read_response(_timeout: float):
            return await responses.get()

        def response(
            status: int,
            reason: str,
            method: str,
            *,
            body: bytes = b"",
        ) -> tuple[sip.SipMessage, tuple[str, int]]:
            headers = [
                (
                    "Via",
                    "SIP/2.0/UDP 127.0.0.1:5060;branch="
                    f"{client.dialog_ids.branch}",
                ),
                (
                    "From",
                    f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}",
                ),
                ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                ("Contact", "<sip:dialog@127.0.0.2:5090>"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{client._invite_cseq} {method}"),
            ]
            if body:
                headers.append(("Content-Type", "application/sdp"))
            return (
                sip.parse_message(sip.build_response(status, reason, headers, body)),
                ("127.0.0.2", 5060),
            )

        client._read_response = read_response  # type: ignore[method-assign]
        owner = asyncio.create_task(
            client.invite(
                target="ESP",
                remote_host="127.0.0.2",
                remote_sip_port=5060,
            )
        )
        while not transport.sent:
            await asyncio.sleep(0)
        responses.put_nowait(response(180, "Ringing", "INVITE"))
        self.assertEqual(await asyncio.wait_for(owner, timeout=1), "ringing")

        final_waiter = asyncio.create_task(client.wait_for_final(timeout=2))
        while client._final_response_task is None:
            await asyncio.sleep(0)
        close_task = asyncio.create_task(client.close())
        while not any(
            sip.parse_message(raw).method == "CANCEL"
            for raw, _addr in transport.sent
        ):
            await asyncio.sleep(0)
        answer = sdp.build_answer_directional(
            "127.0.0.2",
            "127.0.0.2",
            42000,
            negotiated,
            negotiated,
        ).encode()
        responses.put_nowait(response(200, "OK", "INVITE", body=answer))

        self.assertEqual(
            await asyncio.wait_for(final_waiter, timeout=1),
            "cancelled",
        )
        await asyncio.wait_for(close_task, timeout=1)

        self.assertIsNone(client.dialog)
        self.assertTrue(client._closed)
        self.assertTrue(transport.closed)
        self.assertEqual(
            [sip.parse_message(raw).method for raw, _addr in transport.sent],
            ["INVITE", "CANCEL", "ACK", "BYE"],
        )

    async def test_cancelled_dialog_waiter_joins_both_child_tasks(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        client.dialog = types.SimpleNamespace(remote_host="127.0.0.2")  # type: ignore[assignment]
        read_started = asyncio.Event()
        read_cancelled = asyncio.Event()
        release_read = asyncio.Event()

        async def blocked_read(_timeout: float):
            read_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                read_cancelled.set()
                await release_read.wait()
                raise

        client._read_response = blocked_read  # type: ignore[method-assign]
        waiter = asyncio.create_task(client.wait_for_dialog_termination())
        await asyncio.wait_for(read_started.wait(), timeout=1)
        waiter.cancel()
        await asyncio.wait_for(read_cancelled.wait(), timeout=1)
        waiter.cancel()
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        release_read.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(waiter, timeout=1)

        pending_names = {
            task.get_name()
            for task in asyncio.all_tasks()
            if not task.done()
        }
        self.assertFalse(
            any(name.startswith("voip-sip-dialog-") for name in pending_names)
        )
        await client.close()

    async def test_udp_start_cannot_publish_transport_after_close(self) -> None:
        entered = asyncio.Event()
        release_endpoint = asyncio.Event()

        class Transport:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

            def get_extra_info(self, _name: str):
                return ("0.0.0.0", 5099)

        transport = Transport()

        async def create_endpoint(*_args, **_kwargs):
            entered.set()
            await release_endpoint.wait()
            return transport, object()

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        loop = asyncio.get_running_loop()
        with patch.object(
            loop,
            "create_datagram_endpoint",
            new=create_endpoint,
        ):
            start_task = asyncio.create_task(client.start())
            await asyncio.wait_for(entered.wait(), timeout=1)
            await asyncio.wait_for(client.close(), timeout=1)
            release_endpoint.set()
            with self.assertRaisesRegex(RuntimeError, "closed while starting"):
                await start_task

        self.assertTrue(transport.closed)
        self.assertIsNone(client.transport)
        self.assertIsNone(client.protocol)
        self.assertTrue(client._closed)

    def test_video_endpoint_manager_requires_media_update_handler(self) -> None:
        async def on_invite(_invite):
            return None

        with self.assertRaisesRegex(ValueError, "media-update handler"):
            sip_endpoint.SipEndpointManager(
                host="0.0.0.0",
                port=5060,
                local_ip="127.0.0.1",
                local_rtp_port=41000,
                supported_formats=[
                    audio_format.AudioFormat(16000, "s16le", 1, 20)
                ],
                on_invite=on_invite,
                enable_video=True,
            )

    async def test_endpoint_manager_cancelled_partial_start_stops_both_servers(self) -> None:
        class Server:
            def __init__(self, *, blocked: bool = False) -> None:
                self.blocked = blocked
                self.entered = asyncio.Event()
                self.stop_calls = 0

            async def start(self) -> bool:
                self.entered.set()
                if self.blocked:
                    await asyncio.Event().wait()
                return True

            async def stop(self) -> None:
                self.stop_calls += 1

        udp = Server()
        tcp = Server(blocked=True)

        async def on_invite(_invite):
            return None

        manager = sip_endpoint.SipEndpointManager(
            host="0.0.0.0",
            port=5060,
            local_ip="127.0.0.1",
            local_rtp_port=41000,
            supported_formats=[
                audio_format.AudioFormat(16000, "s16le", 1, 20)
            ],
            on_invite=on_invite,
        )
        with (
            patch.object(sip_endpoint, "SipUdpServer", return_value=udp),
            patch.object(sip_endpoint, "SipTcpServer", return_value=tcp),
        ):
            starting = asyncio.create_task(manager.start())
            await asyncio.wait_for(tcp.entered.wait(), timeout=1)
            starting.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(starting, timeout=1)

        self.assertEqual(udp.stop_calls, 1)
        self.assertEqual(tcp.stop_calls, 1)
        self.assertIsNone(manager.udp_server)
        self.assertIsNone(manager.tcp_server)

    async def test_endpoint_manager_stop_cancels_inflight_start_without_resurrection(self) -> None:
        class Server:
            def __init__(self, *, blocked: bool = False) -> None:
                self.blocked = blocked
                self.entered = asyncio.Event()
                self.stop_calls = 0

            async def start(self) -> bool:
                self.entered.set()
                if self.blocked:
                    await asyncio.Event().wait()
                return True

            async def stop(self) -> None:
                self.stop_calls += 1

        udp = Server()
        tcp = Server(blocked=True)

        async def on_invite(_invite):
            return None

        manager = sip_endpoint.SipEndpointManager(
            host="0.0.0.0",
            port=5060,
            local_ip="127.0.0.1",
            local_rtp_port=41000,
            supported_formats=[
                audio_format.AudioFormat(16000, "s16le", 1, 20)
            ],
            on_invite=on_invite,
        )
        with (
            patch.object(sip_endpoint, "SipUdpServer", return_value=udp),
            patch.object(sip_endpoint, "SipTcpServer", return_value=tcp),
        ):
            starting = asyncio.create_task(manager.start())
            await asyncio.wait_for(tcp.entered.wait(), timeout=1)
            await asyncio.wait_for(manager.stop(), timeout=1)
            with self.assertRaises(asyncio.CancelledError):
                await starting

        self.assertEqual(udp.stop_calls, 1)
        self.assertEqual(tcp.stop_calls, 1)
        self.assertIsNone(manager.udp_server)
        self.assertIsNone(manager.tcp_server)
        self.assertTrue(manager._stopped)

    async def test_trunk_stop_cannot_resurrect_delayed_tcp_connection(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="tcp",
            server="127.0.0.1",
            port=5060,
            domain="127.0.0.1",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(
            config=config,
            local_ip="127.0.0.1",
            local_sip_port=5060,
        )
        entered = asyncio.Event()
        cancelled = asyncio.Event()
        release = asyncio.Event()
        reader = asyncio.StreamReader()

        class Writer:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                return None

            def is_closing(self) -> bool:
                return self.closed

            def get_extra_info(self, _name: str):
                return ("127.0.0.1", 5060)

        writer = Writer()

        async def delayed_open_connection(*_args, **_kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return reader, writer

        with patch.object(
            sip_trunk.asyncio,
            "open_connection",
            new=delayed_open_connection,
        ):
            starting = asyncio.create_task(trunk.start())
            await asyncio.wait_for(entered.wait(), timeout=1)
            stopping = asyncio.create_task(trunk.stop())
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            self.assertFalse(stopping.done())
            release.set()
            await asyncio.wait_for(stopping, timeout=1)
            await asyncio.wait_for(starting, timeout=1)

        self.assertTrue(writer.closed)
        self.assertTrue(trunk._stopped)
        self.assertIsNone(trunk.reader)
        self.assertIsNone(trunk.writer)
        self.assertIsNone(trunk._tcp_writer)
        self.assertIsNone(trunk._receive_task)
        self.assertIsNone(trunk._refresh_task)

    async def test_video_capable_trunk_profile_negotiates_h264_end_to_end(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(5) as ports:
            sip_port, server_audio, server_video, client_audio, client_video = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        seen: dict[str, object] = {}

        async def on_invite(invite):
            seen["video"] = invite.video_format
            seen["local_video"] = invite.local_video_format
            seen["video_payload_types"] = invite.remote_video_payload_types
            self.assertIsNotNone(invite.video_format)
            answer = sdp.build_answer_directional(
                local,
                local,
                server_audio,
                invite.send_format,
                invite.recv_format,
                remote_sdp=invite.remote_sdp,
                video_port=server_video,
                video_format=invite.recv_video_format,
                video_direction="sendrecv",
            )
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer)

        server = sip_listener.SipUdpServer(
            host=local,
            port=sip_port,
            local_ip=local,
            local_rtp_port=server_audio,
            supported_formats=[audio],
            on_invite=on_invite,
            enable_video=True,
        )
        self.assertTrue(await server.start())
        client = sip_client.SipCallClient(
            local_ip=local,
            local_name="HA Mare",
            local_sip_port=5060,
            local_rtp_port=client_audio,
            supported_formats=[audio],
            local_video_rtp_port=client_video,
            video_formats=(sdp.DEFAULT_H264_FORMAT,),
            video_direction="sendrecv",
            username="390000000001",
            auth_username="390000000001",
            password="test-only",
        )
        try:
            self.assertEqual(
                await client.invite(
                    target="390000000002",
                    remote_host=local,
                    remote_sip_port=sip_port,
                ),
                "in_call",
            )
            self.assertIsNotNone(client.dialog)
            assert client.dialog is not None
            self.assertIsNotNone(client.dialog.video_format)
            assert client.dialog.video_format is not None
            self.assertEqual(client.dialog.video_format.encoding, "H264")
            self.assertIsNotNone(client.dialog.local_video_format)
            assert client.dialog.local_video_format is not None
            self.assertEqual(client.dialog.local_video_format.encoding, "H264")
            self.assertEqual(client.dialog.remote_video_rtp_port, server_video)
            self.assertEqual(client.dialog.local_video_direction, "sendrecv")
            self.assertIsNotNone(seen.get("video"))
            self.assertIsNotNone(seen.get("local_video"))
            self.assertEqual(
                seen.get("video_payload_types"),
                (sdp.DEFAULT_H264_FORMAT.payload_type,),
            )
        finally:
            client.bye()
            await client.close()
            await server.stop()

    async def test_outbound_dialog_keeps_h264_offer_and_answer_levels(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(5) as ports:
            sip_port, server_audio, server_video, client_audio, client_video = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        high = sdp.RtpVideoFormat(
            payload_type=103,
            profile_level_id="42801f",
            level_asymmetry_allowed=True,
        )

        async def on_invite(invite):
            self.assertEqual(invite.video_format.profile_level_id, "42801f")
            low_answer = sdp.RtpVideoFormat(
                payload_type=invite.video_format.payload_type,
                profile_level_id="42800d",
                packetization_mode=invite.video_format.packetization_mode,
                level_asymmetry_allowed=True,
                direction=invite.video_format.direction,
                transport_profile=invite.video_format.transport_profile,
            )
            answer = sdp.build_answer_directional(
                local,
                local,
                server_audio,
                invite.send_format,
                invite.recv_format,
                remote_sdp=invite.remote_sdp,
                video_port=server_video,
                video_format=low_answer,
                video_direction="sendrecv",
            )
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer)

        server = sip_listener.SipUdpServer(
            host=local,
            port=sip_port,
            local_ip=local,
            local_rtp_port=server_audio,
            supported_formats=[audio],
            on_invite=on_invite,
            enable_video=True,
        )
        self.assertTrue(await server.start())
        client = sip_client.SipCallClient(
            local_ip=local,
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=client_audio,
            supported_formats=[audio],
            local_video_rtp_port=client_video,
            video_formats=(high,),
            video_direction="sendrecv",
        )
        try:
            result = await client.invite(
                target="peer",
                remote_host=local,
                remote_sip_port=sip_port,
            )
            self.assertEqual(result, "in_call")
            self.assertIsNotNone(client.dialog)
            assert client.dialog is not None
            self.assertEqual(
                client.dialog.send_video_format.profile_level_id,
                "42800d",
            )
            self.assertEqual(
                client.dialog.recv_video_format.profile_level_id,
                "42801f",
            )
        finally:
            client.bye()
            await client.close()
            await server.stop()

    async def test_invite_100_trying_stops_udp_retransmission_without_reporting_ringing(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        sends: list[bytes] = []
        read_timeouts: list[float] = []

        async def fake_start() -> None:
            return None

        async def fake_send(raw: bytes, _host: str, _port: int) -> None:
            sends.append(raw)

        async def fake_read(timeout: float):
            read_timeouts.append(timeout)
            if len(read_timeouts) > 1:
                return None
            response = sip.build_response(
                100,
                "Trying",
                [
                    ("Via", f"SIP/2.0/UDP 127.0.0.1:5060;branch={client.dialog_ids.branch}"),
                    ("From", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                    ("To", "<sip:ESP@127.0.0.2>"),
                    ("Call-ID", client.dialog_ids.call_id),
                    ("CSeq", f"{client._invite_cseq} INVITE"),
                ],
            )
            return sip.parse_message(response), ("127.0.0.2", 5060)

        client.start = fake_start  # type: ignore[method-assign]
        client._send_raw = fake_send  # type: ignore[method-assign]
        client._read_response = fake_read  # type: ignore[method-assign]

        result = await client.invite(
            target="ESP",
            remote_host="127.0.0.2",
            remote_sip_port=5060,
            timeout=2.0,
        )

        self.assertEqual(result, "timeout")
        self.assertEqual(len(sends), 1)
        self.assertGreater(read_timeouts[-1], 1.0)

    async def test_outbound_client_advertises_bound_socket_port(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="Casa",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        try:
            await client.start()
            self.assertNotEqual(client.local_sip_port, 5060)
            self.assertGreater(client.local_sip_port, 0)
        finally:
            await client.close()

    def test_sip_listener_prefers_intercom_display_identity_headers(self) -> None:
        body = sdp.build_offer(
            "192.168.1.47",
            "192.168.1.47",
            40000,
            [audio_format.AudioFormat(16000, "s16le", 1, 32)],
        ).encode()
        raw = sip.build_request(
            "INVITE",
            "sip:Spotpear_Ball_v2@192.168.1.10",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.47:5060;branch=z9hG4bKdisplay"),
                ("From", "<sip:Waveshare_S3_Audio@192.168.1.47>;tag=src"),
                ("To", "<sip:Spotpear_Ball_v2@192.168.1.10>"),
                ("Call-ID", "call-display"),
                ("CSeq", "1 INVITE"),
                ("Contact", "<sip:Waveshare_S3_Audio@192.168.1.47:5060>"),
                ("Content-Type", "application/sdp"),
                ("X-Voip-Stack-Caller-Name", "Waveshare S3 Audio"),
                ("X-Voip-Stack-Dest-Name", "Spotpear Ball v2"),
            ],
            body,
        )
        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40002,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 32)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
        )
        invite = endpoint._parse_invite(sip.parse_message(raw), ("192.168.1.47", 5060))
        self.assertIsNotNone(invite)
        assert invite is not None
        self.assertEqual(invite.caller, "Waveshare S3 Audio")
        self.assertEqual(invite.target, "Spotpear Ball v2")

    async def test_listener_replies_405_and_501_for_unsupported_methods(self) -> None:
        sent: list[bytes] = []
        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40002,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 32)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=lambda data, _addr: sent.append(data),
        )

        register = (
            b"REGISTER sip:Casa@192.168.1.10 SIP/2.0\r\n"
            b"Via: SIP/2.0/UDP 192.168.1.20:5060;branch=z9hG4bKreg;rport\r\n"
            b"From: <sip:ESP@192.168.1.20>;tag=src\r\n"
            b"To: <sip:Casa@192.168.1.10>\r\n"
            b"Call-ID: reg-1\r\n"
            b"CSeq: 1 REGISTER\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        await endpoint._handle_datagram(register, ("192.168.1.20", 5060))
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 405)
        self.assertIn("INVITE", sip.parse_message(sent[-1]).header("Allow"))

        custom = register.replace(b"REGISTER", b"BREW")
        await endpoint._handle_datagram(custom, ("192.168.1.20", 5060))
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 501)

    async def test_listener_200_ok_invite_includes_contact(self) -> None:
        sent: list[bytes] = []
        fmt = audio_format.AudioFormat(48000, "s16le", 1, 10)
        rtp_fmt = sdp.audio_format_to_rtp(fmt, 96)
        offer = sdp.build_offer("192.168.1.48", "192.168.1.48", 40900, [fmt]).encode()
        answer = sdp.build_answer_directional("192.168.1.10", "192.168.1.10", 40000, rtp_fmt, rtp_fmt)

        async def on_invite(_invite):
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer)

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            send_override=lambda data, _addr: sent.append(data),
            signaling_transport="TCP",
        )
        invite = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10;transport=tcp",
            [
                ("Via", "SIP/2.0/TCP 192.168.1.48:38946;branch=z9hG4bKcontact;rport"),
                ("From", '"Test Baresip" <sip:test@192.168.1.48>;tag=src'),
                ("To", "<sip:Casa@192.168.1.10;transport=tcp>"),
                ("Call-ID", "contact-200-ok"),
                ("CSeq", "1 INVITE"),
                ("Contact", "<sip:test@192.168.1.48:38946;transport=tcp>"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )

        await endpoint._handle_datagram(invite, ("192.168.1.48", 38946))

        response = sip.parse_message(sent[-1])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.header("Contact"), "<sip:Casa@192.168.1.10:5060;transport=tcp>")
        self.assertIn("L16/48000/1", response.body.decode())

    async def test_listener_retransmits_udp_invite_2xx_until_matching_ack(self) -> None:
        sent: list[bytes] = []
        second_2xx = asyncio.Event()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        rtp_fmt = sdp.audio_format_to_rtp(fmt, 96)
        offer = sdp.build_offer("192.168.1.48", "192.168.1.48", 40900, [fmt]).encode()
        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            rtp_fmt,
            rtp_fmt,
        )

        def capture(data: bytes, _addr: tuple[str, int]) -> None:
            sent.append(data)
            message = sip.parse_message(data)
            if message.status_code == 200 and sum(
                sip.parse_message(raw).status_code == 200 for raw in sent
            ) >= 2:
                second_2xx.set()

        async def on_invite(_invite):
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer)

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            send_override=capture,
        )
        invite = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bK2xx;rport"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Call-ID", "udp-2xx-call"),
                ("CSeq", "7 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )
        addr = ("192.168.1.48", 5060)

        try:
            with (
                patch.object(sip_listener, "_SIP_T1", 0.005),
                patch.object(sip_listener, "_SIP_T2", 0.01),
                patch.object(sip_listener, "_INVITE_2XX_TIMEOUT", 0.1),
            ):
                await endpoint._handle_datagram(invite, addr)
                await asyncio.wait_for(second_2xx.wait(), timeout=0.2)
                dialog = endpoint.active_dialogs["udp-2xx-call"]
                self.assertEqual(dialog.pending_ack_cseq, 7)
                self.assertGreaterEqual(dialog.invite_2xx_retransmissions, 1)
                self.assertEqual(endpoint.snapshot()["pending_invite_acks"], 1)

                ack = sip.build_request(
                    "ACK",
                    "sip:Casa@192.168.1.10:5060",
                    [
                        ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKack"),
                        ("From", "<sip:test@192.168.1.48>;tag=remote"),
                        ("To", f"<sip:Casa@192.168.1.10>;tag={dialog.to_tag}"),
                        ("Call-ID", "udp-2xx-call"),
                        ("CSeq", "7 ACK"),
                    ],
                )
                await endpoint._handle_datagram(ack, (addr[0], 5090))
                count_after_ack = len(sent)
                await asyncio.sleep(0.025)

                self.assertEqual(len(sent), count_after_ack)
                self.assertEqual(dialog.pending_ack_cseq, 0)
                self.assertIsNone(dialog.invite_2xx_task)
                self.assertEqual(endpoint.snapshot()["pending_invite_acks"], 0)
        finally:
            endpoint.cancel_request_tasks()

    async def test_listener_retransmits_tcp_invite_2xx_and_accepts_proxy_ack(self) -> None:
        sent: list[bytes] = []
        retransmitted = asyncio.Event()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)

        def capture(data: bytes, _addr: tuple[str, int]) -> None:
            sent.append(data)
            if sip.parse_message(data).status_code == 200:
                retransmitted.set()

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.0.2.20",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=capture,
            signaling_transport="TCP",
        )
        request = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:Casa@192.0.2.20",
                [
                    ("Via", "SIP/2.0/TCP 192.0.2.10;branch=z9hG4bKtcp-2xx"),
                    ("From", "<sip:test@192.0.2.10>;tag=remote"),
                    ("To", "<sip:Casa@192.0.2.20>"),
                    ("Contact", "<sip:test@192.0.2.10:5060;transport=tcp>"),
                    ("Call-ID", "tcp-2xx-call"),
                    ("CSeq", "7 INVITE"),
                ],
            )
        )
        dialog = sip_listener._ActiveDialog(
            request,
            ("192.0.2.10", 5060),
            "local",
            8,
            "TCP",
            answer_sdp="v=0\r\n",
        )
        endpoint.active_dialogs["tcp-2xx-call"] = dialog

        try:
            with (
                patch.object(sip_listener, "_SIP_T1", 0.005),
                patch.object(sip_listener, "_SIP_T2", 0.01),
                patch.object(sip_listener, "_INVITE_2XX_TIMEOUT", 0.1),
            ):
                endpoint._arm_invite_2xx(
                    dialog,
                    request,
                    dialog.addr,
                    200,
                    "OK",
                    "v=0\r\n",
                )
                await asyncio.wait_for(retransmitted.wait(), timeout=0.2)
                ack = sip.build_request(
                    "ACK",
                    "sip:Casa@192.0.2.20",
                    [
                        ("Via", "SIP/2.0/TCP 192.0.2.99;branch=z9hG4bKproxy-ack"),
                        ("From", "<sip:test@192.0.2.10>;tag=remote"),
                        ("To", "<sip:Casa@192.0.2.20>;tag=local"),
                        ("Call-ID", "tcp-2xx-call"),
                        ("CSeq", "7 ACK"),
                    ],
                )
                await endpoint._handle_datagram(ack, ("192.0.2.99", 5060))
                count_after_ack = len(sent)
                await asyncio.sleep(0.025)

                self.assertEqual(len(sent), count_after_ack)
                self.assertEqual(dialog.pending_ack_cseq, 0)
                self.assertIsNone(dialog.invite_2xx_task)
        finally:
            endpoint.cancel_request_tasks()

    async def test_listener_invite_2xx_ack_timeout_sends_bye_and_terminates(self) -> None:
        sent: list[bytes] = []
        terminated = asyncio.Event()
        reasons: list[tuple[str, str]] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)

        async def on_terminated(call_id: str, reason: str) -> None:
            reasons.append((call_id, reason))
            terminated.set()

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.0.2.20",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            on_terminated=on_terminated,
            send_override=lambda data, _addr: sent.append(data),
        )
        request = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:Casa@192.0.2.20",
                [
                    ("Via", "SIP/2.0/UDP 192.0.2.10;branch=z9hG4bKtimeout"),
                    ("From", "<sip:test@192.0.2.10>;tag=remote"),
                    ("To", "<sip:Casa@192.0.2.20>"),
                    ("Contact", "<sip:test@192.0.2.10:5060>"),
                    ("Call-ID", "ack-timeout-call"),
                    ("CSeq", "1 INVITE"),
                ],
            )
        )
        dialog = sip_listener._ActiveDialog(
            request,
            ("192.0.2.10", 5060),
            "local",
            2,
            "UDP",
            answer_sdp="v=0\r\n",
        )
        endpoint.active_dialogs["ack-timeout-call"] = dialog

        with (
            patch.object(sip_listener, "_SIP_T1", 0.001),
            patch.object(sip_listener, "_SIP_T2", 0.002),
            patch.object(sip_listener, "_INVITE_2XX_TIMEOUT", 0.006),
        ):
            endpoint._arm_invite_2xx(
                dialog,
                request,
                dialog.addr,
                200,
                "OK",
                "v=0\r\n",
            )
            await asyncio.wait_for(terminated.wait(), timeout=0.2)

        self.assertEqual(reasons, [("ack-timeout-call", "ack_timeout")])
        self.assertEqual(dialog.pending_ack_cseq, 0)
        self.assertNotIn("ack-timeout-call", endpoint.active_dialogs)
        self.assertIn("BYE", [sip.parse_message(raw).method for raw in sent])

    async def test_listener_coalesces_invite_retransmits_and_replays_final_response(self) -> None:
        sent: list[bytes] = []
        calls = 0
        started = asyncio.Event()
        release = asyncio.Event()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        rtp_fmt = sdp.audio_format_to_rtp(fmt, 96)
        offer = sdp.build_offer("192.168.1.48", "192.168.1.48", 40900, [fmt]).encode()
        answer = sdp.build_answer_directional("192.168.1.10", "192.168.1.10", 40000, rtp_fmt, rtp_fmt)

        async def on_invite(_invite):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return sip_listener.SipInviteResult(180, "Ringing", defer_final=True)

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            send_override=lambda data, _addr: sent.append(data),
        )
        invite = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKdedupe"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Call-ID", "dedupe-call"),
                ("CSeq", "7 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )
        addr = ("192.168.1.48", 5060)

        first = asyncio.create_task(endpoint._handle_datagram(invite, addr))
        await started.wait()
        await endpoint._handle_datagram(invite, (addr[0], 5090))
        self.assertEqual(calls, 1)
        self.assertEqual([sip.parse_message(raw).status_code for raw in sent[-2:]], [100, 100])

        release.set()
        await first
        await endpoint._handle_datagram(invite, addr)
        self.assertEqual(calls, 1)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 180)

        self.assertTrue(endpoint.send_final_response("dedupe-call", 200, "OK", answer_sdp=answer))
        await endpoint._handle_datagram(invite, addr)
        replay = sip.parse_message(sent[-1])
        self.assertEqual(replay.status_code, 200)
        rendered_answer = endpoint.active_dialogs["dedupe-call"].answer_sdp
        self.assertEqual(replay.body.decode(), rendered_answer)
        origin = next(
            line for line in rendered_answer.splitlines() if line.startswith("o=")
        ).split()
        self.assertNotEqual(origin[1], "0")
        self.assertEqual(origin[2], "0")
        self.assertEqual(calls, 1)

    async def test_listener_sends_sdp_in_deferred_183_early_media_response(self) -> None:
        sent: list[bytes] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        rtp_fmt = sdp.audio_format_to_rtp(fmt, 96)
        offer = sdp.build_offer(
            "192.168.1.48", "192.168.1.48", 40900, [fmt]
        ).encode()
        answer = sdp.build_answer_directional(
            "192.168.1.10", "192.168.1.10", 40000, rtp_fmt, rtp_fmt
        )

        async def on_invite(_invite):
            return sip_listener.SipInviteResult(
                183,
                "Session Progress",
                answer_sdp=answer,
                defer_final=True,
            )

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            send_override=lambda data, _addr: sent.append(data),
        )
        invite = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKearly"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Call-ID", "early-media-call"),
                ("CSeq", "1 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )

        await endpoint._handle_datagram(invite, ("192.168.1.48", 5060))

        progress = sip.parse_message(sent[-1])
        self.assertEqual(progress.status_code, 183)
        self.assertEqual(progress.header("Content-Type"), "application/sdp")
        self.assertIn(b"m=audio 40000", progress.body)
        self.assertIn("early-media-call", endpoint.pending_invites)
        self.assertNotIn("early-media-call", endpoint.active_dialogs)

    async def test_listener_response_preserves_complete_via_chain(self) -> None:
        sent: list[bytes] = []
        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=lambda data, _addr: sent.append(data),
        )
        options = sip.build_request(
            "OPTIONS",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.1:5060;branch=z9hG4bKproxy;rport"),
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKclient"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Call-ID", "via-chain-call"),
                ("CSeq", "7 OPTIONS"),
            ],
        )

        await endpoint._handle_datagram(options, ("192.168.1.1", 5090))

        response = sip.parse_message(sent[-1])
        vias = response.header_values("Via")
        self.assertEqual(len(vias), 2)
        self.assertIn("branch=z9hG4bKproxy", vias[0])
        self.assertIn(";rport=5090", vias[0])
        self.assertNotIn(";received=", vias[0])
        self.assertEqual(vias[1], "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKclient")

    async def test_listener_response_adds_received_without_rport(self) -> None:
        sent: list[bytes] = []
        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=lambda data, _addr: sent.append(data),
        )
        options = sip.build_request(
            "OPTIONS",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 10.0.0.50:5060;branch=z9hG4bKnat"),
                ("From", "<sip:test@10.0.0.50>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Call-ID", "via-received-call"),
                ("CSeq", "7 OPTIONS"),
            ],
        )

        await endpoint._handle_datagram(options, ("198.51.100.50", 5090))

        via = sip.parse_message(sent[-1]).header("Via")
        self.assertIn("received=198.51.100.50", via)
        self.assertNotIn(";rport=", via)

    async def test_listener_replays_negative_invite_final_without_rerouting(self) -> None:
        sent: list[bytes] = []
        calls = 0
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        offer = sdp.build_offer("192.168.1.48", "192.168.1.48", 40900, [fmt]).encode()

        async def on_invite(_invite):
            nonlocal calls
            calls += 1
            return sip_listener.SipInviteResult(486, "Busy Here", decline_reason="busy")

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            send_override=lambda data, _addr: sent.append(data),
        )
        invite = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKbusy"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Call-ID", "busy-replay-call"),
                ("CSeq", "7 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )

        await endpoint._handle_datagram(invite, ("192.168.1.48", 5060))
        await endpoint._handle_datagram(invite, ("192.168.1.48", 5090))

        self.assertEqual(calls, 1)
        self.assertEqual([sip.parse_message(raw).status_code for raw in sent[-2:]], [486, 486])
        self.assertIn("busy-replay-call", endpoint.completed_invites)
        self.assertNotIn("busy-replay-call", endpoint.pending_invites)
        endpoint.cancel_request_tasks()

    async def test_listener_retransmits_negative_invite_final_until_transaction_ack(self) -> None:
        sent: list[bytes] = []
        retransmitted = asyncio.Event()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        offer = sdp.build_offer("192.0.2.10", "192.0.2.10", 40900, [fmt]).encode()

        def capture(data: bytes, _addr: tuple[str, int]) -> None:
            sent.append(data)
            finals = [
                message
                for raw in sent
                if (message := sip.parse_message(raw)).status_code == 486
            ]
            if len(finals) >= 2:
                retransmitted.set()

        async def on_invite(_invite):
            return sip_listener.SipInviteResult(486, "Busy Here", decline_reason="busy")

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.0.2.20",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            send_override=capture,
        )
        invite = sip.build_request(
            "INVITE",
            "sip:Casa@192.0.2.20:5060",
            [
                ("Via", "SIP/2.0/UDP 192.0.2.10:5060;branch=z9hG4bKbusy-final"),
                ("From", "<sip:test@192.0.2.10>;tag=remote"),
                ("To", "<sip:Casa@192.0.2.20>"),
                ("Call-ID", "busy-timer-call"),
                ("CSeq", "7 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )
        try:
            with (
                patch.object(sip_listener, "_SIP_T1", 0.002),
                patch.object(sip_listener, "_SIP_T2", 0.004),
                patch.object(sip_listener, "_INVITE_NON2XX_TIMEOUT", 0.1),
            ):
                await endpoint._handle_datagram(invite, ("192.0.2.10", 5060))
                await asyncio.wait_for(retransmitted.wait(), timeout=0.2)
                completed = endpoint.completed_invites["busy-timer-call"]
                self.assertGreaterEqual(completed.final_retransmissions, 1)
                self.assertEqual(endpoint.snapshot()["pending_invite_error_acks"], 1)

                ack = sip.build_request(
                    "ACK",
                    "sip:Casa@192.0.2.20:5060",
                    [
                        (
                            "Via",
                            "SIP/2.0/UDP 192.0.2.10:5060;branch=z9hG4bKbusy-final",
                        ),
                        ("From", "<sip:test@192.0.2.10>;tag=remote"),
                        ("To", f"<sip:Casa@192.0.2.20>;tag={completed.to_tag}"),
                        ("Call-ID", "busy-timer-call"),
                        ("CSeq", "7 ACK"),
                    ],
                )
                # Packet source may move between SBC nodes; transaction
                # identity is the top Via branch/sent-by plus CSeq.
                await endpoint._handle_datagram(ack, ("192.0.2.99", 5060))
                count_after_ack = len(sent)
                await asyncio.sleep(0.012)

                self.assertEqual(len(sent), count_after_ack)
                self.assertNotIn("busy-timer-call", endpoint.completed_invites)
                self.assertEqual(endpoint.snapshot()["pending_invite_error_acks"], 0)
        finally:
            endpoint.cancel_request_tasks()

    async def test_listener_final_answer_wins_while_invite_policy_is_awaiting(self) -> None:
        sent: list[bytes] = []
        terminated: list[tuple[str, str]] = []
        started = asyncio.Event()
        release = asyncio.Event()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        rtp_fmt = sdp.audio_format_to_rtp(fmt, 96)
        offer = sdp.build_offer("192.168.1.48", "192.168.1.48", 40900, [fmt]).encode()
        answer = sdp.build_answer_directional("192.168.1.10", "192.168.1.10", 40000, rtp_fmt, rtp_fmt)

        async def on_invite(_invite):
            started.set()
            await release.wait()
            return sip_listener.SipInviteResult(180, "Ringing", defer_final=True)

        async def on_terminated(call_id: str, reason: str) -> None:
            terminated.append((call_id, reason))

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            on_terminated=on_terminated,
            send_override=lambda data, _addr: sent.append(data),
        )
        invite = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKfastanswer"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Call-ID", "fast-answer-call"),
                ("CSeq", "7 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )
        task = asyncio.create_task(endpoint._handle_datagram(invite, ("192.168.1.48", 5060)))
        await started.wait()
        self.assertTrue(endpoint.send_final_response("fast-answer-call", 200, "OK", answer_sdp=answer))
        release.set()
        await task

        self.assertFalse(terminated)
        self.assertIn("fast-answer-call", endpoint.active_dialogs)

    async def test_listener_cancel_wins_while_invite_policy_is_awaiting(self) -> None:
        sent: list[bytes] = []
        terminated: list[tuple[str, str]] = []
        started = asyncio.Event()
        release = asyncio.Event()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        offer = sdp.build_offer("192.168.1.48", "192.168.1.48", 40900, [fmt]).encode()

        async def on_invite(_invite):
            started.set()
            await release.wait()
            return sip_listener.SipInviteResult(180, "Ringing", defer_final=True)

        async def on_terminated(call_id: str, reason: str) -> None:
            terminated.append((call_id, reason))

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            on_terminated=on_terminated,
            send_override=lambda data, _addr: sent.append(data),
        )
        headers = [
            ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKcancelwait"),
            ("From", "<sip:test@192.168.1.48>;tag=remote"),
            ("To", "<sip:Casa@192.168.1.10>"),
            ("Call-ID", "cancel-wait-call"),
            ("CSeq", "9 INVITE"),
            ("Content-Type", "application/sdp"),
        ]
        invite = sip.build_request("INVITE", "sip:Casa@192.168.1.10:5060", headers, offer)
        addr = ("192.168.1.48", 5060)
        task = asyncio.create_task(endpoint._handle_datagram(invite, addr))
        await started.wait()
        cancel_headers = [(key, "9 CANCEL" if key == "CSeq" else value) for key, value in headers if key != "Content-Type"]
        cancel = sip.build_request("CANCEL", "sip:Casa@192.168.1.10:5060", cancel_headers, b"")
        await endpoint._handle_datagram(cancel, (addr[0], 5090))
        self.assertEqual([sip.parse_message(raw).status_code for raw in sent[-2:]], [200, 487])
        self.assertNotIn("cancel-wait-call", endpoint.pending_invites)

        release.set()
        await task
        self.assertGreaterEqual(terminated.count(("cancel-wait-call", "cancelled")), 1)
        self.assertNotIn("cancel-wait-call", endpoint.active_dialogs)

    async def test_listener_keeps_cancel_and_bye_transaction_scopes_separate(self) -> None:
        sent: list[bytes] = []
        terminated: list[tuple[str, str]] = []

        async def on_terminated(call_id: str, reason: str) -> None:
            terminated.append((call_id, reason))

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            on_terminated=on_terminated,
            send_override=lambda data, _addr: sent.append(data),
        )
        addr = ("192.168.1.48", 5060)

        def request(method: str, call_id: str, *, cseq: int = 2, branch: str = "z9hG4bKscope") -> bytes:
            return sip.build_request(
                method,
                "sip:Casa@192.168.1.10:5060",
                [
                    ("Via", f"SIP/2.0/UDP 192.168.1.48:5060;branch={branch}"),
                    ("From", "<sip:test@192.168.1.48>;tag=remote"),
                    ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                    ("Call-ID", call_id),
                    ("CSeq", f"{cseq} {method}"),
                ],
                b"",
            )

        invite = sip.parse_message(request("INVITE", "pending-call"))
        active_invite = sip.parse_message(request("INVITE", "active-call"))
        endpoint.pending_invites["pending-call"] = sip_listener._PendingInvite(invite, addr, "local", "UDP")
        endpoint.active_dialogs["active-call"] = sip_listener._ActiveDialog(active_invite, addr, "local", 3, "UDP")

        await endpoint._handle_datagram(request("CANCEL", "active-call"), addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 481)
        self.assertIn("active-call", endpoint.active_dialogs)

        await endpoint._handle_datagram(request("BYE", "pending-call"), addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 481)
        self.assertIn("pending-call", endpoint.pending_invites)

        await endpoint._handle_datagram(request("CANCEL", "pending-call"), ("192.168.1.99", 5060))
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 481)
        self.assertIn("pending-call", endpoint.pending_invites)

        await endpoint._handle_datagram(request("CANCEL", "pending-call", cseq=3), addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 481)
        self.assertIn("pending-call", endpoint.pending_invites)

        await endpoint._handle_datagram(request("CANCEL", "pending-call", branch="z9hG4bKother"), addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 481)
        self.assertIn("pending-call", endpoint.pending_invites)

        translated_addr = (addr[0], 5090)
        await endpoint._handle_datagram(request("CANCEL", "pending-call"), translated_addr)
        self.assertEqual([sip.parse_message(raw).status_code for raw in sent[-2:]], [200, 487])
        self.assertNotIn("pending-call", endpoint.pending_invites)
        await endpoint._handle_datagram(request("CANCEL", "pending-call"), translated_addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)

        await endpoint._handle_datagram(request("BYE", "active-call", cseq=3), translated_addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertNotIn("active-call", endpoint.active_dialogs)
        await endpoint._handle_datagram(request("BYE", "active-call", cseq=3), translated_addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertEqual(terminated, [("pending-call", "cancelled"), ("active-call", "remote_hangup")])

    async def test_listener_accepts_in_dialog_reinvite_without_restarting_route(self) -> None:
        sent: list[bytes] = []
        applied_ports: list[int] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        original_offer = sdp.build_offer(
            "192.168.1.48", "192.168.1.48", 40000, [fmt]
        ).encode()
        negotiated = sdp.audio_format_to_rtp(fmt, 96)
        initial_answer = sdp.rewrite_sdp_origin(
            sdp.build_answer_directional(
                "192.168.1.10",
                "192.168.1.10",
                42000,
                negotiated,
                negotiated,
                remote_sdp=original_offer,
            ),
            5151,
            0,
        )
        async def on_media_update(_previous, updated, _method):
            answer = sdp.build_answer_directional(
                "192.168.1.10",
                "192.168.1.10",
                42000,
                updated.send_format,
                updated.recv_format,
                remote_sdp=updated.remote_sdp,
            )

            async def commit() -> None:
                applied_ports.append(updated.remote_rtp_port)

            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer, commit=commit)

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            on_media_update=on_media_update,
            send_override=lambda data, _addr: sent.append(data),
        )
        addr = ("192.168.1.48", 5060)
        original = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:Casa@192.168.1.10",
                [
                    ("Via", "SIP/2.0/UDP 192.168.1.48;branch=z9hG4bKinitial"),
                    ("From", "<sip:test@192.168.1.48>;tag=remote"),
                    ("To", "<sip:Casa@192.168.1.10>"),
                    ("Call-ID", "reinvite-call"),
                    ("CSeq", "1 INVITE"),
                    ("Content-Type", "application/sdp"),
                ],
                original_offer,
            )
        )
        endpoint.active_dialogs["reinvite-call"] = sip_listener._ActiveDialog(
            original,
            addr,
            "local",
            2,
            "UDP",
            answer_sdp=initial_answer,
            local_sdp_session_id=5151,
        )
        reinvite = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48;branch=z9hG4bKrefresh"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                ("Call-ID", "reinvite-call"),
                ("CSeq", "2 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            original_offer,
        )
        await endpoint._handle_datagram(reinvite, addr)
        response = sip.parse_message(sent[-1])
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"m=audio 42000", response.body)
        self.assertIn(b"o=- 5151 0 IN IP4 192.168.1.10", response.body)
        self.assertIn("reinvite-call", endpoint.active_dialogs)
        self.assertEqual(applied_ports, [40000])

        compatible_media_change = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48;branch=z9hG4bKhold"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                ("Call-ID", "reinvite-call"),
                ("CSeq", "3 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            sdp.build_offer_directional(
                "192.168.1.48",
                "192.168.1.48",
                45000,
                [fmt],
                [fmt],
                audio_direction="sendonly",
            ).encode(),
        )
        await endpoint._handle_datagram(compatible_media_change, addr)
        hold_response = sip.parse_message(sent[-1])
        self.assertEqual(hold_response.status_code, 200)
        self.assertIn(b"a=recvonly", hold_response.body)
        self.assertIn(b"o=- 5151 1 IN IP4 192.168.1.10", hold_response.body)
        self.assertEqual(
            endpoint.active_dialogs["reinvite-call"].request.body,
            original_offer,
        )
        self.assertEqual(endpoint.active_dialogs["reinvite-call"].invite.remote_rtp_port, 45000)
        self.assertEqual(applied_ports, [40000, 45000])

        incompatible_media_change = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48;branch=z9hG4bKincompatible"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                ("Call-ID", "reinvite-call"),
                ("CSeq", "4 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            sdp.build_offer(
                "192.168.1.48",
                "192.168.1.48",
                45002,
                [audio_format.AudioFormat(8000, "s16le", 1, 20)],
            ).encode(),
        )
        await endpoint._handle_datagram(incompatible_media_change, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 488)
        self.assertIn("reinvite-call", endpoint.active_dialogs)
        self.assertEqual(applied_ports, [40000, 45000])

    async def test_listener_bye_invalidates_pending_reinvite_before_media_commit(self) -> None:
        sent: list[bytes] = []
        commits: list[int] = []
        rollbacks: list[int] = []
        started = asyncio.Event()
        release = asyncio.Event()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        negotiated = sdp.audio_format_to_rtp(fmt, 96)
        original_offer = sdp.build_offer("192.0.2.10", "192.0.2.10", 40000, [fmt]).encode()
        updated_offer = sdp.build_offer("192.0.2.10", "192.0.2.10", 41000, [fmt]).encode()
        answer = sdp.build_answer_directional(
            "192.0.2.20",
            "192.0.2.20",
            42000,
            negotiated,
            negotiated,
        )

        async def on_media_update(_previous, updated, _method):
            started.set()
            await release.wait()

            async def commit() -> None:
                commits.append(updated.remote_rtp_port)

            async def rollback() -> None:
                rollbacks.append(updated.remote_rtp_port)

            return sip_listener.SipInviteResult(
                200,
                "OK",
                answer_sdp=answer,
                commit=commit,
                rollback=rollback,
            )

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.0.2.20",
            local_rtp_port=42000,
            supported_formats=[fmt],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            on_media_update=on_media_update,
            send_override=lambda data, _addr: sent.append(data),
        )
        addr = ("192.0.2.10", 5060)
        initial = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:b@192.0.2.20",
                [
                    ("Via", "SIP/2.0/UDP 192.0.2.10;branch=z9hG4bKinitial"),
                    ("From", "<sip:a@192.0.2.10>;tag=remote"),
                    ("To", "<sip:b@192.0.2.20>"),
                    ("Contact", "<sip:a@192.0.2.10>"),
                    ("Call-ID", "bye-during-reinvite"),
                    ("CSeq", "1 INVITE"),
                    ("Content-Type", "application/sdp"),
                ],
                original_offer,
            )
        )
        endpoint.active_dialogs["bye-during-reinvite"] = sip_listener._ActiveDialog(
            initial,
            addr,
            "local",
            2,
            "UDP",
            answer_sdp=answer,
            invite=endpoint._parse_invite(initial, addr),
        )
        reinvite = sip.build_request(
            "INVITE",
            "sip:b@192.0.2.20",
            [
                ("Via", "SIP/2.0/UDP 192.0.2.10;branch=z9hG4bKreinvite"),
                ("From", "<sip:a@192.0.2.10>;tag=remote"),
                ("To", "<sip:b@192.0.2.20>;tag=local"),
                ("Call-ID", "bye-during-reinvite"),
                ("CSeq", "2 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            updated_offer,
        )
        bye = sip.build_request(
            "BYE",
            "sip:b@192.0.2.20",
            [
                ("Via", "SIP/2.0/UDP 192.0.2.10;branch=z9hG4bKbye"),
                ("From", "<sip:a@192.0.2.10>;tag=remote"),
                ("To", "<sip:b@192.0.2.20>;tag=local"),
                ("Call-ID", "bye-during-reinvite"),
                ("CSeq", "3 BYE"),
            ],
        )

        reinvite_task = asyncio.create_task(endpoint._handle_datagram(reinvite, addr))
        await started.wait()
        await endpoint._handle_datagram(bye, addr)
        release.set()
        await reinvite_task

        responses = [sip.parse_message(raw) for raw in sent]
        self.assertEqual(
            [(item.status_code, item.header("CSeq")) for item in responses[-2:]],
            [(200, "3 BYE"), (487, "2 INVITE")],
        )
        self.assertEqual(commits, [])
        self.assertEqual(rollbacks, [41000])
        self.assertNotIn("bye-during-reinvite", endpoint.active_dialogs)

        await endpoint._handle_datagram(reinvite, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 487)
        self.assertEqual(commits, [])
        self.assertEqual(rollbacks, [41000])

    async def test_listener_update_requires_dialog_and_commits_once(self) -> None:
        sent: list[bytes] = []
        routed: list[str] = []
        commits: list[int] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        offer = sdp.build_offer("192.0.2.10", "192.0.2.10", 40000, [fmt]).encode()

        async def on_invite(invite):
            routed.append(invite.call_id)
            return sip_listener.SipInviteResult(488, "Not Acceptable Here")

        async def on_media_update(_previous, updated, method):
            self.assertEqual(method, "UPDATE")
            answer = sdp.build_answer_directional(
                "192.0.2.20",
                "192.0.2.20",
                42000,
                updated.send_format,
                updated.recv_format,
                remote_sdp=updated.remote_sdp,
            )

            async def commit() -> None:
                commits.append(updated.remote_rtp_port)

            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer, commit=commit)

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.0.2.20",
            local_rtp_port=42000,
            supported_formats=[fmt],
            on_invite=on_invite,
            on_media_update=on_media_update,
            send_override=lambda data, _addr: sent.append(data),
        )
        addr = ("192.0.2.10", 5060)

        def update(call_id: str, *, cseq: int, branch: str, body: bytes = offer) -> bytes:
            headers = [
                ("Via", f"SIP/2.0/UDP 192.0.2.10;branch={branch}"),
                ("From", "<sip:a@192.0.2.10>;tag=remote"),
                ("To", "<sip:b@192.0.2.20>;tag=local"),
                ("Call-ID", call_id),
                ("CSeq", f"{cseq} UPDATE"),
            ]
            if body:
                headers.append(("Content-Type", "application/sdp"))
            return sip.build_request("UPDATE", "sip:b@192.0.2.20", headers, body)

        await endpoint._handle_datagram(update("unknown", cseq=2, branch="z9hG4bKunknown"), addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 481)
        self.assertEqual(routed, [])

        initial = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:b@192.0.2.20",
                [
                    ("Via", "SIP/2.0/UDP 192.0.2.10;branch=z9hG4bKinitial"),
                    ("From", "<sip:a@192.0.2.10>;tag=remote"),
                    ("To", "<sip:b@192.0.2.20>"),
                    ("Call-ID", "active"),
                    ("CSeq", "1 INVITE"),
                    ("Content-Type", "application/sdp"),
                ],
                offer,
            )
        )
        endpoint.active_dialogs["active"] = sip_listener._ActiveDialog(
            initial,
            addr,
            "local",
            2,
            "UDP",
            answer_sdp="v=0\r\n",
        )
        request = update("active", cseq=2, branch="z9hG4bKupdate")
        await endpoint._handle_datagram(request, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertEqual(commits, [40000])
        await endpoint._handle_datagram(request, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertEqual(commits, [40000])

        stale_bye = sip.build_request(
            "BYE",
            "sip:b@192.0.2.20",
            [
                ("Via", "SIP/2.0/UDP 192.0.2.10;branch=z9hG4bKstale-bye"),
                ("From", "<sip:a@192.0.2.10>;tag=remote"),
                ("To", "<sip:b@192.0.2.20>;tag=local"),
                ("Call-ID", "active"),
                ("CSeq", "2 BYE"),
            ],
        )
        await endpoint._handle_datagram(stale_bye, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 481)
        self.assertIn("active", endpoint.active_dialogs)

        refresh = update("active", cseq=3, branch="z9hG4bKrefresh", body=b"")
        await endpoint._handle_datagram(refresh, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertEqual(commits, [40000])

        # A delayed UDP duplicate remains part of its original transaction
        # even after a newer in-dialog request has completed.
        await endpoint._handle_datagram(request, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertEqual(commits, [40000])

    async def test_listener_rejects_in_dialog_video_transport_change(self) -> None:
        sent: list[bytes] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)

        def offer(video_port: int) -> bytes:
            return sdp.build_offer_directional(
                "192.168.1.48",
                "192.168.1.48",
                40000,
                [fmt],
                [fmt],
                video_port=video_port,
                video_format=sdp.DEFAULT_H264_FORMAT,
            ).encode()

        def request(body: bytes, *, cseq: int, branch: str) -> bytes:
            return sip.build_request(
                "INVITE",
                "sip:Casa@192.168.1.10",
                [
                    ("Via", f"SIP/2.0/UDP 192.168.1.48;branch={branch}"),
                    ("From", "<sip:test@192.168.1.48>;tag=remote"),
                    ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                    ("Call-ID", "video-reinvite-call"),
                    ("CSeq", f"{cseq} INVITE"),
                    ("Content-Type", "application/sdp"),
                ],
                body,
            )

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=lambda data, _addr: sent.append(data),
            enable_video=True,
        )
        addr = ("192.168.1.48", 5060)
        original = sip.parse_message(
            request(offer(41002), cseq=1, branch="z9hG4bKvideo-initial")
        )
        endpoint.active_dialogs["video-reinvite-call"] = sip_listener._ActiveDialog(
            original,
            addr,
            "local",
            2,
            "UDP",
            answer_sdp="v=0\r\n",
        )

        changed = request(offer(41004), cseq=2, branch="z9hG4bKvideo-change")
        await endpoint._handle_datagram(changed, addr)

        self.assertEqual(sip.parse_message(sent[-1]).status_code, 488)
        self.assertEqual(
            endpoint.active_dialogs["video-reinvite-call"].request.body,
            original.body,
        )

    async def test_listener_delivers_in_dialog_sip_info_dtmf(self) -> None:
        sent: list[bytes] = []
        received: list[str] = []

        async def on_info(request, _addr, _transport) -> None:
            received.append(dtmf.parse_sip_info_digit(request.header("Content-Type"), request.body))

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            on_info=on_info,
            send_override=lambda data, _addr: sent.append(data),
        )
        addr = ("192.168.1.48", 5060)
        original = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:Casa@192.168.1.10",
                [
                    ("Via", "SIP/2.0/UDP 192.168.1.48;branch=z9hG4bKinitial"),
                    ("From", "<sip:test@192.168.1.48>;tag=remote"),
                    ("To", "<sip:Casa@192.168.1.10>"),
                    ("Call-ID", "info-call"),
                    ("CSeq", "1 INVITE"),
                ],
            )
        )
        endpoint.active_dialogs["info-call"] = sip_listener._ActiveDialog(original, addr, "local", 2, "UDP")
        info = sip.build_request(
            "INFO",
            "sip:Casa@192.168.1.10",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48;branch=z9hG4bKinfo"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                ("Call-ID", "info-call"),
                ("CSeq", "2 INFO"),
                ("Content-Type", "application/dtmf-relay"),
            ],
            b"Signal=6\r\nDuration=160\r\n",
        )
        await endpoint._handle_datagram(info, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertEqual(received, ["6"])
        await endpoint._handle_datagram(info, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertEqual(received, ["6"])

    def test_listener_bye_uses_contact_as_target_and_from_as_identity(self) -> None:
        sent: list[bytes] = []
        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=lambda data, _addr: sent.append(data),
        )
        request = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:Casa@192.168.1.10:5060",
                [
                    ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKtarget"),
                    ("From", '"Desk" <sip:desk@192.168.1.48>;tag=remote'),
                    ("To", "<sip:Casa@192.168.1.10>"),
                    ("Contact", '"Desk phone" <sip:dialog@192.168.1.48:5090;transport=udp>'),
                    ("Call-ID", "remote-target-call"),
                    ("CSeq", "4 INVITE"),
                ],
                b"",
            )
        )
        endpoint.active_dialogs["remote-target-call"] = sip_listener._ActiveDialog(
            request,
            ("192.168.1.48", 5060),
            "local",
            5,
            "UDP",
        )

        self.assertTrue(endpoint.send_bye("remote-target-call"))
        bye = sip.parse_message(sent[0])
        self.assertEqual(bye.uri, "sip:dialog@192.168.1.48:5090;transport=udp")
        self.assertEqual(bye.header("To"), "<sip:desk@192.168.1.48>;tag=remote")

    async def test_listener_echoes_and_uses_incoming_record_route_set(self) -> None:
        sent: list[tuple[bytes, tuple[str, int]]] = []
        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)

        async def on_invite(invite) -> sip_listener.SipInviteResult:
            answer = sdp.build_answer_directional(
                "192.0.2.10",
                "192.0.2.10",
                41000,
                invite.send_format,
                invite.recv_format,
                remote_sdp=invite.remote_sdp,
            )
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer)

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.0.2.10",
            local_rtp_port=41000,
            supported_formats=[pcm],
            on_invite=on_invite,
            send_override=lambda data, addr: sent.append((data, addr)),
        )
        route_field = (
            "<sip:edge@192.0.2.11:5080;lr>, "
            '"Core, proxy" <sip:core@192.0.2.12:5070;lr>'
        )
        offer = sdp.build_offer(
            "192.0.2.20",
            "192.0.2.20",
            42000,
            [pcm],
        ).encode()
        invite = sip.build_request(
            "INVITE",
            "sip:HA@192.0.2.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.0.2.20:5060;branch=z9hG4bKrouted;rport"),
                ("From", "<sip:desk@192.0.2.20>;tag=remote"),
                ("To", "<sip:HA@192.0.2.10>"),
                ("Contact", "<sip:dialog@192.0.2.20:5090>"),
                ("Record-Route", route_field),
                ("Call-ID", "routed-listener-call"),
                ("CSeq", "1 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )

        await endpoint._handle_datagram(invite, ("192.0.2.20", 5060))

        dialog = endpoint.active_dialogs["routed-listener-call"]
        self.assertEqual(
            dialog.route_set,
            (
                "<sip:edge@192.0.2.11:5080;lr>",
                '"Core, proxy" <sip:core@192.0.2.12:5070;lr>',
            ),
        )
        ok = next(
            sip.parse_message(raw)
            for raw, _addr in sent
            if sip.parse_message(raw).status_code == 200
        )
        self.assertEqual(ok.header_values("Record-Route"), [route_field])

        self.assertTrue(endpoint.send_bye("routed-listener-call"))
        bye_raw, bye_addr = sent[-1]
        bye = sip.parse_message(bye_raw)
        self.assertEqual(bye.uri, "sip:dialog@192.0.2.20:5090")
        self.assertEqual(
            bye.header_values("Route"),
            [
                "<sip:edge@192.0.2.11:5080;lr>",
                '"Core, proxy" <sip:core@192.0.2.12:5070;lr>',
            ],
        )
        self.assertEqual(bye_addr, ("192.0.2.11", 5080))

    async def test_listener_offerless_update_refreshes_remote_target(self) -> None:
        sent: list[tuple[bytes, tuple[str, int]]] = []
        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=lambda data, addr: sent.append((data, addr)),
        )
        original = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:Casa@192.168.1.10:5060",
                [
                    ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKtarget-old"),
                    ("From", "<sip:desk@192.168.1.48>;tag=remote"),
                    ("To", "<sip:Casa@192.168.1.10>"),
                    ("Contact", "<sip:desk@192.168.1.48:5060>"),
                    ("Call-ID", "target-refresh-listener"),
                    ("CSeq", "4 INVITE"),
                ],
            )
        )
        endpoint.active_dialogs["target-refresh-listener"] = sip_listener._ActiveDialog(
            original,
            ("192.168.1.48", 5060),
            "local",
            5,
            "UDP",
            remote_target_uri="sip:desk@192.168.1.48:5060",
        )
        update = sip.build_request(
            "UPDATE",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKtarget-new"),
                ("From", "<sip:desk@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                ("Contact", "<sip:desk@192.168.1.48:5090;transport=udp>"),
                ("Call-ID", "target-refresh-listener"),
                ("CSeq", "5 UPDATE"),
            ],
        )

        await endpoint._handle_datagram(update, ("192.168.1.48", 5060))
        self.assertEqual(sip.parse_message(sent[-1][0]).status_code, 200)
        self.assertTrue(endpoint.send_bye("target-refresh-listener"))
        bye, target = sent[-1]
        self.assertEqual(
            sip.parse_message(bye).uri,
            "sip:desk@192.168.1.48:5090;transport=udp",
        )
        self.assertEqual(target, ("192.168.1.48", 5090))

    async def test_listener_bounds_and_expires_deferred_invites(self) -> None:
        sent: list[sip.SipMessage] = []
        terminated: list[tuple[str, str]] = []
        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)

        async def on_invite(_invite) -> sip_listener.SipInviteResult:
            return sip_listener.SipInviteResult(
                180,
                "Ringing",
                defer_final=True,
            )

        async def on_terminated(call_id: str, reason: str) -> None:
            terminated.append((call_id, reason))

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[pcm],
            on_invite=on_invite,
            on_terminated=on_terminated,
            send_override=lambda data, _addr: sent.append(sip.parse_message(data)),
            max_pending_invites=1,
            deferred_invite_timeout=0.05,
        )
        body = sdp.build_offer(
            "192.168.1.48",
            "192.168.1.48",
            41000,
            [pcm],
        ).encode()

        def invite(call_id: str, cseq: int) -> bytes:
            return sip.build_request(
                "INVITE",
                "sip:Casa@192.168.1.10:5060",
                [
                    ("Via", f"SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bK{call_id}"),
                    ("From", "<sip:desk@192.168.1.48>;tag=remote"),
                    ("To", "<sip:Casa@192.168.1.10>"),
                    ("Contact", "<sip:desk@192.168.1.48:5060>"),
                    ("Call-ID", call_id),
                    ("CSeq", f"{cseq} INVITE"),
                    ("Content-Type", "application/sdp"),
                ],
                body,
            )

        await endpoint._handle_datagram(
            invite("deferred-one", 1),
            ("192.168.1.48", 5060),
        )
        self.assertIn("deferred-one", endpoint.pending_invites)
        await endpoint._handle_datagram(
            invite("deferred-two", 2),
            ("192.168.1.48", 5060),
        )
        self.assertEqual(sent[-1].status_code, 503)
        self.assertEqual(sent[-1].header("Retry-After"), "1")

        await asyncio.sleep(0.08)
        self.assertNotIn("deferred-one", endpoint.pending_invites)
        self.assertEqual(sent[-1].status_code, 480)
        self.assertEqual(terminated, [("deferred-one", "no_answer")])
        endpoint.cancel_request_tasks()

    async def test_listener_rejects_invite_sdp_with_wrong_content_type(self) -> None:
        sent: list[sip.SipMessage] = []
        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[pcm],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=lambda data, _addr: sent.append(sip.parse_message(data)),
        )
        body = sdp.build_offer(
            "192.168.1.48",
            "192.168.1.48",
            41000,
            [pcm],
        ).encode()
        request = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKwrong-type"),
                ("From", "<sip:desk@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Contact", "<sip:desk@192.168.1.48:5060>"),
                ("Call-ID", "wrong-content-type"),
                ("CSeq", "1 INVITE"),
                ("Content-Type", "application/octet-stream"),
            ],
            body,
        )

        await endpoint._handle_datagram(request, ("192.168.1.48", 5060))

        self.assertEqual(sent[-1].status_code, 415)
        self.assertEqual(sent[-1].header("Accept"), "application/sdp")

    async def test_call_client_offerless_update_refreshes_remote_target(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        pcm = sdp.RtpPcmFormat(96, "L16", 16000, 1, 32)
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client.dialog_ids.remote_tag = "remote"
        client.dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="127.0.0.2",
            remote_sip_port=5060,
            remote_rtp_host="127.0.0.2",
            remote_rtp_port=42000,
            local_rtp_port=41000,
            call_id=client.dialog_ids.call_id,
            local_uri="sip:HA@127.0.0.1:5060",
            remote_uri="sip:ESP@127.0.0.2:5060",
            send_format=pcm,
            recv_format=pcm,
            remote_target_uri="sip:ESP@127.0.0.2:5060",
        )
        update = sip.parse_message(
            sip.build_request(
                "UPDATE",
                "sip:HA@127.0.0.1:5060",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bKrefresh"),
                    ("From", "<sip:ESP@127.0.0.2>;tag=remote"),
                    ("To", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                    ("Contact", "<sip:ESP@127.0.0.2:5090;transport=udp>"),
                    ("Call-ID", client.dialog_ids.call_id),
                    ("CSeq", "2 UPDATE"),
                ],
            )
        )

        self.assertIsNone(
            await client._handle_dialog_media_request(update, "127.0.0.2", 5060)
        )
        assert client.dialog is not None
        self.assertEqual(
            client.dialog.remote_target_uri,
            "sip:ESP@127.0.0.2:5090;transport=udp",
        )
        self.assertTrue(client.bye())
        bye, target = transport.sent[-1]
        self.assertEqual(
            sip.parse_message(bye).uri,
            "sip:ESP@127.0.0.2:5090;transport=udp",
        )
        self.assertEqual(target, ("127.0.0.2", 5090))

    def test_decline_reason_header_overrides_generic_status(self) -> None:
        msg = sip.SipMessage(
            status_code=486,
            reason="Busy Here",
            headers=(
                ("Reason", 'X-Voip-Stack;cause=486;text="DND"'),
                ("X-Voip-Stack-Decline-Reason", "DND"),
            ),
        )
        self.assertEqual(sip_client._sip_decline_reason(msg), "DND")

    def test_roster_target_matching_ignores_spaces_and_underscores(self) -> None:
        entries = [
            roster.RosterEntry(
                id="Spotpear Ball v2",
                name="Spotpear Ball v2",
                address="192.168.1.31",
                metadata={"sip_port": 5060},
            ),
            roster.RosterEntry(
                id="Casa",
                name="Casa",
                address="192.168.1.10",
                metadata={"sip_port": 5060},
            ),
        ]
        decision = router.resolve_esp_origin("Spotpear_Ball_v2", entries, "sip:Spotpear_Ball_v2@192.168.1.10:5060")
        self.assertEqual(decision.action, router.RouteAction.DIRECT)
        self.assertIsNotNone(decision.entry)
        assert decision.entry is not None
        self.assertEqual(decision.entry.address, "192.168.1.31")

    def test_esp_roster_entry_with_address_is_direct_even_without_transport_param(self) -> None:
        entries = [
            roster.RosterEntry(
                id="Casa",
                name="Casa",
                address="192.168.1.10",
                metadata={"sip_port": 5060, "sip_transport": "tcp"},
            ),
            roster.RosterEntry(
                id="Cucina",
                name="Cucina",
                address="192.168.1.31",
                metadata={"sip_port": 5060},
            ),
        ]
        decision = router.resolve_esp_origin("Cucina", entries, "sip:Cucina@192.168.1.10:5060;transport=tcp")
        self.assertEqual(decision.action, router.RouteAction.DIRECT)
        self.assertEqual(decision.sip_uri, "sip:Cucina@192.168.1.31")


class SdpPcmProfileTest(unittest.TestCase):
    def test_dahua_pcm_is_profile_gated_and_preserves_little_endian_samples(
        self,
    ) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.0.2.10\r\n"
            "s=Dahua\r\n"
            "c=IN IP4 192.0.2.10\r\n"
            "t=0 0\r\n"
            "m=audio 20000 RTP/AVP 97 0\r\n"
            "a=rtpmap:97 PCM/16000\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )
        local = [audio_format.AudioFormat(16000, "s16le", 1, 20)]

        self.assertIsNone(sdp.negotiate_directional(offer, local, local))
        selected = sdp.negotiate_directional(
            offer,
            local,
            local,
            allow_dahua_pcm=True,
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send.encoding, "PCM")
        self.assertEqual(selected.send.audio_format, local[0])
        samples = b"\xf7\xff\x0c\x00\x0e\x00\xf3\xff"
        self.assertEqual(sip_client.pcm_to_rtp_payload(samples, selected.send), samples)
        self.assertEqual(sip_client.rtp_payload_to_pcm(samples, selected.recv), samples)

    def test_dahua_pcm_offer_is_explicit_and_does_not_replace_standard_l16(
        self,
    ) -> None:
        local = [audio_format.AudioFormat(16000, "s16le", 1, 20)]

        standard = sdp.build_offer_directional(
            "192.0.2.20",
            "192.0.2.20",
            40000,
            local,
            local,
        )
        dahua = sdp.build_offer_directional(
            "192.0.2.20",
            "192.0.2.20",
            40000,
            local,
            local,
            include_dahua_pcm=True,
        )

        self.assertNotIn(" PCM/16000", standard)
        self.assertIn(" L16/16000/1", standard)
        self.assertIn(" PCM/16000/1", dahua)
        self.assertIn(" L16/16000/1", dahua)
        self.assertEqual(
            [
                item.encoding
                for item in sdp.offered_pcm_formats(
                    dahua,
                    allow_dahua_pcm=True,
                )
            ],
            ["PCM", "L16"],
        )

    def test_dahua_pcm_rejects_wrong_rate_channels_and_static_payload(self) -> None:
        template = (
            "v=0\r\n"
            "c=IN IP4 192.0.2.10\r\n"
            "t=0 0\r\n"
            "m=audio 20000 RTP/AVP {payload}\r\n"
            "a=rtpmap:{payload} PCM/{rate}/{channels}\r\n"
            "a=ptime:20\r\n"
        )
        for payload, rate, channels in (
            (97, 8000, 1),
            (97, 16000, 2),
            (0, 16000, 1),
        ):
            with self.subTest(payload=payload, rate=rate, channels=channels):
                self.assertEqual(
                    sdp.offered_pcm_formats(
                        template.format(
                            payload=payload,
                            rate=rate,
                            channels=channels,
                        ),
                        allow_dahua_pcm=True,
                    ),
                    [],
                )

    def test_dahua_user_agent_detection_is_narrow(self) -> None:
        self.assertTrue(
            codec_capabilities.supports_dahua_pcm("Dahua UAC/3.0")
        )
        self.assertTrue(
            codec_capabilities.supports_dahua_pcm("  dahua uac/4.1 ")
        )
        self.assertFalse(codec_capabilities.supports_dahua_pcm("baresip v4.6.0"))
        self.assertFalse(codec_capabilities.supports_dahua_pcm("Dahua Camera/1.0"))

    def test_listener_accepts_dahua_pcm_only_for_the_dahua_uac_profile(self) -> None:
        local = audio_format.AudioFormat(16000, "s16le", 1, 20)
        body = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.0.2.85\r\n"
            "s=Dahua\r\n"
            "c=IN IP4 192.0.2.85\r\n"
            "t=0 0\r\n"
            "m=audio 20000 RTP/AVP 97\r\n"
            "a=rtpmap:97 PCM/16000\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        ).encode()

        def request(user_agent: str) -> sip.SipMessage:
            return sip.parse_message(
                sip.build_request(
                    "INVITE",
                    "sip:HA@192.0.2.10",
                    [
                        ("Via", "SIP/2.0/UDP 192.0.2.85:5060;branch=z9hG4bKdahua"),
                        ("From", "<sip:100@VDP>;tag=dahua"),
                        ("To", "<sip:HA@192.0.2.10>"),
                        ("Call-ID", f"dahua-{user_agent}"),
                        ("CSeq", "1 INVITE"),
                        ("Contact", "<sip:100@192.0.2.85:5060>"),
                        ("User-Agent", user_agent),
                        ("Content-Type", "application/sdp"),
                    ],
                    body,
                )
            )

        async def on_invite(_invite):
            return sip_listener.SipInviteResult(486, "Busy Here")

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.0.2.10",
            local_sip_port=5060,
            local_rtp_port=40000,
            supported_formats=[local],
            on_invite=on_invite,
        )

        accepted = endpoint._parse_invite(
            request("Dahua UAC/3.0"),
            ("192.0.2.85", 5060),
        )
        rejected = endpoint._parse_invite(
            request("Generic SIP Phone/1.0"),
            ("192.0.2.85", 5060),
        )

        self.assertIsNotNone(accepted)
        assert accepted is not None
        self.assertEqual(accepted.peer_profile, "dahua")
        self.assertEqual(accepted.send_format.encoding, "PCM")
        self.assertIsNone(rejected)

    def test_answer_cannot_remap_a_selected_dynamic_payload_type(self) -> None:
        offer = (
            "v=0\r\no=- 1 1 IN IP4 192.0.2.10\r\n"
            "s=offer\r\nc=IN IP4 192.0.2.10\r\nt=0 0\r\n"
            "m=audio 40000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=sendrecv\r\n"
        )
        answer = (
            "v=0\r\no=- 2 1 IN IP4 192.0.2.20\r\n"
            "s=answer\r\nc=IN IP4 192.0.2.20\r\nt=0 0\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/48000/1\r\na=sendrecv\r\n"
        )

        with self.assertRaisesRegex(sdp.SdpError, "remapped payload type 96"):
            sdp.validate_sdp_answer(offer, answer)

    def test_answer_may_use_a_different_payload_for_the_same_audio_codec(self) -> None:
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        offer = sdp.build_offer_directional(
            "192.0.2.10",
            "192.0.2.10",
            40000,
            [audio],
            [audio],
        )
        offered = sdp.offered_pcm_formats(offer)[0]
        answer = (
            "v=0\r\no=- 2 1 IN IP4 192.0.2.20\r\n"
            "s=answer\r\nc=IN IP4 192.0.2.20\r\nt=0 0\r\n"
            "m=audio 41000 RTP/AVP 120 121\r\n"
            "a=rtpmap:120 L16/16000/1\r\n"
            "a=rtpmap:121 telephone-event/8000\r\n"
            "a=fmtp:121 0-16\r\na=sendrecv\r\n"
        )

        sdp.validate_sdp_answer(offer, answer)
        selected = sdp.negotiate_answer_directional(
            answer,
            [audio],
            [audio],
            local_offer_sdp=offer,
        )

        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send.payload_type, 120)
        self.assertEqual(selected.recv.payload_type, offered.payload_type)
        offered_dtmf = sdp.offered_dtmf_formats(offer)[0]
        selected_dtmf = sdp.negotiate_dtmf_answer(answer, offer)
        self.assertIsNotNone(selected_dtmf)
        assert selected_dtmf is not None
        self.assertEqual(selected_dtmf.payload_type, offered_dtmf.payload_type)
        self.assertEqual(selected_dtmf.events, frozenset(range(16)))

    def test_dtmf_negotiation_restricts_events_to_remote_fmtp(self) -> None:
        audio = audio_format.AudioFormat(8000, "s16le", 1, 20)
        offer = sdp.build_offer("192.0.2.10", "192.0.2.10", 40000, [audio])
        answer = (
            "v=0\r\no=- 2 1 IN IP4 192.0.2.20\r\n"
            "s=answer\r\nc=IN IP4 192.0.2.20\r\nt=0 0\r\n"
            "m=audio 41000 RTP/AVP 96 97\r\n"
            "a=rtpmap:96 L16/8000/1\r\n"
            "a=rtpmap:97 telephone-event/8000\r\n"
            "a=fmtp:97 1,3-4\r\na=sendrecv\r\n"
        )
        negotiated = sdp.negotiate_dtmf_answer(answer, offer)
        self.assertIsNotNone(negotiated)
        assert negotiated is not None
        self.assertEqual(negotiated.events, frozenset({1, 3, 4}))

    def test_answer_cannot_add_rtcp_mux_or_feedback_capabilities(self) -> None:
        offer = (
            "v=0\r\no=- 1 1 IN IP4 192.0.2.10\r\n"
            "s=offer\r\nc=IN IP4 192.0.2.10\r\nt=0 0\r\n"
            "m=video 40002 RTP/AVPF 103\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=fmtp:103 packetization-mode=1;profile-level-id=42801f\r\n"
            "a=rtcp-fb:103 nack pli\r\na=sendrecv\r\n"
        )
        base_answer = offer.replace("192.0.2.10", "192.0.2.20").replace(
            "m=video 40002", "m=video 41002"
        )

        with self.subTest("rtcp-mux"), self.assertRaisesRegex(
            sdp.SdpError, "unoffered rtcp-mux"
        ):
            sdp.validate_sdp_answer(
                offer,
                base_answer.replace("a=sendrecv", "a=rtcp-mux\r\na=sendrecv"),
            )
        with self.subTest("rtcp-feedback"), self.assertRaisesRegex(
            sdp.SdpError, "unoffered RTCP feedback"
        ):
            sdp.validate_sdp_answer(
                offer,
                base_answer.replace(
                    "a=rtcp-fb:103 nack pli",
                    "a=rtcp-fb:103 nack pli\r\na=rtcp-fb:103 ccm fir",
                ),
            )

    def test_answer_feedback_can_use_an_offered_wildcard(self) -> None:
        offer = (
            "v=0\r\no=- 1 1 IN IP4 192.0.2.10\r\n"
            "s=offer\r\nc=IN IP4 192.0.2.10\r\nt=0 0\r\n"
            "m=video 40002 RTP/AVPF 103\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=rtcp-fb:* nack pli\r\na=sendrecv\r\n"
        )
        answer = offer.replace("192.0.2.10", "192.0.2.20").replace(
            "m=video 40002", "m=video 41002"
        ).replace("a=rtcp-fb:* nack pli", "a=rtcp-fb:103 nack pli")

        sdp.validate_sdp_answer(offer, answer)

    def test_avp_answer_feedback_is_ignored_as_inapplicable(self) -> None:
        offer = (
            "v=0\r\no=- 1 1 IN IP4 192.0.2.10\r\n"
            "s=offer\r\nc=IN IP4 192.0.2.10\r\nt=0 0\r\n"
            "m=video 40002 RTP/AVP 103\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=fmtp:103 packetization-mode=1;profile-level-id=42801f\r\n"
            "a=sendrecv\r\n"
        )
        answer = (
            "v=0\r\no=- 2 1 IN IP4 192.0.2.20\r\n"
            "s=answer\r\nc=IN IP4 192.0.2.20\r\nt=0 0\r\n"
            "m=video 41002 RTP/AVP 103\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=fmtp:103 packetization-mode=1;profile-level-id=42801f\r\n"
            "a=rtcp-fb:103 nack pli\r\n"
            "a=sendrecv\r\n"
        )

        sdp.validate_sdp_answer(offer, answer)
        selected = sdp.offered_video_formats(answer)
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].transport_profile, "RTP/AVP")
        self.assertEqual(selected[0].rtcp_feedback, ())

    def test_answer_must_preserve_media_count_order_transport_and_formats(self) -> None:
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        offer = sdp.build_offer_directional(
            "192.0.2.10",
            "192.0.2.10",
            40000,
            [audio],
            [audio],
            video_port=40002,
            video_formats=(sdp.DEFAULT_H264_FORMAT,),
        )
        answer = sdp.build_answer_directional(
            "192.0.2.20",
            "192.0.2.20",
            41000,
            sdp.audio_format_to_rtp(audio, 96),
            sdp.audio_format_to_rtp(audio, 96),
            remote_sdp=offer,
            video_port=41002,
            video_format=sdp.DEFAULT_H264_FORMAT,
        )
        sdp.validate_sdp_answer(offer, answer)

        _session, _direction, sections = sdp._parse_media_sections(answer)
        self.assertEqual([item["media"] for item in sections], ["audio", "video"])
        cases = {
            "missing": "\r\n".join(
                line
                for line in answer.split("\r\n")
                if not line.startswith(("m=video", "a=rtpmap:103", "a=fmtp:103", "a=rtcp:41003"))
            ),
            "reordered": answer[answer.index("m=video") :] + answer[: answer.index("m=video")],
            "media-type": answer.replace("m=video ", "m=application ", 1),
            "transport": answer.replace("m=video 41002 RTP/AVP", "m=video 41002 RTP/AVPF", 1),
            "payload": answer.replace("m=video 41002 RTP/AVP 103", "m=video 41002 RTP/AVP 120", 1),
        }
        for name, invalid in cases.items():
            with self.subTest(name=name), self.assertRaises(sdp.SdpError):
                sdp.validate_sdp_answer(offer, invalid)

    def test_uac_interop_may_treat_omitted_trailing_video_as_rejected(self) -> None:
        audio = audio_format.AudioFormat(8000, "s16le", 1, 20)
        offer = sdp.build_offer_directional(
            "192.0.2.10",
            "192.0.2.10",
            40000,
            [audio],
            [audio],
            video_port=40002,
            video_formats=(sdp.DEFAULT_H264_FORMAT,),
        )
        payload = sdp.offered_pcm_formats(offer)[0]
        audio_only_answer = sdp.build_answer_directional(
            "192.0.2.20",
            "192.0.2.20",
            41000,
            payload,
            payload,
            remote_sdp=sdp.build_offer_directional(
                "192.0.2.10",
                "192.0.2.10",
                40000,
                [audio],
                [audio],
            ),
        )

        with self.assertRaisesRegex(sdp.SdpError, "count and order"):
            sdp.validate_sdp_answer(offer, audio_only_answer)
        sdp.validate_sdp_answer(
            offer,
            audio_only_answer,
            allow_omitted_trailing_media=True,
        )

    def test_answer_direction_matrix_matches_rfc3264(self) -> None:
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        inverse = {
            "sendrecv": {"sendrecv", "sendonly", "recvonly", "inactive"},
            "sendonly": {"recvonly", "inactive"},
            "recvonly": {"sendonly", "inactive"},
            "inactive": {"inactive"},
        }
        for offer_direction, allowed in inverse.items():
            offer = sdp.build_offer_directional(
                "192.0.2.10",
                "192.0.2.10",
                40000,
                [audio],
                [audio],
                audio_direction=offer_direction,
            )
            payload = sdp.offered_pcm_formats(offer)[0]
            for answer_direction in ("sendrecv", "sendonly", "recvonly", "inactive"):
                answer = sdp.build_answer_directional(
                    "192.0.2.20",
                    "192.0.2.20",
                    41000,
                    payload,
                    payload,
                    remote_sdp=offer,
                    audio_direction=answer_direction,
                )
                if answer_direction in allowed:
                    sdp.validate_sdp_answer(offer, answer)
                else:
                    with self.assertRaises(sdp.SdpError):
                        sdp.validate_sdp_answer(offer, answer)

    def test_sdp_origin_rewrite_preserves_identity_and_detects_real_changes(self) -> None:
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        initial = sdp.rewrite_sdp_origin(
            sdp.build_offer("192.0.2.10", "192.0.2.10", 40000, [fmt]),
            123456,
            0,
        )
        refresh = sdp.rewrite_sdp_origin(initial, 123456, 1)
        held = refresh.replace("a=sendrecv", "a=inactive")

        self.assertIn("o=- 123456 0 IN IP4 192.0.2.10", initial)
        self.assertIn("o=- 123456 1 IN IP4 192.0.2.10", refresh)
        self.assertFalse(sdp.sdp_description_changed(initial, refresh))
        self.assertTrue(sdp.sdp_description_changed(refresh, held))

    def test_static_audio_payload_type_cannot_be_remapped(self) -> None:
        invalid = (
            "v=0\r\nc=IN IP4 192.0.2.10\r\nt=0 0\r\n"
            "m=audio 40000 RTP/AVP 0\r\n"
            "a=rtpmap:0 L16/16000/1\r\na=ptime:20\r\n"
        )
        self.assertEqual(sdp.offered_pcm_formats(invalid), [])

        canonical = invalid.replace("L16/16000/1", "PCMU/8000/1")
        offered = sdp.offered_pcm_formats(canonical)
        self.assertEqual(
            [(item.payload_type, item.encoding, item.sample_rate) for item in offered],
            [(0, "PCMU", 8000)],
        )

    def test_audio_direction_is_parsed_and_answered_per_rfc3264(self) -> None:
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        selected = sdp.audio_format_to_rtp(fmt, 96)

        for remote_direction, local_direction in (
            ("sendrecv", "sendrecv"),
            ("sendonly", "recvonly"),
            ("recvonly", "sendonly"),
            ("inactive", "inactive"),
        ):
            with self.subTest(remote_direction=remote_direction):
                offer = sdp.build_offer_directional(
                    "192.0.2.10",
                    "192.0.2.10",
                    40000,
                    [fmt],
                    [fmt],
                    audio_direction=remote_direction,
                )
                self.assertEqual(sdp.parse_sdp(offer)["direction"], remote_direction)
                answer = sdp.build_answer_directional(
                    "192.0.2.20",
                    "192.0.2.20",
                    41000,
                    selected,
                    selected,
                    remote_sdp=offer,
                )
                audio = sdp.parse_sdp(answer)
                self.assertEqual(audio["direction"], local_direction)

    def test_audio_direction_defaults_to_sendrecv_and_rejects_bad_values(self) -> None:
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        offer = sdp.build_offer("192.0.2.10", "192.0.2.10", 40000, [fmt])
        self.assertEqual(sdp.parse_sdp(offer)["direction"], "sendrecv")
        with self.assertRaises(sdp.SdpError):
            sdp.build_offer_directional(
                "192.0.2.10",
                "192.0.2.10",
                40000,
                [fmt],
                [fmt],
                audio_direction="sideways",
            )

    def test_legacy_zero_connection_hold_suppresses_only_local_send(self) -> None:
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        selected = sdp.audio_format_to_rtp(fmt, 96)

        for remote_direction, local_direction in (
            ("sendrecv", "recvonly"),
            ("sendonly", "recvonly"),
            ("recvonly", "inactive"),
            ("inactive", "inactive"),
        ):
            with self.subTest(remote_direction=remote_direction):
                offer = sdp.build_offer_directional(
                    "192.0.2.10",
                    "192.0.2.10",
                    40000,
                    [fmt],
                    [fmt],
                    audio_direction=remote_direction,
                ).replace("c=IN IP4 192.0.2.10", "c=IN IP4 0.0.0.0")
                parsed = sdp.parse_sdp(offer)
                self.assertTrue(parsed["connection_held"])
                self.assertEqual(parsed["media_port"], 40000)
                self.assertEqual(parsed["direction"], remote_direction)

                answer = sdp.build_answer_directional(
                    "192.0.2.20",
                    "192.0.2.20",
                    41000,
                    selected,
                    selected,
                    remote_sdp=offer,
                    # Exercise the explicit-direction fail-safe as well.
                    audio_direction=sdp.local_direction_for_remote(remote_direction),
                )
                self.assertEqual(sdp.parse_sdp(answer)["direction"], local_direction)
                self.assertIn("m=audio 41000", answer)

    def test_media_connection_override_can_resume_one_held_stream(self) -> None:
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        offer = sdp.build_offer_directional(
            "192.0.2.10",
            "192.0.2.10",
            40000,
            [fmt],
            [fmt],
            video_port=42000,
            video_formats=sdp.DEFAULT_VIDEO_FORMATS[:1],
        ).replace("c=IN IP4 192.0.2.10", "c=IN IP4 0.0.0.0", 1)
        offer = offer.replace(
            "m=video 42000 RTP/AVP 103\r\n",
            "m=video 42000 RTP/AVP 103\r\nc=IN IP4 192.0.2.30\r\n",
        )

        self.assertTrue(sdp.parse_sdp(offer)["connection_held"])
        video = sdp.parse_video_sdp(offer)
        self.assertIsNotNone(video)
        assert video is not None
        self.assertFalse(video["connection_held"])
        self.assertEqual(video["connection_ip"], "192.0.2.30")

    def test_negotiate_l16_48k(self) -> None:
        offer = sdp.build_offer(
            "192.168.1.20",
            "192.168.1.20",
            40000,
            [
                audio_format.AudioFormat(48000, "s16le", 1, 10),
                audio_format.AudioFormat(16000, "s16le", 1, 20),
            ],
        )
        self.assertNotIn("a=fmtp:96", offer)
        self.assertIn("telephone-event/8000", offer)
        self.assertIn("a=fmtp:97 0-15", offer)
        self.assertIn("a=maxptime:10", offer)
        selected_direction = sdp.negotiate_directional(
            offer,
            [
                audio_format.AudioFormat(16000, "s16le", 1, 20),
                audio_format.AudioFormat(48000, "s16le", 1, 10),
            ],
            [
                audio_format.AudioFormat(16000, "s16le", 1, 20),
                audio_format.AudioFormat(48000, "s16le", 1, 10),
            ],
        )
        self.assertIsNotNone(selected_direction)
        assert selected_direction is not None
        selected = selected_direction.send
        self.assertEqual(selected.encoding, "L16")
        self.assertEqual(selected.sample_rate, 48000)
        self.assertEqual(selected.payload_type, 96)

    def test_negotiate_missing_ptime_prefers_best_local_pcm(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.48\r\n"
            "s=pjmedia\r\n"
            "c=IN IP4 192.168.1.48\r\n"
            "t=0 0\r\n"
            "m=audio 40760 RTP/AVP 96 97 98\r\n"
            "a=rtpmap:96 L16/16000\r\n"
            "a=rtpmap:97 L16/48000\r\n"
            "a=rtpmap:98 opus/48000/2\r\n"
            "a=sendrecv\r\n"
        )
        selected_direction = sdp.negotiate_directional(
            offer,
            [
                audio_format.AudioFormat(48000, "s16le", 1, 10),
                audio_format.AudioFormat(16000, "s16le", 1, 20),
            ],
            [
                audio_format.AudioFormat(48000, "s16le", 1, 10),
                audio_format.AudioFormat(16000, "s16le", 1, 20),
            ],
        )
        self.assertIsNotNone(selected_direction)
        assert selected_direction is not None
        selected = selected_direction.send
        self.assertEqual(selected.payload_type, 97)
        self.assertEqual(selected.audio_format, audio_format.AudioFormat(48000, "s16le", 1, 10))

        answer = sdp.build_answer_directional(
            "192.168.1.10", "192.168.1.10", 40000, selected, selected
        )
        self.assertIn("m=audio 40000 RTP/AVP 97", answer)
        self.assertIn("a=rtpmap:97 L16/48000/1", answer)
        self.assertIn("a=ptime:10", answer)

    def test_negotiate_l24_from_s24(self) -> None:
        offer = sdp.build_offer(
            "192.168.1.20",
            "192.168.1.20",
            40000,
            [audio_format.AudioFormat(16000, "s24le", 1, 20)],
        )
        offered = sdp.offered_pcm_formats(offer)
        self.assertEqual(offered[0].encoding, "L24")
        self.assertEqual(offered[0].sample_rate, 16000)

    def test_sendrecv_offer_and_negotiation_use_one_common_wire_format(self) -> None:
        tx_preferred = audio_format.AudioFormat(48000, "s16le", 1, 10)
        rx_preferred = audio_format.AudioFormat(32000, "s16le", 1, 10)
        common = audio_format.AudioFormat(16000, "s16le", 1, 10)
        offer = sdp.build_offer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40020,
            [tx_preferred, common],
            [rx_preferred, common],
        )
        offered = sdp.offered_pcm_formats(offer)
        self.assertEqual([fmt.audio_format for fmt in offered], [common])

        selected = sdp.negotiate_directional(
            offer,
            [common],
            [common],
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send, selected.recv)
        self.assertEqual(selected.send.audio_format, common)

        answer = sdp.build_answer_directional(
            "192.168.1.47",
            "192.168.1.47",
            40000,
            selected.send,
            selected.recv,
            remote_sdp=offer,
        )
        self.assertIn("L16/16000/1", answer)
        self.assertEqual(
            sdp.parse_sdp(answer)["payload_order"],
            [selected.send.payload_type],
        )
        self.assertNotIn("a=fmtp:", answer)
        self.assertIn("a=maxptime:10", answer)

    def test_directional_offer_requires_common_ptime(self) -> None:
        with self.assertRaises(sdp.SdpError):
            sdp.build_offer_directional(
                "192.168.1.10",
                "192.168.1.10",
                40020,
                [audio_format.AudioFormat(16000, "s16le", 1, 16)],
                [audio_format.AudioFormat(48000, "s16le", 1, 10)],
            )

    def test_sendrecv_negotiation_rejects_disjoint_directional_formats(self) -> None:
        ha_to_esp = audio_format.AudioFormat(48000, "s16le", 1, 10)
        esp_to_ha = audio_format.AudioFormat(16000, "s16le", 1, 10)
        answer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.47\r\n"
            "s=VoIP Stack\r\n"
            "c=IN IP4 192.168.1.47\r\n"
            "t=0 0\r\n"
            "m=audio 40000 RTP/AVP 96 97\r\n"
            "a=rtpmap:96 L16/48000/1\r\n"
            "a=rtpmap:97 L16/16000/1\r\n"
            "a=ptime:10\r\n"
            "a=maxptime:10\r\n"
            "a=sendrecv\r\n"
        )
        selected = sdp.negotiate_answer_directional(
            answer,
            [ha_to_esp],
            [esp_to_ha],
        )
        self.assertIsNone(selected)

        with self.assertRaisesRegex(sdp.SdpError, "one RTP payload"):
            sdp.build_answer_directional(
                "192.168.1.47",
                "192.168.1.47",
                40000,
                sdp.audio_format_to_rtp(ha_to_esp, 96),
                sdp.audio_format_to_rtp(esp_to_ha, 97),
            )

    def test_one_way_audio_uses_only_the_active_local_capability(self) -> None:
        local_send = audio_format.AudioFormat(48000, "s16le", 1, 10)
        local_recv = audio_format.AudioFormat(16000, "s16le", 1, 20)

        send_offer = sdp.build_offer_directional(
            "192.0.2.10",
            "192.0.2.10",
            40000,
            [local_send],
            [local_recv],
            audio_direction="sendonly",
        )
        recv_offer = sdp.build_offer_directional(
            "192.0.2.10",
            "192.0.2.10",
            40000,
            [local_send],
            [local_recv],
            audio_direction="recvonly",
        )
        self.assertEqual(
            sdp.offered_pcm_formats(send_offer)[0].audio_format,
            local_send,
        )
        self.assertEqual(
            sdp.offered_pcm_formats(recv_offer)[0].audio_format,
            local_recv,
        )

        recv_selected = sdp.negotiate_directional(
            send_offer,
            [local_recv],
            [local_send],
        )
        send_selected = sdp.negotiate_directional(
            recv_offer,
            [local_recv],
            [local_send],
        )
        self.assertIsNotNone(recv_selected)
        self.assertIsNotNone(send_selected)
        assert recv_selected is not None and send_selected is not None
        self.assertEqual(recv_selected.recv.audio_format, local_send)
        self.assertEqual(send_selected.send.audio_format, local_recv)

        recv_answer = sdp.build_answer_directional(
            "192.0.2.20",
            "192.0.2.20",
            41000,
            recv_selected.send,
            recv_selected.recv,
            remote_sdp=send_offer,
        )
        send_answer = sdp.build_answer_directional(
            "192.0.2.20",
            "192.0.2.20",
            41000,
            send_selected.send,
            send_selected.recv,
            remote_sdp=recv_offer,
        )
        self.assertEqual(sdp.parse_sdp(recv_answer)["direction"], "recvonly")
        self.assertEqual(sdp.parse_sdp(send_answer)["direction"], "sendonly")
        self.assertEqual(len(sdp.offered_pcm_formats(recv_answer)), 1)
        self.assertEqual(len(sdp.offered_pcm_formats(send_answer)), 1)

    def test_inactive_answer_format_list_follows_original_offer_direction(self) -> None:
        send = sdp.RtpPcmFormat(96, "L16", 48000, 1, 10)
        recv = sdp.RtpPcmFormat(97, "L16", 16000, 1, 10)

        def remote_offer(direction: str) -> str:
            return (
                "v=0\r\n"
                "o=- 0 0 IN IP4 192.0.2.10\r\n"
                "s=-\r\n"
                "c=IN IP4 192.0.2.10\r\n"
                "t=0 0\r\n"
                "m=audio 40000 RTP/AVP 96 97\r\n"
                "a=rtpmap:96 L16/48000/1\r\n"
                "a=rtpmap:97 L16/16000/1\r\n"
                "a=ptime:10\r\n"
                f"a={direction}\r\n"
            )

        for direction, expected_payload in (("sendonly", 97), ("recvonly", 96)):
            with self.subTest(direction=direction):
                answer = sdp.build_answer_directional(
                    "192.0.2.20",
                    "192.0.2.20",
                    41000,
                    send,
                    recv,
                    remote_sdp=remote_offer(direction),
                    audio_direction="inactive",
                )
                parsed = sdp.parse_sdp(answer)
                self.assertEqual(parsed["direction"], "inactive")
                self.assertEqual(parsed["payload_order"], [expected_payload])

        for direction in ("sendrecv", "inactive"):
            with self.subTest(direction=direction):
                with self.assertRaisesRegex(sdp.SdpError, "one RTP payload"):
                    sdp.build_answer_directional(
                        "192.0.2.20",
                        "192.0.2.20",
                        41000,
                        send,
                        recv,
                        remote_sdp=remote_offer(direction),
                        audio_direction="inactive",
                    )

    def test_inactive_answer_negotiation_uses_local_offer_capability_direction(self) -> None:
        local_send = audio_format.AudioFormat(48000, "s16le", 1, 10)
        local_recv = audio_format.AudioFormat(16000, "s16le", 1, 10)
        inactive_answer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.0.2.20\r\n"
            "s=-\r\n"
            "c=IN IP4 192.0.2.20\r\n"
            "t=0 0\r\n"
            "m=audio 41000 RTP/AVP 96 97\r\n"
            "a=rtpmap:96 L16/48000/1\r\n"
            "a=rtpmap:97 L16/16000/1\r\n"
            "a=ptime:10\r\n"
            "a=inactive\r\n"
        )
        sendonly = sdp.negotiate_answer_directional(
            inactive_answer,
            [local_send],
            [local_recv],
            local_offer_direction="sendonly",
        )
        recvonly = sdp.negotiate_answer_directional(
            inactive_answer,
            [local_send],
            [local_recv],
            local_offer_direction="recvonly",
        )
        sendrecv = sdp.negotiate_answer_directional(
            inactive_answer,
            [local_send],
            [local_recv],
        )
        self.assertIsNotNone(sendonly)
        self.assertIsNotNone(recvonly)
        assert sendonly is not None and recvonly is not None
        self.assertEqual(sendonly.send.audio_format, local_send)
        self.assertEqual(recvonly.recv.audio_format, local_recv)
        self.assertIsNone(sendrecv)

    def test_standard_softphone_answer_uses_one_common_payload_when_profiles_are_symmetric(self) -> None:
        answer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.48\r\n"
            "s=baresip\r\n"
            "c=IN IP4 192.168.1.48\r\n"
            "t=0 0\r\n"
            "m=audio 45686 RTP/AVP 96 98\r\n"
            "a=rtpmap:96 L16/48000\r\n"
            "a=rtpmap:98 L16/16000\r\n"
            "a=minptime:10\r\n"
            "a=ptime:10\r\n"
            "a=sendrecv\r\n"
        )
        selected = sdp.negotiate_answer_directional(
            answer,
            list(audio_format.HA_SIP_PCM_FORMATS),
            list(audio_format.HA_SIP_PCM_FORMATS),
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send.payload_type, 96)
        self.assertEqual(selected.recv.payload_type, 96)
        self.assertEqual(selected.send.audio_format, audio_format.AudioFormat(48000, "s16le", 1, 10))
        self.assertEqual(selected.recv.audio_format, audio_format.AudioFormat(48000, "s16le", 1, 10))

    def test_rejects_oversized_pcm_rtp_frame(self) -> None:
        with self.assertRaises(sdp.SdpError):
            sdp.build_offer(
                "192.168.1.20",
                "192.168.1.20",
                40000,
                [audio_format.AudioFormat(48000, "s16le", 1, 20)],
            )

    def test_accepts_g711_trunk_offer_as_pcm_edge_codec(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.20\r\n"
            "s=Phone\r\n"
            "c=IN IP4 192.168.1.20\r\n"
            "t=0 0\r\n"
            "m=audio 40000 RTP/AVP 0 8 96\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
            "a=rtpmap:96 opus/48000/2\r\n"
            "a=ptime:20\r\n"
        )
        preferred = [audio_format.AudioFormat(8000, "s16le", 1, 20)]
        selected_direction = sdp.negotiate_directional(offer, preferred, preferred)
        self.assertIsNotNone(selected_direction)
        assert selected_direction is not None
        selected = selected_direction.send
        self.assertEqual(selected.encoding, "PCMU")
        self.assertEqual(selected.payload_type, 0)
        self.assertEqual(selected.audio_format, audio_format.AudioFormat(8000, "s16le", 1, 20))

    def test_prefers_l16_48k_over_g711_when_both_are_offered(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.20\r\n"
            "s=Phone\r\n"
            "c=IN IP4 192.168.1.20\r\n"
            "t=0 0\r\n"
            "m=audio 40000 RTP/AVP 0 96 8\r\n"
            "a=rtpmap:96 L16/48000/1\r\n"
            "a=ptime:10\r\n"
        )
        preferred = [
            audio_format.AudioFormat(48000, "s16le", 1, 10),
            audio_format.AudioFormat(8000, "s16le", 1, 20),
        ]
        selected_direction = sdp.negotiate_directional(
            offer,
            preferred,
            preferred,
        )
        self.assertIsNotNone(selected_direction)
        assert selected_direction is not None
        selected = selected_direction.send
        self.assertEqual(selected.encoding, "L16")
        self.assertEqual(selected.sample_rate, 48000)

    def test_negotiate_48k_10ms_when_softphone_offers_20ms_with_minptime_10(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.48\r\n"
            "s=baresip\r\n"
            "c=IN IP4 192.168.1.48\r\n"
            "t=0 0\r\n"
            "m=audio 12456 RTP/AVP 96 97 8 0 101\r\n"
            "a=rtpmap:96 L16/48000\r\n"
            "a=rtpmap:97 L16/16000\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:101 telephone-event/8000\r\n"
            "a=fmtp:101 0-15\r\n"
            "a=sendrecv\r\n"
            "a=minptime:10\r\n"
            "a=ptime:20\r\n"
        )
        selected = sdp.negotiate_directional(
            offer,
            list(audio_format.HA_SIP_PCM_FORMATS),
            list(audio_format.HA_SIP_PCM_FORMATS),
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send.audio_format, audio_format.AudioFormat(48000, "s16le", 1, 10))
        self.assertEqual(selected.recv.audio_format, audio_format.AudioFormat(48000, "s16le", 1, 10))
        answer = sdp.build_answer_directional("192.168.1.10", "192.168.1.10", 40000, selected.send, selected.recv)
        self.assertIn("a=rtpmap:96 L16/48000/1", answer)
        self.assertIn("a=ptime:10", answer)

    def test_g711_rtp_payload_converts_to_internal_s16le(self) -> None:
        pcm = b"\x00\x00\x00\x10\x00\xf0"
        alaw = sip_client.pcm_to_rtp_payload(pcm, sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20))
        ulaw = sip_client.pcm_to_rtp_payload(pcm, sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20))
        self.assertEqual(len(alaw), 3)
        self.assertEqual(len(ulaw), 3)
        self.assertEqual(len(sip_client.rtp_payload_to_pcm(alaw, sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20))), len(pcm))
        self.assertEqual(len(sip_client.rtp_payload_to_pcm(ulaw, sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20))), len(pcm))

    def test_linear_pcm_rtp_endianness_round_trips_exactly(self) -> None:
        vectors = (
            (audio_format.AudioFormat(16000, "s16le", 1, 20), b"\x01\x02\xfe\xff", b"\x02\x01\xff\xfe"),
            (
                audio_format.AudioFormat(16000, "s24le", 1, 20),
                b"\x01\x02\x03\xfe\xfd\xfc",
                b"\x03\x02\x01\xfc\xfd\xfe",
            ),
            (
                audio_format.AudioFormat(16000, "s24le_in_s32", 1, 20),
                b"\x56\x34\x12\x00\x01\x00\x80\xff",
                b"\x12\x34\x56\x80\x00\x01",
            ),
        )
        for fmt, pcm, wire in vectors:
            with self.subTest(fmt=fmt.pcm_format):
                self.assertEqual(sip_client.pcm_to_rtp_payload(pcm, fmt), wire)
                self.assertEqual(sip_client.rtp_payload_to_pcm(wire, fmt), pcm)

    def test_s24_in_s32_bridge_conversion_is_right_aligned(self) -> None:
        s24 = audio_format.AudioFormat(16000, "s24le_in_s32", 1, 10)
        s16 = audio_format.AudioFormat(16000, "s16le", 1, 10)
        s24_pair = b"\x00\x00\x40\x00\x00\x00\xc0\xff"
        source = s24_pair * (s24.nominal_frame_samples // 2)

        converted = audio_pcm.PcmFrameConverter(s24, s16).convert(source)
        self.assertEqual(len(converted), 1)
        self.assertEqual(converted[0][:4], b"\x00\x40\x00\xc0")

        restored = audio_pcm.PcmFrameConverter(s16, s24).convert(converted[0])
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0][:8], s24_pair)

    def test_bounded_sip_udp_queue_keeps_freshest_datagram(self) -> None:
        queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue(maxsize=2)
        protocol = sip_client._SipClientProtocol(queue)
        protocol.datagram_received(b"one", ("127.0.0.1", 1))
        protocol.datagram_received(b"two", ("127.0.0.1", 2))
        protocol.datagram_received(b"three", ("127.0.0.1", 3))

        self.assertEqual(protocol.dropped_packets, 1)
        self.assertEqual(queue.get_nowait()[0], b"two")
        self.assertEqual(queue.get_nowait()[0], b"three")

    def test_rejects_s32_wire_mapping(self) -> None:
        with self.assertRaises(sdp.SdpError):
            sdp.audio_format_to_rtp(audio_format.AudioFormat(48000, "s32le", 1, 20), 96)

    def test_offer_filters_non_rtp_mappable_pcm_formats(self) -> None:
        offer = sdp.build_offer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40020,
            [
                audio_format.AudioFormat(48000, "s32le", 1, 10),
                audio_format.AudioFormat(48000, "s16le", 1, 10),
                audio_format.AudioFormat(16000, "s16le", 1, 10),
            ],
            [
                audio_format.AudioFormat(48000, "s16le", 1, 10),
                audio_format.AudioFormat(48000, "s16le", 2, 10),
                audio_format.AudioFormat(16000, "s16le", 1, 10),
            ],
        )
        offered = sdp.offered_pcm_formats(offer)
        self.assertEqual(
            [(fmt.encoding, fmt.sample_rate, fmt.channels, fmt.frame_ms) for fmt in offered],
            [("L16", 48000, 1, 10), ("L16", 16000, 1, 10)],
        )

    def test_offer_caps_payloads_to_compact_udp_safe_profile_without_losing_esp_baseline(self) -> None:
        browser_tx_formats = [
            audio_format.AudioFormat(rate, fmt, 1, frame_ms)
            for rate in sorted(audio_format.SUPPORTED_SAMPLE_RATES)
            for frame_ms in sorted(audio_format.SUPPORTED_FRAME_MS)
            if (rate * frame_ms) % 1000 == 0
            for fmt in audio_format.PcmFormat
        ]
        browser_rx_formats = [
            audio_format.AudioFormat(rate, fmt, channels, frame_ms)
            for rate in sorted(audio_format.SUPPORTED_SAMPLE_RATES)
            for frame_ms in sorted(audio_format.SUPPORTED_FRAME_MS)
            if (rate * frame_ms) % 1000 == 0
            for fmt in audio_format.PcmFormat
            for channels in (1, 2)
        ]
        offer = sdp.build_offer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40020,
            browser_tx_formats,
            browser_rx_formats,
        )
        offered = sdp.offered_pcm_formats(offer)
        self.assertLessEqual(len(offered), 12)
        self.assertLess(len(offer.encode()), 900)
        self.assertEqual(offered[0].audio_format, audio_format.AudioFormat(48000, "s16le", 1, 10))
        self.assertIn(
            audio_format.AudioFormat(16000, "s16le", 1, 10),
            [fmt.audio_format for fmt in offered],
        )

    def test_ha_sip_profile_rejects_browser_only_sample_rates(self) -> None:
        offer = sdp.build_offer_directional(
            "192.168.1.48",
            "192.168.1.48",
            40020,
            [audio_format.AudioFormat(44100, "s16le", 1, 10)],
            [audio_format.AudioFormat(44100, "s16le", 1, 10)],
        )
        selected = sdp.negotiate_directional(
            offer,
            list(audio_format.HA_SIP_PCM_TX_FORMATS),
            list(audio_format.HA_SIP_PCM_RX_FORMATS),
        )
        self.assertIsNone(selected)

    def test_ha_sip_profile_keeps_esp_baseline_16k_16ms(self) -> None:
        baseline = audio_format.AudioFormat(16000, "s16le", 1, 16)
        offer = sdp.build_offer_directional(
            "192.168.1.48",
            "192.168.1.48",
            40020,
            [baseline],
            [baseline],
        )
        selected = sdp.negotiate_directional(
            offer,
            list(audio_format.HA_SIP_PCM_TX_FORMATS),
            list(audio_format.HA_SIP_PCM_RX_FORMATS),
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.send.audio_format, baseline)


class RtpProfileTest(unittest.TestCase):
    def test_rtp_packet_round_trip(self) -> None:
        packet = rtp.RtpPacket(
            payload_type=96,
            marker=True,
            sequence=65535,
            timestamp=0xFFFFFFF0,
            ssrc=0x12345678,
            payload=b"\x00\x01\x02\x03",
        )
        raw = rtp.build_packet(packet)
        parsed = rtp.parse_packet(raw)
        self.assertEqual(parsed, packet)
        self.assertEqual(rtp.next_sequence(packet.sequence), 0)
        self.assertEqual(rtp.next_timestamp(packet.timestamp, 32), 16)

    def test_rejects_bad_rtp_version(self) -> None:
        raw = bytearray(rtp.build_packet(rtp.RtpPacket(96, 1, 2, 3, b"x")))
        raw[0] = 0
        with self.assertRaises(rtp.RtpError):
            rtp.parse_packet(bytes(raw))

    def test_receiver_accepts_standard_mtu_payload_larger_than_tx_budget(self) -> None:
        packet = rtp.RtpPacket(96, 1, 2, 3, b"x" * rtp.MAX_RTP_PAYLOAD_BYTES)
        raw = rtp.build_packet(packet) + (b"y" * 60)
        parsed = rtp.parse_packet(raw)
        self.assertEqual(len(parsed.payload), rtp.MAX_RTP_PAYLOAD_BYTES + 60)
        with self.assertRaises(rtp.RtpError):
            rtp.build_packet(rtp.RtpPacket(96, 1, 2, 3, parsed.payload))

    def test_audio_receive_payload_limit_accepts_boundary_and_rejects_oversized(
        self,
    ) -> None:
        fmt = sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20)
        limit = rtp.audio_payload_size_limit(fmt)

        self.assertEqual(limit, 160)
        rtp.validate_audio_payload_size(b"x" * limit, fmt)
        with self.assertRaisesRegex(rtp.RtpError, r"161 bytes; max is 160"):
            rtp.validate_audio_payload_size(b"x" * (limit + 1), fmt)

    def test_opus_receive_payload_limit_is_ptime_aware_and_hard_capped(self) -> None:
        opus_20_ms = sdp.RtpPcmFormat(98, "OPUS", 48000, 2, 20)
        opus_120_ms = sdp.RtpPcmFormat(98, "OPUS", 48000, 2, 120)

        self.assertEqual(rtp.audio_payload_size_limit(opus_20_ms), 8 * 1277)
        self.assertEqual(
            rtp.audio_payload_size_limit(opus_120_ms),
            rtp.MAX_AUDIO_RTP_PAYLOAD_BYTES,
        )

    def test_g722_receive_limit_uses_the_rtp_octet_clock(self) -> None:
        fmt = sdp.RtpPcmFormat(9, "G722", 8000, 1, 20)

        self.assertEqual(rtp.audio_payload_size_limit(fmt), 160)

    def test_dahua_pcm_receive_limit_accepts_exact_twenty_ms_frame(self) -> None:
        fmt = sdp.RtpPcmFormat(97, "PCM", 16000, 1, 20)

        self.assertEqual(rtp.audio_payload_size_limit(fmt), 640)
        rtp.validate_audio_payload_size(bytes(640), fmt)
        with self.assertRaisesRegex(rtp.RtpError, r"642 bytes; max is 640"):
            rtp.validate_audio_payload_size(bytes(642), fmt)

    def test_rfc4733_decoder_emits_one_event_per_press(self) -> None:
        decoder = dtmf.RtpDtmfDecoder(101)

        def event(sequence: int, timestamp: int, code: int, *, ssrc: int = 0x1234, ended: bool = False) -> bytes:
            return rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=101,
                    sequence=sequence,
                    timestamp=timestamp,
                    ssrc=ssrc,
                    payload=bytes((code, 0x80 if ended else 0x00, 0, 160)),
                )
            )

        self.assertEqual(decoder.decode(event(1, 1000, 1)), "1")
        self.assertEqual(decoder.decode(event(2, 1000, 1, ended=True)), "")
        self.assertEqual(decoder.decode(event(3, 2000, 10)), "*")
        self.assertEqual(decoder.decode(event(4, 3000, 12)), "A")
        self.assertEqual(decoder.decode(event(5, 4000, 2, ssrc=0x9999)), "")

    def test_legacy_sip_info_accepts_digit_and_event_code_forms(self) -> None:
        self.assertEqual(dtmf.parse_sip_info_digit("application/dtmf-relay", b"Signal=1\r\nDuration=160"), "1")
        self.assertEqual(dtmf.parse_sip_info_digit("application/dtmf-relay", b"Signal=10\r\nDuration=160"), "*")
        self.assertEqual(dtmf.parse_sip_info_digit("application/dtmf", b"#"), "#")

    def test_rfc4733_payload_encodes_event_end_volume_and_duration(self) -> None:
        self.assertEqual(
            dtmf.build_telephone_event_payload("#", duration=1280, end=True),
            bytes((11, 0x8A, 0x05, 0x00)),
        )
        self.assertEqual(dtmf.telephone_event_code("d"), 15)
        self.assertIsNone(dtmf.telephone_event_code("x"))
        with self.assertRaisesRegex(ValueError, "duration"):
            dtmf.build_telephone_event_payload("1", duration=0)

    def test_relay_follows_same_ssrc_nat_port_rebind(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        fmt = audio_format.AudioFormat(16000, "s16le", 1, 16)
        left = sip_rtp_bridge.RtpPeer("192.0.2.10", 40000, 96, fmt)
        right = sip_rtp_bridge.RtpPeer("192.0.2.20", 41000, 96, fmt)
        relay = sip_rtp_bridge.SipRtpRelay(left=left, right=right, left_port=42000, right_port=42002)
        output = FakeTransport()
        relay.right_transport = output  # type: ignore[assignment]

        def packet(ssrc: int) -> bytes:
            return rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=96,
                    sequence=1,
                    timestamp=1,
                    ssrc=ssrc,
                    payload=b"\0" * fmt.nominal_frame_bytes,
                )
            )

        relay.handle_packet("left", packet(0x1234), (left.host, 45000))
        self.assertEqual(left.port, 45000)
        relay.handle_packet("left", packet(0x1234), (left.host, 45002))
        self.assertEqual(left.port, 45002)
        relay.handle_packet("left", packet(0x9999), (left.host, 45004))
        self.assertEqual(left.port, 45002)
        self.assertEqual(len(output.sent), 2)

    def test_relay_accepts_authenticated_signaling_host_for_nat_media(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        left = sip_rtp_bridge.RtpPeer(
            "10.0.0.20",
            40000,
            96,
            fmt,
            signaling_host="198.51.100.20",
        )
        right = sip_rtp_bridge.RtpPeer("192.0.2.20", 41000, 96, fmt)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=left,
            right=right,
            left_port=42000,
            right_port=42002,
        )
        output = FakeTransport()
        return_output = FakeTransport()
        relay.right_transport = output  # type: ignore[assignment]
        relay.left_transport = return_output  # type: ignore[assignment]
        packet = rtp.build_packet(
            rtp.RtpPacket(
                payload_type=96,
                sequence=1,
                timestamp=1,
                ssrc=0x1234,
                payload=b"\0" * fmt.nominal_frame_bytes,
            )
        )

        relay.handle_packet("left", packet, ("198.51.100.20", 45000))
        relay.handle_packet("left", packet, ("203.0.113.20", 45000))
        relay.handle_packet("right", packet, (right.host, right.port))

        self.assertEqual(left.host, "198.51.100.20")
        self.assertEqual(left.port, 45000)
        self.assertEqual(len(output.sent), 1)
        self.assertEqual(return_output.sent[-1][1], ("198.51.100.20", 45000))
        self.assertEqual(relay.dropped, 1)

    def test_relay_enforces_negotiated_audio_direction(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        left = sip_rtp_bridge.RtpPeer(
            "192.0.2.10", 40000, 96, fmt, can_send=False, can_receive=True
        )
        right = sip_rtp_bridge.RtpPeer(
            "192.0.2.20", 41000, 96, fmt, can_send=True, can_receive=True
        )
        relay = sip_rtp_bridge.SipRtpRelay(
            left=left, right=right, left_port=42000, right_port=42002
        )
        output = FakeTransport()
        relay.right_transport = output  # type: ignore[assignment]
        packet = rtp.build_packet(
            rtp.RtpPacket(
                payload_type=96,
                sequence=1,
                timestamp=1,
                ssrc=1,
                payload=b"\0" * fmt.nominal_frame_bytes,
            )
        )

        relay.handle_packet("left", packet, (left.host, left.port))
        self.assertEqual(output.sent, [])
        left.can_send = True
        right.can_receive = False
        relay.handle_packet("left", packet, (left.host, left.port))
        self.assertEqual(output.sent, [])
        right.can_receive = True
        relay.handle_packet("left", packet, (left.host, left.port))
        self.assertEqual(len(output.sent), 1)

    def test_relay_connection_hold_blocks_only_traffic_toward_held_leg(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        left = sip_rtp_bridge.RtpPeer(
            "192.0.2.10",
            40000,
            96,
            fmt,
            connection_held=True,
        )
        right = sip_rtp_bridge.RtpPeer("192.0.2.20", 41000, 96, fmt)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=left,
            right=right,
            left_port=42000,
            right_port=42002,
        )
        toward_left = FakeTransport()
        toward_right = FakeTransport()
        relay.left_transport = toward_left  # type: ignore[assignment]
        relay.right_transport = toward_right  # type: ignore[assignment]
        packet = rtp.build_packet(
            rtp.RtpPacket(
                payload_type=96,
                sequence=1,
                timestamp=1,
                ssrc=1,
                payload=b"\0" * fmt.nominal_frame_bytes,
            )
        )

        relay.handle_packet("right", packet, (right.host, right.port))
        self.assertEqual(toward_left.sent, [])
        self.assertEqual(relay.drop_connection_hold, 1)
        relay.handle_packet("left", packet, (left.host, left.port))
        self.assertEqual(len(toward_right.sent), 1)

    def test_relay_reconfiguration_is_prepared_before_atomic_commit(self) -> None:
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        left = sip_rtp_bridge.RtpPeer("192.0.2.10", 40000, 96, fmt)
        right = sip_rtp_bridge.RtpPeer("192.0.2.20", 41000, 96, fmt)
        right.sequence = 100
        right.timestamp = 200
        right.ssrc = 300
        relay = sip_rtp_bridge.SipRtpRelay(
            left=left,
            right=right,
            left_port=42000,
            right_port=42002,
        )
        updated = sip_rtp_bridge.RtpPeer(
            "198.51.100.20",
            43000,
            97,
            fmt,
            send_payload_type=98,
            send_audio_format=fmt,
            can_send=False,
            can_receive=True,
        )

        commit = relay.prepare_peer_reconfiguration("right", updated)
        self.assertIs(relay.right, right)
        self.assertEqual(relay.right.port, 41000)
        # Model media continuing while the final SIP response is sent.
        right.sequence = 101
        right.timestamp = 520
        commit()

        self.assertIs(relay.right, updated)
        self.assertEqual((updated.host, updated.port), ("198.51.100.20", 43000))
        self.assertEqual((updated.sequence, updated.timestamp, updated.ssrc), (101, 520, 300))
        self.assertFalse(updated.can_send)
        self.assertTrue(updated.can_receive)

    def test_opposite_relay_reconfigurations_do_not_overwrite_each_other(self) -> None:
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        left = sip_rtp_bridge.RtpPeer("192.0.2.10", 40000, 96, fmt)
        right = sip_rtp_bridge.RtpPeer("192.0.2.20", 41000, 96, fmt)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=left,
            right=right,
            left_port=42000,
            right_port=42002,
        )
        updated_left = sip_rtp_bridge.RtpPeer("198.51.100.10", 43000, 96, fmt)
        updated_right = sip_rtp_bridge.RtpPeer("198.51.100.20", 43002, 96, fmt)

        commit_left = relay.prepare_peer_reconfiguration("left", updated_left)
        commit_right = relay.prepare_peer_reconfiguration("right", updated_right)
        commit_left()
        commit_right()

        self.assertIs(relay.left, updated_left)
        self.assertIs(relay.right, updated_right)
        self.assertEqual(relay.left.host, "198.51.100.10")
        self.assertEqual(relay.right.host, "198.51.100.20")


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


class RouterContractTest(unittest.TestCase):
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


class SipProtocolBugFixTest(unittest.TestCase):
    def test_dtmf_collector_emits_one_digit_per_event(self) -> None:
        digits: list[str] = []
        proto = dtmf._DtmfProtocol(101, digits.append, remote_host="127.0.0.1")

        def packet(*, sequence: int, timestamp: int, ended: bool, ssrc: bytes = b"ssrc") -> bytes:
            header = bytearray(12)
            header[0] = 0x80
            header[1] = 101
            header[2:4] = int(sequence).to_bytes(2, "big")
            header[4:8] = int(timestamp).to_bytes(4, "big")
            header[8:12] = ssrc
            payload = bytes([5, 0x80 if ended else 0x00, 0x00, 0xA0])
            return bytes(header) + payload

        proto.datagram_received(packet(sequence=0, timestamp=999, ended=False), ("127.0.0.2", 5000))
        proto.datagram_received(packet(sequence=1, timestamp=1234, ended=False), ("127.0.0.1", 5000))
        proto.datagram_received(packet(sequence=2, timestamp=1234, ended=True), ("127.0.0.1", 5000))
        proto.datagram_received(packet(sequence=3, timestamp=1234, ended=True), ("127.0.0.1", 5000))
        proto.datagram_received(
            packet(sequence=4, timestamp=5678, ended=False, ssrc=b"evil"),
            ("127.0.0.1", 5000),
        )
        self.assertEqual(digits, ["5"])

    def test_response_contact_uses_configured_local_sip_port(self) -> None:
        request = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:HA@192.168.1.10:9999",
                [
                    ("Via", "SIP/2.0/UDP 192.168.1.30:5060;branch=z9hG4bKx;rport"),
                    ("From", "<sip:ESP@192.168.1.30>;tag=a"),
                    ("To", "<sip:HA@192.168.1.10>"),
                    ("Call-ID", "contact-port"),
                    ("CSeq", "1 INVITE"),
                    ("Content-Length", "0"),
                ],
                b"",
            )
        )
        uri = sip_listener._response_contact_uri(
            request,
            local_ip="192.168.1.10",
            local_sip_port=5060,
            transport="UDP",
        )
        self.assertEqual(uri, "sip:HA@192.168.1.10:5060;transport=udp")

    def test_invite_error_ack_uses_invite_transaction(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(local_ip="192.168.1.10", local_name="HA", local_sip_port=5060, local_rtp_port=41000)
        client.transport = FakeTransport()  # type: ignore[assignment]
        client.dialog_ids = sip.SipDialogIds(call_id="call-error", local_tag="ltag", cseq=3, branch="z9hG4bKorig")
        client._invite_cseq = 3
        client._pending_request_uri = "sip:ESP@192.168.1.30:5060"
        client._pending_local_uri = "sip:HA@192.168.1.10:5060"
        client._pending_remote_uri = "sip:ESP@192.168.1.30:5060"
        msg = sip.parse_message(
            sip.build_response(
                486,
                "Busy Here",
                [
                    ("Via", "SIP/2.0/UDP 192.168.1.10:5060;branch=z9hG4bKorig"),
                    ("From", "<sip:HA@192.168.1.10>;tag=ltag"),
                    ("To", "<sip:ESP@192.168.1.30>;tag=rtag"),
                    ("Call-ID", "call-error"),
                    ("CSeq", "3 INVITE"),
                ],
                b"",
            )
        )
        client._send_invite_error_ack(msg, "192.168.1.30", 5060)
        raw, addr = client.transport.sent[0]  # type: ignore[union-attr]
        parsed = sip.parse_message(raw)
        self.assertEqual(addr, ("192.168.1.30", 5060))
        self.assertEqual(parsed.method, "ACK")
        self.assertEqual(parsed.header("CSeq"), "3 ACK")
        self.assertIn("z9hG4bKorig", parsed.header("Via"))

    def test_dialog_offer_accepts_legacy_connection_hold_and_resume(self) -> None:
        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        negotiated = sdp.audio_format_to_rtp(pcm, 96)
        client = sip_client.SipCallClient(
            local_ip="192.0.2.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
        )
        client.dialog_ids.remote_tag = "remote"
        client.dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="192.0.2.20",
            remote_sip_port=5060,
            remote_rtp_host="192.0.2.20",
            remote_rtp_port=42000,
            local_rtp_port=41000,
            call_id=client.dialog_ids.call_id,
            local_uri="sip:HA@192.0.2.10:5060",
            remote_uri="sip:ESP@192.0.2.20:5060",
            send_format=negotiated,
            recv_format=negotiated,
            local_sdp_body=sdp.build_answer_directional(
                "192.0.2.10",
                "192.0.2.10",
                41000,
                negotiated,
                negotiated,
            ),
        )

        def offer(connection: str, cseq: int) -> sip.SipMessage:
            body = sdp.build_offer_directional(
                "192.0.2.20",
                connection,
                42000,
                [pcm],
                [pcm],
            )
            return sip.parse_message(
                sip.build_request(
                    "UPDATE",
                    "sip:HA@192.0.2.10:5060",
                    [
                        ("From", "<sip:ESP@192.0.2.20>;tag=remote"),
                        (
                            "To",
                            f"<sip:HA@192.0.2.10>;tag={client.dialog_ids.local_tag}",
                        ),
                        ("Call-ID", client.dialog_ids.call_id),
                        ("CSeq", f"{cseq} UPDATE"),
                        ("Content-Type", "application/sdp"),
                    ],
                    body.encode("utf-8"),
                )
            )

        held_result = client._answer_remote_offer(offer("0.0.0.0", 2))
        self.assertIsNotNone(held_result)
        assert held_result is not None
        held, held_answer = held_result
        self.assertTrue(held.remote_audio_connection_held)
        self.assertEqual(held.local_audio_direction, "recvonly")
        self.assertIn("m=audio 41000", held_answer)
        self.assertIn("a=recvonly", held_answer)

        client.dialog = held
        resumed_result = client._answer_remote_offer(offer("192.0.2.20", 3))
        self.assertIsNotNone(resumed_result)
        assert resumed_result is not None
        resumed, resumed_answer = resumed_result
        self.assertFalse(resumed.remote_audio_connection_held)
        self.assertEqual(resumed.local_audio_direction, "sendrecv")
        self.assertIn("a=sendrecv", resumed_answer)

    def test_dialog_video_reinvite_keeps_directional_levels_through_hold(self) -> None:
        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        negotiated = sdp.audio_format_to_rtp(pcm, 96)
        high = sdp.RtpVideoFormat(
            payload_type=103,
            profile_level_id="42801f",
            level_asymmetry_allowed=True,
        )
        low = sdp.RtpVideoFormat(
            payload_type=103,
            profile_level_id="42800d",
            level_asymmetry_allowed=True,
        )
        client = sip_client.SipCallClient(
            local_ip="192.0.2.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
            local_video_rtp_port=41002,
            video_formats=(high,),
            video_direction="sendrecv",
        )
        client.dialog_ids.remote_tag = "remote"
        client.dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="192.0.2.20",
            remote_sip_port=5060,
            remote_rtp_host="192.0.2.20",
            remote_rtp_port=42000,
            local_rtp_port=41000,
            call_id=client.dialog_ids.call_id,
            local_uri="sip:HA@192.0.2.10:5060",
            remote_uri="sip:ESP@192.0.2.20:5060",
            send_format=negotiated,
            recv_format=negotiated,
            video_format=high,
            local_video_format=high,
            local_video_rtp_port=41002,
            local_video_direction="sendrecv",
        )

        def offer(video_direction: str, cseq: int) -> sip.SipMessage:
            body = sdp.build_offer_directional(
                "192.0.2.20",
                "192.0.2.20",
                42000,
                [pcm],
                [pcm],
                video_port=42002,
                video_formats=(low,),
                video_direction=video_direction,
            )
            return sip.parse_message(
                sip.build_request(
                    "UPDATE",
                    "sip:HA@192.0.2.10:5060",
                    [
                        ("From", "<sip:ESP@192.0.2.20>;tag=remote"),
                        (
                            "To",
                            f"<sip:HA@192.0.2.10>;tag={client.dialog_ids.local_tag}",
                        ),
                        ("Call-ID", client.dialog_ids.call_id),
                        ("CSeq", f"{cseq} UPDATE"),
                        ("Content-Type", "application/sdp"),
                    ],
                    body.encode(),
                )
            )

        held_result = client._answer_remote_offer(offer("sendonly", 2))
        self.assertIsNotNone(held_result)
        assert held_result is not None
        held, held_answer = held_result
        self.assertEqual(held.local_video_direction, "recvonly")
        self.assertEqual(held.send_video_format.profile_level_id, "42800d")
        self.assertEqual(held.recv_video_format.profile_level_id, "42801f")
        self.assertIn("profile-level-id=42801f", held_answer)
        self.assertIn("a=recvonly", held_answer)

        client.dialog = held
        resumed_result = client._answer_remote_offer(offer("sendrecv", 3))
        self.assertIsNotNone(resumed_result)
        assert resumed_result is not None
        resumed, resumed_answer = resumed_result
        self.assertEqual(resumed.local_video_direction, "sendrecv")
        self.assertEqual(resumed.send_video_format.profile_level_id, "42800d")
        self.assertEqual(resumed.recv_video_format.profile_level_id, "42801f")
        self.assertIn("a=sendrecv", resumed_answer)


class SipProtocolBugFixAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_rtp_relay_concurrent_stop_is_single_owner_and_failure_safe(self) -> None:
        released: list[tuple[int, int]] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer("127.0.0.2", 40000, 96, fmt),
            right=sip_rtp_bridge.RtpPeer("127.0.0.3", 41000, 96, fmt),
            left_port=42000,
            right_port=42002,
            on_release=released.append,
        )

        class FakeTransport:
            def __init__(self) -> None:
                self.closed = 0

            def close(self) -> None:
                self.closed += 1

        class FailingVideoRelay:
            def __init__(self) -> None:
                self.calls = 0
                self.entered = asyncio.Event()
                self.release = asyncio.Event()

            async def stop(self) -> None:
                self.calls += 1
                self.entered.set()
                await self.release.wait()
                raise OSError("video teardown failed")

        left_transport = FakeTransport()
        right_transport = FakeTransport()
        video = FailingVideoRelay()
        relay.left_transport = left_transport  # type: ignore[assignment]
        relay.right_transport = right_transport  # type: ignore[assignment]
        relay.video_relay = video

        first = asyncio.create_task(relay.stop())
        second = asyncio.create_task(relay.stop())
        await video.entered.wait()
        self.assertEqual(released, [(42000, 42002)])
        self.assertEqual(left_transport.closed, 1)
        self.assertEqual(right_transport.closed, 1)
        first.cancel()
        await asyncio.sleep(0)
        first.cancel()
        await asyncio.sleep(0)
        self.assertFalse(first.done())
        video.release.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        await second
        await relay.stop()

        self.assertEqual(video.calls, 1)
        self.assertEqual(released, [(42000, 42002)])
        self.assertEqual(left_transport.closed, 1)
        self.assertEqual(right_transport.closed, 1)

    async def test_rtp_relay_stop_cancels_and_joins_inflight_start(self) -> None:
        released: list[tuple[int, int]] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer("127.0.0.2", 40000, 96, fmt),
            right=sip_rtp_bridge.RtpPeer("127.0.0.3", 41000, 96, fmt),
            left_port=42000,
            right_port=42002,
            on_release=released.append,
        )

        class FakeSocket:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        class FakeTransport:
            def __init__(self) -> None:
                self.closed = False

            def close(self) -> None:
                self.closed = True

        sockets = [FakeSocket(), FakeSocket()]
        transports: list[FakeTransport] = []
        entered = asyncio.Event()
        resume = asyncio.Event()

        def fake_socket(_port: int) -> FakeSocket:
            return sockets.pop(0)

        async def delayed_endpoint(*_args, **_kwargs):
            entered.set()
            try:
                await resume.wait()
            except asyncio.CancelledError:
                # Model an endpoint factory that completes ownership transfer
                # while cancellation is already in flight.
                await resume.wait()
            transport = FakeTransport()
            transports.append(transport)
            return transport, object()

        relay._rtp_socket = fake_socket  # type: ignore[method-assign]
        loop = asyncio.get_running_loop()
        with patch.object(
            loop,
            "create_datagram_endpoint",
            side_effect=delayed_endpoint,
        ):
            start = asyncio.create_task(relay.start())
            await entered.wait()
            stop = asyncio.create_task(relay.stop())
            await asyncio.sleep(0)
            self.assertFalse(stop.done())
            resume.set()
            with self.assertRaises(asyncio.CancelledError):
                await start
            await stop
            await relay.stop()

        self.assertEqual(released, [(42000, 42002)])
        self.assertIsNone(relay.left_transport)
        self.assertIsNone(relay.right_transport)
        self.assertTrue(all(transport.closed for transport in transports))
        with self.assertRaisesRegex(RuntimeError, "already been stopped"):
            await relay.start()

    async def test_rtp_relay_debug_write_failure_does_not_break_teardown(self) -> None:
        released: list[tuple[int, int]] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer("127.0.0.2", 40000, 96, fmt),
            right=sip_rtp_bridge.RtpPeer("127.0.0.3", 41000, 96, fmt),
            left_port=42000,
            right_port=42002,
            on_release=released.append,
        )
        relay._capture_buffers["left"] = bytearray(b"audio")

        def fail_write() -> None:
            raise OSError("diagnostic filesystem unavailable")

        relay._write_debug_capture_files = fail_write  # type: ignore[method-assign]

        await relay.stop()
        await relay.stop()

        self.assertEqual(released, [(42000, 42002)])
        self.assertEqual(relay._capture_buffers, {})

    def test_rtp_relay_debug_capture_rolls_back_partial_leg_group(self) -> None:
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer("127.0.0.2", 40000, 96, fmt),
            right=sip_rtp_bridge.RtpPeer("127.0.0.3", 41000, 96, fmt),
            left_port=42000,
            right_port=42002,
        )
        real_commit = sip_rtp_bridge.commit_capture_file
        commits = 0

        def fail_second_commit(temporary: Path, destination: Path) -> None:
            nonlocal commits
            commits += 1
            if commits == 2:
                raise OSError("relay diagnostic rename failed")
            real_commit(temporary, destination)

        with (
            tempfile.TemporaryDirectory() as temp_dir,
            patch.object(
                sip_rtp_bridge,
                "debug_capture_transaction",
                side_effect=lambda: contextlib.nullcontext(),
            ),
            patch.object(
                sip_rtp_bridge,
                "commit_capture_file",
                side_effect=fail_second_commit,
            ),
            patch.object(sip_rtp_bridge, "prune_debug_captures"),
        ):
            directory = Path(temp_dir)
            relay._capture_snapshot = {
                "left": (b"left", fmt, directory / "left.wav"),
                "right": (b"right", fmt, directory / "right.wav"),
            }
            with self.assertRaisesRegex(OSError, "rename failed"):
                relay._write_debug_capture_files()
            self.assertEqual(list(directory.iterdir()), [])

    async def test_rtp_relay_child_cancellation_does_not_poison_teardown(self) -> None:
        released: list[tuple[int, int]] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer("127.0.0.2", 40000, 96, fmt),
            right=sip_rtp_bridge.RtpPeer("127.0.0.3", 41000, 96, fmt),
            left_port=42000,
            right_port=42002,
            on_release=released.append,
        )

        class CancelledVideoRelay:
            calls = 0

            async def stop(self) -> None:
                self.calls += 1
                raise asyncio.CancelledError

        video = CancelledVideoRelay()
        relay.video_relay = video

        await relay.stop()
        await relay.stop()

        self.assertEqual(video.calls, 1)
        self.assertEqual(released, [(42000, 42002)])
        self.assertIsNone(relay.video_relay)

    async def test_rtp_relay_drops_debug_snapshot_when_writer_pool_is_full(self) -> None:
        capture_limits = _load_intercom_module("debug_capture")
        reserved = 0
        released: list[tuple[int, int]] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer("127.0.0.2", 40000, 96, fmt),
            right=sip_rtp_bridge.RtpPeer("127.0.0.3", 41000, 96, fmt),
            left_port=42000,
            right_port=42002,
            debug_capture=True,
            capture_name="bounded-relay-debug",
            on_release=released.append,
        )
        relay._capture_buffers["left"].extend(b"audio")
        try:
            for _index in range(
                capture_limits.DEBUG_CAPTURE_MAX_PENDING_WRITES
            ):
                self.assertTrue(capture_limits.try_reserve_debug_capture_write())
                reserved += 1
            await relay.stop()
        finally:
            for _index in range(reserved):
                capture_limits.release_debug_capture_write()

        self.assertEqual(relay.debug_capture_dropped_writes, 1)
        self.assertEqual(relay._capture_snapshot, {})
        self.assertEqual(released, [(42000, 42002)])

    async def test_rtp_relay_partial_bind_failure_releases_first_socket(self) -> None:
        first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        first.bind(("127.0.0.1", 0))
        blocker.bind(("127.0.0.1", 0))
        left_port = first.getsockname()[1]
        right_port = blocker.getsockname()[1]
        first.close()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer("127.0.0.2", 40000, 96, fmt),
            right=sip_rtp_bridge.RtpPeer("127.0.0.3", 41000, 96, fmt),
            left_port=left_port,
            right_port=right_port,
        )
        try:
            with self.assertRaises(OSError):
                await relay.start()
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.bind(("0.0.0.0", left_port))
            finally:
                probe.close()
        finally:
            blocker.close()

    def test_incompatible_invite_200_is_acked_then_closed_with_bye(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._invite_cseq = 7
        response = sip.parse_message(
            sip.build_response(
                200,
                "OK",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest"),
                    ("From", "<sip:HA@127.0.0.1>;tag=local"),
                    ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                    ("Contact", '"ESP handset" <sip:dialog@127.0.0.2:5090;transport=udp>'),
                    ("Call-ID", client.dialog_ids.call_id),
                    ("CSeq", "7 INVITE"),
                ],
                b"",
            )
        )
        compatible = client._commit_200_ok(
            response,
            "ESP",
            "127.0.0.2",
            5060,
            "sip:ESP@127.0.0.2:5060",
            "sip:HA@127.0.0.1:5060",
            "sip:ESP@127.0.0.2:5060",
        )

        self.assertFalse(compatible)
        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        methods = [message.method for message in messages]
        self.assertEqual(methods, ["ACK", "BYE"])
        self.assertEqual([message.uri for message in messages], ["sip:dialog@127.0.0.2:5090;transport=udp"] * 2)
        self.assertEqual(
            [message.header("To") for message in messages],
            ["<sip:ESP@127.0.0.2:5060>;tag=remote"] * 2,
        )

    def test_invite_200_does_not_select_one_target_from_contact_list(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._invite_cseq = 7
        request_uri = "sip:ESP@127.0.0.2:5060"
        response = sip.parse_message(
            sip.build_response(
                200,
                "OK",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest"),
                    ("From", "<sip:HA@127.0.0.1>;tag=local"),
                    ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                    (
                        "Contact",
                        "<sip:first@127.0.0.2:5090>, <sip:second@127.0.0.3:5090>",
                    ),
                    ("Call-ID", client.dialog_ids.call_id),
                    ("CSeq", "7 INVITE"),
                ],
                b"",
            )
        )

        self.assertFalse(
            client._commit_200_ok(
                response,
                "ESP",
                "127.0.0.2",
                5060,
                request_uri,
                "sip:HA@127.0.0.1:5060",
                request_uri,
            )
        )

        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertEqual([message.method for message in messages], ["ACK", "BYE"])
        self.assertEqual([message.uri for message in messages], [request_uri] * 2)

    def test_invite_200_commits_directional_audio_and_dtmf_payloads(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_send_formats=[audio],
            supported_recv_formats=[audio],
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._invite_cseq = 7
        client._local_sdp_body = sdp.build_offer_directional(
            "127.0.0.1",
            "127.0.0.1",
            41000,
            [audio],
            [audio],
        )
        offered_audio = sdp.offered_pcm_formats(client._local_sdp_body)[0]
        offered_dtmf = sdp.offered_dtmf_formats(client._local_sdp_body)[0]
        answer = (
            "v=0\r\no=- 2 1 IN IP4 127.0.0.2\r\n"
            "s=answer\r\nc=IN IP4 127.0.0.2\r\nt=0 0\r\n"
            "m=audio 42000 RTP/AVP 120 121\r\n"
            "a=rtpmap:120 L16/16000/1\r\n"
            "a=rtpmap:121 telephone-event/8000\r\n"
            "a=fmtp:121 0-16\r\na=ptime:20\r\na=sendrecv\r\n"
        ).encode()
        response = sip.parse_message(
            sip.build_response(
                200,
                "OK",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest"),
                    ("From", "<sip:HA@127.0.0.1>;tag=local"),
                    ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                    ("Contact", "<sip:ESP@127.0.0.2:5060>"),
                    ("Call-ID", client.dialog_ids.call_id),
                    ("CSeq", "7 INVITE"),
                    ("Content-Type", "application/sdp"),
                ],
                answer,
            )
        )

        self.assertTrue(
            client._commit_200_ok(
                response,
                "ESP",
                "127.0.0.2",
                5060,
                "sip:ESP@127.0.0.2:5060",
                "sip:HA@127.0.0.1:5060",
                "sip:ESP@127.0.0.2:5060",
            )
        )

        self.assertIsNotNone(client.dialog)
        assert client.dialog is not None
        self.assertEqual(client.dialog.send_format.payload_type, 120)
        self.assertEqual(
            client.dialog.recv_format.payload_type,
            offered_audio.payload_type,
        )
        self.assertEqual(client.dialog.dtmf_payload_type, offered_dtmf.payload_type)
        self.assertEqual(
            [sip.parse_message(raw).method for raw, _addr in transport.sent],
            ["ACK"],
        )

    def test_invite_200_routes_ack_and_bye_through_reversed_record_route(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._invite_cseq = 7
        client._local_sdp_body = sdp.build_offer(
            "127.0.0.1",
            "127.0.0.1",
            41000,
            [pcm],
        )
        offered = sdp.offered_pcm_formats(client._local_sdp_body)[0]
        answer = sdp.build_answer_directional(
            "127.0.0.2",
            "127.0.0.2",
            42000,
            offered,
            offered,
        ).encode()
        response = sip.parse_message(
            sip.build_response(
                200,
                "OK",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest"),
                    ("From", "<sip:HA@127.0.0.1>;tag=local"),
                    ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                    ("Contact", "<sip:dialog@127.0.0.2:5090>"),
                    ("Record-Route", "<sip:core@127.0.0.3:5070;lr>"),
                    ("Record-Route", "<sip:edge@127.0.0.4:5080;lr>"),
                    ("Call-ID", client.dialog_ids.call_id),
                    ("CSeq", "7 INVITE"),
                    ("Content-Type", "application/sdp"),
                ],
                answer,
            )
        )

        self.assertTrue(
            client._commit_200_ok(
                response,
                "ESP",
                "127.0.0.2",
                5060,
                "sip:ESP@127.0.0.2:5060",
                "sip:HA@127.0.0.1:5060",
                "sip:ESP@127.0.0.2:5060",
            )
        )
        self.assertIsNotNone(client.dialog)
        assert client.dialog is not None
        self.assertEqual(
            client.dialog.route_set,
            (
                "<sip:edge@127.0.0.4:5080;lr>",
                "<sip:core@127.0.0.3:5070;lr>",
            ),
        )
        self.assertTrue(client.bye())

        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertEqual([message.method for message in messages], ["ACK", "BYE"])
        self.assertEqual(
            [message.uri for message in messages],
            ["sip:dialog@127.0.0.2:5090"] * 2,
        )
        self.assertEqual(
            [message.header_values("Route") for message in messages],
            [
                [
                    "<sip:edge@127.0.0.4:5080;lr>",
                    "<sip:core@127.0.0.3:5070;lr>",
                ],
            ]
            * 2,
        )
        self.assertEqual(
            [addr for _raw, addr in transport.sent],
            [("127.0.0.4", 5080)] * 2,
        )

    async def test_sip_tcp_reader_rejects_oversized_header(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"OPTIONS sip:ha SIP/2.0\r\nX-Fill: " + b"x" * sip.MAX_SIP_MESSAGE_BYTES + b"\r\n\r\n")
        reader.feed_eof()
        self.assertIsNone(await sip_tcp_io.read_sip_stream_message(reader))

    async def test_sip_tcp_reader_expires_idle_and_partial_frames(self) -> None:
        idle = asyncio.StreamReader()
        self.assertIsNone(
            await sip_tcp_io.read_sip_stream_message(
                idle,
                first_byte_timeout=0.005,
                frame_timeout=0.02,
            )
        )

        partial = asyncio.StreamReader()
        partial.feed_data(b"I")
        self.assertIsNone(
            await sip_tcp_io.read_sip_stream_message(
                partial,
                first_byte_timeout=None,
                frame_timeout=0.005,
            )
        )

    async def test_sip_tcp_reader_rejects_oversized_combined_record_without_waiting_for_body(self) -> None:
        reader = asyncio.StreamReader()
        padding = b"x" * (sip.MAX_SIP_MESSAGE_BYTES - sip.MAX_SIP_BODY_BYTES)
        reader.feed_data(
            b"OPTIONS sip:ha SIP/2.0\r\nContent-Length: "
            + str(sip.MAX_SIP_BODY_BYTES).encode()
            + b"\r\nX-Fill: "
            + padding
            + b"\r\n\r\n"
        )
        self.assertIsNone(await asyncio.wait_for(sip_tcp_io.read_sip_stream_message(reader), timeout=0.1))

    async def test_sip_tcp_reader_rejects_ambiguous_content_length(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(
            b"OPTIONS sip:ha SIP/2.0\r\nContent-Length: 0\r\nContent-Length: 1\r\n\r\n"
        )
        reader.feed_eof()
        self.assertIsNone(await sip_tcp_io.read_sip_stream_message(reader))

    async def test_sip_tcp_reader_accepts_compact_content_length(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"OPTIONS sip:ha SIP/2.0\r\nl: 4\r\n\r\ntest")
        reader.feed_eof()
        self.assertEqual(
            await sip_tcp_io.read_sip_stream_message(reader),
            b"OPTIONS sip:ha SIP/2.0\r\nl: 4\r\n\r\ntest",
        )

    async def test_cancelled_tcp_send_cannot_enqueue_later(self) -> None:
        class BlockingWriter:
            def __init__(self) -> None:
                self.release = asyncio.Event()

            def is_closing(self) -> bool:
                return False

            def write(self, _data: bytes) -> None:
                pass

            async def drain(self) -> None:
                await self.release.wait()

        stream = BlockingWriter()
        writer = sip_tcp_io.SipTcpWriter(stream, label="test", max_queue=1)
        self.assertTrue(writer.send_nowait(b"in-flight"))
        await asyncio.sleep(0)
        self.assertTrue(writer.send_nowait(b"queued"))

        blocked_send = asyncio.create_task(writer.send(b"stale"))
        await asyncio.sleep(0)
        blocked_send.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await blocked_send

        self.assertEqual(writer.queue.get_nowait(), b"queued")
        await asyncio.sleep(0)
        self.assertTrue(writer.queue.empty())
        stream.release.set()
        await writer.close()

    async def test_tcp_send_unblocks_when_writer_task_dies_with_full_queue(self) -> None:
        class FailingWriter:
            def __init__(self) -> None:
                self.release = asyncio.Event()
                self.writes: list[bytes] = []

            def is_closing(self) -> bool:
                return False

            def write(self, data: bytes) -> None:
                self.writes.append(data)

            async def drain(self) -> None:
                await self.release.wait()
                raise OSError("connection lost")

        stream = FailingWriter()
        writer = sip_tcp_io.SipTcpWriter(stream, label="test", max_queue=1)
        self.assertTrue(writer.send_nowait(b"first"))
        await asyncio.sleep(0)
        self.assertTrue(writer.send_nowait(b"second"))
        blocked_send = asyncio.create_task(writer.send(b"third"))
        await asyncio.sleep(0)

        stream.release.set()
        self.assertFalse(await asyncio.wait_for(blocked_send, timeout=0.2))
        self.assertTrue(writer.task.done())

    async def test_client_ignores_response_for_another_call_id(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        right_call_id = client.dialog_ids.call_id

        def response(call_id: str) -> bytes:
            return sip.build_response(
                180,
                "Ringing",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest"),
                    ("From", "<sip:HA@127.0.0.1>;tag=local"),
                    ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                    ("Call-ID", call_id),
                    ("CSeq", "1 INVITE"),
                ],
                b"",
            )

        client.queue.put_nowait((response("stale-call"), ("127.0.0.2", 5060)))
        client.queue.put_nowait((response(right_call_id), ("127.0.0.2", 5060)))
        received = await client._read_response(0.1)
        self.assertIsNotNone(received)
        assert received is not None
        self.assertEqual(received[0].header("Call-ID"), right_call_id)

    async def test_trunk_registration_filters_call_id_method_and_cseq(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="udp",
            server="127.0.0.1",
            port=5060,
            domain="127.0.0.1",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(config=config, local_ip="127.0.0.1", local_sip_port=5060)

        def response(call_id: str, cseq: str) -> sip.SipMessage:
            return sip.parse_message(
                sip.build_response(
                    200,
                    "OK",
                    [
                        ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest"),
                        ("From", "<sip:ha@127.0.0.1>;tag=local"),
                        ("To", "<sip:ha@127.0.0.1>;tag=remote"),
                        ("Call-ID", call_id),
                        ("CSeq", cseq),
                    ],
                    b"",
                )
            )

        trunk.responses.put_nowait(response("other", "2 REGISTER"))
        trunk.responses.put_nowait(response(trunk.call_id, "2 INVITE"))
        trunk.responses.put_nowait(response(trunk.call_id, "1 REGISTER"))
        provisional = sip.parse_message(
            sip.build_response(
                100,
                "Trying",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest"),
                    ("From", "<sip:ha@127.0.0.1>;tag=local"),
                    ("To", "<sip:ha@127.0.0.1>;tag=remote"),
                    ("Call-ID", trunk.call_id),
                    ("CSeq", "2 REGISTER"),
                ],
                b"",
            )
        )
        trunk.responses.put_nowait(provisional)
        trunk.responses.put_nowait(response(trunk.call_id, "2 REGISTER"))
        received = await trunk._read_response(0.1, expected_cseq=2)
        self.assertEqual(received.header("CSeq"), "2 REGISTER")

    def test_trunk_outbound_proxy_uri_selects_host_and_port(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="tcp",
            server="pbx.example",
            port=5060,
            domain="pbx.example",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
            outbound_proxy="sip:proxy.example:5070;transport=tcp",
        )
        trunk = sip_trunk.SipTrunkClient(config=config, local_ip="127.0.0.1", local_sip_port=5060)

        self.assertEqual(trunk.registrar_target, ("proxy.example", 5070))

    async def test_udp_trunk_drops_packets_outside_resolved_proxy_hosts(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="udp",
            server="pbx.example",
            port=5060,
            domain="pbx.example",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(
            config=config,
            local_ip="127.0.0.1",
            local_sip_port=5060,
        )
        trunk._trusted_udp_hosts = frozenset({"192.0.2.10"})
        handled: list[tuple[str, int]] = []
        received = asyncio.Event()

        async def handler(_raw: bytes, addr: tuple[str, int]) -> None:
            handled.append(addr)
            received.set()

        trunk.set_request_handler(handler)
        raw = sip.build_request(
            "OPTIONS",
            "sip:ha@pbx.example",
            [
                ("Via", "SIP/2.0/UDP pbx.example;branch=z9hG4bKsource"),
                ("From", "<sip:pbx@pbx.example>;tag=remote"),
                ("To", "<sip:ha@pbx.example>"),
                ("Call-ID", "udp-source-policy"),
                ("CSeq", "1 OPTIONS"),
            ],
            b"",
        )
        task = asyncio.create_task(trunk._receive_loop())
        try:
            trunk.queue.put_nowait((raw, ("198.51.100.66", 5060)))
            trunk.queue.put_nowait((raw, ("192.0.2.10", 5099)))
            await asyncio.wait_for(received.wait(), timeout=1)
            await asyncio.sleep(0)
            self.assertEqual(handled, [("192.0.2.10", 5099)])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def test_video_trunk_endpoint_rejects_missing_media_update_handler(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="tcp",
            server="pbx.example",
            port=5060,
            domain="pbx.example",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(
            config=config,
            local_ip="127.0.0.1",
            local_sip_port=5060,
        )
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        manager = types.SimpleNamespace(
            local_ip="127.0.0.1",
            port=5060,
            local_rtp_port=41000,
            supported_formats=[audio],
            supported_send_formats=[audio],
            supported_recv_formats=[audio],
            on_invite=lambda _invite: None,
            on_terminated=None,
            on_media_update=None,
            enable_video=True,
            enable_video_transcoding=False,
            prefer_browser_video_send=True,
        )

        with self.assertRaisesRegex(ValueError, "media-update handler"):
            trunk.attach_endpoint_manager(manager)

        self.assertIsNone(trunk.inbound_endpoint)

    def test_trunk_inbound_endpoint_inherits_video_policy(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="tcp",
            server="pbx.example",
            port=5060,
            domain="pbx.example",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(
            config=config,
            local_ip="127.0.0.1",
            local_sip_port=5060,
        )
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        manager = types.SimpleNamespace(
            local_ip="127.0.0.1",
            port=5060,
            local_rtp_port=41000,
            supported_formats=[audio],
            supported_send_formats=[audio],
            supported_recv_formats=[audio],
            on_invite=lambda _invite: None,
            on_terminated=None,
            on_media_update=lambda _old, _new, _method: None,
            enable_video=True,
            enable_video_transcoding=True,
            prefer_browser_video_send=True,
        )

        trunk.attach_endpoint_manager(manager)

        endpoint = trunk.inbound_endpoint
        self.assertIsNotNone(endpoint)
        assert endpoint is not None
        self.assertTrue(endpoint.enable_video)
        self.assertTrue(endpoint.enable_video_transcoding)
        self.assertTrue(endpoint.prefer_browser_video_send)
        self.assertIs(endpoint.on_media_update, manager.on_media_update)
        self.assertEqual(endpoint.signaling_transport, "TCP")
        self.assertTrue(endpoint.trusted_trunk)
        request = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:ha@127.0.0.1:5060",
                [
                    ("Via", "SIP/2.0/TCP 192.0.2.10:5060;branch=z9hG4bKtrunk"),
                    ("From", "<sip:caller@pbx.example>;tag=remote"),
                    ("To", "<sip:ha@127.0.0.1>"),
                    ("Call-ID", "trusted-trunk-call"),
                    ("CSeq", "1 INVITE"),
                    ("Content-Type", "application/sdp"),
                ],
                sdp.build_offer(
                    "192.0.2.10",
                    "192.0.2.10",
                    42000,
                    [audio],
                ).encode(),
            )
        )
        parsed_invite = endpoint._parse_invite(request, ("192.0.2.10", 5060))
        self.assertIsNotNone(parsed_invite)
        assert parsed_invite is not None
        self.assertTrue(parsed_invite.received_via_trunk)
        self.assertEqual(parsed_invite.signaling_transport, "TCP")

    def test_trunk_refresh_precedes_short_granted_registration_expiry(self) -> None:
        self.assertEqual(sip_trunk._registration_refresh_delay(300, 1020.0, 1000.0), 10.0)
        self.assertEqual(sip_trunk._registration_refresh_delay(300, 1005.0, 1000.0), 1.0)
        self.assertEqual(sip_trunk._registration_refresh_delay(300, 1300.0, 1000.0), 240.0)

    async def test_confirmed_dialog_accepts_proxy_bye_without_ending_on_cancel(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1", local_name="HA", local_sip_port=5060, local_rtp_port=41000
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client.dialog = types.SimpleNamespace(remote_host="127.0.0.2")  # type: ignore[assignment]
        client.dialog_ids.remote_tag = "remote"
        call_id = client.dialog_ids.call_id

        def request(method: str, cseq: int) -> bytes:
            return sip.build_request(
                method,
                "sip:HA@127.0.0.1:5060",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bKdialog"),
                    ("From", "<sip:ESP@127.0.0.2>;tag=remote"),
                    ("To", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                    ("Call-ID", call_id),
                    ("CSeq", f"{cseq} {method}"),
                ],
                b"",
            )

        client.queue.put_nowait((request("CANCEL", 1), ("127.0.0.2", 5060)))
        # RFC 3261 dialog identity is Call-ID plus the two tags.  A proxy/SBC
        # may originate a sequential request from a different signaling IP.
        client.queue.put_nowait((request("BYE", 2), ("127.0.0.99", 5060)))
        self.assertEqual(await client.wait_for_dialog_termination(timeout=0.1), "remote_hangup")
        self.assertEqual(
            [sip.parse_message(raw).status_code for raw, _addr in transport.sent],
            [481, 200],
        )

    async def test_confirmed_dialog_rejects_reinvite_but_keeps_call_alive(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1", local_name="HA", local_sip_port=5060, local_rtp_port=41000
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client.dialog = types.SimpleNamespace(
            remote_host="127.0.0.2",
            remote_sip_port=5060,
            remote_target_uri="sip:ESP@127.0.0.2:5060",
        )  # type: ignore[assignment]
        client.dialog_ids.remote_tag = "remote"

        def request(method: str, cseq: int, *, remote_tag: str = "remote") -> bytes:
            return sip.build_request(
                method,
                "sip:HA@127.0.0.1:5060",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bKdialog"),
                    ("From", f"<sip:ESP@127.0.0.2>;tag={remote_tag}"),
                    ("To", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                    ("Call-ID", client.dialog_ids.call_id),
                    ("CSeq", f"{cseq} {method}"),
                    ("Content-Type", "application/sdp"),
                ],
                b"v=0\r\na=sendonly\r\n",
            )

        client.queue.put_nowait((request("INVITE", 2, remote_tag="wrong"), ("127.0.0.2", 5060)))
        client.queue.put_nowait((request("INVITE", 3), ("127.0.0.2", 5060)))
        client.queue.put_nowait((request("ACK", 3), ("127.0.0.2", 5060)))
        client.queue.put_nowait((request("BYE", 4), ("127.0.0.2", 5060)))

        self.assertEqual(await client.wait_for_dialog_termination(timeout=0.1), "remote_hangup")
        responses = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertEqual([response.status_code for response in responses], [481, 488, 200])
        self.assertIn("Session renegotiation is not supported", responses[1].header("Warning"))

    async def test_confirmed_dialog_commits_remote_update_once_after_200(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        negotiated = sdp.audio_format_to_rtp(pcm, 96)
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client.dialog_ids.remote_tag = "remote"
        initial_local_sdp = sdp.rewrite_sdp_origin(
            sdp.build_answer_directional(
                "127.0.0.1",
                "127.0.0.1",
                41000,
                negotiated,
                negotiated,
            ),
            4242,
            0,
        )
        client.dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="127.0.0.2",
            remote_sip_port=5060,
            remote_rtp_host="127.0.0.2",
            remote_rtp_port=42000,
            local_rtp_port=41000,
            call_id=client.dialog_ids.call_id,
            local_uri="sip:HA@127.0.0.1:5060",
            remote_uri="sip:ESP@127.0.0.2:5060",
            send_format=negotiated,
            recv_format=negotiated,
            remote_target_uri="sip:ESP@127.0.0.2:5060",
            local_sdp_session_id=4242,
            local_sdp_session_version=0,
            local_sdp_body=initial_local_sdp,
        )
        prepared: list[int] = []
        committed: list[int] = []

        async def on_media_update(_previous, updated, method):
            self.assertEqual(method, "UPDATE")
            prepared.append(updated.remote_rtp_port)

            async def commit() -> None:
                committed.append(updated.remote_rtp_port)

            return commit

        client.on_media_update = on_media_update

        def request(method: str, cseq: int, branch: str, body: bytes = b"") -> bytes:
            headers = [
                ("Via", f"SIP/2.0/UDP 127.0.0.2:5060;branch={branch}"),
                ("From", "<sip:ESP@127.0.0.2>;tag=remote"),
                ("To", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{cseq} {method}"),
            ]
            if body:
                headers.append(("Content-Type", "application/sdp"))
            return sip.build_request(
                method,
                "sip:HA@127.0.0.1:5060",
                headers,
                body,
            )

        offer = sdp.build_offer_directional(
            "127.0.0.2",
            "127.0.0.2",
            43000,
            [pcm],
            [pcm],
            audio_direction="sendonly",
        ).encode()
        update = request("UPDATE", 2, "z9hG4bKupdate", offer)
        client.queue.put_nowait((update, ("127.0.0.2", 5060)))
        client.queue.put_nowait((update, ("127.0.0.2", 5060)))
        client.queue.put_nowait(
            (request("OPTIONS", 3, "z9hG4bKoptions"), ("127.0.0.2", 5060))
        )
        # A delayed UDP retransmission of the older UPDATE remains the same
        # transaction even after a newer in-dialog request has completed.
        client.queue.put_nowait((update, ("127.0.0.3", 5060)))
        client.queue.put_nowait(
            (request("BYE", 3, "z9hG4bKstale-bye"), ("127.0.0.2", 5060))
        )
        client.queue.put_nowait(
            (request("BYE", 4, "z9hG4bKbye"), ("127.0.0.2", 5060))
        )

        self.assertEqual(
            await client.wait_for_dialog_termination(timeout=0.1),
            "remote_hangup",
        )
        self.assertEqual(prepared, [43000])
        self.assertEqual(committed, [43000])
        responses = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertEqual(
            [response.status_code for response in responses],
            [200, 200, 200, 200, 500, 200],
        )
        self.assertIn(b"m=audio 41000", responses[0].body)
        self.assertIn(b"o=- 4242 1 IN IP4 127.0.0.1", responses[0].body)
        self.assertEqual(responses[0].body, responses[1].body)
        self.assertEqual(responses[0].body, responses[3].body)
        self.assertEqual(responses[4].header("Retry-After"), "1")

    async def test_remote_reinvite_2xx_retransmits_over_tcp_until_ack(self) -> None:
        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        negotiated = sdp.audio_format_to_rtp(pcm, 96)
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
            signaling_transport="TCP",
        )
        sent: list[bytes] = []
        incoming: asyncio.Queue[bytes] = asyncio.Queue()
        client.use_reused_tcp_connection(
            send=lambda raw: sent.append(raw) is None,
            responses=incoming,
            close=lambda: None,
        )
        client._pending_remote_host = "127.0.0.2"
        client._pending_remote_sip_port = 5060
        client.dialog_ids.remote_tag = "remote"
        client.dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="127.0.0.2",
            remote_sip_port=5060,
            remote_rtp_host="127.0.0.2",
            remote_rtp_port=42000,
            local_rtp_port=41000,
            call_id=client.dialog_ids.call_id,
            local_uri="sip:HA@127.0.0.1:5060",
            remote_uri="sip:ESP@127.0.0.2:5060",
            send_format=negotiated,
            recv_format=negotiated,
            remote_target_uri="sip:ESP@127.0.0.2:5060",
        )

        def request(method: str, cseq: int, branch: str, body: bytes = b"") -> bytes:
            headers = [
                ("Via", f"SIP/2.0/TCP 127.0.0.2:5060;branch={branch}"),
                ("From", "<sip:ESP@127.0.0.2>;tag=remote"),
                ("To", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{cseq} {method}"),
            ]
            if body:
                headers.append(("Content-Type", "application/sdp"))
            return sip.build_request(method, "sip:HA@127.0.0.1:5060", headers, body)

        offer = sdp.build_offer_directional(
            "127.0.0.2",
            "127.0.0.2",
            42000,
            [pcm],
            [pcm],
        ).encode()
        with (
            patch.object(sip_client, "SIP_T1", 0.002),
            patch.object(sip_client, "SIP_T2", 0.004),
            patch.object(sip_client, "SIP_TIMER_B", 0.1),
        ):
            waiter = asyncio.create_task(client.wait_for_dialog_termination(timeout=0.2))
            incoming.put_nowait(request("INVITE", 2, "z9hG4bKreinvite", offer))
            for _ in range(20):
                invite_responses = [
                    message
                    for raw in sent
                    if (message := sip.parse_message(raw)).is_response
                    and message.header("CSeq") == "2 INVITE"
                    and message.status_code == 200
                ]
                if len(invite_responses) >= 2:
                    break
                await asyncio.sleep(0.002)
            incoming.put_nowait(request("ACK", 2, "z9hG4bKack"))
            incoming.put_nowait(request("BYE", 3, "z9hG4bKbye"))
            self.assertEqual(await waiter, "remote_hangup")

        invite_responses = [
            message
            for raw in sent
            if (message := sip.parse_message(raw)).is_response
            and message.header("CSeq") == "2 INVITE"
            and message.status_code == 200
        ]
        self.assertGreaterEqual(len(invite_responses), 2)
        self.assertEqual(client.snapshot()["pending_remote_invite_ack"], 0)
        self.assertGreaterEqual(client.snapshot()["remote_invite_2xx_retransmissions"], 1)
        await client.close()

    async def test_remote_reinvite_ack_timeout_sends_bye_and_ends_dialog(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

            def close(self) -> None:
                return

        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        negotiated = sdp.audio_format_to_rtp(pcm, 96)
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client.dialog_ids.remote_tag = "remote"
        client.dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="127.0.0.2",
            remote_sip_port=5060,
            remote_rtp_host="127.0.0.2",
            remote_rtp_port=42000,
            local_rtp_port=41000,
            call_id=client.dialog_ids.call_id,
            local_uri="sip:HA@127.0.0.1:5060",
            remote_uri="sip:ESP@127.0.0.2:5060",
            send_format=negotiated,
            recv_format=negotiated,
            remote_target_uri="sip:ESP@127.0.0.2:5060",
        )
        offer = sdp.build_offer_directional(
            "127.0.0.2",
            "127.0.0.2",
            42000,
            [pcm],
            [pcm],
        ).encode()
        reinvite = sip.build_request(
            "INVITE",
            "sip:HA@127.0.0.1:5060",
            [
                ("Via", "SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bKtimeout"),
                ("From", "<sip:ESP@127.0.0.2>;tag=remote"),
                ("To", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", "2 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )
        client.queue.put_nowait((reinvite, ("127.0.0.2", 5060)))
        with (
            patch.object(sip_client, "SIP_T1", 0.001),
            patch.object(sip_client, "SIP_T2", 0.002),
            patch.object(sip_client, "SIP_TIMER_B", 0.006),
        ):
            self.assertEqual(
                await client.wait_for_dialog_termination(timeout=0.1),
                "ack_timeout",
            )

        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertGreaterEqual(
            sum(message.status_code == 200 for message in messages if message.is_response),
            2,
        )
        self.assertTrue(any(message.method == "BYE" for message in messages))
        self.assertIsNone(client.dialog)
        self.assertEqual(client.snapshot()["pending_remote_invite_ack"], 0)
        await client.close()

    async def test_confirmed_dialog_reacks_retransmitted_invite_2xx(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        fmt = sdp.audio_format_to_rtp(audio_format.AudioFormat(16000, "s16le", 1, 20), 96)
        client.dialog_ids.remote_tag = "remote"
        client.dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="127.0.0.2",
            remote_sip_port=5060,
            remote_rtp_host="127.0.0.2",
            remote_rtp_port=42000,
            local_rtp_port=41000,
            call_id=client.dialog_ids.call_id,
            local_uri="sip:HA@127.0.0.1:5060",
            remote_uri="sip:ESP@127.0.0.2:5060",
            send_format=fmt,
            recv_format=fmt,
            remote_target_uri="sip:ESP@127.0.0.2:5060",
        )
        duplicate_ok = sip.build_response(
            200,
            "OK",
            [
                ("Via", f"SIP/2.0/UDP 127.0.0.1:5060;branch={client.dialog_ids.branch}"),
                ("From", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{client._invite_cseq} INVITE"),
            ],
        )
        bye = sip.build_request(
            "BYE",
            "sip:HA@127.0.0.1:5060",
            [
                ("Via", "SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bKbye"),
                ("Via", "SIP/2.0/UDP 127.0.0.3:5060;branch=z9hG4bKproxy"),
                ("From", "<sip:ESP@127.0.0.2>;tag=remote"),
                ("To", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", "2 BYE"),
            ],
        )
        client.queue.put_nowait((duplicate_ok, ("127.0.0.2", 5060)))
        client.queue.put_nowait((bye, ("127.0.0.2", 5060)))

        self.assertEqual(await client.wait_for_dialog_termination(timeout=0.1), "remote_hangup")
        self.assertEqual(sip.parse_message(transport.sent[0][0]).method, "ACK")
        bye_response = sip.parse_message(transport.sent[1][0])
        self.assertEqual(bye_response.status_code, 200)
        self.assertEqual(len(bye_response.header_values("Via")), 2)

    async def test_cancelled_invite_final_response_is_acked(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1", local_name="HA", local_sip_port=5060, local_rtp_port=41000
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._pending_remote_host = "127.0.0.2"
        client._pending_remote_sip_port = 5060
        client._pending_request_uri = "sip:ESP@127.0.0.2:5060"
        client._pending_local_uri = "sip:HA@127.0.0.1:5060"
        client._pending_remote_uri = "sip:ESP@127.0.0.2:5060"
        client._invite_transaction_active = True
        client._received_provisional = True
        call_id = client.dialog_ids.call_id
        client.queue.put_nowait(
            (
                sip.build_response(
                    200,
                    "OK",
                    [
                        ("Via", f"SIP/2.0/UDP 127.0.0.1:5060;branch={client.dialog_ids.branch}"),
                        ("From", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                        ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                        ("Call-ID", call_id),
                        ("CSeq", f"{client._invite_cseq} CANCEL"),
                    ],
                    b"",
                ),
                ("127.0.0.2", 5060),
            )
        )
        client.queue.put_nowait(
            (
                sip.build_response(
                    487,
                    "Request Terminated",
                    [
                        ("Via", f"SIP/2.0/UDP 127.0.0.1:5060;branch={client.dialog_ids.branch}"),
                        ("From", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                        ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                        ("Call-ID", call_id),
                        ("CSeq", f"{client._invite_cseq} INVITE"),
                    ],
                    b"",
                ),
                ("127.0.0.2", 5060),
            )
        )

        self.assertEqual(await client.terminate(timeout=0.1), "cancelled")
        self.assertEqual([sip.parse_message(raw).method for raw, _addr in transport.sent], ["CANCEL", "ACK"])

    async def test_cancelled_invite_final_response_does_not_wait_for_cancel_ok(
        self,
    ) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._pending_remote_host = "127.0.0.2"
        client._pending_remote_sip_port = 5060
        client._pending_request_uri = "sip:ESP@127.0.0.2:5060"
        client._pending_local_uri = "sip:HA@127.0.0.1:5060"
        client._pending_remote_uri = "sip:ESP@127.0.0.2:5060"
        client._invite_transaction_active = True
        client._received_provisional = True
        read_count = 0

        async def read_response(_timeout: float):
            nonlocal read_count
            read_count += 1
            if read_count > 1:
                await asyncio.Future()
            return (
                sip.parse_message(
                    sip.build_response(
                        487,
                        "Request Terminated",
                        [
                            (
                                "Via",
                                "SIP/2.0/UDP 127.0.0.1:5060;branch="
                                f"{client.dialog_ids.branch}",
                            ),
                            (
                                "From",
                                "<sip:HA@127.0.0.1>;tag="
                                f"{client.dialog_ids.local_tag}",
                            ),
                            ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                            ("Call-ID", client.dialog_ids.call_id),
                            ("CSeq", f"{client._invite_cseq} INVITE"),
                        ],
                    )
                ),
                ("127.0.0.2", 5060),
            )

        client._read_response = read_response  # type: ignore[method-assign]

        self.assertEqual(
            await asyncio.wait_for(client.terminate(timeout=10), timeout=0.1),
            "cancelled",
        )
        self.assertEqual(read_count, 1)
        self.assertEqual(
            [sip.parse_message(raw).method for raw, _addr in transport.sent],
            ["CANCEL", "ACK"],
        )

    async def test_cancel_awaits_existing_final_response_owner(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._pending_remote_host = "127.0.0.2"
        client._pending_remote_sip_port = 5060
        client._pending_request_uri = "sip:ESP@127.0.0.2:5060"
        client._pending_local_uri = "sip:HA@127.0.0.1:5060"
        client._pending_remote_uri = "sip:ESP@127.0.0.2:5060"
        client._invite_transaction_active = True
        client._received_provisional = True
        response_ready = asyncio.Event()
        response_release = asyncio.Event()
        read_count = 0

        async def read_response(_timeout: float):
            nonlocal read_count
            read_count += 1
            response_ready.set()
            await response_release.wait()
            return (
                sip.parse_message(
                    sip.build_response(
                        487,
                        "Request Terminated",
                        [
                            (
                                "Via",
                                "SIP/2.0/UDP 127.0.0.1:5060;branch="
                                f"{client.dialog_ids.branch}",
                            ),
                            (
                                "From",
                                "<sip:HA@127.0.0.1>;tag="
                                f"{client.dialog_ids.local_tag}",
                            ),
                            ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                            ("Call-ID", client.dialog_ids.call_id),
                            ("CSeq", f"{client._invite_cseq} INVITE"),
                        ],
                    )
                ),
                ("127.0.0.2", 5060),
            )

        client._read_response = read_response  # type: ignore[method-assign]

        waiter = asyncio.create_task(client.wait_for_final(timeout=10))
        await response_ready.wait()
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        async def release_response() -> None:
            await asyncio.sleep(0)
            response_release.set()

        release = asyncio.create_task(release_response())
        self.assertEqual(
            await asyncio.wait_for(client.terminate(timeout=10), timeout=0.1),
            "cancelled",
        )
        await release
        self.assertEqual(read_count, 1)
        self.assertEqual(
            [sip.parse_message(raw).method for raw, _addr in transport.sent],
            ["CANCEL", "ACK"],
        )

    async def test_cancel_race_accepts_the_separate_bye_transaction(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1", local_name="HA", local_sip_port=5060, local_rtp_port=41000
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._pending_target = "ESP"
        client._pending_remote_host = "127.0.0.2"
        client._pending_remote_sip_port = 5060
        client._pending_request_uri = "sip:ESP@127.0.0.2:5060"
        client._pending_local_uri = "sip:HA@127.0.0.1:5060"
        client._pending_remote_uri = "sip:ESP@127.0.0.2:5060"
        client._invite_transaction_active = True
        client._received_provisional = True
        call_id = client.dialog_ids.call_id
        rtp_fmt = sdp.audio_format_to_rtp(audio_format.AudioFormat(16000, "s16le", 1, 20), 96)
        answer = sdp.build_answer_directional(
            "127.0.0.2", "127.0.0.2", 42000, rtp_fmt, rtp_fmt
        ).encode()

        def response(status: int, reason: str, method: str, cseq: int, branch: str, body: bytes = b"") -> bytes:
            headers = [
                ("Via", f"SIP/2.0/UDP 127.0.0.1:5060;branch={branch}"),
                ("From", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                ("Call-ID", call_id),
                ("CSeq", f"{cseq} {method}"),
            ]
            if body:
                headers.append(("Content-Type", "application/sdp"))
            return sip.build_response(status, reason, headers, body)

        client.queue.put_nowait((
            response(200, "OK", "INVITE", client._invite_cseq, client.dialog_ids.branch, answer),
            ("127.0.0.2", 5060),
        ))
        terminating = asyncio.create_task(client.terminate(timeout=0.5))
        while not any(sip.parse_message(raw).method == "BYE" for raw, _addr in transport.sent):
            await asyncio.sleep(0)
        client.queue.put_nowait((
            response(200, "OK", "BYE", client._bye_cseq, client._bye_branch),
            ("127.0.0.2", 5060),
        ))

        self.assertEqual(await terminating, "cancelled")
        self.assertEqual([sip.parse_message(raw).method for raw, _addr in transport.sent], ["CANCEL", "ACK", "BYE"])

    async def test_trunk_old_tcp_reader_cannot_clear_replacement(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="tcp",
            server="127.0.0.1",
            port=5060,
            domain="127.0.0.1",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(config=config, local_ip="127.0.0.1", local_sip_port=5060)
        old_reader = asyncio.StreamReader()
        new_reader = asyncio.StreamReader()

        class Writer:
            def is_closing(self) -> bool:
                return False

            def get_extra_info(self, _name: str):
                return ("127.0.0.1", 5060)

            def close(self) -> None:
                pass

        old_writer = Writer()
        new_writer = Writer()
        trunk.reader = old_reader
        trunk.writer = old_writer  # type: ignore[assignment]
        trunk._reader_ready.set()
        replacement_read = asyncio.Event()

        async def fake_read(reader):
            if reader is old_reader:
                trunk.reader = new_reader
                trunk.writer = new_writer  # type: ignore[assignment]
                return None
            replacement_read.set()
            await asyncio.Event().wait()

        original_read = sip_trunk._read_sip_stream_message
        sip_trunk._read_sip_stream_message = fake_read
        task = asyncio.create_task(trunk._receive_loop())
        try:
            await asyncio.wait_for(replacement_read.wait(), timeout=0.1)
            self.assertIs(trunk.reader, new_reader)
            self.assertIs(trunk.writer, new_writer)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            sip_trunk._read_sip_stream_message = original_read

    async def test_trunk_transport_loss_preserves_confirmed_dialog_for_new_flow(self) -> None:
        """A confirmed trunk dialog must outlive the TCP flow that carried it."""

        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="tcp",
            server="pbx.example",
            port=5060,
            domain="pbx.example",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(
            config=config,
            local_ip="127.0.0.1",
            local_sip_port=5060,
        )
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        terminated: list[tuple[str, str]] = []
        media_updates: list[tuple[int, str]] = []

        def answer(invite):
            return sdp.build_answer_directional(
                "127.0.0.1",
                "127.0.0.1",
                41000,
                invite.send_format,
                invite.recv_format,
                remote_sdp=invite.remote_sdp,
                video_port=41002,
                video_format=invite.answer_video_format,
                video_direction=sdp.local_direction_for_offer(
                    invite.video_format.direction
                    if invite.video_format is not None
                    else "inactive"
                ),
            )

        async def on_invite(invite):
            return sip_listener.SipInviteResult(
                200,
                "OK",
                answer_sdp=answer(invite),
            )

        async def on_media_update(_previous, updated, _method):
            media_updates.append(
                (
                    updated.remote_rtp_port,
                    updated.video_format.direction
                    if updated.video_format is not None
                    else "",
                )
            )
            return sip_listener.SipInviteResult(
                200,
                "OK",
                answer_sdp=answer(updated),
            )

        async def on_terminated(call_id: str, reason: str) -> None:
            terminated.append((call_id, reason))

        manager = types.SimpleNamespace(
            local_ip="127.0.0.1",
            port=5060,
            local_rtp_port=41000,
            supported_formats=[audio],
            supported_send_formats=[audio],
            supported_recv_formats=[audio],
            on_invite=on_invite,
            on_terminated=on_terminated,
            on_register=None,
            on_info=None,
            on_media_update=on_media_update,
            enable_video=True,
            enable_video_transcoding=False,
            prefer_browser_video_send=True,
        )
        trunk.attach_endpoint_manager(manager)
        endpoint = trunk.inbound_endpoint
        assert endpoint is not None

        call_id = "trunk-flow-replacement"
        remote_tag = "remote-dialog-tag"

        def invite(
            cseq: int,
            branch: str,
            remote_rtp_port: int,
            *,
            to_tag: str = "",
            source_ip: str = "192.0.2.10",
            video: bool = False,
        ) -> bytes:
            headers = [
                (
                    "Via",
                    f"SIP/2.0/TCP {source_ip}:5060;branch={branch};rport",
                ),
                ("From", f"<sip:caller@pbx.example>;tag={remote_tag}"),
                (
                    "To",
                    "<sip:ha@pbx.example>" + (f";tag={to_tag}" if to_tag else ""),
                ),
                ("Call-ID", call_id),
                ("CSeq", f"{cseq} INVITE"),
                ("Contact", f"<sip:caller@{source_ip}:5060;transport=tcp>"),
                ("Content-Type", "application/sdp"),
            ]
            body = sdp.build_offer_directional(
                source_ip,
                source_ip,
                remote_rtp_port,
                [audio],
                [audio],
                video_port=remote_rtp_port + 2 if video else None,
                video_formats=sdp.DEFAULT_VIDEO_FORMATS if video else (),
                video_direction="sendrecv" if video else "inactive",
            ).encode()
            return sip.build_request(
                "INVITE",
                "sip:ha@pbx.example",
                headers,
                body,
            )

        class StreamWriter:
            def __init__(self, peer: tuple[str, int]) -> None:
                self.peer = peer
                self.closed = False

            def is_closing(self) -> bool:
                return self.closed

            def get_extra_info(self, name: str):
                return self.peer if name == "peername" else None

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                return None

        final_response = asyncio.Event()

        class TrunkWriter:
            def __init__(self) -> None:
                self.sent: list[bytes] = []
                self.closed = False

            def send_nowait(self, raw: bytes) -> bool:
                self.sent.append(raw)
                message = sip.parse_message(raw)
                if message.status_code == 200 and message.header("CSeq") == "1 INVITE":
                    final_response.set()
                return True

            async def close(self) -> None:
                self.closed = True

        first_reader = asyncio.StreamReader()
        first_stream_writer = StreamWriter(("192.0.2.10", 5060))
        first_tx = TrunkWriter()
        trunk.reader = first_reader
        trunk.writer = first_stream_writer  # type: ignore[assignment]
        trunk._tcp_writer = first_tx  # type: ignore[assignment]
        trunk._reader_ready.set()
        first_invite = invite(1, "z9hG4bKinitial", 42000)
        read_count = 0

        async def read_first_flow(_reader):
            nonlocal read_count
            read_count += 1
            if read_count == 1:
                return first_invite
            if read_count == 2:
                await asyncio.wait_for(final_response.wait(), timeout=1)
                local_tag = sip.extract_tag(
                    next(
                        sip.parse_message(raw)
                        for raw in first_tx.sent
                        if sip.parse_message(raw).status_code == 200
                    ).header("To")
                )
                return sip.build_request(
                    "ACK",
                    "sip:ha@pbx.example",
                    [
                        (
                            "Via",
                            "SIP/2.0/TCP 192.0.2.10:5060;branch=z9hG4bKack;rport",
                        ),
                        ("From", f"<sip:caller@pbx.example>;tag={remote_tag}"),
                        ("To", f"<sip:ha@pbx.example>;tag={local_tag}"),
                        ("Call-ID", call_id),
                        ("CSeq", "1 ACK"),
                    ],
                    b"",
                )
            for _ in range(100):
                dialog = endpoint.active_dialogs.get(call_id)
                if dialog is not None and dialog.pending_ack_cseq == 0:
                    break
                await asyncio.sleep(0)
            return None

        original_read = sip_trunk._read_sip_stream_message
        sip_trunk._read_sip_stream_message = read_first_flow
        receive_task = asyncio.create_task(trunk._receive_loop())
        try:
            for _ in range(200):
                if trunk.reader is None:
                    break
                await asyncio.sleep(0)
            self.assertIsNone(trunk.reader)
            self.assertTrue(trunk._refresh_wakeup.is_set())
        finally:
            receive_task.cancel()
            await asyncio.gather(receive_task, return_exceptions=True)
            sip_trunk._read_sip_stream_message = original_read

        self.assertIn(call_id, endpoint.active_dialogs)
        self.assertEqual(terminated, [])
        local_tag = endpoint.active_dialogs[call_id].to_tag

        replacement_tx = TrunkWriter()
        trunk._tcp_writer = replacement_tx  # type: ignore[assignment]
        reinvite = invite(
            2,
            "z9hG4bKvideo",
            43000,
            to_tag=local_tag,
            source_ip="198.51.100.20",
            video=True,
        )
        await endpoint._handle_datagram(reinvite, ("198.51.100.20", 5090))
        self.assertEqual(media_updates, [(43000, "sendrecv")])
        self.assertIn(
            200,
            [sip.parse_message(raw).status_code for raw in replacement_tx.sent],
        )

        bye = sip.build_request(
            "BYE",
            "sip:ha@pbx.example",
            [
                (
                    "Via",
                    "SIP/2.0/TCP 198.51.100.20:5090;branch=z9hG4bKbye;rport",
                ),
                ("From", f"<sip:caller@pbx.example>;tag={remote_tag}"),
                ("To", f"<sip:ha@pbx.example>;tag={local_tag}"),
                ("Call-ID", call_id),
                ("CSeq", "3 BYE"),
            ],
            b"",
        )
        await endpoint._handle_datagram(bye, ("198.51.100.20", 5090))
        self.assertNotIn(call_id, endpoint.active_dialogs)
        self.assertEqual(terminated, [(call_id, "remote_hangup")])

    async def test_udp_endpoint_caps_concurrent_handler_tasks(self) -> None:
        async def on_invite(_invite):
            raise AssertionError("not used")

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="127.0.0.1",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=on_invite,
        )
        release = asyncio.Event()

        async def blocked_handler(_data, _addr):
            await release.wait()

        endpoint._handle_datagram = blocked_handler  # type: ignore[method-assign]
        for index in range(100):
            endpoint.datagram_received(b"test", ("127.0.0.1", index))
        await asyncio.sleep(0)

        self.assertEqual(len(endpoint._request_tasks), sip_listener._MAX_SIP_INVITE_TASKS)
        self.assertEqual(endpoint.dropped_datagrams, 100 - sip_listener._MAX_SIP_INVITE_TASKS)
        release.set()
        await asyncio.gather(*tuple(endpoint._request_tasks))

    async def test_udp_endpoint_reserves_control_capacity_under_invite_load(self) -> None:
        async def on_invite(_invite):
            raise AssertionError("not used")

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="127.0.0.1",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=on_invite,
        )
        release = asyncio.Event()

        async def blocked_handler(_data, _addr):
            await release.wait()

        endpoint._handle_datagram = blocked_handler  # type: ignore[method-assign]
        for index in range(100):
            endpoint.datagram_received(b"INVITE sip:test SIP/2.0\r\n\r\n", ("127.0.0.1", index))
        for index in range(8):
            endpoint.datagram_received(b"CANCEL sip:test SIP/2.0\r\n\r\n", ("127.0.0.1", index))
        await asyncio.sleep(0)

        self.assertEqual(len(endpoint._invite_tasks), sip_listener._MAX_SIP_INVITE_TASKS)
        self.assertEqual(len(endpoint._request_tasks), sip_listener._MAX_SIP_UDP_TASKS)
        release.set()
        await asyncio.gather(*tuple(endpoint._request_tasks))

    async def test_trunk_reserves_control_capacity_under_invite_load(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="udp",
            server="127.0.0.1",
            port=5060,
            domain="127.0.0.1",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(config=config, local_ip="127.0.0.1", local_sip_port=5060)
        release = asyncio.Event()

        async def blocked_handler(_data, _addr):
            await release.wait()

        trunk.request_handler = blocked_handler
        for index in range(100):
            trunk._submit_request(b"invite", ("127.0.0.1", index), "INVITE")
        for index in range(8):
            trunk._submit_request(b"cancel", ("127.0.0.1", index), "CANCEL")
        await asyncio.sleep(0)

        self.assertEqual(len(trunk._invite_tasks), sip_trunk._MAX_TRUNK_INVITE_TASKS)
        self.assertEqual(len(trunk._request_tasks), sip_trunk._MAX_TRUNK_REQUEST_TASKS)
        release.set()
        await asyncio.gather(*tuple(trunk._request_tasks))

    async def test_udp_read_response_timeout_returns_none(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            signaling_transport="UDP",
        )

        self.assertIsNone(await client._read_response(0.001))

    async def test_final_response_waiter_stops_on_transport_failure(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        client._pending_target = "P4"
        client._pending_remote_host = "192.0.2.10"
        client._pending_remote_sip_port = 5060
        client._invite_transaction_active = True
        reads = 0

        async def read_response(_timeout: float):
            nonlocal reads
            reads += 1
            raise OSError("socket closed")

        client._read_response = read_response  # type: ignore[method-assign]

        self.assertEqual(
            await client.wait_for_final(timeout=1),
            "transport_unreachable",
        )
        self.assertEqual(reads, 1)
        self.assertFalse(client._invite_transaction_active)
        self.assertEqual(client.last_sip_event, "TRANSPORT_ERROR")

    async def test_final_response_waiter_skips_one_malformed_message(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        reads = 0

        async def read_response(_timeout: float):
            nonlocal reads
            reads += 1
            if reads == 1:
                raise sip.SipError("malformed packet")
            return None

        client._read_response = read_response  # type: ignore[method-assign]

        self.assertEqual(await client.wait_for_final(timeout=1), "timeout")
        self.assertEqual(reads, 2)

    def test_invite_auth_retry_rebuilds_transaction_headers(self) -> None:
        source = (PKG_DIR / "sip_client.py").read_text()
        auth_branch = source[
            source.index("if msg.status_code in {401, 407}")
            : source.index("if msg.status_code and msg.status_code >= 300")
        ]

        self.assertIn("self.dialog_ids.branch = sip.make_branch()", auth_branch)
        self.assertIn(
            "self._pending_invite_auth_header = (auth_header, auth_value)",
            auth_branch,
        )
        self.assertIn("raw = self._build_pending_invite()", auth_branch)
        self.assertIn("transaction.restart_retransmissions()", auth_branch)

    async def test_invite_honors_retry_after_once_as_a_new_transaction(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        response_count = 0

        async def read_response(_timeout: float):
            nonlocal response_count
            response_count += 1
            status, reason = (
                (503, "Service Unavailable")
                if response_count == 1
                else (180, "Ringing")
            )
            headers = [
                (
                    "Via",
                    "SIP/2.0/UDP 192.168.1.10:5060;"
                    f"branch={client.dialog_ids.branch}",
                ),
                (
                    "From",
                    "<sip:HA@192.168.1.10:5060>;"
                    f"tag={client.dialog_ids.local_tag}",
                ),
                ("To", "<sip:P4@192.0.2.10>;tag=remote"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{client._invite_cseq} INVITE"),
            ]
            if status == 503:
                headers.append(("Retry-After", "0"))
            return (
                sip.parse_message(sip.build_response(status, reason, headers)),
                ("192.0.2.10", 5060),
            )

        client._read_response = read_response  # type: ignore[method-assign]
        with patch.object(sip_client, "_MIN_INVITE_RETRY_AFTER", 0.001):
            result = await client.invite(
                target="P4",
                remote_host="192.0.2.10",
                remote_sip_port=5060,
                request_uri="sip:P4@192.0.2.10:5060;transport=udp",
            )

        self.assertEqual(result, "ringing")
        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertEqual(
            [message.method for message in messages],
            ["INVITE", "ACK", "INVITE"],
        )
        first, _ack, second = messages
        self.assertEqual(first.header("Call-ID"), second.header("Call-ID"))
        self.assertEqual(first.header("From"), second.header("From"))
        self.assertEqual(first.header("To"), second.header("To"))
        self.assertEqual(
            sip.parse_cseq(second.header("CSeq")).number,
            sip.parse_cseq(first.header("CSeq")).number + 1,
        )
        self.assertNotEqual(
            sip.parse_via(first.header("Via")).branch,
            sip.parse_via(second.header("Via")).branch,
        )

    async def test_authenticated_retry_after_advances_digest_nonce_count(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="17770000000",
            local_sip_port=5060,
            local_rtp_port=41000,
            username="17770000000",
            auth_username="17770000000",
            password="secret",
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        response_count = 0

        async def read_response(_timeout: float):
            nonlocal response_count
            response_count += 1
            if response_count == 1:
                status, reason = 407, "Proxy Authentication Required"
            elif response_count == 2:
                status, reason = 503, "Service Unavailable"
            else:
                status, reason = 180, "Ringing"
            headers = [
                (
                    "Via",
                    "SIP/2.0/UDP 192.168.1.10:5060;"
                    f"branch={client.dialog_ids.branch}",
                ),
                (
                    "From",
                    "<sip:17770000000@192.168.1.10:5060>;"
                    f"tag={client.dialog_ids.local_tag}",
                ),
                ("To", "<sip:P4@sip.example>;tag=provider"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{client._invite_cseq} INVITE"),
            ]
            if status == 407:
                headers.append(
                    (
                        "Proxy-Authenticate",
                        'Digest realm="sip.example", nonce="nonce", qop="auth"',
                    )
                )
            if status == 503:
                headers.append(("Retry-After", "0"))
            return (
                sip.parse_message(sip.build_response(status, reason, headers)),
                ("192.0.2.10", 5060),
            )

        client._read_response = read_response  # type: ignore[method-assign]
        with patch.object(sip_client, "_MIN_INVITE_RETRY_AFTER", 0.001):
            result = await client.invite(
                target="P4",
                remote_host="192.0.2.10",
                remote_sip_port=5060,
                request_uri="sip:P4@sip.example:5060;transport=udp",
            )

        self.assertEqual(result, "ringing")
        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        invites = [message for message in messages if message.method == "INVITE"]
        self.assertEqual(len(invites), 3)
        first_auth = sip_auth.parse_digest_challenge(
            invites[1].header("Proxy-Authorization")
        )
        retry_auth = sip_auth.parse_digest_challenge(
            invites[2].header("Proxy-Authorization")
        )
        self.assertEqual(first_auth["nc"], "00000001")
        self.assertEqual(retry_auth["nc"], "00000002")
        self.assertNotEqual(first_auth["cnonce"], retry_auth["cnonce"])

    async def test_invite_retry_after_is_limited_to_one_retry(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]

        async def read_response(_timeout: float):
            return (
                sip.parse_message(
                    sip.build_response(
                        503,
                        "Service Unavailable",
                        [
                            (
                                "Via",
                                "SIP/2.0/UDP 192.168.1.10:5060;"
                                f"branch={client.dialog_ids.branch}",
                            ),
                            (
                                "From",
                                "<sip:HA@192.168.1.10:5060>;"
                                f"tag={client.dialog_ids.local_tag}",
                            ),
                            ("To", "<sip:P4@192.0.2.10>;tag=remote"),
                            ("Call-ID", client.dialog_ids.call_id),
                            ("CSeq", f"{client._invite_cseq} INVITE"),
                            ("Retry-After", "0"),
                        ],
                    )
                ),
                ("192.0.2.10", 5060),
            )

        client._read_response = read_response  # type: ignore[method-assign]
        with patch.object(sip_client, "_MIN_INVITE_RETRY_AFTER", 0.001):
            result = await client.invite(
                target="P4",
                remote_host="192.0.2.10",
                remote_sip_port=5060,
                request_uri="sip:P4@192.0.2.10:5060;transport=udp",
            )

        self.assertEqual(result, "sip_503")
        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertEqual(
            [message.method for message in messages],
            ["INVITE", "ACK", "INVITE", "ACK"],
        )

    async def test_retry_after_also_replaces_a_proceeding_invite(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        audio_rtp = sdp.audio_format_to_rtp(pcm, 96)
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        response_count = 0

        async def read_response(_timeout: float):
            nonlocal response_count
            response_count += 1
            invite = next(
                sip.parse_message(raw)
                for raw, _addr in reversed(transport.sent)
                if sip.parse_message(raw).method == "INVITE"
            )
            status, reason = (
                (180, "Ringing")
                if response_count == 1
                else (503, "Service Unavailable")
                if response_count == 2
                else (200, "OK")
            )
            headers = [
                ("Via", invite.header("Via")),
                ("From", invite.header("From")),
                ("To", "<sip:P4@192.0.2.10>;tag=remote"),
                ("Call-ID", invite.header("Call-ID")),
                ("CSeq", invite.header("CSeq")),
            ]
            body = b""
            if status == 503:
                headers.append(("Retry-After", "0"))
            elif status == 200:
                headers.extend(
                    [
                        ("Contact", "<sip:P4@192.0.2.10:5060>"),
                        ("Content-Type", "application/sdp"),
                    ]
                )
                body = sdp.build_answer_directional(
                    "192.0.2.10",
                    "192.0.2.10",
                    42000,
                    audio_rtp,
                    audio_rtp,
                    remote_sdp=invite.body,
                ).encode()
            return (
                sip.parse_message(
                    sip.build_response(status, reason, headers, body)
                ),
                ("192.0.2.10", 5060),
            )

        client._read_response = read_response  # type: ignore[method-assign]
        self.assertEqual(
            await client.invite(
                target="P4",
                remote_host="192.0.2.10",
                remote_sip_port=5060,
                request_uri="sip:P4@192.0.2.10:5060;transport=udp",
            ),
            "ringing",
        )
        with patch.object(sip_client, "_MIN_INVITE_RETRY_AFTER", 0.001):
            self.assertEqual(await client.wait_for_final(), "in_call")

        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertEqual(
            [message.method for message in messages],
            ["INVITE", "ACK", "INVITE", "ACK"],
        )

    async def test_direct_200_ok_prefers_remote_display_identity(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        pcm = audio_format.AudioFormat(16000, "s16le", 1, 20)
        audio_rtp = sdp.audio_format_to_rtp(pcm, 96)
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[pcm],
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]

        async def read_response(_timeout: float):
            invite = sip.parse_message(transport.sent[-1][0])
            answer = sdp.build_answer_directional(
                "192.0.2.10",
                "192.0.2.10",
                42000,
                audio_rtp,
                audio_rtp,
                remote_sdp=invite.body,
            ).encode()
            return (
                sip.parse_message(
                    sip.build_response(
                        200,
                        "OK",
                        [
                            ("Via", invite.header("Via")),
                            ("From", invite.header("From")),
                            (
                                "To",
                                '"Portineria" <sip:1000@sip.example>;tag=remote',
                            ),
                            ("Call-ID", invite.header("Call-ID")),
                            ("CSeq", invite.header("CSeq")),
                            ("Contact", "<sip:1000@192.0.2.10:5060>"),
                            ("Content-Type", "application/sdp"),
                        ],
                        answer,
                    )
                ),
                ("192.0.2.10", 5060),
            )

        client._read_response = read_response  # type: ignore[method-assign]
        result = await client.invite(
            target="1000",
            remote_host="192.0.2.10",
            remote_sip_port=5060,
            request_uri="sip:1000@sip.example:5060;transport=udp",
        )

        self.assertEqual(result, "in_call")
        self.assertEqual(client.connected_party, "Portineria")
        self.assertEqual(
            sip.name_addr_identity("<sip:1000@sip.example>"),
            "1000",
        )

    async def test_proxy_auth_retry_uses_trunk_identity(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="17770000000",
            local_sip_port=5060,
            local_rtp_port=41000,
            local_video_rtp_port=41002,
            video_formats=(sdp.DEFAULT_H264_FORMAT,),
            video_direction="sendrecv",
            username="17770000000",
            auth_username="17770000000",
            password="secret",
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        response_count = 0

        async def read_response(_timeout: float):
            nonlocal response_count
            response_count += 1
            status, reason = (407, "Proxy Authentication Required") if response_count == 1 else (180, "Ringing")
            headers = [
                ("Via", f"SIP/2.0/UDP 192.168.1.10:5060;branch={client.dialog_ids.branch}"),
                ("From", f"<sip:17770000000@192.168.1.10:5060>;tag={client.dialog_ids.local_tag}"),
                ("To", "<sip:+15551234567@sip.example>;tag=provider"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{client._invite_cseq} INVITE"),
            ]
            if status == 407:
                headers.append(("Proxy-Authenticate", 'Digest realm="sip.example", nonce="nonce", qop="auth"'))
            return sip.parse_message(sip.build_response(status, reason, headers)), ("192.0.2.10", 5060)

        client._read_response = read_response  # type: ignore[method-assign]
        result = await client.invite(
            target="+15551234567",
            remote_host="192.0.2.10",
            remote_sip_port=5060,
            request_uri="sip:+15551234567@sip.example:5060;transport=udp",
        )

        self.assertEqual(result, "ringing")
        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        invites = [message for message in messages if message.method == "INVITE"]
        self.assertEqual(len(invites), 2)
        self.assertEqual(invites[0].header("From"), invites[1].header("From"))
        self.assertTrue(invites[0].header("From").startswith("<sip:17770000000@192.168.1.10:5060"))
        self.assertTrue(invites[0].header("Contact").startswith("<sip:17770000000@192.168.1.10:5060"))
        self.assertEqual(invites[0].header("X-Voip-Stack-Caller-Name"), "17770000000")
        self.assertEqual(invites[1].header("X-Voip-Stack-Caller-Name"), "17770000000")
        self.assertEqual(invites[0].body, invites[1].body)
        video_payload = sdp.DEFAULT_H264_FORMAT.payload_type
        self.assertIn(
            f"m=video 41002 RTP/AVP {video_payload}".encode(),
            invites[0].body,
        )
        self.assertIn(
            f"a=rtpmap:{video_payload} H264/90000".encode(),
            invites[0].body,
        )
        self.assertIn(b"a=sendrecv", invites[0].body)
        self.assertFalse(invites[0].header("Proxy-Authorization"))
        self.assertIn('username="17770000000"', invites[1].header("Proxy-Authorization"))
        self.assertIn('uri="sip:+15551234567@sip.example:5060;transport=udp"', invites[1].header("Proxy-Authorization"))
        self.assertNotEqual(invites[0].header("Via"), invites[1].header("Via"))
        self.assertEqual(sip.parse_cseq(invites[1].header("CSeq")).number, sip.parse_cseq(invites[0].header("CSeq")).number + 1)

    async def test_pending_cancel_waits_for_provisional_then_terminates_invite(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="420",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        response_count = 0

        async def read_response(_timeout: float):
            nonlocal response_count
            response_count += 1
            if response_count == 1:
                self.assertTrue(client.request_cancel())
                status, reason, method = 100, "Trying", "INVITE"
            elif response_count == 2:
                status, reason, method = 200, "OK", "CANCEL"
            else:
                status, reason, method = 487, "Request Terminated", "INVITE"
            headers = [
                ("Via", f"SIP/2.0/UDP 192.168.1.10:5060;branch={client.dialog_ids.branch}"),
                ("From", f"<sip:420@192.168.1.10:5060>;tag={client.dialog_ids.local_tag}"),
                ("To", "<sip:3519968203@sip.example>;tag=provider"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{client._invite_cseq} {method}"),
            ]
            return sip.parse_message(sip.build_response(status, reason, headers)), ("192.0.2.10", 5060)

        client._read_response = read_response  # type: ignore[method-assign]
        result = await client.invite(
            target="3519968203",
            remote_host="192.0.2.10",
            remote_sip_port=5060,
            request_uri="sip:3519968203@sip.example:5060;transport=udp",
        )

        self.assertEqual(result, "cancelled")
        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertEqual([message.method for message in messages], ["INVITE", "CANCEL", "ACK"])
        invite, cancel, ack = messages
        self.assertEqual(sip.parse_cseq(cancel.header("CSeq")).number, sip.parse_cseq(invite.header("CSeq")).number)
        self.assertEqual(sip.parse_cseq(ack.header("CSeq")).number, sip.parse_cseq(invite.header("CSeq")).number)
        self.assertEqual(sip.parse_via(cancel.header("Via")).branch, sip.parse_via(invite.header("Via")).branch)
        self.assertEqual(sip.parse_via(ack.header("Via")).branch, sip.parse_via(invite.header("Via")).branch)

    async def test_invite_transaction_survives_owner_task_cancellation(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        responses: asyncio.Queue[tuple[int, str, str]] = asyncio.Queue()

        async def read_response(_timeout: float):
            status, reason, method = await responses.get()
            headers = [
                ("Via", f"SIP/2.0/UDP 192.168.1.10:5060;branch={client.dialog_ids.branch}"),
                ("From", f"<sip:HA@192.168.1.10:5060>;tag={client.dialog_ids.local_tag}"),
                ("To", "<sip:ESP@192.0.2.10>;tag=remote"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{client._invite_cseq} {method}"),
            ]
            return sip.parse_message(sip.build_response(status, reason, headers)), ("192.0.2.10", 5060)

        client._read_response = read_response  # type: ignore[method-assign]
        owner = asyncio.create_task(
            client.invite(
                target="ESP",
                remote_host="192.0.2.10",
                remote_sip_port=5060,
                request_uri="sip:ESP@192.0.2.10:5060;transport=udp",
            )
        )
        while not transport.sent:
            await asyncio.sleep(0)
        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        self.assertIsNotNone(client._invite_task)
        assert client._invite_task is not None
        self.assertFalse(client._invite_task.done())

        responses.put_nowait((100, "Trying", "INVITE"))
        responses.put_nowait((200, "OK", "CANCEL"))
        responses.put_nowait((487, "Request Terminated", "INVITE"))
        self.assertEqual(await client._invite_task, "cancelled")
        self.assertEqual(
            [sip.parse_message(raw).method for raw, _addr in transport.sent],
            ["INVITE", "CANCEL", "ACK"],
        )

    async def test_invite_treats_183_session_progress_as_ringing(self) -> None:
        sent: list[bytes] = []
        responses: asyncio.Queue[bytes] = asyncio.Queue()
        responses.put_nowait(
            sip.build_response(
                183,
                "Session Progress",
                [
                    ("Via", "SIP/2.0/TCP 192.168.1.10:5060;branch=z9hG4bKorig"),
                    ("From", "<sip:420@192.168.1.10>;tag=ltag"),
                    ("To", "<sip:3519968203@provider.example>;tag=rtag"),
                    ("Call-ID", "progress-call"),
                    ("CSeq", "1 INVITE"),
                ],
                b"",
            )
        )
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="420",
            local_sip_port=5060,
            local_rtp_port=41000,
            signaling_transport="TCP",
        )
        client.dialog_ids.call_id = "progress-call"
        client.dialog_ids.branch = "z9hG4bKorig"
        client.use_reused_tcp_connection(
            send=sent.append,
            responses=responses,
            close=lambda: None,
        )
        result = await client.invite(
            target="3519968203",
            remote_host="provider.example",
            remote_sip_port=5060,
            timeout=0.2,
        )
        self.assertEqual(result, "ringing")
        self.assertEqual(client.last_sip_status_code, 183)
        self.assertTrue(sent)


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

    def test_digest_client_rejects_auth_int_only_challenge(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported SIP digest qop"):
            sip_auth.build_digest_authorization(
                challenge_header='Digest realm="test", nonce="nonce", qop="auth-int"',
                username="desk",
                password="secret",
                method="REGISTER",
                uri="sip:pbx.example",
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


class SipBridgeTest(unittest.IsolatedAsyncioTestCase):
    def test_local_client_relay_does_not_require_a_synthetic_sdp_offer(self) -> None:
        local_to_relay = sdp.RtpPcmFormat(96, "L16", 16000, 1, 16)
        relay_to_local = sdp.RtpPcmFormat(97, "L16", 48000, 1, 10)
        dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="192.0.2.20",
            remote_sip_port=5060,
            remote_rtp_host="192.0.2.20",
            remote_rtp_port=41000,
            local_rtp_port=42002,
            call_id="dest-call",
            local_uri="sip:HA@192.0.2.10",
            remote_uri="sip:ESP@192.0.2.20",
            send_format=local_to_relay,
            recv_format=local_to_relay,
        )
        client = types.SimpleNamespace(dialog=dialog)

        relay = sip_bridge.build_local_client_relay(
            client=client,
            local_host="127.0.0.1",
            local_to_relay_format=local_to_relay,
            relay_to_local_format=relay_to_local,
            source_relay_port=42000,
            dest_relay_port=42002,
            capture_name="source_dest",
        )

        self.assertEqual((relay.left.host, relay.left.port), ("127.0.0.1", 0))
        self.assertEqual(relay.left.inbound_rtp_format, local_to_relay)
        self.assertEqual(relay.left.outbound_rtp_format, relay_to_local)
        self.assertEqual((relay.right.host, relay.right.port), ("192.0.2.20", 41000))

    async def test_local_browser_loopback_latches_ephemeral_rtp_port_bidirectionally(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(3) as ports:
            relay_left_port, relay_right_port, destination_port = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 32)

        class Capture(asyncio.DatagramProtocol):
            def __init__(self) -> None:
                self.queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()

            def datagram_received(self, data: bytes, addr) -> None:
                self.queue.put_nowait((data, addr))

        loop = asyncio.get_running_loop()
        browser = Capture()
        destination = Capture()
        browser_transport, _ = await loop.create_datagram_endpoint(
            lambda: browser,
            local_addr=(local, 0),
        )
        destination_transport, _ = await loop.create_datagram_endpoint(
            lambda: destination,
            local_addr=(local, destination_port),
        )
        dtmf_events: list[tuple[str, str, str]] = []
        dtmf_received = asyncio.Event()

        def on_dtmf(side: str, digit: str, transport: str) -> None:
            dtmf_events.append((side, digit, transport))
            dtmf_received.set()

        relay = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer(local, 0, 96, audio, dtmf_payload_type=101),
            right=sip_rtp_bridge.RtpPeer(local, destination_port, 96, audio, dtmf_payload_type=101),
            left_port=relay_left_port,
            right_port=relay_right_port,
            on_dtmf=on_dtmf,
        )

        def frame(*, sequence: int, ssrc: int) -> bytes:
            return rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=96,
                    sequence=sequence,
                    timestamp=sequence * audio.nominal_frame_samples,
                    ssrc=ssrc,
                    payload=bytes(audio.nominal_frame_bytes),
                )
            )

        try:
            await relay.start()
            browser_port = int(browser_transport.get_extra_info("sockname")[1])
            browser_transport.sendto(frame(sequence=1, ssrc=101), (local, relay_left_port))
            forwarded, _ = await asyncio.wait_for(destination.queue.get(), timeout=1.0)
            self.assertEqual(rtp.parse_packet(forwarded).payload_type, 96)
            self.assertEqual(relay.left.port, browser_port)

            dtmf_packet = rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=101,
                    sequence=2,
                    timestamp=audio.nominal_frame_samples,
                    ssrc=101,
                    payload=bytes((1, 0x80, 0x01, 0x40)),
                )
            )
            browser_transport.sendto(dtmf_packet, (local, relay_left_port))
            browser_transport.sendto(dtmf_packet, (local, relay_left_port))
            await asyncio.wait_for(dtmf_received.wait(), timeout=1.0)
            self.assertEqual(dtmf_events, [("left", "1", "rtp_event")])
            relayed_dtmf: list[rtp.RtpPacket] = []
            while sum(bool(packet.payload[1] & 0x80) for packet in relayed_dtmf) < 3:
                raw, _ = await asyncio.wait_for(destination.queue.get(), timeout=1.0)
                relayed_dtmf.append(rtp.parse_packet(raw))
            self.assertEqual({packet.payload_type for packet in relayed_dtmf}, {101})
            self.assertEqual(
                {packet.timestamp for packet in relayed_dtmf},
                {relayed_dtmf[0].timestamp},
            )
            self.assertTrue(relayed_dtmf[0].marker)
            self.assertFalse(any(packet.marker for packet in relayed_dtmf[1:]))
            self.assertEqual([packet.payload[0] for packet in relayed_dtmf], [1] * 7)
            self.assertEqual(
                sum(bool(packet.payload[1] & 0x80) for packet in relayed_dtmf),
                3,
            )
            self.assertEqual(relay.right_dtmf_tx_events, 1)

            destination_transport.sendto(frame(sequence=3, ssrc=202), (local, relay_right_port))
            returned, _ = await asyncio.wait_for(browser.queue.get(), timeout=1.0)
            self.assertEqual(rtp.parse_packet(returned).payload_type, 96)
            self.assertGreaterEqual(relay.forwarded, 2)
        finally:
            await relay.stop()
            browser_transport.close()
            destination_transport.close()

    async def test_busy_bridge_target_returns_terminal_response_without_ringing(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(4) as ports:
            ha_sip, caller_rtp, dest_sip, caller_sip = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 32)
        stats = {"dest_invites": 0}

        async def dest_invite(invite):
            stats["dest_invites"] += 1
            return sip_listener.SipInviteResult(
                486,
                "Busy Here",
                decline_reason="busy",
            )

        async def ha_invite(invite):
            entries = [
                roster.RosterEntry(id="HA", address=local, metadata={"sip_port": ha_sip}),
                roster.RosterEntry(id="Cucina", address=local, metadata={"sip_port": dest_sip, "sip_transport": "udp"}),
            ]
            decision = router.resolve_ha_router(invite.target, entries, trunk_ready=False)
            self.assertIsNotNone(decision.entry)
            dest_client = sip_client.SipCallClient(
                local_ip=local,
                local_name=invite.caller or "HA",
                local_sip_port=ha_sip,
                local_rtp_port=caller_rtp + 2,
                supported_formats=[invite.selected_format.audio_format],
            )
            try:
                result = await dest_client.invite(
                    target=decision.entry.id,
                    remote_host=decision.entry.address,
                    remote_sip_port=decision.entry.metadata["sip_port"],
                )
            finally:
                await dest_client.close()
            self.assertNotEqual(result, "ringing")
            return sip_listener.SipInviteResult(
                486,
                "Busy Here",
                decline_reason=result if result != "sip_486" else "busy",
            )

        dest_server = sip_listener.SipUdpServer(
            host=local,
            port=dest_sip,
            local_ip=local,
            local_rtp_port=caller_rtp + 4,
            supported_formats=[audio],
            on_invite=dest_invite,
        )
        ha_server = sip_listener.SipUdpServer(
            host=local,
            port=ha_sip,
            local_ip=local,
            local_rtp_port=caller_rtp + 6,
            supported_formats=[audio],
            on_invite=ha_invite,
        )
        self.assertTrue(await dest_server.start())
        self.assertTrue(await ha_server.start())
        caller = sip_client.SipCallClient(
            local_ip=local,
            local_name="Spotpear",
            local_sip_port=caller_sip,
            local_rtp_port=caller_rtp,
            supported_formats=[audio],
        )
        try:
            self.assertEqual(
                await caller.invite(target="Cucina", remote_host=local, remote_sip_port=ha_sip),
                "busy",
            )
            self.assertIsNone(caller.dialog)
            self.assertEqual(stats["dest_invites"], 1)
        finally:
            await caller.close()
            await ha_server.stop()
            await dest_server.stop()

    async def test_symbolic_target_bridges_through_ha_with_rtp_relay(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(7) as ports:
            ha_sip, caller_rtp, dest_sip, dest_rtp, ha_rtp_left, ha_rtp_right, caller_sip = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 32)
        stats = {"dest_invites": 0, "caller_rtp_rx": 0, "dest_rtp_rx": 0}

        class DestRtp(asyncio.DatagramProtocol):
            def __init__(self) -> None:
                self.transport = None
                self.remote: tuple[str, int, int] | None = None
                self.sequence = 10
                self.timestamp = 0

            def connection_made(self, transport) -> None:
                self.transport = transport

            def datagram_received(self, data: bytes, addr) -> None:
                rtp.parse_packet(data)
                stats["dest_rtp_rx"] += 1

            async def send_loop(self) -> None:
                while True:
                    await asyncio.sleep(0.032)
                    if self.transport is None or self.remote is None:
                        continue
                    host, port, pt = self.remote
                    packet = rtp.build_packet(
                        rtp.RtpPacket(
                            payload_type=pt,
                            sequence=self.sequence,
                            timestamp=self.timestamp,
                            ssrc=0x2222,
                            payload=b"\0" * 1024,
                        )
                    )
                    self.transport.sendto(packet, (host, port))
                    self.sequence = rtp.next_sequence(self.sequence)
                    self.timestamp = rtp.next_timestamp(self.timestamp, 512)

        class CallerRtp(asyncio.DatagramProtocol):
            def __init__(self) -> None:
                self.transport = None
                self.remote: tuple[str, int, int] | None = None
                self.sequence = 500
                self.timestamp = 0

            def connection_made(self, transport) -> None:
                self.transport = transport

            def datagram_received(self, data: bytes, addr) -> None:
                rtp.parse_packet(data)
                stats["caller_rtp_rx"] += 1

            async def send_loop(self) -> None:
                while True:
                    await asyncio.sleep(0.032)
                    if self.transport is None or self.remote is None:
                        continue
                    host, port, pt = self.remote
                    packet = rtp.build_packet(
                        rtp.RtpPacket(
                            payload_type=pt,
                            sequence=self.sequence,
                            timestamp=self.timestamp,
                            ssrc=0x1111,
                            payload=b"\0" * 1024,
                        )
                    )
                    self.transport.sendto(packet, (host, port))
                    self.sequence = rtp.next_sequence(self.sequence)
                    self.timestamp = rtp.next_timestamp(self.timestamp, 512)

        dest_rtp_proto = DestRtp()
        caller_rtp_proto = CallerRtp()
        relay: sip_rtp_bridge.SipRtpRelay | None = None
        dest_client: sip_client.SipCallClient | None = None
        tasks: list[asyncio.Task] = []

        async def dest_invite(invite):
            stats["dest_invites"] += 1
            dest_rtp_proto.remote = (
                invite.remote_rtp_host,
                invite.remote_rtp_port,
                invite.selected_format.payload_type,
            )
            return sip_listener.SipInviteResult(
                200,
                "OK",
                answer_sdp=sdp.build_answer_directional(
                    local,
                    local,
                    dest_rtp,
                    invite.send_format,
                    invite.recv_format,
                ),
            )

        async def ha_invite(invite):
            nonlocal relay, dest_client
            entries = [
                roster.RosterEntry(id="HA", address=local, metadata={"sip_port": ha_sip}),
                roster.RosterEntry(id="Cucina", address=local, metadata={"sip_port": dest_sip, "sip_transport": "udp"}),
            ]
            decision = router.resolve_ha_router(invite.target, entries, trunk_ready=False)
            self.assertEqual(decision.sip_uri, f"sip:Cucina@{local}:{dest_sip};transport=udp")
            self.assertIsNotNone(decision.entry)
            dest_client = sip_client.SipCallClient(
                local_ip=local,
                local_name="HA",
                local_sip_port=ha_sip,
                local_rtp_port=ha_rtp_right,
                supported_formats=[invite.selected_format.audio_format],
            )
            result = await dest_client.invite(
                target=decision.entry.id,
                remote_host=decision.entry.address,
                remote_sip_port=decision.entry.metadata["sip_port"],
            )
            self.assertEqual(result, "in_call")
            assert dest_client.dialog is not None
            relay = sip_rtp_bridge.SipRtpRelay(
                left=sip_rtp_bridge.RtpPeer(
                    invite.remote_rtp_host,
                    invite.remote_rtp_port,
                    invite.selected_format.payload_type,
                    invite.selected_format.audio_format,
                ),
                right=sip_rtp_bridge.RtpPeer(
                    dest_client.dialog.remote_rtp_host,
                    dest_client.dialog.remote_rtp_port,
                    dest_client.dialog.selected_format.payload_type,
                    dest_client.dialog.selected_format.audio_format,
                ),
                left_port=ha_rtp_left,
                right_port=ha_rtp_right,
            )
            await relay.start()
            return sip_listener.SipInviteResult(
                200,
                "OK",
                answer_sdp=sdp.build_answer_directional(
                    local,
                    local,
                    ha_rtp_left,
                    invite.send_format,
                    invite.recv_format,
                ),
            )

        dest_server = sip_listener.SipUdpServer(
            host=local,
            port=dest_sip,
            local_ip=local,
            local_rtp_port=dest_rtp,
            supported_formats=[audio],
            on_invite=dest_invite,
        )
        ha_server = sip_listener.SipUdpServer(
            host=local,
            port=ha_sip,
            local_ip=local,
            local_rtp_port=ha_rtp_left,
            supported_formats=[audio],
            on_invite=ha_invite,
        )
        self.assertTrue(await dest_server.start())
        self.assertTrue(await ha_server.start())
        loop = asyncio.get_running_loop()
        dest_transport, _ = await loop.create_datagram_endpoint(lambda: dest_rtp_proto, local_addr=(local, dest_rtp))
        caller_transport, _ = await loop.create_datagram_endpoint(
            lambda: caller_rtp_proto,
            local_addr=(local, caller_rtp),
        )
        tasks.append(asyncio.create_task(dest_rtp_proto.send_loop()))
        tasks.append(asyncio.create_task(caller_rtp_proto.send_loop()))
        caller = sip_client.SipCallClient(
            local_ip=local,
            local_name="Spotpear",
            local_sip_port=caller_sip,
            local_rtp_port=caller_rtp,
            supported_formats=[audio],
        )
        try:
            self.assertEqual(await caller.invite(target="Cucina", remote_host=local, remote_sip_port=ha_sip), "in_call")
            assert caller.dialog is not None
            caller_rtp_proto.remote = (
                caller.dialog.remote_rtp_host,
                caller.dialog.remote_rtp_port,
                caller.dialog.selected_format.payload_type,
            )
            deadline = asyncio.get_running_loop().time() + 3.0
            while (
                asyncio.get_running_loop().time() < deadline
                and (stats["caller_rtp_rx"] == 0 or stats["dest_rtp_rx"] == 0)
            ):
                await asyncio.sleep(0.05)
            self.assertEqual(stats["dest_invites"], 1)
            assert relay is not None
            relay_snapshot = relay.snapshot()
            self.assertGreater(stats["caller_rtp_rx"], 0, relay_snapshot)
            self.assertGreater(stats["dest_rtp_rx"], 0, relay_snapshot)
            self.assertGreater(relay.forwarded, 0)
            self.assertEqual(relay.dropped, 0)
        finally:
            caller.bye()
            await caller.close()
            if dest_client is not None:
                dest_client.bye()
                await dest_client.close()
            if relay is not None:
                await relay.stop()
            await ha_server.stop()
            await dest_server.stop()
            dest_transport.close()
            caller_transport.close()
            for task in tasks:
                task.cancel()


class SipTcpProfileTest(unittest.IsolatedAsyncioTestCase):
    async def test_tcp_listener_caps_connections_per_source(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(2) as ports:
            sip_port, rtp_port = ports
        server = sip_listener.SipTcpServer(
            host=local,
            port=sip_port,
            local_ip=local,
            local_rtp_port=rtp_port,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            max_connections=2,
            max_connections_per_host=1,
            initial_message_timeout=1.0,
            frame_timeout=1.0,
        )
        self.assertTrue(await server.start())
        first_reader, first_writer = await asyncio.open_connection(local, sip_port)
        del first_reader
        first_writer.write(b"I")
        await first_writer.drain()
        for _ in range(20):
            if server.endpoints:
                break
            await asyncio.sleep(0.005)
        second_reader, second_writer = await asyncio.open_connection(local, sip_port)
        try:
            self.assertEqual(
                await asyncio.wait_for(second_reader.read(1), timeout=0.2),
                b"",
            )
            self.assertEqual(len(server.endpoints), 1)
        finally:
            first_writer.close()
            second_writer.close()
            await first_writer.wait_closed()
            await second_writer.wait_closed()
            await server.stop()

    async def test_tcp_listener_accepts_pcm_invite(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(2) as ports:
            sip_port, rtp_port = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 32)
        seen = {"invite": False}

        async def on_invite(invite):
            seen["invite"] = True
            answer = sdp.build_answer_directional(
                local,
                local,
                rtp_port,
                invite.send_format,
                invite.recv_format,
            )
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer)

        server = sip_listener.SipTcpServer(
            host=local,
            port=sip_port,
            local_ip=local,
            local_rtp_port=rtp_port,
            supported_formats=[audio],
            on_invite=on_invite,
        )
        self.assertTrue(await server.start())
        reader, writer = await asyncio.open_connection(local, sip_port)
        try:
            body = sdp.build_offer(local, local, rtp_port + 2, [audio]).encode()
            raw = sip.build_request(
                "INVITE",
                f"sip:HA@{local}:{sip_port}",
                [
                    ("Via", f"SIP/2.0/TCP {local}:43210;branch=z9hG4bKtcp;rport"),
                    ("From", f"<sip:ESP@{local}:43210>;tag=src"),
                    ("To", f"<sip:HA@{local}:{sip_port}>"),
                    ("Call-ID", "tcp-call-1"),
                    ("CSeq", "1 INVITE"),
                    ("Contact", f"<sip:ESP@{local}:43210>"),
                    ("Content-Type", "application/sdp"),
                    ("X-Voip-Stack-Caller-Name", "ESP"),
                    ("X-Voip-Stack-Dest-Name", "HA"),
                ],
                body,
            )
            writer.write(raw)
            await writer.drain()
            first_raw = await sip_listener._read_sip_stream_message(reader)
            second_raw = await sip_listener._read_sip_stream_message(reader)
            assert first_raw is not None and second_raw is not None
            first = sip.parse_message(first_raw)
            second = sip.parse_message(second_raw)
            statuses = {first.status_code, second.status_code}
            self.assertEqual(statuses, {100, 200})
            final = first if first.status_code == 200 else second
            self.assertIn(b"m=audio", final.body)
            self.assertTrue(seen["invite"])
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()

    async def test_tcp_register_flow_can_carry_outbound_dahua_dialog(self) -> None:
        """A registered TCP flow is reusable for a new HA-originated dialog."""

        local = "127.0.0.1"
        with _reserved_udp_ports(3) as ports:
            sip_port, ha_rtp_port, dahua_rtp_port = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("100", "Dahua VTO", "secret")],
            local_ip=local,
            local_sip_port=sip_port,
        )

        async def unexpected_invite(_invite):
            raise AssertionError("registered client must receive, not originate, INVITE")

        server = sip_listener.SipTcpServer(
            host=local,
            port=sip_port,
            local_ip=local,
            local_rtp_port=ha_rtp_port,
            supported_formats=[audio],
            on_invite=unexpected_invite,
            on_register=registrar.handle_register,
        )
        self.assertTrue(await server.start())
        reader, writer = await asyncio.open_connection(local, sip_port)
        source = writer.get_extra_info("sockname")
        assert source is not None
        source_addr = (str(source[0]), int(source[1]))
        request_uri = f"sip:{local}:{sip_port}"
        base_headers = [
            (
                "Via",
                f"SIP/2.0/TCP {local}:{source_addr[1]};"
                "branch=z9hG4bKtcp-register;rport",
            ),
            ("From", "<sip:100@VDP>;tag=dahua-register"),
            ("To", "<sip:100@VDP>"),
            ("Call-ID", "dahua-tcp-register"),
            ("CSeq", "1 REGISTER"),
            (
                "Contact",
                "<sip:100@10.0.0.85:5060;transport=tcp>",
            ),
            ("Expires", "120"),
            ("User-Agent", "Dahua UAC/3.0"),
        ]

        async def send_and_read(raw: bytes) -> sip.SipMessage:
            writer.write(raw)
            await writer.drain()
            response = await asyncio.wait_for(
                sip_listener._read_sip_stream_message(reader),
                timeout=1,
            )
            assert response is not None
            return sip.parse_message(response)

        try:
            challenge = await send_and_read(
                sip.build_request("REGISTER", request_uri, base_headers, b"")
            )
            self.assertEqual(challenge.status_code, 401)
            authorization = sip_auth.build_digest_authorization(
                challenge_header=challenge.header("WWW-Authenticate"),
                username="100",
                password="secret",
                method="REGISTER",
                uri=request_uri,
            )
            registered = await send_and_read(
                sip.build_request(
                    "REGISTER",
                    request_uri,
                    [*base_headers, ("Authorization", authorization)],
                    b"",
                )
            )
            self.assertEqual(registered.status_code, 200)
            binding = registrar.registered_contacts("100")[0]
            self.assertEqual(
                (binding.source_host, binding.source_port, binding.transport),
                (source_addr[0], source_addr[1], "TCP"),
            )

            client = sip_client.SipCallClient(
                local_ip=local,
                local_name="HA-Test",
                local_sip_port=sip_port,
                local_rtp_port=ha_rtp_port,
                supported_formats=[audio],
                signaling_transport="TCP",
                include_common_codecs=True,
                peer_user_agent=binding.user_agent,
            )
            hass = types.SimpleNamespace(
                data={
                    "voip_stack": {
                        "sip_endpoint": types.SimpleNamespace(
                            tcp_server=server,
                        )
                    }
                }
            )
            self.assertTrue(
                sip_runtime.enable_reused_tcp_connection(
                    hass,
                    client,
                    sip.parse_sip_uri(binding.contact_uri),
                    target="Dahua VTO",
                    default_sip_port=sip_port,
                )
            )

            async def dahua_answer() -> None:
                raw_invite = await asyncio.wait_for(
                    sip_listener._read_sip_stream_message(reader),
                    timeout=1,
                )
                assert raw_invite is not None
                invite = sip.parse_message(raw_invite)
                self.assertEqual(invite.method, "INVITE")
                offered = sdp.offered_pcm_formats(
                    invite.body,
                    allow_dahua_pcm=True,
                )
                selected = next(item for item in offered if item.encoding == "PCM")
                answer = sdp.build_answer_directional(
                    local,
                    local,
                    dahua_rtp_port,
                    selected,
                    selected,
                    remote_sdp=invite.body,
                ).encode()
                headers = sip_listener._response_headers(
                    invite,
                    addr=source_addr,
                    to_tag="dahua-call",
                )
                headers.extend(
                    (
                        ("Contact", f"<sip:100@{local}:{source_addr[1]};transport=tcp>"),
                        ("Content-Type", "application/sdp"),
                    )
                )
                writer.write(sip.build_response(180, "Ringing", headers))
                writer.write(sip.build_response(200, "OK", headers, answer))
                await writer.drain()

            answer_task = asyncio.create_task(dahua_answer())
            result = await client.invite(
                target="100",
                remote_host=source_addr[0],
                remote_sip_port=source_addr[1],
                request_uri=(
                    f"sip:100@{source_addr[0]}:{source_addr[1]};transport=tcp"
                ),
            )
            if result == "ringing":
                result = await client.wait_for_final()
            await answer_task

            self.assertEqual(result, "in_call")
            self.assertIsNotNone(client.dialog)
            assert client.dialog is not None
            self.assertEqual(client.dialog.send_format.encoding, "PCM")
            self.assertEqual(client.dialog.recv_format.encoding, "PCM")
            client.bye()
            await client.close()
            self.assertEqual(server._dialog_queues, {})
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()

    async def test_tcp_dialog_survives_connection_replacement_for_reinvite(self) -> None:
        """An in-dialog offer may arrive on a new RFC 3261 TCP connection."""

        local = "127.0.0.1"
        with _reserved_udp_ports(2) as ports:
            sip_port, rtp_port = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 20)
        updates: list[tuple[int, str]] = []

        def answer(invite):
            return sdp.build_answer_directional(
                local,
                local,
                rtp_port,
                invite.send_format,
                invite.recv_format,
                remote_sdp=invite.remote_sdp,
                video_port=rtp_port + 2,
                video_format=invite.answer_video_format,
                video_direction=sdp.local_direction_for_offer(
                    invite.video_format.direction
                    if invite.video_format is not None
                    else "inactive"
                ),
            )

        async def on_invite(invite):
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer(invite))

        async def on_media_update(_previous, updated, _method):
            updates.append((updated.remote_rtp_port, updated.video_format.direction))
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer(updated))

        server = sip_listener.SipTcpServer(
            host=local,
            port=sip_port,
            local_ip=local,
            local_rtp_port=rtp_port,
            supported_formats=[audio],
            on_invite=on_invite,
            on_media_update=on_media_update,
            enable_video=True,
            max_connections_per_host=2,
        )
        self.assertTrue(await server.start())

        def invite(
            cseq: int,
            branch: str,
            remote_rtp: int,
            to_tag: str = "",
            video_direction: str = "recvonly",
        ) -> bytes:
            headers = [
                ("Via", f"SIP/2.0/TCP {local}:43210;branch={branch};rport"),
                ("From", f"<sip:Wildix@{local}:43210>;tag=remote"),
                ("To", f"<sip:HA@{local}:{sip_port}>" + (f";tag={to_tag}" if to_tag else "")),
                ("Call-ID", "tcp-reconnect-reinvite"),
                ("CSeq", f"{cseq} INVITE"),
                ("Contact", f"<sip:Wildix@{local}:43210>"),
                ("Content-Type", "application/sdp"),
            ]
            body = sdp.build_offer_directional(
                local,
                local,
                remote_rtp,
                [audio],
                [audio],
                video_port=remote_rtp + 2,
                video_formats=sdp.DEFAULT_VIDEO_FORMATS,
                video_direction=video_direction,
            ).encode()
            return sip.build_request("INVITE", f"sip:HA@{local}:{sip_port}", headers, body)

        first_reader, first_writer = await asyncio.open_connection(local, sip_port)
        second_writer = None
        try:
            first_writer.write(invite(1, "z9hG4bKinitial", 41000))
            await first_writer.drain()
            responses = []
            while 200 not in [item.status_code for item in responses]:
                raw = await asyncio.wait_for(
                    sip_listener._read_sip_stream_message(first_reader), timeout=1
                )
                assert raw is not None
                responses.append(sip.parse_message(raw))
            final = next(item for item in responses if item.status_code == 200)
            local_tag = sip.extract_tag(final.header("To"))
            self.assertTrue(local_tag)

            first_writer.close()
            await first_writer.wait_closed()
            await asyncio.sleep(0)

            second_reader, second_writer = await asyncio.open_connection(local, sip_port)
            second_writer.write(
                invite(
                    2,
                    "z9hG4bKvideo-on",
                    42000,
                    local_tag,
                    video_direction="sendrecv",
                )
            )
            await second_writer.drain()
            responses = []
            while 200 not in [item.status_code for item in responses]:
                raw = await asyncio.wait_for(
                    sip_listener._read_sip_stream_message(second_reader), timeout=1
                )
                assert raw is not None
                responses.append(sip.parse_message(raw))
            self.assertEqual(updates, [(42000, "sendrecv")])
            self.assertEqual(len(server.endpoint.active_dialogs), 1)
        finally:
            if second_writer is not None:
                second_writer.close()
                await second_writer.wait_closed()
            if not first_writer.is_closing():
                first_writer.close()
                await first_writer.wait_closed()
            await server.stop()

    async def test_tcp_client_establishes_pcm_dialog(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(3) as ports:
            sip_port, server_rtp, client_rtp = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 32)

        async def on_invite(invite):
            answer = sdp.build_answer_directional(
                local,
                local,
                server_rtp,
                invite.send_format,
                invite.recv_format,
            )
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer)

        server = sip_listener.SipTcpServer(
            host=local,
            port=sip_port,
            local_ip=local,
            local_rtp_port=server_rtp,
            supported_formats=[audio],
            on_invite=on_invite,
        )
        self.assertTrue(await server.start())
        client = sip_client.SipCallClient(
            local_ip=local,
            local_name="Casa",
            local_sip_port=5060,
            local_rtp_port=client_rtp,
            supported_formats=[audio],
            signaling_transport="TCP",
        )
        try:
            self.assertEqual(
                await client.invite(target="ESP", remote_host=local, remote_sip_port=sip_port),
                "in_call",
            )
            self.assertIsNotNone(client.dialog)
            assert client.dialog is not None
            self.assertEqual(client.dialog.remote_rtp_port, server_rtp)
            self.assertNotEqual(client.local_sip_port, 5060)
        finally:
            client.bye()
            await client.close()
            await server.stop()


if __name__ == "__main__":
    unittest.main()
