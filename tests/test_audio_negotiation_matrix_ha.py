"""Every offered audio shape gets a legal, bounded and predictable negotiation.

A PBX meets any codec, rate, channel count and packet time. This matrix feeds
each combination to the real listener and, for bridged calls, to the real
caller-leg resolution. It reports every combination that breaks a rule rather
than stopping at the first one.
"""

from itertools import product
from types import SimpleNamespace

import pytest

from custom_components.voip_stack.core import sdp, sip
from custom_components.voip_stack.core.audio_capabilities import HA_AUDIO_CAPABILITIES
from custom_components.voip_stack.core.audio_format import (
    HA_SIP_PCM_RX_FORMATS,
    HA_SIP_PCM_TX_FORMATS,
    HA_TRUNK_AUDIO_FORMATS,
)
from custom_components.voip_stack.core.audio_pcm import PcmFrameConverter
from custom_components.voip_stack.core.rtp_payload import RtpPayloadEncoder
from custom_components.voip_stack.rtp_audio_receiver import RtpAudioReceiver
from custom_components.voip_stack.sip_bridge import bridge_source_audio
from custom_components.voip_stack.sip_listener import SipUdpEndpoint

pytestmark = pytest.mark.ha

# name: (payload type, rtpmap)
CODECS = {
    "L16/8000": (100, "L16/8000/1"),
    "L16/16000": (101, "L16/16000/1"),
    "L16/32000": (102, "L16/32000/1"),
    "L16/44100": (105, "L16/44100/1"),
    "L16/48000": (103, "L16/48000/1"),
    "L16/48000/2": (104, "L16/48000/2"),
    "PCMU": (0, "PCMU/8000"),
    "PCMA": (8, "PCMA/8000"),
    "G722": (9, "G722/8000"),
    "OPUS": (111, "opus/48000/2"),
    # Dahua's dynamic payload for signed little-endian samples; not RFC 3551 L16.
    "PCM/16000": (97, "PCM/16000"),
}
DAHUA_OFFERS = [
    ("PCM/16000",),
    ("PCM/16000", "PCMA", "PCMU"),
    ("PCMU", "PCM/16000"),
    ("PCMA", "PCMU"),
    ("G722", "PCM/16000", "PCMA"),
]
OFFERS = [(name,) for name in CODECS if name != "PCM/16000"] + [
    ("L16/48000", "L16/16000"),
    ("L16/16000", "L16/48000"),
    ("L16/48000/2", "L16/48000"),
    ("L16/44100", "L16/16000"),
    ("G722", "PCMA", "PCMU"),
    ("PCMA", "G722"),
    ("OPUS", "G722", "PCMA"),
]
PACKET_TIMES = (None, 5, 10, 16, 20, 30, 40, 60)
MAX_PACKET_TIMES = (None, 10, 20, 60)
PROFILES = {
    "phone": (HA_SIP_PCM_TX_FORMATS, HA_SIP_PCM_RX_FORMATS),
    "trunk": (HA_TRUNK_AUDIO_FORMATS, HA_TRUNK_AUDIO_FORMATS),
}
# One RTP packet must fit an Ethernet frame with its headers.
PAYLOAD_BUDGET_BYTES = 1200
# TX fallback when the peer states none: 10 ms at 48 kHz, 20 ms otherwise.
DEFAULT_PACKET_MS = {48000: 10, 16000: 20, 8000: 20}


def offer_sdp(names, packet_ms, max_packet_ms):
    body = (
        "v=0\r\no=- 1 1 IN IP4 127.0.0.2\r\ns=Matrix peer\r\nc=IN IP4 127.0.0.2\r\nt=0 0\r\n"
        f"m=audio 35000 RTP/AVP {' '.join(str(CODECS[name][0]) for name in names)}\r\n"
    )
    body += "".join(f"a=rtpmap:{CODECS[name][0]} {CODECS[name][1]}\r\n" for name in names)
    if packet_ms:
        body += f"a=ptime:{packet_ms}\r\n"
    if max_packet_ms:
        body += f"a=maxptime:{max_packet_ms}\r\n"
    return body + "a=sendrecv\r\n"


