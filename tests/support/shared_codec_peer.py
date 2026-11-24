"""Independent SIP peer, explicit RTP packets and external FFmpeg media oracles."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import errno
import json
import math
import re
import secrets
import signal
import socket
import struct
import subprocess

from custom_components.voip_stack.core import sip
from custom_components.voip_stack.core.sip_auth import build_digest_authorization
from tests.test_ha_generic_negotiation_ha import GenericRegisteredPeer
from .g726_itu_reference import qualified_reference


@dataclass(frozen=True)
class CodecCase:
    name: str
    clock: int
    pcm_rate: int
    channels: int
    peer_tx_ms: int
    peer_rx_ms: int
    raw_format: str
    encoder: str
    bits: int = 0
    oracle_gap: str = ""

    @property
    def payload(self):
        return {"PCMU": 0, "PCMA": 8, "G722": 9, "G723": 4}.get(self.name, 110)

    @property
    def user_agent(self):
        return "Dahua UAC/1.0" if self.name == "PCM" else "IndependentCodecPeer/1.0"


# The packet/clock oracle is independent of the production codec registry.
# G722 has a16kHz PCM clock but an8kHz RTP clock (RFC3551 4.5.2).
CASES = (
    CodecCase("PCMU", 8000, 8000, 1, 10, 20, "mulaw", "pcm_mulaw"),
    CodecCase("PCMA", 8000, 8000, 1, 10, 20, "alaw", "pcm_alaw"),
    CodecCase("G722", 8000, 16000, 1, 10, 20, "g722", "g722"),
    CodecCase("L16", 48000, 48000, 1, 20, 20, "s16be", "pcm_s16be"),
    CodecCase("L24", 16000, 16000, 1, 10, 20, "s24be", "pcm_s24be"),
    CodecCase("PCM", 16000, 16000, 1, 10, 20, "s16le", "pcm_s16le"),
    CodecCase("OPUS", 48000, 48000, 2, 10, 20, "opus", "libopus"),
    *(
        CodecCase(
            f"G726-{rate}",
            8000,
            8000,
            1,
            10,
            20,
            "g726le",
            "g726le",
            rate // 8,
            "Requires a private corpus-validated official ITU G.191 encoder and decoder"
            if rate == 40
            else "",
        )
        for rate in (16, 24, 32, 40)
    ),
    CodecCase("G723", 8000, 8000, 1, 30, 60, "g723_1", "g723_1"),
)


def tone_pcm(rate, frequency, seconds=2):
    return struct.pack(
        f"<{round(rate * seconds)}h",
        *(
            round(5000 * math.sin(2 * math.pi * frequency * i / rate))
            for i in range(round(rate * seconds))
        ),
    )


def opus_packets(ogg):
    packets, current, offset = [], bytearray(), 0
    while offset < len(ogg):
        assert ogg[offset : offset + 4] == b"OggS"
        count = ogg[offset + 26]
        sizes = ogg[offset + 27 : offset + 27 + count]
        data = offset + 27 + count
        for size in sizes:
            current.extend(ogg[data : data + size])
            data += size
            if size < 255:
                if not current.startswith((b"OpusHead", b"OpusTags")):
                    packets.append(bytes(current))
                current.clear()
        offset = data
    return packets


def encoded_tone(case, frequency):
    if case.name == "G726-40":
        reference = qualified_reference()
        if reference is None:
            raise RuntimeError(
                "G72640 peer encoding requires the validated external ITU reference"
            )
        payload = reference.encode_pcm(tone_pcm(case.pcm_rate, frequency))
        size = case.clock * case.peer_tx_ms * case.bits // 8000
        assert len(payload) % size == 0
        return [payload[index : index + size] for index in range(0, len(payload), size)]
    args = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "s16le",
        "-ar",
        str(case.pcm_rate),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        "-c:a",
        case.encoder,
    ]
    if case.bits:
        args += ["-b:a", str(case.bits * 8000)]
    elif case.name == "G723":
        args += ["-b:a", "6300"]
    elif case.name == "OPUS":
        args += [
            "-b:a",
            "32000",
            "-vbr",
            "off",
            "-frame_duration",
            str(case.peer_tx_ms),
            "-application",
            "voip",
        ]
    args += ["-f", case.raw_format, "pipe:1"]
    completed = subprocess.run(
        args, input=tone_pcm(case.pcm_rate, frequency), capture_output=True, check=True
    )
    if case.name == "OPUS":
        return opus_packets(completed.stdout)
    if case.bits:
        size = case.clock * case.peer_tx_ms * case.bits // 8000
    elif case.name in {"PCMU", "PCMA", "G722"}:
        size = case.clock * case.peer_tx_ms // 1000
    elif case.name == "G723":
        size = 24 * case.peer_tx_ms // 30
    else:
        size = case.clock * case.peer_tx_ms * (3 if case.name == "L24" else 2) // 1000
    assert len(completed.stdout) % size == 0
    return [
        completed.stdout[index : index + size]
        for index in range(0, len(completed.stdout), size)
    ]


def audio_sdp(case, port, payload=None):
    pt = case.payload if payload is None else payload
    return (
        "v=0\r\no=- 701 1 IN IP4 127.0.0.1\r\ns=Independent codec peer\r\n"
        "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
        f"m=audio {port} RTP/AVP {pt}\r\na=rtpmap:{pt} {case.name}/{case.clock}/{case.channels}\r\n"
        + (f"a=fmtp:{pt} stereo=0;sprop-stereo=0\r\n" if case.name == "OPUS" else "")
        + f"a=ptime:{case.peer_rx_ms}\r\na=maxptime:{case.peer_rx_ms}\r\na=sendrecv\r\n"
    ).encode()


def mixed_default_sdp(port):
    """An independent generic UA advertises the standard default codec set."""
    mappings = (
        (0, "PCMU", 8000, 1),
        (8, "PCMA", 8000, 1),
        (9, "G722", 8000, 1),
        (110, "L16", 48000, 1),
        (111, "L24", 16000, 1),
        (113, "OPUS", 48000, 2),
    )
    body = (
        "v=0\r\no=- 702 1 IN IP4 127.0.0.1\r\ns=Independent mixed-codec peer\r\n"
        "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
        f"m=audio {port} RTP/AVP {' '.join(str(row[0]) for row in mappings)}\r\n"
    )
    body += "".join(
        f"a=rtpmap:{pt} {name}/{rate}/{channels}\r\n"
        for pt, name, rate, channels in mappings
    )
    return (
        body
        + "a=fmtp:113 stereo=0;sprop-stereo=0\r\na=ptime:20\r\na=maxptime:20\r\na=sendrecv\r\n"
    ).encode()


def audio_target(body, case):
    text = body.decode() if isinstance(body, bytes) else body
    port = int(re.search(r"^m=audio (\d+)", text, re.MULTILINE)[1])
    maps = [
        (int(pt), name.upper(), int(rate), int(channels or 1))
        for pt, name, rate, channels in re.findall(
            r"^a=rtpmap:(\d+) ([^/\s]+)/([0-9]+)(?:/([0-9]+))?\r?$", text, re.MULTILINE
        )
    ]
    selected = [
        row for row in maps if row[1:] == (case.name, case.clock, case.channels)
    ]
    assert len(selected) == 1, (case.name, maps)
    address = re.search(r"^c=IN IP4 ([^\r\n]+)", text, re.MULTILINE)[1]
    assert address == "127.0.0.1", (
        "Independent fixtures must not contact HOME or other networks"
    )
    return selected[0][0], (address, port)


def parse_packet(raw):
    first, pt, sequence, timestamp, ssrc = struct.unpack("!BBHII", raw[:12])
    assert first == 0x80, (
        "The independent peer requires plain RTPv2 without invented extensions"
    )
    return {
        "pt": pt & 127,
        "sequence": sequence,
        "timestamp": timestamp,
        "ssrc": ssrc,
        "payload": raw[12:],
    }


class CodecPeer(GenericRegisteredPeer):
    """Reuse the independent ordinary peer's wire transport, not HA's dialog/media owners."""

    def __init__(self, case, username, password):
        self.media = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.media.bind(("127.0.0.1", 0))
        self.media.setblocking(False)
        super().__init__(True, self.media.getsockname()[1])
        self.case, self.username, self.password = case, username, password
        self.user_agent = case.user_agent
        self.options_body = audio_sdp(case, 0)
        self.ringing, self.cancelled, self.ended = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        self.active = False
        self.current = None
        self.remote_sdp = None
        self.receive_sdp = None
        self.received, self.transmitted = [], []
        self.producer = self.receiver = None
        self.call_id = None
        self.cseq = 1
        self.from_header = None
        self.to_header = None
        self.target_uri = None
        self.remote_signal = None
        self.signalling = []

    def answer(self, request, address, status=200, body=b""):
        headers = [
            (key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")
        ]
        to = request.header("To")
        headers.append(
            ("To", to if ";tag=" in to else to + ";tag=independent-codec-peer")
        )
        headers.append(("User-Agent", self.user_agent))
        if body:
            headers.extend(
                [
                    ("Content-Type", "application/sdp"),
                    (
                        "Contact",
                        f"<sip:{self.username}@127.0.0.1:{self.transport.get_extra_info('sockname')[1]}>",
                    ),
                ]
            )
        reason = {
            180: "Ringing",
            200: "OK",
            405: "Method Not Allowed",
            487: "Request Terminated",
            488: "Not Acceptable Here",
        }[status]
        self.transport.sendto(
            sip.build_response(status, reason, headers, body), address
        )

    def datagram_received(self, raw, address):
        self.signalling.append(raw)
        message = sip.parse_message(raw)
        if message.is_response:
            self.messages.put_nowait(message)
        elif message.method == "OPTIONS":
            self.answer(message, address, body=self.options_body)
            self.options_replied.set()
        elif message.method == "INVITE":
            assert not self.active
            self.ringing.clear()
            self.cancelled.clear()
            self.ended.clear()
            self.acked.clear()
            audio_target(message.body, self.case)
            self.current = (message, address)
            self.offers.append(message.body)
            self.remote_sdp = message.body
            self.call_id = message.header("Call-ID")
            self.answer(message, address, status=180)
            self.ringing.set()
        elif message.method == "CANCEL":
            self.answer(message, address)
            if self.current and not self.active:
                invite, original_address = self.current
                self.answer(invite, original_address, status=487)
            self.current = None
            self.cancelled.set()
        elif message.method == "ACK":
            self.acked.set()
        elif message.method == "BYE":
            self.answer(message, address)
            self.active = False
            self.current = None
            self.ended.set()
        elif message.method in {"NOTIFY", "INFO"}:
            self.answer(message, address)

    async def response(self, method):
        async with asyncio.timeout(4):
            while True:
                message = await self.messages.get()
                if message.header("CSeq").endswith(" " + method):
                    return message

    def headers(self, method, cseq, *, to=None):
        values = [
            (
                "Via",
                f"SIP/2.0/UDP 127.0.0.1:{self.transport.get_extra_info('sockname')[1]};branch=z9hG4bK-{secrets.token_hex(8)};rport",
            ),
            ("From", self.from_header),
            ("To", to or self.to_header),
            ("Call-ID", self.call_id),
            ("CSeq", f"{cseq} {method}"),
            (
                "Contact",
                f"<sip:{self.username}@127.0.0.1:{self.transport.get_extra_info('sockname')[1]}>",
            ),
            ("User-Agent", self.user_agent),
        ]
        return values

    async def register(self, listener):
        uri = f"sip:127.0.0.1:{listener}"
        self.call_id = f"registration-{self.username}-{secrets.token_hex(4)}"
        self.from_header = f"<sip:{self.username}@127.0.0.1>;tag=registered-peer"
        self.to_header = f"<sip:{self.username}@127.0.0.1>"
        challenge = None
        for sequence in [1, 2]:
            headers = self.headers("REGISTER", sequence) + [("Expires", "120")]
            if sequence == 2:
                assert challenge is not None
                headers.append(
                    (
                        "Authorization",
                        build_digest_authorization(
                            challenge_header=challenge.header("WWW-Authenticate"),
                            username=self.username,
                            password=self.password,
                            method="REGISTER",
                            uri=uri,
                        ),
                    )
                )
            self.transport.sendto(
                sip.build_request("REGISTER", uri, headers), ("127.0.0.1", listener)
            )
            message = await self.response("REGISTER")
            if sequence == 1:
                challenge = message
                assert message.status_code == 401
            else:
                assert message.status_code == 200
        await asyncio.wait_for(self.options_replied.wait(), 3)

    async def dial(self, listener, target="221"):
        self.cseq = 1
        self.ringing.clear()
        self.cancelled.clear()
        self.ended.clear()
        self.acked.clear()
        self.remote_signal = ("127.0.0.1", listener)
        self.target_uri = f"sip:{target}@127.0.0.1:{listener}"
        self.call_id = "codec-call-" + secrets.token_hex(8)
        self.from_header = f"<sip:{self.username}@127.0.0.1>;tag={secrets.token_hex(5)}"
        self.to_header = f"<sip:{target}@127.0.0.1>"
        self.receive_sdp = audio_sdp(self.case, self.media_port)
        headers = self.headers("INVITE", 1) + [("Content-Type", "application/sdp")]
        self.transport.sendto(
            sip.build_request("INVITE", self.target_uri, headers, self.receive_sdp),
            self.remote_signal,
        )
        message = await self.response("INVITE")
        if message.status_code == 401:
            self.transport.sendto(
                sip.build_request(
                    "ACK",
                    self.target_uri,
                    self.headers("ACK", 1, to=message.header("To")),
                ),
                self.remote_signal,
            )
            headers = self.headers("INVITE", 2) + [
                ("Content-Type", "application/sdp"),
                (
                    "Authorization",
                    build_digest_authorization(
                        challenge_header=message.header("WWW-Authenticate"),
                        username=self.username,
                        password=self.password,
                        method="INVITE",
                        uri=self.target_uri,
                    ),
                ),
            ]
            self.cseq = 2
            self.transport.sendto(
                sip.build_request("INVITE", self.target_uri, headers, self.receive_sdp),
                self.remote_signal,
            )
            message = await self.response("INVITE")
        assert message.status_code in {100, 180, 183}, (
            message.status_code,
            message.reason,
        )

    async def established(self):
        async with asyncio.timeout(5):
            while True:
                message = await self.response("INVITE")
                if message.status_code == 200:
                    break
                assert message.status_code < 200, (message.status_code, message.reason)
        self.remote_sdp = message.body
        audio_target(self.remote_sdp, self.case)
        self.to_header = message.header("To")
        contact = message.header("Contact")
        target = re.search(r"<([^>]+)>", contact)
        self.target_uri = target[1] if target else self.target_uri
        self.transport.sendto(
            sip.build_request("ACK", self.target_uri, self.headers("ACK", self.cseq)),
            self.remote_signal,
        )
        self.active = True

    async def accept(self):
        invite, address = self.current
        payload, _ = audio_target(invite.body, self.case)
        self.receive_sdp = audio_sdp(self.case, self.media_port, payload)
        self.answer(invite, address, body=self.receive_sdp)
        self.active = True
        await asyncio.wait_for(self.acked.wait(), 3)

    async def start_media(self, frequency):
        self.received = []
        self.transmitted = []
        payload, target = audio_target(self.remote_sdp, self.case)
        expected_pt, _ = audio_target(self.receive_sdp, self.case)
        packets = await asyncio.to_thread(encoded_tone, self.case, frequency)
        clock_step = self.case.clock * self.case.peer_tx_ms // 1000
        ssrc = secrets.randbits(32)

        async def send():
            deadline = asyncio.get_running_loop().time()
            for index, encoded in enumerate(packets):
                raw = (
                    struct.pack(
                        "!BBHII", 0x80, payload, index & 65535, index * clock_step, ssrc
                    )
                    + encoded
                )
                await asyncio.get_running_loop().sock_sendto(self.media, raw, target)
                self.transmitted.append(raw)
                deadline += self.case.peer_tx_ms / 1000
                await asyncio.sleep(
                    max(0, deadline - asyncio.get_running_loop().time())
                )

        async def receive():
            while True:
                raw, _ = await asyncio.get_running_loop().sock_recvfrom(
                    self.media, 8192
                )
                packet = parse_packet(raw)
                assert packet["pt"] == expected_pt
                self.received.append(raw)

        self.producer, self.receiver = (
            asyncio.create_task(send()),
            asyncio.create_task(receive()),
        )

    async def stop_media(self):
        errors = [
            task.exception()
            for task in (self.producer, self.receiver)
            if task is not None
            and task.done()
            and not task.cancelled()
            and task.exception() is not None
        ]
        for task in (self.producer, self.receiver):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (self.producer, self.receiver) if task is not None),
            return_exceptions=True,
        )
        self.producer = self.receiver = None
        assert not errors, errors

    def drain_media(self):
        """Observe and discard only the previous completed call's UDP tail."""
        count = 0
        while True:
            try:
                self.media.recvfrom(8192)
                count += 1
            except BlockingIOError:
                return count

    async def close(self):
        await self.stop_media()
        if hasattr(self, "transport"):
            self.transport.close()
        self.media.close()


