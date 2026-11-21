"""Turn a SIP capture into media corpus cases.

tshark dissects the capture. This tool pairs every offer with its answer, redacts addresses and
names, and writes one directory per dialog in the layout described by
``tests/media_corpus/README.md``. The expected contract of a case is written by hand afterwards:
a capture shows what a peer sent, not what the right answer is.

Usage:
    media_corpus_capture.py index CAPTURE
    media_corpus_capture.py cases CAPTURE --local-ip ADDRESS --out DIRECTORY
        [--map OLD=NEW ...] [--rename OLD=NEW ...] [--windows FILE] [--origin TEXT]
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess

FIELDS = (
    "frame.time_epoch", "ip.src", "ip.dst", "sip.Method", "sip.Status-Code", "sip.CSeq.method",
    "sip.CSeq.seq", "sip.Call-ID", "sip.User-Agent", "sip.Server", "sip.msg_body",
)


@dataclass(frozen=True, slots=True)
class SdpMessage:
    """One SIP message that carries a session description."""

    time: float
    source: str
    destination: str
    method: str
    status: int
    cseq_method: str
    cseq: int
    call_id: str
    agent: str
    body: bytes

    @property
    def is_request(self) -> bool:
        return bool(self.method)


def read_messages(capture: Path) -> list[SdpMessage]:
    """Return the SDP bearing SIP messages of a capture, without retransmissions."""
    command = ["tshark", "-r", str(capture), "-Y", "sip && sdp", "-T", "fields", "-E", "separator=\t"]
    for field in FIELDS:
        command += ["-e", field]
    rows = subprocess.run(command, check=True, capture_output=True, text=True).stdout.splitlines()
    messages: list[SdpMessage] = []
    seen: set[tuple] = set()
    for row in rows:
        columns = (row.split("\t") + [""] * len(FIELDS))[: len(FIELDS)]
        time, source, destination, method, status, cseq_method, cseq, call_id, agent, server, body = columns
        if not body:
            continue
        raw = bytes.fromhex(body.replace(":", ""))
        key = (call_id, cseq, method, status, source, hashlib.sha256(raw).hexdigest())
        if key in seen:
            continue
        seen.add(key)
        messages.append(SdpMessage(
            float(time), source, destination, method, int(status or 0), cseq_method,
            int(cseq or 0), call_id, agent or server, raw,
        ))
    return messages


def redact(body: bytes, addresses: dict[str, str], names: dict[str, str]) -> bytes:
    """Replace addresses and names. Longest keys first, so one key never splits another."""
    text = body.decode("utf-8", "surrogateescape")
    for old, new in sorted({**addresses, **names}.items(), key=lambda item: -len(item[0])):
        text = text.replace(old, new)
    return text.encode("utf-8", "surrogateescape")


def audio_summary(body: bytes) -> str:
    """Return the audio media line and its codec names, for a human reading the index."""
    text = body.decode("utf-8", "replace")
    media = re.search(r"^m=audio [^\r\n]*", text, re.M)
    codecs = re.findall(r"^a=rtpmap:\d+ ([^\r\n]+)", text, re.M)
    packet = re.findall(r"^a=((?:min|max)?ptime:[^\r\n]+)", text, re.M)
    video = " +video" if re.search(r"^m=video [1-9]", text, re.M) else ""
    return f"{media.group(0) if media else 'no audio'} [{', '.join(codecs)}] {' '.join(packet)}{video}"


def dialogs(messages: list[SdpMessage]) -> list[dict]:
    """Pair the first offer of every Call-ID with its first answer."""
    result = []
    for call_id in dict.fromkeys(message.call_id for message in messages):
        own = [message for message in messages if message.call_id == call_id]
        invites = [message for message in own if message.method == "INVITE"]
        if not invites:
            continue
        candidates = [
            message for message in own
            if not message.is_request and message.cseq_method == "INVITE"
            and message.status in range(180, 300)
        ]
        # An INVITE repeated with credentials after a 401 or 407 has a new CSeq: the offer
        # that counts is the one the first answer belongs to.
        answered = [invite for invite in invites if candidates and invite.cseq == candidates[0].cseq]
        offer = answered[0] if answered else invites[0]
        answers = [message for message in candidates if message.cseq == offer.cseq]
        reoffers = [message for message in invites if message.cseq > offer.cseq]
        result.append({"call_id": call_id, "offer": offer, "answer": answers[0] if answers else None,
                       "reoffers": reoffers})
    return result


def window_label(time: float, windows: list[dict]) -> str:
    for window in windows:
        if window["start"] <= time <= window["end"]:
            return window["label"]
    return ""


def write_cases(args: argparse.Namespace) -> int:
    addresses = dict(item.split("=", 1) for item in args.map)
    names = dict(item.split("=", 1) for item in args.rename)
    windows = json.loads(Path(args.windows).read_text()) if args.windows else []
    out = Path(args.out)
    written = 0
    for dialog in dialogs(read_messages(Path(args.capture))):
        offer, answer = dialog["offer"], dialog["answer"]
        local_offers = offer.source == args.local_ip
        peer = answer if local_offers else offer
        label = window_label(offer.time, windows) or hashlib.sha256(dialog["call_id"].encode()).hexdigest()[:10]
        peer_address = offer.destination if local_offers else offer.source
        role = "to" if local_offers else "from"
        case_id = re.sub(r"[^a-z0-9]+", "-", f"{label}-{role}-{addresses.get(peer_address, peer_address)}".lower()).strip("-")
        directory = out / case_id
        if directory.exists():
            # Two dialogs of one window with the same role and peer must not overwrite each other.
            raise SystemExit(f"duplicate case id {case_id}: add a window or a discriminator")
        directory.mkdir(parents=True)
        steps = []
        if local_offers:
            (directory / "01_local_offer.sdp").write_bytes(redact(offer.body, addresses, names))
            if answer is not None:
                (directory / "02_answer.sdp").write_bytes(redact(answer.body, addresses, names))
                steps.append({"op": "apply_answer", "local_offer": "01_local_offer.sdp",
                              "sdp": "02_answer.sdp", "policy": args.policy})
        else:
            (directory / "01_offer.sdp").write_bytes(redact(offer.body, addresses, names))
            steps.append({"op": "answer_offer", "sdp": "01_offer.sdp", "policy": args.policy})
            if answer is not None:
                # Evidence only: what the engine in production answered on the wire.
                (directory / "02_local_answer.captured.sdp").write_bytes(redact(answer.body, addresses, names))
        case = {
            "id": case_id,
            "title": f"{label or 'captured dialog'}: {audio_summary(offer.body)}",
            "source": {"kind": "capture", "origin": args.origin,
                       "redaction": "addresses mapped to RFC 5737 ranges; device and account names replaced"},
            "peer": {"kind": "", "user_agent": redact((peer.agent if peer else "").encode(), addresses, names).decode(),
                     "profile": ""},
            "steps": steps,
            "expect": [],
            "rules": [],
            "cc": [],
            "captured": {"status": answer.status if answer else None, "reoffers": len(dialog["reoffers"])},
        }
        (directory / "case.json").write_text(json.dumps(case, indent=2) + "\n")
        written += 1
    print(f"{written} cases written to {out}")
    return 0


def print_index(args: argparse.Namespace) -> int:
    for message in read_messages(Path(args.capture)):
        what = message.method or f"{message.status} ({message.cseq_method})"
        print(f"{message.time:.3f} {message.source} -> {message.destination} {what} "
              f"[{message.agent}] {audio_summary(message.body)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    index = commands.add_parser("index", help="list the SDP bearing messages of a capture")
    index.add_argument("capture")
    index.set_defaults(run=print_index)
    cases = commands.add_parser("cases", help="write one case directory per dialog")
    cases.add_argument("capture")
    cases.add_argument("--local-ip", required=True, help="address of VoIP Stack in the capture")
    cases.add_argument("--out", required=True)
    cases.add_argument("--map", action="append", default=[], metavar="OLD=NEW", help="address replacement")
    cases.add_argument("--rename", action="append", default=[], metavar="OLD=NEW", help="name replacement")
    cases.add_argument("--windows", help="JSON list of {label, start, end} time windows")
    cases.add_argument("--origin", default="", help="where and when the capture was taken")
    cases.add_argument("--policy", default="phone", help="local policy preset recorded in the steps")
    cases.set_defaults(run=write_cases)
    args = parser.parse_args()
    return args.run(args)


if __name__ == "__main__":
    raise SystemExit(main())
