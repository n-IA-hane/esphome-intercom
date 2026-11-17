#!/usr/bin/env python3
"""Qualify REFER/NOTIFY and modern Digest against the isolated HA runtime."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import hmac
import json
from pathlib import Path
import re
import socket
import sys
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tools"), str(ROOT / "scripts")]

from inbound_routing_qualification import (  # noqa: E402
    FlowSnapshot,
    HomeAssistantApi,
)
from live_voip_qualification import candidate_revision  # noqa: E402
from run_answered_sipp_lab import lab_token, runtime_quiescence  # noqa: E402


_DIGEST_PARAM = re.compile(r'([\w-]+)=(?:"([^"]*)"|([^,\s]+))')
_PASSWORD = "qualification-only"


def _digest(value: str, algorithm: str) -> str:
    name = {
        "SHA-256": "sha256",
        "SHA-512-256": "sha512_256",
    }[algorithm]
    return hashlib.new(name, value.encode(), usedforsecurity=False).hexdigest()


def _digest_params(value: str) -> dict[str, str]:
    return {
        match.group(1).lower(): match.group(2) or match.group(3) or ""
        for match in _DIGEST_PARAM.finditer(value)
    }


def _headers(raw: bytes) -> tuple[str, dict[str, list[str]], bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("utf-8", "replace").split("\r\n")
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if separator:
            headers.setdefault(name.strip().lower(), []).append(value.strip())
    return lines[0], headers, body


def _response(
    headers: dict[str, list[str]],
    status: int,
    reason: str,
    extra: tuple[tuple[str, str], ...] = (),
) -> bytes:
    values = [f"SIP/2.0 {status} {reason}"]
    for name in ("via", "from", "to", "call-id", "cseq"):
        values.extend(f"{name.title()}: {value}" for value in headers.get(name, ()))
    values.extend(f"{name}: {value}" for name, value in extra)
    values.extend(("Content-Length: 0", "", ""))
    return "\r\n".join(values).encode()


@dataclass(slots=True)
class ReferCalleePeer:
    """External dialog peer that completes one REFER subscription."""

    port: int
    result: dict[str, object] | None = None
    error: str = ""
    ready: threading.Event = field(init=False)
    done: threading.Event = field(init=False)

    def __post_init__(self) -> None:
        self.ready = threading.Event()
        self.done = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()
        if not self.ready.wait(3):
            raise RuntimeError("REFER callee peer did not bind")

    def wait(self) -> dict[str, object]:
        if not self.done.wait(30):
            raise RuntimeError("REFER callee peer timed out")
        if self.error:
            raise RuntimeError(self.error)
        assert self.result is not None
        return self.result

    @staticmethod
    def _tagged(value: str, tag: str) -> str:
        return value if ";tag=" in value.lower() else f"{value};tag={tag}"

    def _reply(
        self,
        sock: socket.socket,
        request: bytes,
        address: tuple[str, int],
        status: int,
        reason: str,
        *,
        body: bytes = b"",
    ) -> None:
        _start, headers, _body = _headers(request)
        values = [f"SIP/2.0 {status} {reason}"]
        values.extend(f"Via: {value}" for value in headers.get("via", ()))
        values.extend(
            (
                f"From: {headers['from'][0]}",
                f"To: {self._tagged(headers['to'][0], 'refer-callee')}",
                f"Call-ID: {headers['call-id'][0]}",
                f"CSeq: {headers['cseq'][0]}",
                f"Contact: <sip:callee@127.0.0.1:{self.port}>",
            )
        )
        if body:
            values.append("Content-Type: application/sdp")
        values.extend((f"Content-Length: {len(body)}", "", ""))
        sock.sendto("\r\n".join(values).encode() + body, address)

    def _notify(
        self,
        sock: socket.socket,
        address: tuple[str, int],
        invite_headers: dict[str, list[str]],
        cseq: int,
        status: int,
        *,
        terminated: bool,
    ) -> None:
        body = f"SIP/2.0 {status} {'OK' if status < 300 else 'Failed'}\r\n".encode()
        request = [
            f"NOTIFY sip:ha@{address[0]}:{address[1]} SIP/2.0",
            f"Via: SIP/2.0/UDP 127.0.0.1:{self.port};branch=z9hG4bK-notify-{cseq}",
            f"From: {self._tagged(invite_headers['to'][0], 'refer-callee')}",
            f"To: {invite_headers['from'][0]}",
            f"Call-ID: {invite_headers['call-id'][0]}",
            f"CSeq: {cseq} NOTIFY",
            "Event: refer;id=2",
            (
                "Subscription-State: terminated;reason=noresource"
                if terminated
                else "Subscription-State: active;expires=60"
            ),
            "Content-Type: message/sipfrag",
            f"Content-Length: {len(body)}",
            "",
            "",
        ]
        sock.sendto("\r\n".join(request).encode() + body, address)

    @staticmethod
    def _receive_method(
        sock: socket.socket,
        method: str,
    ) -> tuple[bytes, tuple[str, int]]:
        while True:
            raw, address = sock.recvfrom(65535)
            start, _headers_map, _body = _headers(raw)
            if start.startswith(f"{method} "):
                return raw, address

    def _run(self) -> None:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.bind(("127.0.0.1", self.port))
                sock.settimeout(25)
                self.ready.set()
                invite, ha = self._receive_method(sock, "INVITE")
                _start, invite_headers, _body = _headers(invite)
                self._reply(sock, invite, ha, 180, "Ringing")
                answer = (
                    b"v=0\r\no=peer 1 1 IN IP4 127.0.0.1\r\ns=REFER peer\r\n"
                    b"c=IN IP4 127.0.0.1\r\nt=0 0\r\nm=audio 46020 RTP/AVP 8\r\n"
                    b"a=rtpmap:8 PCMA/8000\r\na=sendrecv\r\n"
                )
                self._reply(sock, invite, ha, 200, "OK", body=answer)
                self._receive_method(sock, "ACK")
                refer, ha = self._receive_method(sock, "REFER")
                self._reply(sock, refer, ha, 202, "Accepted")
                self._notify(sock, ha, invite_headers, 2, 100, terminated=False)
                response, _address = sock.recvfrom(65535)
                if not _headers(response)[0].startswith("SIP/2.0 200"):
                    raise RuntimeError("initial NOTIFY was not acknowledged")
                self._notify(sock, ha, invite_headers, 3, 200, terminated=True)
                response, _address = sock.recvfrom(65535)
                if not _headers(response)[0].startswith("SIP/2.0 200"):
                    raise RuntimeError("final NOTIFY was not acknowledged")
                bye, address = self._receive_method(sock, "BYE")
                self._reply(sock, bye, address, 200, "OK")
                self.result = {
                    "status": "passed",
                    "ladder": [
                        "INVITE",
                        "ACK",
                        "REFER",
                        "NOTIFY 100",
                        "NOTIFY 200",
                        "BYE",
                    ],
                }
        except Exception as error:  # noqa: BLE001, preserve peer evidence.
            self.error = str(error)
        finally:
            self.ready.set()
            self.done.set()


@dataclass(slots=True)
class DigestRegistrarPeer:
    port: int
    algorithm: str
    qop: str
    result: dict[str, object] | None = None
    error: str = ""
    stage: str = "created"
    ready: threading.Event = field(init=False)
    done: threading.Event = field(init=False)
    thread: threading.Thread = field(init=False)

    def __post_init__(self) -> None:
        self.ready = threading.Event()
        self.done = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.thread.start()
        if not self.ready.wait(3):
            raise RuntimeError("Digest registrar peer did not bind")

    def wait(self) -> dict[str, object]:
        if not self.done.wait(35):
            raise RuntimeError("Digest registrar peer timed out")
        if self.error:
            raise RuntimeError(self.error)
        assert self.result is not None
        return self.result

    def _run(self) -> None:
        nonce = hashlib.sha256(
            f"{self.algorithm}:{self.qop}:{self.port}".encode()
        ).hexdigest()[:32]
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.bind(("127.0.0.1", self.port))
                sock.settimeout(30)
                self.stage = "waiting_initial_register"
                self.ready.set()
                first, addr = sock.recvfrom(65535)
                self.stage = "received_initial_register"
                start, first_headers, _body = _headers(first)
                if not start.startswith("REGISTER "):
                    raise RuntimeError(f"expected REGISTER, got {start}")
                challenge = (
                    f'Digest realm="voip_stack_lab", nonce="{nonce}", '
                    f'algorithm={self.algorithm}, qop="{self.qop}"'
                )
                sock.sendto(
                    _response(
                        first_headers,
                        401,
                        "Unauthorized",
                        (("WWW-Authenticate", challenge),),
                    ),
                    addr,
                )
                self.stage = "waiting_authenticated_register"
                second, second_addr = sock.recvfrom(65535)
                self.stage = "received_authenticated_register"
                start, headers, body = _headers(second)
                authorization = (headers.get("authorization") or [""])[0]
                params = _digest_params(authorization)
                method, uri, _version = start.split(" ", 2)
                if params.get("algorithm", "").upper() != self.algorithm:
                    raise RuntimeError("client selected the wrong Digest algorithm")
                if params.get("qop", "").lower() != self.qop:
                    raise RuntimeError("client selected the wrong Digest qop")
                ha1 = _digest(f"sipp:voip_stack_lab:{_PASSWORD}", self.algorithm)
                entity = (
                    f":{_digest(body.decode('latin1'), self.algorithm)}"
                    if self.qop == "auth-int"
                    else ""
                )
                ha2 = _digest(f"{method}:{uri}{entity}", self.algorithm)
                expected = _digest(
                    ":".join(
                        (
                            ha1,
                            nonce,
                            params["nc"],
                            params["cnonce"],
                            self.qop,
                            ha2,
                        )
                    ),
                    self.algorithm,
                )
                if not hmac.compare_digest(expected, params.get("response", "")):
                    raise RuntimeError("Digest response verification failed")
                sock.sendto(_response(headers, 200, "OK"), second_addr)
                self.stage = "complete"
                self.result = {
                    "status": "passed",
                    "algorithm": self.algorithm,
                    "qop": self.qop,
                    "nonce_count": params.get("nc"),
                    "request_body_bytes": len(body),
                }
        except Exception as error:  # noqa: BLE001, preserve peer evidence.
            self.error = f"{self.stage}: {error}"
        finally:
            self.ready.set()
            self.done.set()


def _refer_scenario(target: str) -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" ?>
<!DOCTYPE scenario SYSTEM "sipp.dtd">
<scenario name="VoIP Stack REFER NOTIFY qualification">
  <send retrans="500"><![CDATA[
    INVITE sip:[service]@[remote_ip]:[remote_port] SIP/2.0
    Via: SIP/2.0/UDP [local_ip]:[local_port];branch=z9hG4bK-[pid]-refer
    From: "SIPp transferor" <sip:sipp@[local_ip]:[local_port]>;tag=[pid]refer
    To: <sip:[service]@[remote_ip]:[remote_port]>
    Call-ID: [call_id]
    CSeq: 1 INVITE
    Contact: <sip:sipp@[local_ip]:[local_port]>
    Content-Type: application/sdp
    Content-Length: [len]

    v=0
    o=sipp 1 1 IN IP4 [local_ip]
    s=REFER qualification
    c=IN IP4 [local_ip]
    t=0 0
    m=audio 6000 RTP/AVP 8
    a=rtpmap:8 PCMA/8000
    a=sendrecv
  ]]></send>
  <recv response="100" optional="true" />
  <recv response="180" optional="true" />
  <recv response="200" rtd="true" timeout="10000" />
  <send><![CDATA[
    ACK sip:[service]@[remote_ip]:[remote_port] SIP/2.0
    Via: SIP/2.0/UDP [local_ip]:[local_port];branch=z9hG4bK-[pid]-ack
    From: "SIPp transferor" <sip:sipp@[local_ip]:[local_port]>;tag=[pid]refer
    To: <sip:[service]@[remote_ip]:[remote_port]>[peer_tag_param]
    Call-ID: [call_id]
    CSeq: 1 ACK
    Content-Length: 0
  ]]></send>
  <send retrans="500"><![CDATA[
    REFER sip:[service]@[remote_ip]:[remote_port] SIP/2.0
    Via: SIP/2.0/UDP [local_ip]:[local_port];branch=z9hG4bK-[pid]-refer2
    From: "SIPp transferor" <sip:sipp@[local_ip]:[local_port]>;tag=[pid]refer
    To: <sip:[service]@[remote_ip]:[remote_port]>[peer_tag_param]
    Call-ID: [call_id]
    CSeq: 2 REFER
    Refer-To: <{target}>
    Referred-By: <sip:sipp@[local_ip]:[local_port]>
    Content-Length: 0
  ]]></send>
  <recv response="202" timeout="5000" />
  <recv request="NOTIFY" timeout="5000">
    <action>
      <ereg regexp="SIP/2.0 100" search_in="body" check_it="true" assign_to="1" />
      <log message="initial NOTIFY [$1]" />
    </action>
  </recv>
  <send><![CDATA[
    SIP/2.0 200 OK
    [last_Via:]
    [last_From:]
    [last_To:]
    [last_Call-ID:]
    [last_CSeq:]
    Content-Length: 0
  ]]></send>
  <recv request="NOTIFY" timeout="12000">
    <action>
      <ereg regexp="SIP/2.0 2[0-9][0-9]" search_in="body" check_it="true" assign_to="2" />
      <log message="final NOTIFY [$2]" />
    </action>
  </recv>
  <send><![CDATA[
    SIP/2.0 200 OK
    [last_Via:]
    [last_From:]
    [last_To:]
    [last_Call-ID:]
    [last_CSeq:]
    Content-Length: 0
  ]]></send>
  <send retrans="500"><![CDATA[
    BYE sip:[service]@[remote_ip]:[remote_port] SIP/2.0
    Via: SIP/2.0/UDP [local_ip]:[local_port];branch=z9hG4bK-[pid]-bye
    From: "SIPp transferor" <sip:sipp@[local_ip]:[local_port]>;tag=[pid]refer
    To: <sip:[service]@[remote_ip]:[remote_port]>[peer_tag_param]
    Call-ID: [call_id]
    CSeq: 3 BYE
    Content-Length: 0
  ]]></send>
  <recv response="200" timeout="5000" />
</scenario>
"""