def payload_duration_ms(case, payload):
    if case.name == "OPUS":
        config = payload[0] >> 3
        frame = (
            (10, 20, 40, 60)[config & 3]
            if config < 12
            else (10, 20)[config & 1]
            if config < 16
            else 2.5 * (1 << (config & 3))
        )
        count = (1, 2, 2)[payload[0] & 3] if (payload[0] & 3) < 3 else payload[1] & 63
        return frame * count
    if case.name == "G723":
        offset = 0
        frames = 0
        while offset < len(payload):
            size = (24, 20, 4, 0)[payload[offset] & 3]
            assert size and offset + size <= len(payload)
            offset += size
            frames += 1
        return frames * 30
    if case.bits:
        return len(payload) * 8000 / (case.clock * case.bits)
    width = (
        1 if case.name in {"PCMU", "PCMA", "G722"} else 3 if case.name == "L24" else 2
    )
    return len(payload) * 1000 / (case.clock * width)


def check_wire(case, packets, *, payload_limit=1200):
    assert len(packets) >= 12, len(packets)
    rows = [parse_packet(packet) for packet in packets]
    assert len({row["ssrc"] for row in rows}) == 1
    durations = [payload_duration_ms(case, row["payload"]) for row in rows]
    assert all(0 < value <= 120 for value in durations)
    assert all(len(row["payload"]) <= payload_limit for row in rows)
    # Startup silence suppression can create a clock gap. Every steady packet
    # thereafter must advance by its actual independently decoded duration.
    for index in range(6, len(rows) - 1):
        expected = round(case.clock * durations[index] / 1000)
        assert (
            rows[index + 1]["timestamp"] - rows[index]["timestamp"]
        ) & 0xFFFFFFFF == expected
        assert (rows[index + 1]["sequence"] - rows[index]["sequence"]) & 65535 == 1
    return {
        "packets": len(rows),
        "rtp_clock_hz": case.clock,
        "payload_types": sorted({row["pt"] for row in rows}),
        "payload_bytes": sorted({len(row["payload"]) for row in rows}),
        "actual_packet_ms": sorted(set(durations)),
        "ssrc": rows[0]["ssrc"],
        "timestamp_first": rows[0]["timestamp"],
        "timestamp_last": rows[-1]["timestamp"],
    }


