"""Independent UDP STUN witnesses on borrowed, actually-bound media sockets."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import socket
import struct
import unittest
from unittest.mock import patch
import zlib


# This pure protocol module does not import the integration/HA entry point.
_PATH = Path(__file__).resolve().parents[1] / "custom_components/voip_stack/core/media_stun.py"
_SPEC = importlib.util.spec_from_file_location("media_stun_under_test", _PATH)
assert _SPEC is not None and _SPEC.loader is not None
media_stun = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(media_stun)
_COOKIE = 0x2112A442


def _attribute(kind, value, *, padding=b"\0"):
    return struct.pack("!HH", kind, len(value)) + value + padding * (-len(value) % 4)


def _response(request, ip="198.51.100.23", port=47003, *, extra=b"", fingerprint=True):
    # Independent server encoding: XOR individual octets, not the client parser.
    cookie = b"\x21\x12\xa4\x42"
    address = bytes(octet ^ cookie[index] for index, octet in enumerate(socket.inet_aton(ip)))
    value = b"\0\x01" + struct.pack("!H", port ^ 0x2112) + address
    body = _attribute(0x0020, value) + extra
    packet = struct.pack("!HHI", 0x0101, len(body) + (8 if fingerprint else 0), _COOKIE)
    packet += request[8:20] + body
    if fingerprint:
        packet += _attribute(0x8028, struct.pack("!I", zlib.crc32(packet) ^ 0x5354554E))
    return packet


class _Server(asyncio.DatagramProtocol):
    def __init__(self, handler=None):
        self.handler = handler
        self.requests = []
        self.transport = None
        self.closed = asyncio.get_running_loop().create_future()

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, source):
        self.requests.append((asyncio.get_running_loop().time(), data, source))
        if self.handler is not None:
            self.handler(self, data, source)

    def connection_lost(self, exc):
        self.closed.set_result(None)

    @property
    def uri(self):
        return f"stun:127.0.0.1:{self.transport.get_extra_info('sockname')[1]}"


class MediaStunTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.loop = asyncio.get_running_loop()
        self.servers = []
        self.sockets = []

    async def asyncTearDown(self):
        for sock in self.sockets:
            sock.close()
        for server in self.servers:
            server.transport.close()
        await asyncio.gather(*(server.closed for server in self.servers))

    def media_socket(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        self.sockets.append(sock)
        return sock

    async def server(self, handler=None):
        protocol = _Server(handler)
        await self.loop.create_datagram_endpoint(lambda: protocol, local_addr=("127.0.0.1", 0))
        self.servers.append(protocol)
        return protocol

    def assert_no_reader(self, sock):
        # CPython selector evidence is independent of merely seeing task.done().
        try:
            key = self.loop._selector.get_key(sock.fileno())
        except KeyError:
            return
        self.assertFalse(key.events & 1)

    async def assert_socket_usable(self, sock, expected_address):
        self.assertEqual(sock.getsockname(), expected_address)
        self.assertFalse(sock.getblocking())
        self.assertGreaterEqual(sock.fileno(), 0)
        self.assert_no_reader(sock)
        sender = self.media_socket()
        await self.loop.sock_sendto(sender, b"ordinary media", expected_address)
        data, _ = await asyncio.wait_for(self.loop.sock_recvfrom(sock, 256), 0.2)
        self.assertEqual(data, b"ordinary media")

    async def test_actual_component_sockets_nonconsecutive_mapped_ports_no_rebind_or_close(self):
        sockets = {name: self.media_socket() for name in ("audio_rtp", "audio_rtcp", "video_rtp", "video_rtcp")}
        original = {name: sock.getsockname() for name, sock in sockets.items()}
        mapped = {address: port for address, port in zip(original.values(), (47003, 61001, 49152, 50007))}
        server = await self.server(lambda server, data, source: server.transport.sendto(
            _response(data, port=mapped[source]), source,
        ))
        # Any creation/bind/close/mode change in discovery itself is a failure.
        with (patch.object(socket.socket, "__init__", side_effect=AssertionError("new socket")),
              patch.object(socket.socket, "bind", side_effect=AssertionError("rebind")),
              patch.object(socket.socket, "close", side_effect=AssertionError("close borrowed socket")),
              patch.object(socket.socket, "setblocking", side_effect=AssertionError("mode change"))):
            result = await media_stun.discover_media_endpoints(sockets, [server.uri])
        self.assertEqual(result, {name: ("198.51.100.23", mapped[address]) for name, address in original.items()})
        self.assertEqual({item[2] for item in server.requests}, set(original.values()))
        self.assertEqual(len({item[1][8:20] for item in server.requests}), 4)
        for name, sock in sockets.items():
            await self.assert_socket_usable(sock, original[name])

    async def test_accepts_no_fingerprint_and_ignores_optional_padding_octets(self):
        sock = self.media_socket()
        extra = _attribute(0x8022, b"abc", padding=b"\xff") + _attribute(0xBEEF, b"x", padding=b"\x11")
        server = await self.server(lambda server, data, source: server.transport.sendto(
            _response(data, extra=extra, fingerprint=False), source,
        ))
        self.assertEqual(await media_stun.discover_media_endpoints({"rtp": sock}, [server.uri]),
                         {"rtp": ("198.51.100.23", 47003)})

    async def test_legacy_mapped_address_is_not_a_fallback(self):
        sock = self.media_socket()
        def reply(server, request, source):
            body = _attribute(1, b"\0\x01" + struct.pack("!H", 40000) + socket.inet_aton("198.51.100.1"))
            server.transport.sendto(struct.pack("!HHI", 0x0101, len(body), _COOKIE) + request[8:20] + body, source)
        server = await self.server(reply)
        self.assertEqual(await media_stun.discover_media_endpoints({"rtp": sock}, [server.uri], timeout=0.04), {})

    async def test_malformed_responses_are_not_announced(self):
        def mutate_header(packet, index, value):
            return packet[:index] + value + packet[index + len(value):]
        mutations = {
            "short": lambda p: p[:19],
            "trailing": lambda p: p + b"\0\0\0\0",
            "length": lambda p: mutate_header(p, 2, struct.pack("!H", 4)),
            "unaligned": lambda p: mutate_header(p, 2, struct.pack("!H", 13)),
            "cookie": lambda p: mutate_header(p, 4, b"\0" * 4),
            "type": lambda p: mutate_header(p, 0, b"\xc1\x01"),
            "request": lambda p: mutate_header(p, 0, b"\0\x01"),
            "txid": lambda p: mutate_header(p, 8, b"\xff" * 12),
            "attribute_length": lambda p: mutate_header(p, 22, struct.pack("!H", 9)),
            "family": lambda p: mutate_header(p, 25, b"\x02"),
            "fingerprint": lambda p: p[:-1] + bytes([p[-1] ^ 1]),
            "fingerprint_not_last": lambda p: mutate_header(p, 2, struct.pack("!H", len(p) - 20 + 4)) + b"\x80\x22\0\0",
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                sock = self.media_socket()
                server = await self.server(lambda server, data, source, mutate=mutate:
                                          server.transport.sendto(mutate(_response(data)), source))
                self.assertEqual(await media_stun.discover_media_endpoints({"rtp": sock}, [server.uri], timeout=0.025), {})
                self.assert_no_reader(sock)

    async def test_unknown_required_attribute_fails_even_with_valid_fingerprint(self):
        sock = self.media_socket()
        server = await self.server(lambda server, data, source: server.transport.sendto(
            _response(data, extra=_attribute(0x1234, b"")), source,
        ))
        self.assertEqual(await media_stun.discover_media_endpoints({"rtp": sock}, [server.uri], timeout=0.04), {})

    async def test_unknown_required_fails_transaction_not_just_the_first_response(self):
        sock = self.media_socket()
        def reply(server, request, source):
            server.transport.sendto(_response(request, extra=_attribute(0x1234, b"")), source)
            server.transport.sendto(_response(request), source)
        server = await self.server(reply)
        self.assertEqual(await media_stun.discover_media_endpoints({"rtp": sock}, [server.uri], timeout=0.04), {})

    async def test_invalid_xor_addresses_and_ports_fail_with_valid_fingerprint(self):
        for ip, port in (("0.0.0.0", 40000), ("0.0.0.1", 40000), ("224.0.0.1", 40000),
                         ("240.0.0.1", 40000), ("255.255.255.255", 40000), ("198.51.100.1", 0)):
            with self.subTest(ip=ip, port=port):
                sock = self.media_socket()
                server = await self.server(lambda server, data, source, ip=ip, port=port:
                                          server.transport.sendto(_response(data, ip=ip, port=port), source))
                self.assertEqual(await media_stun.discover_media_endpoints({"rtp": sock}, [server.uri], timeout=0.025), {})

    async def test_correct_transaction_from_wrong_source_is_ignored(self):
        sock = self.media_socket()
        wrong = await self.server()
        requested = await self.server(lambda server, data, source: wrong.transport.sendto(_response(data), source))
        self.assertEqual(await media_stun.discover_media_endpoints({"rtp": sock}, [requested.uri], timeout=0.04), {})

    async def test_bad_packets_do_not_consume_transaction_before_valid_response(self):
        sock = self.media_socket()
        def reply(server, request, source):
            server.transport.sendto(b"not STUN", source)
            server.transport.sendto(_response(request[:8] + b"\xff" * 12), source)
            server.transport.sendto(_response(request), source)
        server = await self.server(reply)
        self.assertEqual(await media_stun.discover_media_endpoints({"rtp": sock}, [server.uri]),
                         {"rtp": ("198.51.100.23", 47003)})

    async def test_retransmission_uses_same_transaction_after_initial_rto(self):
        sock = self.media_socket()
        def reply(server, request, source):
            if len(server.requests) == 2:
                server.transport.sendto(_response(request), source)
        server = await self.server(reply)
        result = await media_stun.discover_media_endpoints({"rtp": sock}, [server.uri], timeout=0.9)
        self.assertEqual(result, {"rtp": ("198.51.100.23", 47003)})
        self.assertEqual(server.requests[0][1], server.requests[1][1])
        self.assertGreaterEqual(server.requests[1][0] - server.requests[0][0], 0.48)
        self.assertLess(server.requests[1][0] - server.requests[0][0], 0.8)

    async def test_alternative_server_and_partial_components_share_deadline(self):
        rtp, rtcp = self.media_socket(), self.media_socket()
        silent = await self.server()
        responsive = await self.server(lambda server, data, source: server.transport.sendto(_response(data), source)
                                       if source == rtp.getsockname() else None)
        before = asyncio.all_tasks()
        started = self.loop.time()
        result = await media_stun.discover_media_endpoints({"rtp": rtp, "rtcp": rtcp},
                                                          [silent.uri, responsive.uri], timeout=0.08)
        self.assertEqual(result, {"rtp": ("198.51.100.23", 47003)})
        self.assertLess(self.loop.time() - started, 0.3)
        self.assertEqual(asyncio.all_tasks(), before)
        self.assert_no_reader(rtp)
        self.assert_no_reader(rtcp)

    async def test_slow_dns_does_not_block_other_server_or_component(self):
        sock = self.media_socket()
        server = await self.server(lambda server, data, source: server.transport.sendto(_response(data), source))
        real_getaddrinfo = self.loop.getaddrinfo
        cancelled = asyncio.Event()
        async def resolve(host, port, **kwargs):
            if host == "blocked.invalid":
                try:
                    await asyncio.Future()
                finally:
                    cancelled.set()
            return await real_getaddrinfo(host, port, **kwargs)
        with patch.object(self.loop, "getaddrinfo", side_effect=resolve):
            result = await media_stun.discover_media_endpoints({"rtp": sock}, ["stun:blocked.invalid", server.uri], timeout=0.15)
        self.assertEqual(result, {"rtp": ("198.51.100.23", 47003)})
        self.assertTrue(cancelled.is_set())

    async def test_deadline_includes_dns_and_cancels_resolver(self):
        sock = self.media_socket()
        cancelled = asyncio.Event()
        async def resolve(*args, **kwargs):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()
        before = asyncio.all_tasks()
        started = self.loop.time()
        with patch.object(self.loop, "getaddrinfo", side_effect=resolve):
            result = await media_stun.discover_media_endpoints({"rtp": sock}, ["stun:blocked.invalid"], timeout=0.04)
        self.assertEqual(result, {})
        self.assertLess(self.loop.time() - started, 0.2)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(asyncio.all_tasks(), before)
        self.assert_no_reader(sock)

    async def test_cancellation_clears_reader_and_tasks_without_closing_socket(self):
        sock = self.media_socket()
        address = sock.getsockname()
        server = await self.server()
        before = asyncio.all_tasks()
        discovery = asyncio.create_task(media_stun.discover_media_endpoints({"rtp": sock}, [server.uri]))
        while not server.requests:
            await asyncio.sleep(0)
        discovery.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await discovery
        self.assertEqual(asyncio.all_tasks(), before)
        await self.assert_socket_usable(sock, address)

    async def test_timeout_leaves_socket_usable(self):
        sock = self.media_socket()
        address = sock.getsockname()
        server = await self.server()
        self.assertEqual(await media_stun.discover_media_endpoints({"rtp": sock}, [server.uri], timeout=0.025), {})
        await self.assert_socket_usable(sock, address)

    async def test_uri_default_port_ipv4_dns_and_failed_alternatives(self):
        sock = self.media_socket()
        server = await self.server(lambda server, data, source: server.transport.sendto(_response(data), source))
        port = server.transport.get_extra_info("sockname")[1]
        calls = []
        async def resolve(host, requested_port, **kwargs):
            calls.append((host, requested_port, kwargs))
            if host == "failed.invalid":
                raise socket.gaierror("not found")
            return [(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP, "", ("127.0.0.1", port))]
        with patch.object(self.loop, "getaddrinfo", side_effect=resolve):
            result = await media_stun.discover_media_endpoints({"rtp": sock}, ["stun:failed.invalid", "stun:example.invalid"])
        self.assertEqual(result, {"rtp": ("198.51.100.23", 47003)})
        self.assertTrue(all(call[1] == 3478 and call[2]["family"] == socket.AF_INET
                            and call[2]["type"] == socket.SOCK_DGRAM for call in calls))

    async def test_unsupported_uri_schemes_and_credentials_are_not_resolved(self):
        sock = self.media_socket()
        uris = ["turn:example.org", "stuns:example.org", "stun://example.org", "stun:user:password@example.org",
                "stun:example.org:0", "stun:example.org:65536", "stun:[::1]", "stun:example.org?transport=tcp"]
        with patch.object(self.loop, "getaddrinfo", side_effect=AssertionError("must not resolve")):
            self.assertEqual(await media_stun.discover_media_endpoints({"rtp": sock}, uris), {})

    async def test_invalid_socket_contract_rejected_before_io(self):
        sock = self.media_socket()
        with self.assertRaises(ValueError):
            await media_stun.discover_media_endpoints({"a": sock, "b": sock}, ["stun:example.org"])
        sock.setblocking(True)
        with self.assertRaises(ValueError):
            await media_stun.discover_media_endpoints({"rtp": sock}, ["stun:example.org"])
        sock.setblocking(False)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as unbound:
            unbound.setblocking(False)
            with self.assertRaises(ValueError):
                await media_stun.discover_media_endpoints({"rtp": unbound}, ["stun:example.org"])

    async def test_empty_or_nonpositive_deadline_does_no_io(self):
        sock = self.media_socket()
        with patch.object(self.loop, "getaddrinfo", side_effect=AssertionError("must not resolve")):
            for timeout in (0, -1, float("nan"), float("inf")):
                self.assertEqual(await media_stun.discover_media_endpoints({"rtp": sock}, ["stun:example.org"], timeout=timeout), {})
            self.assertEqual(await media_stun.discover_media_endpoints({}, ["stun:example.org"]), {})
            self.assertEqual(await media_stun.discover_media_endpoints({"rtp": sock}, []), {})


if __name__ == "__main__":
    unittest.main()