def parse_offer(body, profile, peer_profile=""):
    send, receive = PROFILES[profile]
    endpoint = SipUdpEndpoint(
        local_ip="127.0.0.1", local_sip_port=5060, local_rtp_port=40000,
        supported_formats=list(send), supported_send_formats=list(send),
        supported_recv_formats=list(receive), on_invite=None,
    )
    request = sip.parse_message(sip.build_request("INVITE", "sip:phone@127.0.0.1", [
        ("Via", "SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bK-matrix"),
        ("From", "<sip:peer@127.0.0.2>;tag=source"), ("To", "<sip:phone@127.0.0.1>"),
        ("Call-ID", "audio-matrix"), ("CSeq", "1 INVITE"),
        ("Contact", "<sip:peer@127.0.0.2:5060>"), ("Content-Type", "application/sdp"),
    ], body.encode()))
    return endpoint._parse_invite(request, ("127.0.0.2", 5060), peer_profile=peer_profile)


def payload_bytes(fmt):
    """Return the audio bytes in one packet for codecs with a fixed rate."""
    encoding = fmt.encoding.upper()
    if encoding in {"L16", "PCM"}:
        return fmt.sample_rate * fmt.frame_ms * fmt.channels * 2 / 1000
    if encoding in {"PCMU", "PCMA", "G722"}:
        return 8 * fmt.frame_ms
    return 0


def describe(fmt):
    return f"{fmt.encoding}/{fmt.sample_rate}/{fmt.channels}@{fmt.frame_ms}ms"


def leg_violations(invite, names, max_packet_ms):
    """Check one negotiated leg against the rules every combination must keep."""
    found = []
    send, receive = invite.send_format, invite.recv_format
    offered = {CODECS[name][0] for name in names}
    if send.payload_type not in offered or receive.payload_type not in offered:
        found.append(f"payload type outside the offer: send {send.payload_type}, receive {receive.payload_type}")
    if payload_bytes(send) > PAYLOAD_BUDGET_BYTES:
        found.append(f"transmit packet of {int(payload_bytes(send))} bytes exceeds {PAYLOAD_BUDGET_BYTES}: {describe(send)}")
    if max_packet_ms and send.frame_ms > max_packet_ms:
        found.append(f"transmit packet time above the peer's maxptime {max_packet_ms}: {describe(send)}")
    if send.encoding.upper() in {"L16", "PCM"} and (send.sample_rate * send.frame_ms) % 1000:
        found.append(f"transmit packet is not a whole number of samples: {describe(send)}")
    try:
        answer = sdp.parse_sdp(sdp.build_answer_directional(
            "127.0.0.1", "127.0.0.1", 40000, send, receive, remote_sdp=invite.remote_sdp))
    except Exception as error:  # noqa: BLE001 - the matrix reports any failure to answer
        return [*found, f"answer could not be built for {describe(send)} / {describe(receive)}: {error!r}"]
    if answer.get("ptime") != receive.frame_ms:
        found.append(f"answer ptime {answer.get('ptime')} differs from the receive packet time {receive.frame_ms}")
    if answer.get("ptime") is not None and answer["ptime"] != int(answer["ptime"]):
        found.append(f"answer ptime is not a whole number of milliseconds: {answer['ptime']}")
    return found


def combinations(offers=OFFERS):
    for names, packet_ms, max_packet_ms in product(offers, PACKET_TIMES, MAX_PACKET_TIMES):
        if packet_ms and max_packet_ms and packet_ms > max_packet_ms:
            continue
        yield names, packet_ms, max_packet_ms


def report(violations):
    return "\n".join(f"{case}: {problem}" for case, problem in violations)


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_every_offer_is_answered_within_the_wire_rules(profile):
    violations, accepted = [], 0
    for names, packet_ms, max_packet_ms in combinations():
        case = f"{profile} {'+'.join(names)} ptime={packet_ms} maxptime={max_packet_ms}"
        try:
            invite = parse_offer(offer_sdp(names, packet_ms, max_packet_ms), profile)
        except Exception as error:  # noqa: BLE001 - an unusual offer must be rejected, not crash
            violations.append((case, f"negotiation raised {error!r}"))
            continue
        if invite is None:
            continue  # A clean refusal is a valid outcome.
        accepted += 1
        violations += [(case, problem) for problem in leg_violations(invite, names, max_packet_ms)]
    assert accepted > 300
    assert not violations, report(violations)


