#!/usr/bin/env python3
"""Qualify real SIP TLS, IPv6 and RFC 3263 DNS with external peers."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import threading
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))
from live_voip_qualification import candidate_revision  # noqa: E402

from custom_components.voip_stack.core import sip  # noqa: E402
from custom_components.voip_stack.core.sip_resolution import (  # noqa: E402
    SipServerResolver,
)
from custom_components.voip_stack.sip_client import SipCallClient  # noqa: E402


def _sip_message(connection: socket.socket) -> bytes:
    data = b""
    connection.settimeout(8)
    while b"\r\n\r\n" not in data:
        chunk = connection.recv(65536)
        if not chunk:
            break
        data += chunk
    head, separator, body = data.partition(b"\r\n\r\n")
    if not separator:
        return data
    length = next(
        (
            int(line.partition(b":")[2].strip() or b"0")
            for line in head.split(b"\r\n")
            if line.lower().startswith(b"content-length:")
        ),
        0,
    )
    while len(body) < length:
        body += connection.recv(length - len(body))
    return head + separator + body


def _headers(message: bytes) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for raw in message.decode(errors="replace").split("\r\n")[1:]:
        name, separator, value = raw.partition(":")
        if separator:
            values.setdefault(name.casefold(), []).append(value.strip())
    return values


def _busy_response(invite: bytes) -> bytes:
    headers = _headers(invite)
    lines = ["SIP/2.0 486 Busy Here"]
    lines.extend(f"Via: {value}" for value in headers.get("via", ()))
    lines.extend(
        (
            f"From: {headers['from'][0]}",
            f"To: {headers['to'][0]};tag=transport-lab",
            f"Call-ID: {headers['call-id'][0]}",
            f"CSeq: {headers['cseq'][0]}",
            "Content-Length: 0",
            "",
            "",
        )
    )
    return "\r\n".join(lines).encode()


@dataclass(slots=True)
class SipPeer:
    family: socket.AddressFamily
    transport: str
    host: str
    port: int
    tls_context: ssl.SSLContext | None = None
    messages: list[str] = field(default_factory=list)
    error: str = ""
    _ready: threading.Event = field(default_factory=threading.Event)
    _done: threading.Event = field(default_factory=threading.Event)

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()
        if not self._ready.wait(5):
            raise RuntimeError(f"{self.transport} peer did not start")

    def wait(self) -> None:
        if not self._done.wait(12):
            raise RuntimeError(f"{self.transport} peer did not finish")
        if self.error:
            raise RuntimeError(self.error)
        ladder = [item.split(" ", 1)[0] for item in self.messages]
        if ladder != ["INVITE", "ACK"]:
            raise RuntimeError(f"unexpected {self.transport} ladder: {ladder}")

    def _run(self) -> None:
        try:
            kind = socket.SOCK_STREAM if self.transport == "TLS" else socket.SOCK_DGRAM
            with socket.socket(self.family, kind) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if self.family == socket.AF_INET6 and self.host == "::":
                    server.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                server.bind((self.host, self.port))
                if kind == socket.SOCK_STREAM:
                    server.listen(1)
                    server.settimeout(10)
                self._ready.set()
                if kind == socket.SOCK_STREAM:
                    assert self.tls_context is not None
                    raw, _address = server.accept()
                    with (
                        raw,
                        self.tls_context.wrap_socket(raw, server_side=True) as peer,
                    ):
                        invite = _sip_message(peer)
                        self.messages.append(invite.decode(errors="replace"))
                        peer.sendall(_busy_response(invite))
                        self.messages.append(
                            _sip_message(peer).decode(errors="replace")
                        )
                else:
                    server.settimeout(8)
                    invite, address = server.recvfrom(65536)
                    self.messages.append(invite.decode(errors="replace"))
                    server.sendto(_busy_response(invite), address)
                    ack, _address = server.recvfrom(65536)
                    self.messages.append(ack.decode(errors="replace"))
        except BaseException as err:  # noqa: BLE001 - retain external-peer evidence.
            self.error = f"{type(err).__name__}: {err}"
            self._ready.set()
        finally:
            self._done.set()


@dataclass(slots=True)
class DnsPeer:
    port: int
    queries: list[str] = field(default_factory=list)
    error: str = ""
    _ready: threading.Event = field(default_factory=threading.Event)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def __enter__(self) -> DnsPeer:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("controlled DNS peer did not start")
        return self

    def __exit__(self, *_args: object) -> None:
        self._stop.set()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as wake:
            wake.sendto(b"\0", ("127.0.0.1", self.port))
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError("controlled DNS peer did not stop")
        if self.error:
            raise RuntimeError(self.error)

    def _run(self) -> None:
        try:
            import dns.message
            import dns.rcode
            import dns.rrset

            records = {
                ("voip.test.", 35): ('10 10 "S" "SIP+D2U" "" _sip._udp.voip.test.',),
                ("_sip._udp.voip.test.", 33): ("0 0 25063 localhost.",),
            }
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as server:
                server.bind(("127.0.0.1", self.port))
                server.settimeout(1)
                self._ready.set()
                while not self._stop.is_set():
                    try:
                        wire, address = server.recvfrom(4096)
                    except TimeoutError:
                        continue
                    if self._stop.is_set():
                        return
                    request = dns.message.from_wire(wire)
                    question = request.question[0]
                    name = str(question.name).lower()
                    kind = question.rdtype
                    response = dns.message.make_response(request)
                    if answers := records.get((name, kind)):
                        response.answer.append(
                            dns.rrset.from_text(
                                str(question.name), 30, "IN", kind, *answers
                            )
                        )
                    else:
                        response.set_rcode(dns.rcode.NOERROR)
                    self.queries.append(f"{name} {kind}")
                    server.sendto(response.to_wire(), address)
        except BaseException as err:  # noqa: BLE001 - retain DNS-peer evidence.
            self.error = f"{type(err).__name__}: {err}"
            self._ready.set()


async def _scenario(
    name: str,
    peer: SipPeer,
    destination: str,
) -> dict[str, Any]:
    peer.start()
    uri = sip.parse_sip_uri(destination)
    client = SipCallClient(
        local_ip="::1" if peer.family == socket.AF_INET6 else "127.0.0.1",
        local_name="Transport qualification",
        local_sip_port=0,
        local_rtp_port=46000,
        signaling_transport=peer.transport,
    )
    try:
        result = await client.invite(
            target=uri.user,
            remote_host=uri.host,
            remote_sip_port=int(
                uri.port or (5061 if peer.transport == "TLS" else 5060)
            ),
            request_uri=destination,
        )
        peer.wait()
        if result != "busy":
            raise RuntimeError(f"expected busy response, got {result!r}")
    finally:
        await client.close()
    invite = peer.messages[0].encode()
    return {
        "name": name,
        "status": "passed",
        "destination": destination,
        "transport": peer.transport.lower(),
        "request_line": peer.messages[0].split("\r\n", 1)[0],
        "via": (_headers(invite).get("via") or [""])[0],
        "ladder": [item.split(" ", 1)[0] for item in peer.messages],
        "client_result": result,
    }


async def _dns_scenario(peer: DnsPeer) -> dict[str, Any]:
    import dns.asyncresolver

    resolver = dns.asyncresolver.Resolver(configure=False)
    resolver.nameservers = ["127.0.0.1"]
    resolver.port = peer.port
    previous = dns.asyncresolver.default_resolver
    dns.asyncresolver.default_resolver = resolver
    try:
        targets = await SipServerResolver().resolve(
            sip.parse_sip_uri("sip:echo@voip.test")
        )
    finally:
        dns.asyncresolver.default_resolver = previous
    rendered = [
        {
            "host": target.host,
            "port": target.port,
            "transport": target.transport,
            "addresses": list(target.addresses),
        }
        for target in targets
    ]
    if len(rendered) != 1:
        raise RuntimeError(f"unexpected RFC 3263 target count: {rendered}")
    target = rendered[0]
    identity = {key: target[key] for key in ("host", "port", "transport")}
    if identity != {
        "host": "localhost",
        "port": 25063,
        "transport": "UDP",
    } or set(target["addresses"]) != {"127.0.0.1", "::1"}:
        raise RuntimeError(f"unexpected RFC 3263 targets: {rendered}")
    required_queries = {
        "voip.test. 35",
        "_sip._udp.voip.test. 33",
    }
    if not required_queries.issubset(peer.queries):
        raise RuntimeError(f"incomplete RFC 3263 query ladder: {peer.queries}")
    return {
        "name": "rfc3263_naptr_srv_real_dns",
        "status": "passed",
        "queries": list(peer.queries),
        "targets": rendered,
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "test_captures" / "sip-transport-lab",
    )
    parser.add_argument("--ca-cert", type=Path, required=True)
    parser.add_argument("--server-cert", type=Path, required=True)
    parser.add_argument("--server-key", type=Path, required=True)
    parser.add_argument("--dns-port", type=int, default=15353)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    trusted_file = Path(os.environ.get("SSL_CERT_FILE", ""))
    if trusted_file.resolve() != args.ca_cert.resolve():
        raise RuntimeError(
            "SIP client and harness must use --ca-cert via SSL_CERT_FILE"
        )
    run_dir = args.out_dir / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.server_cert, args.server_key)
    results = asyncio.run(_run_transport_scenarios(context))
    with DnsPeer(args.dns_port) as dns_peer:
        results.append(asyncio.run(_dns_scenario(dns_peer)))
    artifact = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "candidate": candidate_revision(),
        "results": results,
    }
    output = run_dir / "summary.json"
    output.write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    print(json.dumps({"artifact": str(output), **artifact}))
    return 0


async def _run_transport_scenarios(
    context: ssl.SSLContext,
) -> list[dict[str, Any]]:
    return [
        await _scenario(
            "sip_tls_verified",
            SipPeer(socket.AF_INET6, "TLS", "::", 25061, context),
            "sip:echo@localhost:25061;transport=tls",
        ),
        await _scenario(
            "sip_ipv6_udp",
            SipPeer(socket.AF_INET6, "UDP", "::1", 25062),
            "sip:echo@[::1]:25062;transport=udp",
        ),
    ]


if __name__ == "__main__":
    raise SystemExit(main())
