"""Run normative corpus inputs through production and load its frozen historical reference."""

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


def production_answer_offer(step: Step) -> dict:
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
    dtmf = invite.send_dtmf_format
    video = invite.video_answer_format or invite.local_video_format
    try:
        answer = sdp.build_answer_directional(
            "192.0.2.1", "192.0.2.1", 40000, invite.send_format, invite.recv_format,
            recv_formats=invite.recv_formats, dtmf=dtmf,
            dtmf_direction=invite.dtmf_direction, remote_sdp=invite.remote_sdp,
            video_port=40002 if video is not None else 0, video_format=video)
    except Exception as error:  # noqa: BLE001
        return {"outcome": "error", "error": f"answer {type(error).__name__}"}
    return {
        "outcome": "accept",
        "send": _describe(invite.send_format),
        "recv": _describe(invite.recv_format),
        "dtmf": {"clock": dtmf.sample_rate, "payload": dtmf.payload_type} if dtmf else None,
        "video": invite.video_format is not None,
        "answer": _audio_lines(answer),
        # The complete description as written, byte for byte: transport, line endings,
        # media order and every attribute.
        "answer_bytes": answer,
    }


def production_apply_answer(step: Step) -> dict:
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


def run_production(step: Step) -> dict:
    return {"answer_offer": production_answer_offer, "apply_answer": production_apply_answer}[step.op](step)


def run_legacy(step: Step) -> dict:
    """Read a case from the immutable pre-migration recording, never run another engine."""
    return json.loads(GOLDEN.read_text())[step.name]


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
        if key == "answer_payloads":
            where = f"{path}answer.payloads"
            got = _value(actual, "answer.payloads")
        if isinstance(wanted, dict):
            found += mismatches(wanted, got or {}, where + ".")
        elif got != wanted:
            found.append(f"{where}: expected {wanted!r}, got {got!r}")
    return found


# Fixed inputs for the offer writer: (send list, receive list, Dahua payload, directional).
OFFER_PRESETS = {
    "phone": (HA_SIP_PCM_TX_FORMATS, HA_SIP_PCM_RX_FORMATS, False, False),
    "phone-directional": (HA_SIP_PCM_TX_FORMATS, HA_SIP_PCM_RX_FORMATS, False, True),
    "trunk": (HA_TRUNK_AUDIO_FORMATS, HA_TRUNK_AUDIO_FORMATS, False, False),
    "dahua": (HA_TRUNK_AUDIO_FORMATS, HA_TRUNK_AUDIO_FORMATS, True, False),
}




ENTRY_POINTS = {"answer_offer": "inbound initial offer", "apply_answer": "answer to a local offer"}
# The only fields each declared contract change may alter.
CC_FIELDS = {
    "CC-1": ("outcome",),
    "CC-2": ("recv.ptime",),
    "CC-4": ("dtmf.clock", "dtmf.payload"),
    "CC-5": ("answer must not contain",),
    "CC-11": ("send.ptime",),
    "CC-12": ("outcome",),
    "RFC3556": ("answer.rtcp_bandwidth",),
    "RFC3264_ACCEPTED_G711_ALIASES": ("answer.payloads",),
    "LOCAL_CAPABILITIES": ("send.codec", "send.ptime", "send.payload", "recv.codec", "recv.payload"),
}



def audio_wire_facts(description):
    """Read audio payload numbers and rtpmap facts independently of negotiation."""
    text = description.decode() if isinstance(description, bytes) else description
    order, mappings, inside = None, {}, False
    for line in text.splitlines():
        if line.startswith("m="):
            inside = line.startswith("m=audio ")
            if inside:
                assert order is None, "Ambiguous audio section"
                order = [int(value) for value in line.split()[3:]]
                assert len(order) == len(set(order)), "Duplicate audio payload"
            continue
        if inside and line.startswith("a=rtpmap:"):
            payload, mapping = line.removeprefix("a=rtpmap:").split(None, 1)
            fields = mapping.split("/")
            assert int(payload) not in mappings, "Duplicate rtpmap"
            mappings[int(payload)] = (fields[0].upper(), int(fields[1]),
                                      int(fields[2]) if len(fields) == 3 else 1)
    return order, mappings


