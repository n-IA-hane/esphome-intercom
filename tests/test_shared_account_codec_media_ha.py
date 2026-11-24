"""Shared-account winners retain independent duplex media and real card ownership."""

import asyncio
import json
from pathlib import Path

import pytest

from custom_components.voip_stack.core.codec_capabilities import codec_directions
from custom_components.voip_stack.codec_settings import effective_codec_policy
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.sip_registrar import SipAccount
from custom_components.voip_stack.store import config_entry
from tests.test_canonical_account_calls_ha import (
    canonical_browser as canonical_browser,
    logical_browser as logical_browser,
    registered_phones as registered_phones,
    trunks as trunks,
    lab as lab,
)
from tests.test_browser_logical_availability_ha import (
    assert_no_browser_media_owner,
    public_phone,
    resources,
)
from tests.support.shared_codec_browser import Card
from tests.support.shared_codec_peer import (
    CASES,
    CodecPeer,
    check_wire,
    decode_named_rtp,
    measure_tone,
    mixed_default_sdp,
    parse_packet,
    payload_duration_ms,
)

pytestmark = [pytest.mark.ha, pytest.mark.browser_real]
REQUIRED_CODECS = frozenset({"PCMU", "PCMA", "G722", "L16", "L24", "PCM", "OPUS"})


async def until(predicate, seconds=5):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(0.01)


def received_seconds(peer):
    return (
        sum(
            payload_duration_ms(peer.case, parse_packet(raw)["payload"])
            for raw in peer.received
        )
        / 1000
    )


def current_browser_media(stats, call_id):
    identities = {
        identity for identity, call in stats["calls"].items() if call == call_id
    }
    return [stream for stream in stats["streams"] if stream["request_id"] in identities]