@pytest.mark.parametrize("name", ["L16/8000", "L16/16000", "L16/48000", "PCMU", "PCMA", "G722"])
def test_packet_time_defaults_follow_the_sample_rate(name):
    """TX respects its wire budget; R1 keeps the independent default RX20 policy."""
    invite = parse_offer(offer_sdp((name,), None, None), "phone")
    assert invite is not None
    decoded_rate = invite.send_format.audio_format.sample_rate
    assert invite.send_format.frame_ms == DEFAULT_PACKET_MS[decoded_rate], describe(invite.send_format)
    assert invite.recv_format.frame_ms == 20, describe(invite.recv_format)
    answer = sdp.build_answer_directional('127.0.0.1', '127.0.0.1', 40000,
        invite.send_format, invite.recv_format, remote_sdp=invite.remote_sdp)
    assert 'a=ptime:20\r\n' in answer
    assert invite.recv_format.max_frame_ms == HA_AUDIO_CAPABILITIES.max_receive_packet_ms(invite.recv_format)


DESTINATIONS = {
    "L16/16000": sdp.RtpPcmFormat(97, "L16", 16000, 1, 20),
    "L16/48000": sdp.RtpPcmFormat(96, "L16", 48000, 1, 10),
    "PCMA": sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20),
    "G722": sdp.RtpPcmFormat(9, "G722", 16000, 1, 20),
}


def test_bridged_caller_leg_stays_legal_for_every_destination():
    violations, checked = [], 0
    for (names, packet_ms, max_packet_ms), (transmit, receive) in product(
            combinations(), product(DESTINATIONS, repeat=2)):
        case = (f"{'+'.join(names)} ptime={packet_ms} maxptime={max_packet_ms} "
                f"destination sends {transmit}, receives {receive}")
        invite = parse_offer(offer_sdp(names, packet_ms, max_packet_ms), "phone")
        if invite is None:
            continue
        # HA receives what the destination sends, and sends what it receives.
        destination = SimpleNamespace(recv_format=DESTINATIONS[transmit], send_format=DESTINATIONS[receive])
        try:
            selected = bridge_source_audio(invite, destination)
        except Exception as error:  # noqa: BLE001
            violations.append((case, f"caller leg resolution raised {error!r}"))
            continue
        checked += 1
        violations += [(case, problem) for problem in leg_violations(selected, names, max_packet_ms)]
        before, after = invite.send_format, selected.send_format
        if (before.encoding.upper() == after.encoding.upper() == "L16"
                and after.sample_rate < before.sample_rate):
            violations.append((case, f"uncompressed audio to the caller lowered from {describe(before)} to {describe(after)}"))
    assert checked > 4000
    assert not violations, report(violations[:60]) + f"\n... {len(violations)} violations in total"


@pytest.mark.parametrize("profile", sorted(PROFILES))
def test_every_dahua_offer_is_answered_within_the_wire_rules(profile):
    """A Dahua station may state any packet time for its own PCM payload."""
    violations, accepted = [], 0
    for names, packet_ms, max_packet_ms in combinations(DAHUA_OFFERS):
        case = f"dahua/{profile} {'+'.join(names)} ptime={packet_ms} maxptime={max_packet_ms}"
        try:
            invite = parse_offer(offer_sdp(names, packet_ms, max_packet_ms), profile, peer_profile="dahua")
        except Exception as error:  # noqa: BLE001
            violations.append((case, f"negotiation raised {error!r}"))
            continue
        if invite is None:
            violations.append((case, "refused although every offered codec is supported"))
            continue
        accepted += 1
        violations += [(case, problem) for problem in leg_violations(invite, names, max_packet_ms)]
    assert accepted > 100
    assert not violations, report(violations)


OUTBOUND = {
    "phone": (HA_SIP_PCM_TX_FORMATS, HA_SIP_PCM_RX_FORMATS, False),
    "trunk": (HA_TRUNK_AUDIO_FORMATS, HA_TRUNK_AUDIO_FORMATS, False),
    "dahua": (HA_TRUNK_AUDIO_FORMATS, HA_TRUNK_AUDIO_FORMATS, True),
}