def declared_answer_bytes(step, frozen, rows):
    """Apply only declared audio bandwidth and accepted G.711 alias insertions."""
    fields = [row["field"] for row in rows]
    assert len(fields) == len(set(fields)), "Duplicate byte delta"
    assert set(fields) <= {"answer.rtcp_bandwidth", "answer.payloads"}
    expected = frozen
    bandwidth = next((row for row in rows if row["field"] == "answer.rtcp_bandwidth"), None)
    if bandwidth is not None:
        assert bandwidth["cc"] == "RFC3556" and bandwidth["prior"] is None
        assert bandwidth["intended"] == {"RS": 0, "RR": 0}
        assert _value({"answer_bytes": frozen}, "answer.rtcp_bandwidth") is None
        media = next(line for line in expected.split("\r\n") if line.startswith("m=audio "))
        expected = expected.replace(media + "\r\n", media + "\r\nb=RS:0\r\nb=RR:0\r\n", 1)
    aliases = next((row for row in rows if row["field"] == "answer.payloads"), None)
    if aliases is not None:
        assert aliases["cc"] == "RFC3264_ACCEPTED_G711_ALIASES"
        prior, _ = audio_wire_facts(frozen)
        intended = aliases["intended"]
        assert aliases["prior"] == prior
        assert intended == step.expect["answer_payloads"]
        assert intended[0] == prior[0] and len(intended) == len(set(intended))
        assert [number for number in intended if number in prior] == prior
        added = [number for number in intended if number not in prior]
        assert added, "An alias declaration must add a payload"
        offered, mappings = audio_wire_facts(step.sdp)
        static = {0: ("PCMU", 8000, 1), 8: ("PCMA", 8000, 1)}
        attributes = []
        for number in added:
            assert number in offered and number in static
            assert mappings[number] == static[number]
            encoding, rate, channels = mappings[number]
            attributes.append(f"a=rtpmap:{number} {encoding}/{rate}/{channels}")
        lines = expected.split("\r\n")
        index = next(index for index, line in enumerate(lines) if line.startswith("m=audio "))
        suffix = " ".join(map(str, prior))
        assert lines[index].endswith(" " + suffix)
        lines[index] = lines[index][:-len(suffix)] + " ".join(map(str, intended))
        event = next(index for index, line in enumerate(lines)
                     if line.startswith("a=rtpmap:") and " telephone-event/" in line)
        lines[event:event] = attributes
        expected = "\r\n".join(lines)
    return expected


def assert_declared_answer_bytes(step, actual, frozen, rows):
    """Validate the exact declared fields and every remaining byte together."""
    expected = declared_answer_bytes(step, frozen, rows)
    for row in rows:
        if row["field"] == "answer.rtcp_bandwidth":
            assert _value({"answer_bytes": actual}, row["field"]) == {"RS": 0, "RR": 0}
        elif row["field"] == "answer.payloads":
            written, written_maps = audio_wire_facts(actual)
            offered, source_maps = audio_wire_facts(step.sdp)
            assert written == row["intended"]
            for number in set(row["intended"]) - set(row["prior"]):
                assert number in offered and written_maps[number] == source_maps[number]
    assert actual == expected


def _value(data, field: str):
    if field == "answer.payloads":
        if data.get("answer_bytes"):
            return audio_wire_facts(data["answer_bytes"])[0]
        return data.get("answer_payloads")
    if field == "answer.rtcp_bandwidth":
        bandwidth, inside = {}, False
        for line in str(data.get("answer_bytes") or "").split("\r\n"):
            if line.startswith("m="):
                inside = line.startswith("m=audio ")
            elif inside and line.startswith(("b=RS:", "b=RR:")):
                key, value = line[2:].split(":", 1)
                bandwidth[key] = int(value)
        return bandwidth or None
    for key in field.split("."):
        data = data.get(key) if isinstance(data, dict) else None
    return data


def ledger_problems(ledger: list[dict], steps: list[Step], observed: dict[str, dict]) -> list[str]:
    """Check every ledger row against the engine and the case, and find what it leaves out.

    A row authorizes one field of one step to differ. Its ``prior`` must be what the engine
    answers, its ``intended`` what the case requires, its entry point the operation of the step,
    and its declared change one that may alter that field.
    """
    problems: list[str] = []
    by_name = {step.name: step for step in steps}
    rows: dict[tuple[str, str], dict] = {}
    for row in ledger:
        key = (row.get("step"), row.get("field"))
        if key in rows:
            problems.append(f"duplicate row {key}")
        rows[key] = row
        step = by_name.get(row.get("step"))
        if step is None:
            problems.append(f"row for unknown step {row.get('step')}")
            continue
        field, text = row["field"], row["field"].startswith("answer ")
        if row.get("entry_point") != ENTRY_POINTS[step.op]:
            problems.append(f"{key}: entry point is {ENTRY_POINTS[step.op]!r}")
        allowed = CC_FIELDS.get(row.get("cc"), ())
        if not any(field == name or (text and field.startswith(name)) for name in allowed):
            problems.append(f"{key}: {row.get('cc')} does not cover this field")
        prior = "written" if text else _value(observed[step.name], field)
        intended = ({"RS": 0, "RR": 0} if field == "answer.rtcp_bandwidth" else
                    "absent" if text else _value(step.expect, field))
        if field == "answer.rtcp_bandwidth" and row.get("prior") is not None:
            problems.append(f"{key}: RFC 3556 delta permits only omitted bandwidth becoming zero")
        if row.get("prior") != prior:
            problems.append(f"{key}: prior is {prior!r}, the row says {row.get('prior')!r}")
        if row.get("intended") != intended:
            problems.append(f"{key}: intended is {intended!r}, the row says {row.get('intended')!r}")
        if field == "outcome" and (row.get("prior"), row.get("intended")) != ("reject", "accept"):
            problems.append(f"{key}: only a refusal that becomes an acceptance may be declared")
    for step in steps:
        for item in mismatches(step.expect, observed[step.name]):
            field = item.split(":", 1)[0]
            if (step.name, field) not in rows:
                problems.append(f"not in the ledger: {step.name} {item}")
    for (name, field), row in rows.items():
        step = by_name.get(name)
        if step is not None and field == "answer.rtcp_bandwidth":
            if _value(observed[name], field) == {"RS": 0, "RR": 0}:
                problems.append(f"stale row: {name} {field}")
            continue
        if step is not None and field not in {item.split(":", 1)[0] for item in mismatches(step.expect, observed[name])}:
            problems.append(f"stale row: {name} {field}")
    return problems