def measure_browser(stats, call_id, direction, frequency):
    streams = [
        stream
        for stream in current_browser_media(stats, call_id)
        if stream["direction"] == direction
    ]
    assert len(streams) == 1, stats
    stream = streams[0]
    fmt = stats["formats"][stream["request_id"]][0 if direction == "tx" else 1]
    rate, pcm_format, channels, frame = fmt.split(":")
    return {
        **measure_tone(
            Path(stream["file"]).read_bytes(),
            int(rate),
            frequency,
            pcm_format=pcm_format,
            channels=int(channels),
        ),
        "wire_pcm_format": fmt,
        "bytes": stream["bytes"],
    }


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("logical_browser", [{"ringing_timeout": 30}], indirect=True)
@pytest.mark.parametrize(
    "case",
    [
        pytest.param(
            case,
            id=case.name,
            marks=()
            if case.name in REQUIRED_CODECS
            else pytest.mark.diagnostic_reference,
        )
        for case in CASES
    ],
)
async def test_shared_account_codec_winner_media_and_reuse(
    canonical_browser,
    hass_ws_client,
    hass_admin_user,
    tmp_path,
    socket_enabled,
    case,
    mixed_defaults=False,
):
    """Real REGISTER, fork, card, external codec and RTP in three consecutive calls."""
    directions = codec_directions(case.name)
    if not all(directions.values()):
        pytest.skip(f"{case.name} actual backend directions: {directions}")
    independent_reference = None
    if case.oracle_gap:
        if case.name == "G726-40":
            from tests.support.g726_itu_reference import qualified_reference

            independent_reference = qualified_reference()
        if independent_reference is None:
            pytest.skip(
                f"{case.name} backend available but independent oracle gap: {case.oracle_gap}"
            )
    lab = canonical_browser
    entry = config_entry(lab.hass)
    # Each optional codec is explicitly enabled for this isolated test only.
    data = {**entry.data, "video_codecs": []}
    if mixed_defaults:
        data.pop("audio_codecs", None)
    else:
        data["audio_codecs"] = [case.name]
    lab.hass.config_entries.async_update_entry(entry, data=data)
    assert "audio_codecs" not in lab.browser_subentry.data
    effective = effective_codec_policy(lab.hass, endpoint_id="casa")
    if mixed_defaults:
        assert set(effective.audio) == {
            "PCMU",
            "PCMA",
            "G722",
            "OPUS",
            "L16",
            "L24",
            "PCM",
        }
        assert not effective.audio_ordered
    else:
        assert effective.audio == (case.name,)
    lab.runtime.endpoints.update("casa", name="Home reception")
    registrar = lab.calls.component("registrar")
    shared = lab.browser_subentry.data
    registrar.update_accounts(
        [
            *registrar.accounts.values(),
            SipAccount(
                shared["username"],
                shared["name"],
                shared["password"],
                extension=str(shared.get("extension") or ""),
            ),
        ]
    )
    caller = CodecPeer(case, "transfer_a", "isolated-test-only")
    callee = CodecPeer(case, shared["username"], shared["password"])
    if mixed_defaults:
        callee.options_body = mixed_default_sdp(0)
    peers = (caller, callee)
    card = Card()

    def detach_commit():
        pass

    evidence = {
        "codec": case.name,
        "backend_directions": directions,
        "isolated_enabled_policy": list(effective.audio),
        "config_entry_audio_policy": entry.data.get("audio_codecs", "absent/default"),
        "account_audio_policy": shared.get("audio_codecs", "absent/inherited"),
        "effective_audio_policy": effective.audio,
        "mixed_default_offer": mixed_defaults,
        "peer_encoder": "external FFmpeg",
        "peer_decoder": "external FFmpeg named RTP demuxer",
        "calls": [],
    }
    if independent_reference is not None:
        evidence.update(
            peer_encoder="external official ITU G.191 reference",
            peer_decoder="external official ITU G.191 reference with RFC3551 RTP unpacking",
            independent_reference=independent_reference.provenance,
        )
    try:
        for peer in peers:
            await lab.hass.loop.create_datagram_endpoint(
                lambda peer=peer: peer, local_addr=("127.0.0.1", 0)
            )
            await peer.register(lab.runtime.transport_config["sip_port"])
        assert len(registrar.registered_contacts(shared["username"])) == 1
        binding = registrar.registered_contacts(shared["username"])[0]
        capabilities = binding.capabilities or {}
        evidence["sip_discovery"] = {
            "wire_options_sdp": callee.options_body.decode(),
            "derived_rx_formats": capabilities.get("sip_audio_rx_formats"),
            "derived_tx_formats": capabilities.get("sip_audio_tx_formats"),
        }
        await card.start(lab, hass_ws_client, tmp_path / "chromium", hass_admin_user)
        assert lab.runtime.sip is lab.calls
        await until(lambda: lab.calls.component("phone_state") is not None)
        observer = lab.calls.component("phone_state")
        assert (
            observer is not None and observer._committed in lab.calls._state_listeners
        )
        from custom_components.voip_stack.browser_phone_projection import (
            _ha_softphone_state,
        )

        commits = []

        def observe_commit(snapshot):
            view = _ha_softphone_state(lab.hass, "casa")
            commits.append(
                {
                    "call_id": snapshot.call_id,
                    "state": snapshot.state,
                    "revision": snapshot.revision,
                    "sequence": snapshot.sequence,
                    "view": {
                        key: view[key]
                        for key in ("state", "sequence", "revision", "can_attach_media")
                    },
                    "legs": [repr(leg) for leg in snapshot.legs],
                }
            )

        detach_commit = lab.calls.subscribe_state(observe_commit)
        call_ids = set()
        ssrcs = set()
        for cycle, winner in enumerate(("ha", "sip", "ha")):
            directory = tmp_path / f"call-{cycle}-{winner}"
            directory.mkdir()
            for peer in peers:
                peer.ringing.clear()
                peer.drain_media()
            await caller.dial(lab.runtime.transport_config["sip_port"])
            await asyncio.wait_for(callee.ringing.wait(), 5)
            if mixed_defaults:
                import re

                actual_names = set(
                    re.findall(rb"a=rtpmap:\d+ ([^/\s]+)", callee.offers[-1])
                )
                assert {b"PCMU", b"G722", b"OPUS"} <= actual_names, callee.offers[
                    -1
                ].decode()
            await card.command("wait-answer")
            phone = await public_phone(lab)
            assert phone["state"] == "ringing"
            call_id = phone["calls"][0]["call_id"]
            assert call_id not in call_ids
            call_ids.add(call_id)
            await card.command("screenshot", path=str(directory / "both-ringing.png"))
            ringing = await card.command("stats")
            assert ringing["card"]["state"] == "ringing"
            assert ringing["card"]["caller"] == "transfer_a"
            assert ringing["card"]["callee"] == "Home reception"
            if winner == "ha":
                await card.command("answer")
                await caller.established()
                await asyncio.wait_for(callee.cancelled.wait(), 5)
                assert not callee.active and callee.current is None
            else:
                await callee.accept()
                await caller.established()
            await until(lambda: lab.calls.get_session(call_id).state == "in_call")
            session = lab.calls.get_session(call_id)
            assert "casa" in session.endpoint_claims
            assert (await public_phone(lab))["state"] == "in_call"
            source_frequency = 450 + cycle * 50
            destination_frequency = 750 + cycle * 50
            await caller.start_media(source_frequency)
            if winner == "sip":
                await callee.start_media(destination_frequency)
            await until(lambda: received_seconds(caller) >= 0.85)
            if winner == "sip":
                await until(lambda: received_seconds(callee) >= 0.85)
            await caller.stop_media()
            if winner == "sip":
                await callee.stop_media()
            stats = await card.command("stats")
            (directory / "active-browser.json").write_text(
                json.dumps(stats, indent=2) + "\n"
            )
            (directory / "active-phone.json").write_text(
                json.dumps(await public_phone(lab), indent=2) + "\n"
            )
            await card.command("screenshot", path=str(directory / "active-card.png"))
            (directory / "observer.json").write_text(
                json.dumps(
                    {
                        "current_registry": lab.runtime.sip is lab.calls,
                        "subscribed_to_current_registry": observer._committed
                        in lab.calls._state_listeners,
                        "state_listeners": len(observer.listeners),
                        "observer_previous": observer.previous,
                        "committed_state": repr(session.committed_state),
                        "sip_clients": {
                            key: client.snapshot()
                            for key, client in lab.calls.sip_clients_snapshot().items()
                        },
                        "commit_views": commits,
                    },
                    indent=2,
                    default=repr,
                )
                + "\n"
            )
            assert stats["errors"] == []
            assert stats["card"]["caller"] == "transfer_a"
            assert stats["card"]["callee"] == "Home reception"
            account_view = [
                message["event"]
                for message in stats["control_events"]
                if message.get("event", {}).get("call_id") == call_id
                and "can_attach_media" in message["event"]
            ][-1]
            assert account_view["state"] == "in_call"
            assert account_view["can_attach_media"] is (winner == "ha")
            assert not account_view["can_answer"]
            call_evidence = {
                "winner": winner,
                "call_id": call_id,
                "caller_sdp": caller.receive_sdp.decode(),
                "ha_answer": caller.remote_sdp.decode(),
                "fork_sip_offer": callee.offers[-1].decode(),
                "browser": stats,
                "wire_to_caller": check_wire(case, caller.received),
                # The1200B limit is HA's output builder budget. This external
                # L16 sender uses its legal20ms/1920B quantum within HA's
                # announced maxptime128; UDP payloads may carry up to65507B.
                "wire_from_caller": check_wire(
                    case, caller.transmitted, payload_limit=65507
                ),
            }
            assert call_evidence["wire_from_caller"]["actual_packet_ms"] == [
                case.peer_tx_ms
            ]
            if case.name == "L16":
                assert call_evidence["wire_from_caller"]["payload_bytes"] == [1920]
            # RFC L16 mono48k/20ms is1920B. The independent1200B packet
            # budget requires the existing legal10ms fallback on HA TX.
            expected_ms = 10 if case.name == "L16" else case.peer_rx_ms
            assert call_evidence["wire_to_caller"]["actual_packet_ms"] == [expected_ms]
            received_ssrc = call_evidence["wire_to_caller"]["ssrc"]
            assert received_ssrc not in ssrcs
            ssrcs.add(received_ssrc)
            decoded = await decode_named_rtp(
                case, caller.received, caller.receive_sdp, directory / "caller-receive"
            )
            call_evidence["caller_tone"] = measure_tone(
                decoded, case.pcm_rate, 650 if winner == "ha" else destination_frequency
            )
            if winner == "ha":
                call_evidence["card_receive_tone"] = measure_browser(
                    stats, call_id, "rx", source_frequency
                )
                call_evidence["card_microphone_tone"] = measure_browser(
                    stats, call_id, "tx", 650
                )
                assert stats["card"]["audioReady"]
                assert stats["card"]["contextState"] == "running"
                assert stats["card"]["tracks"] == ["live"]
                assert callee.drain_media() == 0, (
                    "Losing SIP Contact must not receive media"
                )
            else:
                assert_no_browser_media_owner(lab)
                assert current_browser_media(stats, call_id) == []
                assert not stats["card"]["tracks"]
                assert not stats["card"]["audioReady"]
                call_evidence["wire_to_callee"] = check_wire(case, callee.received)
                assert call_evidence["wire_to_callee"]["actual_packet_ms"] == [
                    expected_ms
                ]
                call_evidence["callee_sdp"] = callee.receive_sdp.decode()
                decoded = await decode_named_rtp(
                    case,
                    callee.received,
                    callee.receive_sdp,
                    directory / "callee-receive",
                )
                call_evidence["callee_tone"] = measure_tone(
                    decoded, case.pcm_rate, source_frequency
                )
            evidence["calls"].append(call_evidence)
            assert stats["card"]["state"] == "in_call"
            await card.command("hangup")
            await asyncio.wait_for(caller.ended.wait(), 5)
            if winner == "sip":
                await asyncio.wait_for(callee.ended.wait(), 5)
            await until(lambda: not lab.calls.sessions)
            # Dialog removal precedes the existing asynchronous media cleanup
            # barrier. Require that owner to finish, rather than sample its race.
            await until(lambda: resources(lab)["call_scoped_quiescent"])
            assert resources(lab)["call_scoped_quiescent"], resources(lab)
            await asyncio.sleep(0.1)
            idle = await card.command("stats")
            assert idle["card"]["state"] == "idle"
            assert not idle["card"]["tracks"]
            assert not idle["card"]["audioReady"]
            assert (await public_phone(lab))["state"] == "idle"
            assert_no_browser_media_owner(lab)
            call_evidence["final_resources"] = resources(lab)
            call_evidence["idle_card"] = idle["card"]
            (directory / "result.json").write_text(
                json.dumps(call_evidence, indent=2) + "\n"
            )
        assert len(evidence["calls"]) == 3
    finally:
        detach_commit()
        (tmp_path / "result.json").write_text(json.dumps(evidence, indent=2) + "\n")
        for session in tuple(lab.calls.sessions.values()):
            barrier = lab.calls.request_termination(
                session.call_id, TerminationIntent("test_done")
            )
            if barrier is not None:
                await barrier
        await card.close()
        for peer in peers:
            await peer.close()
        assert resources(lab)["call_scoped_quiescent"]