@pytest.mark.parametrize("directional", [False, True], ids=["one-payload", "directional"])
@pytest.mark.parametrize("profile", sorted(OUTBOUND))
def test_every_answer_to_our_offer_is_accepted_within_the_wire_rules(profile, directional):
    """Whichever offered payload and packet time the peer answers, the call proceeds."""
    send, receive, dahua = OUTBOUND[profile]
    offer = sdp.build_offer_directional(
        "127.0.0.1", "127.0.0.1", 40000, list(send), list(receive), include_common_codecs=True,
        include_dahua_pcm=dahua, allow_directional_payloads=directional,
        independent_audio_ptime=directional, local_capabilities=HA_AUDIO_CAPABILITIES)
    offered = sdp.offered_pcm_formats(offer, allow_dahua_pcm=dahua)
    assert len(offered) >= 5
    violations, accepted = [], 0
    for fmt, packet_ms, max_packet_ms in product(offered, (None, 10, 20, 30, 40, 60), MAX_PACKET_TIMES):
        if packet_ms and max_packet_ms and packet_ms > max_packet_ms:
            continue
        opus = fmt.encoding.upper() == "OPUS"
        clock = 8000 if fmt.encoding.upper() == "G722" else fmt.sample_rate
        channels = f"/{fmt.channels}" if opus or fmt.channels != 1 else ""
        answer = (
            "v=0\r\no=- 2 2 IN IP4 127.0.0.2\r\ns=Matrix answer\r\nc=IN IP4 127.0.0.2\r\nt=0 0\r\n"
            f"m=audio 35000 RTP/AVP {fmt.payload_type}\r\n"
            f"a=rtpmap:{fmt.payload_type} {fmt.encoding}/{clock}{channels}\r\n"
            + (f"a=ptime:{packet_ms}\r\n" if packet_ms else "")
            + (f"a=maxptime:{max_packet_ms}\r\n" if max_packet_ms else "")
            + "a=sendrecv\r\n"
        )
        case = f"{profile} answer {describe(fmt)} ptime={packet_ms} maxptime={max_packet_ms}"
        try:
            selected = sdp.negotiate_answer_directional(
                answer, list(send), list(receive), local_offer_sdp=offer,
                allow_dahua_pcm=dahua, local_capabilities=HA_AUDIO_CAPABILITIES)
        except Exception as error:  # noqa: BLE001
            violations.append((case, f"answer negotiation raised {error!r}"))
            continue
        if selected is None:
            violations.append((case, "an offered payload was answered and then refused"))
            continue
        accepted += 1
        if selected.send.payload_type != fmt.payload_type or selected.recv.payload_type != fmt.payload_type:
            violations.append((case, f"selected {describe(selected.send)} / {describe(selected.recv)}"))
        if payload_bytes(selected.send) > PAYLOAD_BUDGET_BYTES:
            violations.append((case, f"transmit packet of {int(payload_bytes(selected.send))} bytes: {describe(selected.send)}"))
        if max_packet_ms and selected.send.frame_ms > max_packet_ms:
            violations.append((case, f"transmit packet time above maxptime: {describe(selected.send)}"))
    assert accepted > 80
    assert not violations, report(violations)


def carry_audio(source, target, packets=12):
    """Send real packets from one negotiated format to another through the bridge parts."""
    packer = RtpPayloadEncoder(source)
    receiver = RtpAudioReceiver(source)
    converter = PcmFrameConverter(source.audio_format, target.audio_format)
    encoder = RtpPayloadEncoder(target)
    silence = bytes(packer.input_format.nominal_frame_bytes)
    produced = []
    for _ in range(packets):
        for pcm in receiver.decode(source.payload_type, packer.encode(silence)):
            for frame in converter.convert(pcm):
                produced.append(encoder.encode(frame))
    return produced


def test_every_bridged_pair_carries_audio_in_both_directions():
    """Negotiation is only useful if the bridge can convert what it accepted."""
    violations, pairs = [], set()
    for names, packet_ms, max_packet_ms in combinations([*OFFERS, *DAHUA_OFFERS]):
        dahua = any(name == "PCM/16000" for name in names)
        invite = parse_offer(offer_sdp(names, packet_ms, max_packet_ms), "phone",
                             peer_profile="dahua" if dahua else "")
        if invite is None:
            continue
        for transmit, receive in product(DESTINATIONS, repeat=2):
            destination = SimpleNamespace(recv_format=DESTINATIONS[transmit], send_format=DESTINATIONS[receive])
            selected = bridge_source_audio(invite, destination)
            for label, source, target in (
                ("caller to destination", selected.recv_format, destination.send_format),
                ("destination to caller", destination.recv_format, selected.send_format),
            ):
                pair = (describe(source), describe(target))
                if pair in pairs:
                    continue
                pairs.add(pair)
                case = f"{label}: {pair[0]} -> {pair[1]}"
                try:
                    produced = carry_audio(source, target)
                except Exception as error:  # noqa: BLE001
                    violations.append((case, f"conversion raised {error!r}"))
                    continue
                if not produced:
                    violations.append((case, "no packet came out"))
                elif max(map(len, produced)) > PAYLOAD_BUDGET_BYTES:
                    violations.append((case, f"packet of {max(map(len, produced))} bytes"))
    assert len(pairs) > 60
    assert not violations, report(violations)