async def _run_refer() -> dict[str, object]:
    from custom_components.voip_stack.core import sip_transfer
    from custom_components.voip_stack.sip_client import SipCallClient

    callee = ReferCalleePeer(20020)
    callee.start()
    client = SipCallClient(
        local_ip="127.0.0.1",
        local_name="REFER qualification",
        local_sip_port=0,
        local_rtp_port=46000,
        include_common_codecs=True,
    )
    try:
        result = await client.invite(
            target="callee",
            remote_host="127.0.0.1",
            remote_sip_port=callee.port,
            request_uri=f"sip:callee@127.0.0.1:{callee.port}",
        )
        if result == "ringing":
            result = await client.wait_for_final()
        if result != "in_call":
            raise RuntimeError(f"REFER peer INVITE failed: {result}")
        transfer = await client.refer(
            sip_transfer.SipReferTarget("sip:replacement@127.0.0.1:2501")
        )
        if not transfer.accepted or transfer.status != 200:
            raise RuntimeError(f"REFER failed: {transfer}")
        await client.terminate()
        return {**callee.wait(), "peer": "external UDP dialog peer"}
    finally:
        await client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-url", default="http://127.0.0.1:18123")
    parser.add_argument(
        "--credentials",
        type=Path,
        default=Path("/home/codex/ha-voip-lab/.credentials"),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=ROOT / "test_captures/sip-extensions-lab",
    )
    return parser.parse_args()


