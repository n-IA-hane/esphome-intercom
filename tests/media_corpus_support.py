"""Load media corpus cases and run them through the engine in production today.

The corpus states what the written contract requires. This module answers what the current
engine does, so a test can compare the two and a golden snapshot can prove that a refactoring
changed nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES
from custom_components.voip_stack.core.audio_format import (
    HA_SIP_PCM_RX_FORMATS,
    HA_SIP_PCM_TX_FORMATS,
    HA_TRUNK_AUDIO_FORMATS,
)
from custom_components.voip_stack.sip_listener import SipUdpEndpoint

CORPUS = Path(__file__).parent / "media_corpus"
GOLDEN = CORPUS / "golden" / "legacy.json"
# Local format lists of the current engine, by policy preset named in a case step.
POLICIES = {
    "phone": (HA_SIP_PCM_TX_FORMATS, HA_SIP_PCM_RX_FORMATS),
    "trunk": (HA_TRUNK_AUDIO_FORMATS, HA_TRUNK_AUDIO_FORMATS),
}
LEGACY_PROFILES = {"": "", "dahua_pcm": "dahua"}


@dataclass(frozen=True, slots=True)
class Step:
    case_id: str
    index: int
    op: str
    policy: str
    profile: str
    user_agent: str
    sdp: bytes
    local_offer: bytes
    expect: dict

    @property
    def name(self) -> str:
        return f"{self.case_id}#{self.index}"


def load_steps() -> list[Step]:
    steps: list[Step] = []
    for path in sorted((CORPUS / "cases").glob("*/case.json")):
        case = json.loads(path.read_text())
        expects = case.get("expect") or []
        for index, step in enumerate(case["steps"]):
            steps.append(Step(
                case_id=case["id"],
                index=index,
                op=step["op"],
                policy=step.get("policy", "phone"),
                profile=case.get("peer", {}).get("profile", ""),
                user_agent=case.get("peer", {}).get("user_agent", ""),
                sdp=(path.parent / step["sdp"]).read_bytes(),
                local_offer=(path.parent / step["local_offer"]).read_bytes() if step.get("local_offer") else b"",
                expect=expects[index] if index < len(expects) else {},
            ))
    return steps


def _describe(fmt) -> dict:
    return {
        "codec": f"{fmt.encoding}/{fmt.sample_rate}/{fmt.channels}",
        "ptime": fmt.frame_ms,
        "payload": fmt.payload_type,
    }


def _audio_lines(description: str) -> list[str]:
    """Return the audio section of a description without its transport address."""
    lines, inside = [], False
    for line in description.replace("\r\n", "\n").split("\n"):
        if line.startswith("m="):
            inside = line.startswith("m=audio")
            if inside:
                lines.append("m=audio RTP/AVP " + " ".join(line.split()[3:]))
            continue
        if inside and line.startswith("a="):
            lines.append(line)
    return lines


def legacy_answer_offer(step: Step) -> dict:
    """Answer a remote offer exactly as the listener does for an initial INVITE."""
    send, receive = POLICIES[step.policy]
    endpoint = SipUdpEndpoint(
        local_ip="192.0.2.1", local_sip_port=5060, local_rtp_port=40000,
        supported_formats=list(send), supported_send_formats=list(send),
        supported_recv_formats=list(receive), on_invite=None,
    )
    headers = [
        ("Via", "SIP/2.0/UDP 192.0.2.10:5060;branch=z9hG4bK-corpus"),
        ("From", "<sip:peer@192.0.2.10>;tag=corpus"), ("To", "<sip:local@192.0.2.1>"),
        ("Call-ID", "media-corpus"), ("CSeq", "1 INVITE"),
        ("Contact", "<sip:peer@192.0.2.10:5060>"), ("Content-Type", "application/sdp"),
    ]
    if step.user_agent:
        headers.append(("User-Agent", step.user_agent))
    request = sip.parse_message(sip.build_request("INVITE", "sip:local@192.0.2.1", headers, step.sdp))
    try:
        invite = endpoint._parse_invite(
            request, ("192.0.2.10", 5060), peer_profile=LEGACY_PROFILES.get(step.profile, ""))
    except Exception as error:  # noqa: BLE001 - the corpus records how the engine fails
        return {"outcome": "error", "error": type(error).__name__}
    if invite is None:
        return {"outcome": "reject"}
    dtmf = sdp.first_offered_dtmf_format(invite.remote_sdp)
    try:
        answer = sdp.build_answer_directional(
            "192.0.2.1", "192.0.2.1", 40000, invite.send_format, invite.recv_format,
            dtmf=dtmf, remote_sdp=invite.remote_sdp)
    except Exception as error:  # noqa: BLE001
        return {"outcome": "error", "error": f"answer {type(error).__name__}"}
    return {
        "outcome": "accept",
        "send": _describe(invite.send_format),
        "recv": _describe(invite.recv_format),
        "dtmf": {"clock": dtmf.sample_rate, "payload": dtmf.payload_type} if dtmf else None,
        "video": invite.video_format is not None,
        "answer": _audio_lines(answer),
    }


def legacy_apply_answer(step: Step) -> dict:
    """Apply a remote answer to the description this side offered."""
    send, receive = POLICIES[step.policy]
    try:
        selected = sdp.negotiate_answer_directional(
            step.sdp, list(send), list(receive), local_offer_sdp=step.local_offer,
            allow_dahua_pcm=step.profile == "dahua_pcm", local_capabilities=HA_AUDIO_CAPABILITIES)
    except Exception as error:  # noqa: BLE001
        return {"outcome": "error", "error": type(error).__name__}
    if selected is None:
        return {"outcome": "reject"}
    dtmf = sdp.negotiate_dtmf_answer_directional(step.sdp, step.local_offer)
    return {
        "outcome": "accept",
        "send": _describe(selected.send),
        "recv": _describe(selected.recv),
        "dtmf": {"clock": dtmf.send.sample_rate, "payload": dtmf.send.payload_type} if dtmf else None,
        # Every payload the peer listed in its answer and this side accepts on receive.
        "recv_accept": sorted(_describe(fmt)["codec"] for fmt in (selected.recv_formats or (selected.recv,))),
    }


def run_legacy(step: Step) -> dict:
    return {"answer_offer": legacy_answer_offer, "apply_answer": legacy_apply_answer}[step.op](step)


def mismatches(expect: dict, actual: dict, path: str = "") -> list[str]:
    """Compare only the fields a case states.

    ``answer_contains`` and ``answer_excludes`` are lists of text fragments checked against the
    audio section of the description this side writes.
    """
    found: list[str] = []
    if not path and "outcome" in expect and (actual or {}).get("outcome") != expect["outcome"]:
        # A different outcome makes every other field meaningless.
        return [f"outcome: expected {expect['outcome']!r}, got {(actual or {}).get('outcome')!r}"]
    written = "\n".join(actual.get("answer") or []) if isinstance(actual, dict) else ""
    for fragment in expect.get("answer_contains", ()):
        if fragment not in written:
            found.append(f"answer lacks {fragment!r}")
    for fragment in expect.get("answer_excludes", ()):
        if fragment in written:
            found.append(f"answer must not contain {fragment!r}")
    for key, wanted in expect.items():
        if key in {"answer_contains", "answer_excludes"}:
            continue
        where = f"{path}{key}"
        got = actual.get(key) if isinstance(actual, dict) else None
        if isinstance(wanted, dict):
            found += mismatches(wanted, got or {}, where + ".")
        elif got != wanted:
            found.append(f"{where}: expected {wanted!r}, got {got!r}")
    return found