@pytest.mark.parametrize("listener_mode", ["clean", "one-conflict", "all-conflicts"])
async def test_external_codec_listener_handoff_is_owned(tmp_path, monkeypatch, socket_enabled, listener_mode):
    import errno
    import socket
    import struct

    from tests.support import shared_codec_peer as peer

    case = next(value for value in peer.CASES if value.name == "G722")
    payloads = peer.encoded_tone(case, 650)
    packets = [
        struct.pack("!BBHII", 0x80, case.payload, index,
                    index * case.clock * case.peer_tx_ms // 1000, 0x12345678) + payload
        for index, payload in enumerate(payloads)
    ]
    loop = asyncio.get_running_loop()
    original_probe = peer._rtp_receiver_port
    original_spawn = asyncio.create_subprocess_exec
    original_send = loop.sock_sendto
    foreign, children, ports, replayed = [], [], [], []

    def choose_port():
        # A deliberate foreign owner wins the gap after the real free probe.
        assert not replayed
        port = original_probe()
        ports.append(port)
        if listener_mode == "all-conflicts" or (listener_mode == "one-conflict" and len(ports) == 1):
            for number in (port, port + 1):
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.bind(("127.0.0.1", number))
                foreign.append(sock)
        return port

    async def spawn(*args, **kwargs):
        process = await original_spawn(*args, **kwargs)
        children.append(process)
        return process

    async def send(sock, packet, address):
        if address[0] == "127.0.0.1" and address[1] in ports:
            replayed.append(packet)
        return await original_send(sock, packet, address)

    monkeypatch.setattr(peer, "_rtp_receiver_port", choose_port)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(loop, "sock_sendto", send)
    try:
        if listener_mode == "all-conflicts":
            with pytest.raises(OSError) as error:
                await peer.decode_named_rtp(case, packets, peer.audio_sdp(case, 40000), tmp_path)
            assert error.value.errno == errno.EADDRINUSE
            assert "exit=" in str(error.value) and "attempt=3/3" in str(error.value)
            assert "bind failed" in str(error.value) and "Address already in use" in str(error.value)
            assert len(children) == len(ports) == 3
            assert not replayed
        else:
            decoded = await peer.decode_named_rtp(case, packets, peer.audio_sdp(case, 40000), tmp_path)
            peer.measure_tone(decoded, case.pcm_rate, 650)
            assert replayed == packets
            assert len(children) == len(ports) == (2 if listener_mode == "one-conflict" else 1)
        assert (tmp_path / "packets.hex").read_text() == "\n".join(packet.hex() for packet in packets) + "\n"
        assert all(child.returncode is not None and child.stdout.at_eof() and child.stderr.at_eof()
                   for child in children)
        # Cleanup is scoped to the child and its streams, never foreign sockets.
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for sock in foreign:
                sock.settimeout(0.5)
                sender.sendto(b"still-owned", sock.getsockname())
                assert sock.recvfrom(64)[0] == b"still-owned"
        finally:
            sender.close()
    finally:
        for sock in foreign:
            sock.close()
        for child in children:
            if child.returncode is None:
                child.kill()
                await child.wait()