def _wait_runtime(base_url: str, token: str) -> dict[str, object]:
    deadline = time.monotonic() + 30
    error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return asyncio.run(runtime_quiescence(base_url, token))
        except Exception as current:  # noqa: BLE001, HA is still loading.
            error = current
            time.sleep(0.25)
    raise RuntimeError("VoIP Stack runtime did not become ready") from error


def main() -> int:
    args = parse_args()
    run_dir = args.out_dir / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir.mkdir(parents=True, exist_ok=False)
    token = lab_token(args.ha_url, args.credentials)
    api = HomeAssistantApi(base_url=args.ha_url, token=token)
    _wait_runtime(args.ha_url, token)
    snapshot = FlowSnapshot.capture(api)
    results: list[dict[str, object]] = []
    try:
        for index, (algorithm, qop) in enumerate(
            (("SHA-256", "auth"), ("SHA-512-256", "auth"), ("SHA-256", "auth-int"))
        ):
            peer = DigestRegistrarPeer(19991 + index, algorithm, qop)
            peer.start()
            snapshot.apply(
                api,
                trunk_override={
                    "trunk_transport": "udp",
                    "trunk_server": "127.0.0.1",
                    "trunk_port": peer.port,
                    "trunk_domain": "127.0.0.1",
                    "trunk_username": "sipp",
                    "trunk_auth_username": "sipp",
                    "trunk_password": _PASSWORD,
                    "trunk_register_expires": 60,
                    "trunk_outbound_proxy": "",
                },
            )
            results.append({"scenario": f"digest_{algorithm}_{qop}", **peer.wait()})
        results.append(
            {
                "scenario": "refer_notify_success",
                **asyncio.run(_run_refer()),
            }
        )
        results.append(
            {
                "scenario": "postconditions",
                "status": "passed",
                "runtime": _wait_runtime(args.ha_url, token),
            }
        )
    except Exception as error:  # noqa: BLE001, persist complete live evidence.
        results.append({"scenario": "runner", "status": "failed", "error": str(error)})
    finally:
        with suppress(Exception):
            snapshot.apply(api)
    artifact = {
        "schema_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "candidate": candidate_revision(),
        "results": results,
    }
    (run_dir / "summary.json").write_text(
        json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(artifact, indent=2, sort_keys=True))
    return 1 if any(item.get("status") != "passed" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