def _rtp_receiver_port():
    for _ in range(64):
        port = 40000 + 2 * secrets.randbelow(5000)
        pair = []
        try:
            for number in [port, port + 1]:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                pair.append(sock)
                sock.bind(("127.0.0.1", number))
            return port
        except OSError:
            pass
        finally:
            for sock in pair:
                sock.close()
    raise RuntimeError("No isolated FFmpeg receiver port")


async def decode_named_rtp(case, packets, receive_sdp, directory):
    """Decode the actual negotiated SDP name in an external FFmpeg process."""
    directory.mkdir(parents=True, exist_ok=True)
    if case.name == "G726-40":
        reference = qualified_reference()
        if reference is None:
            raise RuntimeError(
                "G72640 peer decoding requires the validated external ITU reference"
            )
        payload_type, _target = audio_target(receive_sdp, case)
        rows = [parse_packet(raw) for raw in packets]
        assert all(row["pt"] == payload_type for row in rows)
        payload = b"".join(row["payload"] for row in rows)
        decoded = await asyncio.to_thread(reference.decode_payload, payload)
        (directory / "actual-name.sdp").write_bytes(
            receive_sdp if isinstance(receive_sdp, bytes) else receive_sdp.encode()
        )
        (directory / "packets.hex").write_text(
            "\n".join(raw.hex() for raw in packets) + "\n"
        )
        (directory / "decoded.pcm").write_bytes(decoded)
        (directory / "itu-reference.json").write_text(
            json.dumps(reference.provenance, indent=2) + "\n"
        )
        return decoded
    # PCM is a documented Dahua profile, not a standard FFmpeg RTP name.
    # Its explicit little-endian payload is independently decoded as raw PCM.
    if case.name == "PCM":
        payload = b"".join(parse_packet(packet)["payload"] for packet in packets)
        result = await asyncio.to_thread(
            subprocess.run,
            [
                "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "s16le",
                "-ar",
                str(case.pcm_rate),
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-f",
                "s16le",
                "pipe:1",
            ],
            input=payload,
            capture_output=True,
            check=True,
        )
        (directory / "decoded.pcm").write_bytes(result.stdout)
        return result.stdout
    source_template = receive_sdp.decode() if isinstance(receive_sdp, bytes) else receive_sdp
    (directory / "packets.hex").write_text(
        "\n".join(packet.hex() for packet in packets) + "\n"
    )
    # The probe releases its own sockets before FFmpeg binds. Only an explicit
    # bind conflict may retry this startup handoff; no media has been replayed.
    for attempt in range(1, 4):
        port = _rtp_receiver_port()
        source = re.sub(r"(?m)^m=audio \d+", f"m=audio {port}", source_template)
        path = directory / "actual-name.sdp"
        path.write_text(source)
        (directory / f"listener-attempt-{attempt}.sdp").write_text(source)
        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "debug",
            "-protocol_whitelist",
            "file,udp,rtp",
            "-max_delay",
            "0",
            "-reorder_queue_size",
            "0",
            "-analyzeduration",
            "0",
            "-probesize",
            "32",
            "-i",
            str(path),
            "-t",
            str(
                max(
                    0.3,
                    sum(
                        payload_duration_ms(case, parse_packet(raw)["payload"])
                        for raw in packets
                    )
                    / 1000
                    - 0.12,
                )
            ),
            "-ac",
            "1",
            "-ar",
            str(case.pcm_rate),
            "-f",
            "s16le",
            "pipe:1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        logs = []
        ready = asyncio.Event()

        async def log():
            count = 0
            while line := await process.stderr.readline():
                logs.append(line)
                if b"end receive buffer size reported" in line:
                    count += 1
                    if count == 2:
                        ready.set()

        reader = asyncio.create_task(log())
        output = asyncio.create_task(process.stdout.read())
        exited = asyncio.create_task(process.wait())
        listening = asyncio.create_task(ready.wait())
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.setblocking(False)
        try:
            done, _ = await asyncio.wait(
                (listening, exited), timeout=3, return_when=asyncio.FIRST_COMPLETED
            )
            if exited in done or process.returncode is not None:
                await reader
                detail = b"".join(logs).decode(errors="replace")
                conflict = "bind failed" in detail and "Address already in use" in detail
                message = (f"FFmpeg RTP listener exited before replay: exit={process.returncode}, "
                           f"attempt={attempt}/3\n{detail}")
                if conflict:
                    if attempt < 3:
                        continue
                    raise OSError(errno.EADDRINUSE, message)
                raise RuntimeError(message)
            if listening not in done:
                detail = b"".join(logs).decode(errors="replace")
                raise TimeoutError(
                    f"FFmpeg RTP listener not ready within 3s before replay: exit={process.returncode}\n{detail}"
                )
            deadline = asyncio.get_running_loop().time()
            for raw in packets:
                await asyncio.get_running_loop().sock_sendto(
                    sender, raw, ("127.0.0.1", port)
                )
                deadline += payload_duration_ms(case, parse_packet(raw)["payload"]) / 1000
                await asyncio.sleep(max(0, deadline - asyncio.get_running_loop().time()))
            await asyncio.sleep(0.05)
            if process.returncode is None:
                process.send_signal(signal.SIGINT)
            await asyncio.wait_for(process.wait(), 3)
            decoded = await asyncio.wait_for(output, 1)
            assert decoded, b"".join(logs)[-2000:]
            (directory / "decoded.pcm").write_bytes(decoded)
            return decoded
        finally:
            sender.close()
            if process.returncode is None:
                process.kill()
                await process.wait()
            listening.cancel()
            await asyncio.gather(reader, output, exited, listening, return_exceptions=True)
            (directory / f"ffmpeg-attempt-{attempt}.log").write_bytes(b"".join(logs))
            (directory / "ffmpeg.log").write_bytes(b"".join(logs))


def measure_tone(pcm, rate, frequency, *, pcm_format="s16le", channels=1):
    import numpy as np

    if pcm_format == "s24le":
        raw = np.frombuffer(pcm, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        values = raw[:, 0] | (raw[:, 1] << 8) | (raw[:, 2] << 16)
        values = ((values ^ 0x800000) - 0x800000) / 256
    else:
        assert pcm_format == "s16le", pcm_format
        values = np.frombuffer(pcm, dtype="<i2").astype(float)
    values = values.reshape(-1, channels).mean(axis=1)
    assert len(values) >= rate * 0.3, (len(values), rate)
    values = values[round(rate * 0.12) :]
    values = values[-min(len(values), round(rate * 0.5)) :]
    values -= values.mean()
    rms = float(np.sqrt(np.mean(values * values)))
    power = abs(np.fft.rfft(values * np.hanning(len(values)))) ** 2
    bins = np.fft.rfftfreq(len(values), 1 / rate)
    audible = (bins >= 100) & (bins <= 3000)
    peak = float(bins[np.argmax(np.where(audible, power, 0))])
    ratio = float(power[abs(bins - frequency) <= 10].sum() / power[audible].sum())
    assert rms > 30 and abs(peak - frequency) <= 8 and ratio > 0.65, (
        rms,
        peak,
        ratio,
        frequency,
    )
    return {
        "expected_hz": frequency,
        "observed_peak_hz": peak,
        "tone_power_ratio": ratio,
        "rms_s16_scale": rms,
        "samples": len(values),
        "rate": rate,
    }
