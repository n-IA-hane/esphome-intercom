"""Independent registered SIP phones exercise consultation and transfers."""

import asyncio
import math
import os
from pathlib import Path
import secrets
import shutil
import struct
import wave

import pytest

from custom_components.voip_stack.endpoint_routing import assemble_directory_entries, roster_from_peers
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
from custom_components.voip_stack.sip_registrar import SipAccount
from custom_components.voip_stack.trunk_runtime import async_stop_sip_trunk
from tools.ha_softphone_matrix import BareSip
from .test_trunk_calls_ha import trunks as trunks, lab as lab

pytestmark = [pytest.mark.ha, pytest.mark.skipif(shutil.which("baresip") is None, reason="baresip is not installed")]


@pytest.fixture
async def registered_phones(trunks, tmp_path, request):
    lab = trunks
    # Keep the PBX relay pool separate from peer RTP and OS ephemeral sockets.
    assert not lab.runtime.rtp_port_pool.get("used")
    lab.runtime.transport_config["rtp_port"] = 12000 + 2 * secrets.randbelow(2000)
    video_names = getattr(request, "param", {}).get("video", ())
    if video_names:
        module_path = Path(os.environ.get("VOIP_TEST_BARESIP_MODULE_PATH", "/usr/lib/baresip/modules"))
        for module in ("avcodec.so", "fakevideo.so"):
            assert (module_path / module).is_file(), f"Video peer requires {module_path / module}"
        lab.runtime.transport_config["experimental_sip_video"] = True
        for endpoint in lab.calls.component("udp_listener")._dialog_endpoints():
            endpoint.enable_video = True
    await async_stop_sip_trunk(lab.hass)
    lab.runtime.trunk_configs = {}
    lab.runtime.trunk_config = {}
    lab.runtime.default_trunk_id = ""
    registrar = lab.calls.component("registrar")
    registrar.enabled = True
    names = ("transfer_a", "transfer_b", "transfer_c")
    registrar.update_accounts([SipAccount(name, name, "isolated-test-only") for name in names])
    for name in names:
        lab.runtime.endpoints.register(PhoneEndpoint(
            endpoint_id=name, name=name, username=name, kind=EndpointKind.SIP_ACCOUNT,
        ))
    peers = []
    try:
        for index, name in enumerate(names):
            config = tmp_path / name
            config.mkdir(mode=0o700)
            (config / "config").write_text(f"""net_interface 127.0.0.1
sip_listen 127.0.0.1:0
call_max_calls 2
rtp_ports 20000-30000
audio_player aubridge,nil
audio_source ausine,{450 + index * 100}
audio_alert aubridge,nil
snd_path {config}
module_path /usr/lib/baresip/modules
module stdio.so
module menu.so
module g711.so
module auconv.so
module auresamp.so
module ausine.so
module aubridge.so
module sndfile.so
module_app account.so
""")
            if name in video_names:
                with (config / "config").open("a") as stream:
                    stream.write("video_source fakevideo,nil\nvideo_display fakevideo,nil\n"
                                 "video_size 160x120\nvideo_fps 5\nvideo_bitrate 128000\n"
                                 "avcodec_keyint 5\nmodule avcodec.so\nmodule fakevideo.so\n")
            accounts = config / "accounts"
            manual = getattr(request, "param", {}).get("manual", ())
            manual = (manual,) if isinstance(manual, str) else manual
            answer_mode = "manual" if name in manual else "auto"
            accounts.write_text(
                f'<sip:{name}@127.0.0.1:{lab.runtime.transport_config["sip_port"]}>'
                f';auth_user={name};auth_pass=isolated-test-only;regint=60;answermode={answer_mode}\n'
            )
            if name in video_names:
                accounts.write_text(accounts.read_text().replace(">;auth_user=", ";video_codecs=H264>;auth_user="))
            accounts.chmod(0o600)
            peers.append(await asyncio.to_thread(BareSip, config))
        lab.roster[:] = assemble_directory_entries(lab.hass, [], registrar.registered_roster_entries())
        yield peers
    finally:
        for peer in peers:
            if peer.proc.poll() is None:
                await asyncio.to_thread(peer.command, "/hangupall")
        try:
            async with asyncio.timeout(3):
                while lab.calls.sessions:
                    for peer in peers:
                        peer.read()
                    await asyncio.sleep(0.01)
        except TimeoutError:
            import io

            trace = io.StringIO()
            for task in asyncio.all_tasks():
                task.print_stack(file=trace)
            Path("test_captures/ha-pbx/transfer-cleanup-stacks.txt").write_text(trace.getvalue())
            raise
        finally:
            for peer in peers:
                await asyncio.to_thread(peer.close)
        assert not lab.calls.endpoint_claims_snapshot()
        assert not lab.calls.relays_snapshot()


async def establish_consultation(lab, peers):
    caller, operator, _destination = peers
    await asyncio.to_thread(caller.dial, "transfer_b")
    await asyncio.to_thread(operator.wait_for, "Call established", 5)
    original = lab.runtime.endpoints.get("transfer_b").active_call_id
    assert original
    start = len(operator.read())
    target = f'sip:transfer_c@127.0.0.1:{lab.runtime.transport_config["sip_port"]}'
    await asyncio.to_thread(operator.command, f"/atransferstart {target}")
    async with asyncio.timeout(3):
        while "Call established" not in operator.read()[start:]:
            assert "486 Busy Here" not in operator.output[start:], operator.output[start:]
            await asyncio.sleep(0.01)
    return original, next(item for item in lab.calls.sessions.values() if item.call_id != original)


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_registered_phone_consultation_preserves_original_call(registered_phones, trunks):
    lab = trunks
    caller, operator, destination = registered_phones
    original, _consultation = await establish_consultation(lab, registered_phones)
    assert len(lab.calls.sessions) == 2
    assert sum("transfer_b" in session.endpoint_claims for session in lab.calls.sessions.values()) == 2
    assert lab.runtime.endpoints.get("transfer_b").active_call_id == original
    await asyncio.to_thread(destination.hangup)
    async with asyncio.timeout(3):
        while len(lab.calls.sessions) != 1:
            operator.read()
            await asyncio.sleep(0.01)
    assert lab.calls.get_session(original).live
    assert lab.runtime.endpoints.get("transfer_b").active_call_id == original
    assert not lab.runtime.endpoints.get("transfer_c").active_call_id
    await asyncio.to_thread(caller.hangup)


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("hangup_side,redial", [(0, False), (2, False), (2, True)])
async def test_attended_transfer_reuses_destination_and_delivers_duplex_audio(registered_phones, trunks, tmp_path, hangup_side, redial):
    lab = trunks
    caller, operator, destination = registered_phones
    original, consultation = await establish_consultation(lab, registered_phones)
    relay = lab.calls.resource_for(consultation.call_id, "relay")
    _source, wire_id = lab.calls.bridge_for(consultation.call_id)
    client = lab.calls.sip_client_for(wire_id)
    await asyncio.to_thread(operator.command, "/atransferexec")
    async with asyncio.timeout(4):
        while True:
            for peer in registered_phones:
                peer.read()
            sessions = tuple(lab.calls.sessions.values())
            if (len(sessions) == 1 and sessions[0].call_id not in {original, consultation.call_id}
                    and not lab.runtime.endpoints.get("transfer_b").active_call_id):
                break
            assert "transfer failed" not in operator.output.lower(), operator.output
            await asyncio.sleep(0.01)
    replacement = sessions[0]
    assert lab.calls.resource_for(replacement.call_id, "relay") is relay
    assert lab.calls.sip_client_for(wire_id) is client
    assert lab.runtime.endpoints.get("transfer_a").active_call_id == replacement.call_id
    assert lab.runtime.endpoints.get("transfer_c").active_call_id == replacement.call_id
    assert destination.output.count("Call established") == 1
    assert not list(lab.calls.resource_items("replacement_grant"))
    left, right = relay.left_tx_packets, relay.right_tx_packets
    async with asyncio.timeout(4):
        while relay.left_tx_packets < left + 30 or relay.right_tx_packets < right + 30:
            for peer in registered_phones:
                peer.read()
            await asyncio.sleep(0.01)
    await asyncio.to_thread(registered_phones[hangup_side].hangup)
    async with asyncio.timeout(3):
        while lab.calls.sessions:
            for peer in registered_phones:
                peer.read()
            await asyncio.sleep(0.01)
    if redial:
        offset = len(caller.read())
        await asyncio.to_thread(caller.command, "/dial transfer_c")
        async with asyncio.timeout(3):
            while "Call established" not in caller.read()[offset:]:
                await asyncio.sleep(0.01)
        assert len(lab.calls.sessions) == 1
        current = next(iter(lab.calls.sessions.values()))
        assert set(current.endpoint_claims) == {"transfer_a", "transfer_c"}
        next_relay = lab.calls.resource_for(current.call_id, "relay")
        async with asyncio.timeout(3):
            while next_relay.left_tx_packets < 30 or next_relay.right_tx_packets < 30:
                await asyncio.sleep(0.01)
        await asyncio.to_thread(destination.hangup)
        await asyncio.wait_for(current.terminated.wait(), 3)
    # sndfile finalizes WAV headers when the independent peer releases audio.
    # SIP idle alone does not guarantee that its audio filter has flushed yet.
    for peer in registered_phones:
        await asyncio.to_thread(peer.close)
    assert_peer_audio_tones(tmp_path, (("transfer_a", 650), ("transfer_c", 450)))


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_replacement_authorization_races_and_rollback_preserve_existing_calls(registered_phones, trunks):
    from dataclasses import replace
    from custom_components.voip_stack.call_replaces import prepare_replacement, prepare_replacement_grant, ReplacementGrant
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.core.sip_transfer import SipReplaces, SipReferTarget

    lab = trunks
    original, consultation = await establish_consultation(lab, registered_phones)
    manager = lab.calls.component("udp_listener")
    dialog = next(endpoint.active_dialogs[consultation.call_id] for endpoint in manager._dialog_endpoints()
                  if consultation.call_id in endpoint.active_dialogs)
    key = SipReplaces(consultation.call_id, dialog.to_tag, sip.extract_tag(dialog.request.header("From")))
    invite = replace(dialog.invite, call_id="replacement-attempt", replaces=key,
                     caller="transfer_a", identified_source_endpoint_id="transfer_a")
    relay = lab.calls.resource_for(consultation.call_id, "relay")
    previous_peer = relay.left
    mismatch = await prepare_replacement(lab.hass, replace(invite, replaces=replace(key, to_tag="wrong")), "127.0.0.1")
    assert mismatch.status == 481
    early = await prepare_replacement(lab.hass, replace(invite, replaces=replace(key, early_only=True)), "127.0.0.1")
    assert early.status == 486
    uninvited = await prepare_replacement(lab.hass, invite, "127.0.0.1")
    assert uninvited.status == 403
    assert len(lab.calls.sessions) == 2
    local_only = ReplacementGrant(lab.calls.get_session(original), consultation, "", key)
    local_only.attach()
    try:
        anonymous = await prepare_replacement(lab.hass, replace(invite, identified_source_endpoint_id=""), "127.0.0.1")
        assert anonymous.status == 403
        assert len(lab.calls.sessions) == 2
    finally:
        local_only.release()
    _root, referring_id = lab.calls.bridge_for(original)
    referring_client = lab.calls.sip_client_for(referring_id)
    target = SipReferTarget(f'sip:transfer_c@127.0.0.1:{manager.port}', key)
    grant = prepare_replacement_grant(lab.runtime, original, target, referring_client)
    try:
        _source, occupied_id = lab.calls.bridge_for(consultation.call_id)
        collision = await prepare_replacement(lab.hass, replace(invite, call_id=occupied_id), "127.0.0.1")
        assert collision.status == 482
        assert consultation.live and lab.calls.sip_client_for(occupied_id).dialog is not None
        prepared = await prepare_replacement(lab.hass, invite, "127.0.0.1")
        assert prepared.status == 200
        duplicate = await prepare_replacement(lab.hass, replace(invite, call_id="second-attempt"), "127.0.0.1")
        assert duplicate.status == 491
        assert len(lab.calls.sessions) == 3
        await prepared.rollback()
        assert len(lab.calls.sessions) == 2
        assert relay.left is previous_peer
        assert consultation.live and lab.calls.get_session(original).live
        assert not list(lab.calls.resource_items("replacement_grant"))
    finally:
        grant.release()
    grant = prepare_replacement_grant(lab.runtime, original, target, referring_client)
    try:
        prepared = await prepare_replacement(lab.hass, replace(invite, call_id="late-replacement"), "127.0.0.1")
        assert prepared.status == 200
        await asyncio.to_thread(registered_phones[2].hangup)
        await asyncio.wait_for(consultation.terminated.wait(), 3)
        with pytest.raises(RuntimeError, match="ownership changed"):
            await prepared.commit()
        await prepared.rollback()
        assert tuple(lab.calls.sessions) == (original,)
        assert lab.runtime.endpoints.get("transfer_b").active_call_id == original
    finally:
        grant.release()


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_registered_phone_cannot_replace_another_call_without_referral(registered_phones, trunks):
    from dataclasses import replace
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.core.sip_transfer import SipReplaces, SipReferTarget

    lab = trunks
    original, consultation = await establish_consultation(lab, registered_phones)
    manager = lab.calls.component("udp_listener")
    dialog = next(endpoint.active_dialogs[consultation.call_id] for endpoint in manager._dialog_endpoints()
                  if consultation.call_id in endpoint.active_dialogs)
    key = SipReplaces(consultation.call_id, dialog.to_tag, sip.extract_tag(dialog.request.header("From")))
    caller = registered_phones[0]
    for reference, expected in ((key, "403 Forbidden"), (replace(key, to_tag="wrong"), "481 Call")):
        offset = len(caller.read())
        target = SipReferTarget(f"sip:transfer_c@127.0.0.1:{manager.port}", reference)
        await asyncio.to_thread(caller.command, f"/dial {target.as_header()}")
        async with asyncio.timeout(3):
            while expected not in caller.read()[offset:]:
                await asyncio.sleep(0.01)
        assert set(lab.calls.sessions) == {original, consultation.call_id}
        assert consultation.live
        assert lab.runtime.endpoints.get("transfer_c").active_call_id == consultation.call_id


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("origin,represented", [("caller", True), ("callee", True), ("caller", False)])
async def test_local_transfer_keeps_legacy_caller_dialog_and_echoed_audio(registered_phones, trunks, tmp_path, monkeypatch, origin, represented):
    import socket
    import logging
    from pathlib import Path

    executable = shutil.which("sipp")
    if executable is None:
        pytest.skip("SIPp is not installed")
    lab = trunks
    original_claim = lab.calls.claim_endpoint
    def traced_claim(*args, **kwargs):
        try:
            return original_claim(*args, **kwargs)
        except Exception:
            logging.getLogger(__name__).error("Endpoint claim failed args=%s kwargs=%s calls=%s", args, kwargs, [
                {"call_id": item.call_id, "phase": item.phase.value,
                 "claims": dict(item.endpoint_claims),
                 "target": item.metadata.get("target_endpoint_id"),
                 "destination": item.metadata.get("dest_endpoint_id"),
                 "bridge": lab.calls.bridge_link_for(item.call_id),
                 "legs": [(leg.leg_id, leg.phase.value, leg.endpoint_id) for leg in item.legs.values()]}
                for item in lab.calls.sessions.values()
            ])
            raise
    monkeypatch.setattr(lab.calls, "claim_endpoint", traced_claim)
    from custom_components.voip_stack import endpoint_runtime, invite_router, outbound_bridge_commit

    first_media_started = asyncio.Event()
    release_first_media = asyncio.Event()
    start_media = outbound_bridge_commit.async_start_sip_bridge_media
    async def staged_media(relay):
        if origin == "caller" and not first_media_started.is_set():
            first_media_started.set()
            await release_first_media.wait()
        return await start_media(relay)
    monkeypatch.setattr(outbound_bridge_commit, "async_start_sip_bridge_media", staged_media)

    monkeypatch.setattr(endpoint_runtime, "_roster_from_peers", roster_from_peers)
    monkeypatch.setattr(invite_router, "_roster_from_peers", roster_from_peers)
    registrar = lab.calls.component("registrar")
    if origin == "caller":
        registrar.update_accounts([*registrar.accounts.values(), SipAccount("legacy", "Legacy", "isolated-test-only")])
    if represented:
        lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="legacy", name="Legacy", username="legacy",
                                                   kind="sip_account" if origin == "caller" else "esphome", availability="available"))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind(("127.0.0.1", 0))
        media_port = probe.getsockname()[1]
    if origin == "callee":
        from custom_components.voip_stack import peer_snapshot
        from custom_components.voip_stack.peer import Peer

        monkeypatch.setattr(peer_snapshot.async_build_peer_snapshot, "return_value", [Peer(
            name="legacy", host="127.0.0.1", endpoint_id="legacy", endpoint_kind="esphome",
            sip_port=port, rtp_port=media_port, device={"sip_transport": "udp"},
            tx_formats=["16000:s16le:1:20"], rx_formats=["16000:s16le:1:20"],
        )])
    scenario = Path(__file__).parent / f"fixtures/sipp/legacy-{origin}.xml"
    trace = tmp_path / "legacy-sip.log"
    process = await asyncio.create_subprocess_exec(
        executable, *([f'127.0.0.1:{lab.runtime.transport_config["sip_port"]}'] if origin == "caller" else []), "-sf", str(scenario),
        "-i", "127.0.0.1", "-p", str(port), "-mi", "127.0.0.1", "-min_rtp_port", str(media_port), "-rtp_echo", "-m", "1",
        "-trace_msg", "-message_file", str(trace), "-trace_err", "-error_file", str(tmp_path / "legacy-errors.log"),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=tmp_path,
    )
    async def wait_for(predicate):
        async with asyncio.timeout(5):
            while not predicate():
                if process.returncode is not None and process.returncode != 0:
                    raise AssertionError((await process.communicate())[0].decode())
                await asyncio.sleep(0.01)
    try:
        _unused, operator, destination = registered_phones
        if origin == "caller":
            await wait_for(lambda: bool(registrar.registered_contacts("legacy")))
        else:
            await wait_for(lambda: any(line.split()[1].endswith(f":{port:04X}")
                                      for line in Path("/proc/net/udp").read_text().splitlines()[1:]))
            await asyncio.to_thread(operator.dial, "legacy")
        await asyncio.to_thread(operator.wait_for, "Call established", 5)
        if origin == "caller":
            await asyncio.wait_for(first_media_started.wait(), 2)
        original_id = lab.runtime.endpoints.get("transfer_b").active_call_id
        original = lab.calls.get_session(original_id)
        assert original is not None
        prior = lab.calls.resource_for(original_id, "relay")
        offset = len(operator.read())
        target = f'sip:transfer_c@127.0.0.1:{lab.runtime.transport_config["sip_port"]}'
        await asyncio.to_thread(operator.command, f"/atransferstart {target}")
        await wait_for(lambda: "Call established" in operator.read()[offset:])
        if origin == "caller":
            # The consultation must succeed while the parent has not answered
            # its caller yet. Only then finish that first media handoff.
            assert not original.answer_committed
            release_first_media.set()
            await wait_for(lambda: original.answer_committed)
        consultation = next(item for item in lab.calls.sessions.values() if item is not original)
        left_transport = prior.left_transport if origin == "caller" else prior.right_transport
        right_transport = lab.calls.resource_for(consultation.call_id, "relay").right_transport
        # A late connected-identity update can change baresip's selected call.
        # Complete the consultation the user selected, not whichever menu row
        # happened to receive the last update.
        await asyncio.to_thread(operator.command, f"/callfind {consultation.call_id}")
        await asyncio.to_thread(operator.command, "/atransferexec")
        await wait_for(lambda: len(lab.calls.sessions) == 1 and not lab.runtime.endpoints.get("transfer_b").active_call_id)
        current = next(iter(lab.calls.sessions.values()))
        assert (current is original) == (origin == "caller")
        assert current.metadata.get("source_endpoint_id", "") == ("legacy" if represented else "")
        combined = lab.calls.resource_for(current.call_id, "relay")
        assert combined.left_transport is left_transport and combined.right_transport is right_transport
        assert lab.runtime.rtp_port_pool["used"] == {combined.left_port, combined.right_port}
        left, right = combined.left_tx_packets, combined.right_tx_packets
        await wait_for(lambda: combined.left_tx_packets >= left + 30 and combined.right_tx_packets >= right + 30)
        destination.read()
        assert destination.output.count("Call established") == 1
        recordings = [("transfer_c", 650)]
        if origin == "callee":
            offset = len(destination.read())
            await asyncio.to_thread(destination.command,
                                    f'/atransferstart sip:transfer_b@127.0.0.1:{lab.runtime.transport_config["sip_port"]}')
            await wait_for(lambda: "Call established" in destination.read()[offset:])
            await asyncio.to_thread(destination.command, "/atransferexec")
            await wait_for(lambda: len(lab.calls.sessions) == 1 and not lab.runtime.endpoints.get("transfer_c").active_call_id)
            current = next(iter(lab.calls.sessions.values()))
            combined = lab.calls.resource_for(current.call_id, "relay")
            assert current.metadata["dest_endpoint_id"] == "transfer_b"
            assert lab.runtime.rtp_port_pool["used"] == {combined.left_port, combined.right_port}
            left, right = combined.left_tx_packets, combined.right_tx_packets
            await wait_for(lambda: combined.left_tx_packets >= left + 30 and combined.right_tx_packets >= right + 30)
            destination = operator
            recordings.append(("transfer_b", 550))
        await asyncio.to_thread(destination.hangup)
        output, _ = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, output.decode()
        await wait_for(lambda: not lab.calls.sessions)
        assert not lab.runtime.rtp_port_pool.get("used")
        for peer in registered_phones:
            await asyncio.to_thread(peer.close)
        for name, expected in recordings:
            found = False
            for path in (tmp_path / name).glob("*-dec.wav"):
                if name == "transfer_b" and "=>sip:transfer_c@" not in path.name:
                    continue
                with wave.open(str(path), "rb") as audio:
                    rate = audio.getframerate()
                    samples = struct.unpack(f"<{audio.getnframes()}h", audio.readframes(audio.getnframes()))[-1600:]
                if len(samples) < 800:
                    continue
                powers = {frequency: abs(sum(value * complex(math.cos(2 * math.pi * frequency * index / rate),
                                                             math.sin(2 * math.pi * frequency * index / rate))
                                             for index, value in enumerate(samples))) for frequency in (550, 650)}
                other = 550 if expected == 650 else 650
                found |= powers[expected] > powers[other] * 5 and powers[expected] > 10000
            assert found, f"The legacy phone did not return {name}'s audio through the retained RTP path"
        assert ("501 Not Implemented" in trace.read_text()) is represented
    finally:
        release_first_media.set()
        for index, peer in enumerate(registered_phones):
            output = peer.read() if peer.proc.poll() is None else peer.output
            (tmp_path / f"transfer-peer-{index}.log").write_text(output)
        if process.returncode is None:
            process.terminate()
            await process.communicate()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("failure", ["busy", "non2xx", "exception", "cancel", "rollback_exception", "rollback_twice", "original_ends", "send_failure", "send_exception", "sdp_exception", "cancel_cleanup", "generation_reuse", "handoff_register", "handoff_claim", "handoff_bind"])
async def test_replacement_failure_releases_only_candidate(registered_phones, trunks, monkeypatch, failure):
    from dataclasses import replace
    from custom_components.voip_stack import media_renegotiation
    from custom_components.voip_stack.call_replaces import prepare_replacement, prepare_replacement_grant
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.core.sip_transfer import SipReplaces, SipReferTarget
    from custom_components.voip_stack.endpoint_registry import EndpointBusyError
    from custom_components.voip_stack.sip_listener import SipInviteResult

    lab = trunks
    original, consultation = await establish_consultation(lab, registered_phones)
    manager = lab.calls.component("udp_listener")
    dialog = next(endpoint.active_dialogs[consultation.call_id] for endpoint in manager._dialog_endpoints()
                  if consultation.call_id in endpoint.active_dialogs)
    key = SipReplaces(consultation.call_id, dialog.to_tag, sip.extract_tag(dialog.request.header("From")))
    invite = replace(dialog.invite, call_id="failed-replacement", replaces=key,
                     caller="transfer_a", identified_source_endpoint_id="transfer_a")
    _, referring_id = lab.calls.bridge_for(original)
    grant = prepare_replacement_grant(lab.runtime, original, SipReferTarget("sip:transfer_c@127.0.0.1", key),
                                      lab.calls.sip_client_for(referring_id))
    relay = lab.calls.resource_for(consultation.call_id, "relay")
    previous_peer = relay.left
    claims = lab.calls.endpoint_claims_snapshot()
    selected_calls = {name: lab.runtime.endpoints.get(name).active_call_id for name in ("transfer_a", "transfer_b", "transfer_c")}
    ports = set(lab.runtime.rtp_port_pool["used"])
    real_prepare = media_renegotiation.async_prepare_media_update
    started = asyncio.Event()
    rollback_calls = []
    release = asyncio.Event()

    async def prepare(*args):
        if failure == "non2xx":
            return SipInviteResult(488, "Not Acceptable Here")
        if failure == "exception":
            raise RuntimeError("injected media preparation failure")
        if failure == "cancel":
            started.set()
            await asyncio.Future()
        result = await real_prepare(*args)
        if failure == "cancel_cleanup":
            async def slow_rollback():
                rollback_calls.append(True)
                started.set()
                await release.wait()
            return replace(result, rollback=slow_rollback)
        if failure == "rollback_exception":
            async def broken_rollback():
                rollback_calls.append(True)
                raise RuntimeError("injected rollback failure")
            return replace(result, rollback=broken_rollback)
        return result

    if failure == "busy":
        real_claim = lab.calls.claim_endpoint
        def claim(call_id, endpoint_id, **kwargs):
            if call_id == invite.call_id:
                raise EndpointBusyError(lab.runtime.endpoints.get(endpoint_id), call_id)
            return real_claim(call_id, endpoint_id, **kwargs)
        monkeypatch.setattr(lab.calls, "claim_endpoint", claim)
    monkeypatch.setattr(media_renegotiation, "async_prepare_media_update", prepare)
    if failure == "handoff_register":
        real_register = lab.calls.register_bridge
        def broken_register(**kwargs):
            result = real_register(**kwargs)
            if kwargs["source_call_id"] == invite.call_id:
                raise RuntimeError("injected handoff failure")
            return result
        monkeypatch.setattr(lab.calls, "register_bridge", broken_register)
    elif failure == "handoff_claim":
        real_transfer = lab.calls.transfer_endpoint_claim
        def broken_claim(source, target, endpoint):
            real_transfer(source, target, endpoint)
            if target == invite.call_id:
                raise RuntimeError("injected handoff failure")
        monkeypatch.setattr(lab.calls, "transfer_endpoint_claim", broken_claim)
    elif failure == "handoff_bind":
        from custom_components.voip_stack.bridge_media_updates import BridgeMediaUpdateBinder
        real_bind = BridgeMediaUpdateBinder.attach
        def broken_bind(self, *args, **kwargs):
            real_bind(self, *args, **kwargs)
            if kwargs["source_call_id"] == invite.call_id:
                raise RuntimeError("injected handoff failure")
        monkeypatch.setattr(BridgeMediaUpdateBinder, "attach", broken_bind)
    try:
        if failure in {"send_failure", "send_exception", "sdp_exception"}:
            from custom_components.voip_stack import sip_listener
            from custom_components.voip_stack.core.audio_format import AudioFormat
            async def route(_parsed):
                return await prepare_replacement(lab.hass, invite, "127.0.0.1")
            def send(raw, _addr):
                response = sip.parse_message(raw)
                if response.status_code == 200:
                    if failure == "send_exception":
                        raise ValueError("injected response construction failure")
                    return False
                return True
            endpoint = sip_listener.SipUdpEndpoint(
                local_ip="127.0.0.1", local_sip_port=5060, local_rtp_port=41000,
                supported_formats=[AudioFormat(8000, "s16le", 1, 20)], on_invite=route, send_override=send,
            )
            request = sip.build_request("INVITE", dialog.request.uri,
                [(name, invite.call_id if name.casefold() == "call-id" else value)
                 for name, value in dialog.request.headers] + [("Replaces", str(key))], dialog.request.body)
            try:
                with monkeypatch.context() as scoped:
                    if failure == "sdp_exception":
                        def broken_sdp(*_args):
                            raise ValueError("injected response construction failure")
                        scoped.setattr(sip_listener.sdp, "rewrite_sdp_origin", broken_sdp)
                    if failure == "send_failure":
                        await endpoint._handle_datagram(request, dialog.addr)
                    else:
                        with pytest.raises(ValueError, match="injected response"):
                            await endpoint._handle_datagram(request, dialog.addr)
                assert not endpoint.active_dialogs
                assert not endpoint.pending_invites
            finally:
                endpoint.connection_lost(None)
        elif failure == "cancel":
            task = asyncio.create_task(prepare_replacement(lab.hass, invite, "127.0.0.1"))
            await asyncio.wait_for(started.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif failure == "exception":
            with pytest.raises(RuntimeError, match="injected media"):
                await prepare_replacement(lab.hass, invite, "127.0.0.1")
        else:
            result = await prepare_replacement(lab.hass, invite, "127.0.0.1")
            if failure in {"busy", "non2xx"}:
                assert result.status == (486 if failure == "busy" else 488)
            elif failure.startswith("handoff_"):
                with pytest.raises(RuntimeError, match="injected handoff"):
                    await result.commit()
                await result.rollback()
                await result.rollback()
            elif failure == "cancel_cleanup":
                task = asyncio.create_task(result.rollback())
                await asyncio.wait_for(started.wait(), 2)
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
                await result.rollback()
                assert len(rollback_calls) == 1
            elif failure == "rollback_exception":
                for _ in range(2):
                    with pytest.raises(RuntimeError, match="injected rollback"):
                        await result.rollback()
                assert len(rollback_calls) == 1
            else:
                assert result.status == 200
                if failure == "original_ends":
                    old_session = lab.calls.get_session(original)
                    await asyncio.to_thread(registered_phones[0].hangup)
                    await asyncio.wait_for(old_session.terminated.wait(), 3)
                    with pytest.raises(RuntimeError, match="ownership changed"):
                        await result.commit()
                if failure == "generation_reuse":
                    from custom_components.voip_stack.endpoint_session import TerminationIntent, SipTerminationDisposition
                    candidate = lab.calls.get_session(invite.call_id)
                    await lab.calls.terminate_call_wait(invite.call_id, intent=TerminationIntent(
                        "test_reuse", sip_disposition=SipTerminationDisposition.NONE))
                    successor = lab.calls.upsert(invite.call_id, state="connecting", owner="bridge")
                    assert successor.generation != candidate.generation
                    await result.rollback()
                    assert lab.calls.get_session(invite.call_id) is successor and successor.live
                    await lab.calls.terminate_call_wait(invite.call_id, intent=TerminationIntent(
                        "test_done", sip_disposition=SipTerminationDisposition.NONE))
                await result.rollback()
                await result.rollback()
        assert lab.calls.get_session(invite.call_id) is None
        assert not list(lab.calls.resource_items("replacement_grant"))
        assert consultation.live
        assert lab.calls.resource_for(consultation.call_id, "relay") is relay
        assert relay.left is previous_peer
        if failure != "original_ends":
            assert lab.calls.get_session(original).live
            assert lab.calls.endpoint_claims_snapshot() == claims
            assert {name: lab.runtime.endpoints.get(name).active_call_id for name in selected_calls} == selected_calls
            assert set(lab.calls.sessions) == {original, consultation.call_id}
            assert set(lab.runtime.rtp_port_pool["used"]) == ports
            original_relay = lab.calls.resource_for(original, "relay")
            _, original_wire_id = lab.calls.bridge_for(original)
            await asyncio.to_thread(registered_phones[1].command, f"/callfind {original_wire_id}")
            await asyncio.to_thread(registered_phones[1].command, "/resume")
            before = original_relay.left_tx_packets
            async with asyncio.timeout(3):
                while original_relay.left_tx_packets <= before + 5:
                    await asyncio.sleep(0.02)
    finally:
        grant.release()
        if lab.calls.get_session(invite.call_id) is not None:
            from custom_components.voip_stack.endpoint_session import TerminationIntent, SipTerminationDisposition
            await lab.calls.terminate_call_wait(invite.call_id, intent=TerminationIntent(
                "test_cleanup", sip_disposition=SipTerminationDisposition.NONE))


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_replaces_accepts_the_media_commit_identity(registered_phones, trunks, monkeypatch):
    from dataclasses import replace
    from custom_components.voip_stack import media_renegotiation
    from custom_components.voip_stack.call_replaces import prepare_replacement, prepare_replacement_grant
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.core.sip_dialog import PreparedDialogIdentity
    from custom_components.voip_stack.core.sip_transfer import SipReplaces, SipReferTarget

    lab = trunks
    original, consultation = await establish_consultation(lab, registered_phones)
    manager = lab.calls.component("udp_listener")
    dialog = next(endpoint.active_dialogs[consultation.call_id] for endpoint in manager._dialog_endpoints()
                  if consultation.call_id in endpoint.active_dialogs)
    key = SipReplaces(consultation.call_id, dialog.to_tag, sip.extract_tag(dialog.request.header("From")))
    invite = replace(dialog.invite, call_id="committed-replacement", replaces=key,
                     caller="transfer_a", identified_source_endpoint_id="transfer_a")
    _, source_id = lab.calls.bridge_for(original)
    _, destination_id = lab.calls.bridge_for(consultation.call_id)
    client = lab.calls.sip_client_for(destination_id)
    before = client.dialog
    candidate = replace(before)
    grant = prepare_replacement_grant(lab.runtime, original, SipReferTarget("sip:transfer_c@127.0.0.1", key),
                                      lab.calls.sip_client_for(source_id))
    prepare_media = media_renegotiation.async_prepare_media_update
    async def prepare(*args):
        result = await prepare_media(*args)
        async def commit():
            await result.commit()
            client._prepared_reinvite = (before, candidate, (), "inactive")
            assert client.commit_prepared_reinvite(before, candidate)
        return replace(result, commit=commit, dialog_identity=PreparedDialogIdentity(client, before, candidate))
    monkeypatch.setattr(media_renegotiation, "async_prepare_media_update", prepare)
    try:
        result = await prepare_replacement(lab.hass, invite, "127.0.0.1")
        assert result.status == 200
        await result.commit()
        assert client.dialog is candidate
        replacement = lab.calls.get_session(invite.call_id)
        assert replacement.live and replacement.answer_committed
        assert lab.calls.sip_client_for(destination_id) is client
        await result.rollback()
        assert lab.calls.get_session(invite.call_id) is replacement
        await lab.calls.terminate_call_wait(invite.call_id, reason="test_complete")
    finally:
        grant.release()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("reject_progress", [False, True])
async def test_outside_refer_target_dialog_with_baresip_and_duplex_audio(registered_phones, trunks, monkeypatch, tmp_path, reject_progress):
    import time
    from custom_components.voip_stack.core import sip, sip_transfer
    from custom_components.voip_stack.sip_registrar import SipRegistration

    lab = trunks
    source = lab.phones[0]
    source_port = source.transport.get_extra_info("sockname")[1]
    server_port = lab.runtime.transport_config["sip_port"]
    registrar = lab.calls.component("registrar")
    registrar.update_accounts([*registrar.accounts.values(), SipAccount("bedroom0", "Controlled", "test-only")])
    registrar.registrations["bedroom0"] = SipRegistration(username="bedroom0",
        contact_uri=f"sip:bedroom0@127.0.0.1:{source_port}", source_host="127.0.0.1", source_port=source_port,
        transport="UDP", expires_at=time.time() + 120)
    lab.roster[:] = assemble_directory_entries(lab.hass, [], registrar.registered_roster_entries())
    receive = source.datagram_received
    def incoming(raw, addr):
        message = sip.parse_message(raw)
        if message.method == "NOTIFY":
            source.requests.append(message)
            if reject_progress and sip_transfer.parse_sipfrag_status(message.body) == 100:
                headers = [(key, message.header(key)) for key in ("Via", "From", "To", "Call-ID", "CSeq")]
                source.transport.sendto(sip.build_response(481, "Subscription Does Not Exist", headers), addr)
            else:
                source.respond(message, addr, 200)
        else:
            receive(raw, addr)
    monkeypatch.setattr(source, "datagram_received", incoming)
    call_id = "controlled-original"
    from_header = "<sip:bedroom0@127.0.0.1>;tag=original-caller"
    offer = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Independent caller\r\n"
             "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
             f"m=audio {source.rtp_socket.getsockname()[1]} RTP/AVP 96\r\n"
             "a=rtpmap:96 L16/16000\r\na=ptime:20\r\na=sendrecv\r\n").encode()
    base = [("Via", f"SIP/2.0/UDP 127.0.0.1:{source_port};branch=z9hG4bK-controlled"),
            ("From", from_header), ("To", "<sip:transfer_b@127.0.0.1>"),
            ("Call-ID", call_id), ("CSeq", "1 INVITE"),
            ("Contact", f"<sip:bedroom0@127.0.0.1:{source_port}>"), ("Content-Type", "application/sdp")]
    source.transport.sendto(sip.build_request("INVITE", f"sip:transfer_b@127.0.0.1:{server_port}", base, offer), ("127.0.0.1", server_port))
    async def wait_for(predicate):
        async with asyncio.timeout(8):
            while not predicate():
                for peer in registered_phones:
                    peer.read()
                await asyncio.sleep(.01)
    try:
        await wait_for(lambda: any(item.status_code == 200 and item.header("Call-ID") == call_id for item in source.requests))
        answer = next(item for item in source.requests if item.status_code == 200 and item.header("Call-ID") == call_id)
        tag = sip.extract_tag(answer.header("To"))
        ack_headers = [(key, answer.header(key)) for key in ("From", "To", "Call-ID")]
        ack_headers += [("Via", f"SIP/2.0/UDP 127.0.0.1:{source_port};branch=z9hG4bK-controlled-ack"), ("CSeq", "1 ACK")]
        source.transport.sendto(sip.build_request("ACK", f"sip:transfer_b@127.0.0.1:{server_port}", ack_headers), ("127.0.0.1", server_port))
        target = f"sip:transfer_c@127.0.0.1:{server_port}"
        refer = sip.build_request("REFER", f"sip:HA@127.0.0.1:{server_port}", [
            ("Via", f"SIP/2.0/UDP 127.0.0.1:{source_port};branch=z9hG4bK-modern-referral"),
            ("From", "<sip:bedroom0@127.0.0.1>;tag=referral-origin"), ("To", "<sip:HA@127.0.0.1>"),
            ("Call-ID", "controlled-referral"), ("CSeq", "7 REFER"), ("Require", "tdialog"),
            ("Contact", f"<sip:bedroom0@127.0.0.1:{source_port}>"), ("Refer-To", f"<{target}>"),
            ("Target-Dialog", f"{call_id};local-tag={tag};remote-tag=original-caller"),
        ])
        source.transport.sendto(refer, ("127.0.0.1", server_port))
        if not reject_progress:
            await wait_for(lambda: any(item.method == "NOTIFY" and item.header("Call-ID") == "controlled-referral"
                                      and sip_transfer.parse_sipfrag_status(item.body) >= 200 for item in source.requests))
        else:
            await wait_for(lambda: any(set(item.endpoint_claims) == {"transfer_b", "transfer_c"} and item.state == "in_call"
                                      and lab.calls.resource_for(item.call_id, "relay") is not None for item in lab.calls.sessions.values()))
        notifications = [item for item in source.requests if item.method == "NOTIFY" and item.header("Call-ID") == "controlled-referral"]
        assert [sip_transfer.parse_sipfrag_status(item.body) for item in notifications] == ([100] if reject_progress else [100, 200])
        assert all(item.header("Event") == "refer;id=7" for item in notifications)
        assert not any(item.header("Referred-By") for item in [sip.parse_message(refer)])
        await wait_for(lambda: any(set(item.endpoint_claims) == {"transfer_b", "transfer_c"} and item.state == "in_call"
                                      and lab.calls.resource_for(item.call_id, "relay") is not None for item in lab.calls.sessions.values()))
        current = next(item for item in lab.calls.sessions.values() if set(item.endpoint_claims) == {"transfer_b", "transfer_c"})
        relay = lab.calls.resource_for(current.call_id, "relay")
        left, right = relay.left_tx_packets, relay.right_tx_packets
        await wait_for(lambda: relay.left_tx_packets >= left + 30 and relay.right_tx_packets >= right + 30)
        if lab.calls.get_session(call_id) is not None:
            bye_headers = [(key, answer.header(key)) for key in ("From", "To", "Call-ID")]
            bye_headers += [("Via", f"SIP/2.0/UDP 127.0.0.1:{source_port};branch=z9hG4bK-controlled-bye"), ("CSeq", "2 BYE")]
            source.transport.sendto(sip.build_request("BYE", f"sip:HA@127.0.0.1:{server_port}", bye_headers), ("127.0.0.1", server_port))
        await asyncio.to_thread(registered_phones[2].hangup)
        await wait_for(lambda: not lab.calls.sessions)
        for peer in registered_phones:
            await asyncio.to_thread(peer.close)
        for name, expected in (("transfer_b", 650), ("transfer_c", 550)):
            found = False
            for path in (tmp_path / name).glob("*-dec.wav"):
                with wave.open(str(path), "rb") as audio:
                    rate = audio.getframerate()
                    samples = struct.unpack(f"<{audio.getnframes()}h", audio.readframes(audio.getnframes()))[-1600:]
                if len(samples) >= 800:
                    powers = {frequency: abs(sum(value * complex(math.cos(2 * math.pi * frequency * index / rate),
                        math.sin(2 * math.pi * frequency * index / rate)) for index, value in enumerate(samples))) for frequency in (450, 550, 650)}
                    found |= max(powers, key=powers.get) == expected and powers[expected] > 10000
            assert found, f"missing independent peer tone in {name}"
    finally:
        for session in tuple(lab.calls.sessions.values()):
            await lab.calls.terminate_call_wait(session.call_id, reason="test_cleanup")


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones,initial_video,final_video", [
    ({"video": ("transfer_b", "transfer_c")}, True, False),
    ({"video": ("transfer_a", "transfer_c")}, False, True),
    ({"video": ("transfer_a", "transfer_b", "transfer_c")}, True, True),
], indirect=["registered_phones"])
async def test_replaces_changes_video_contract_on_real_peer_dialogs(registered_phones, trunks, initial_video, final_video, monkeypatch, tmp_path):
    from collections import defaultdict
    from custom_components.voip_stack.sip_video_relay import SipVideoRtpRelay
    captured = defaultdict(list)
    start_video = SipVideoRtpRelay.start

    class CaptureTransport:
        def __init__(self, transport, key):
            self.transport, self.key = transport, key
        def sendto(self, raw, address):
            if len(captured[self.key]) < 512:
                captured[self.key].append(bytes(raw))
            self.transport.sendto(raw, address)
        def __getattr__(self, name):
            return getattr(self.transport, name)

    async def start(relay):
        await start_video(relay)
        for key, transport in tuple(relay._transports.items()):
            if not key[1] and not isinstance(transport, CaptureTransport):
                relay._transports[key] = CaptureTransport(transport, (id(relay), key[0]))

    monkeypatch.setattr(SipVideoRtpRelay, "start", start)
    lab = trunks
    original, consultation = await establish_consultation(lab, registered_phones)
    relay = lab.calls.resource_for(consultation.call_id, "relay")
    _, destination_id = lab.calls.bridge_for(consultation.call_id)
    client = lab.calls.sip_client_for(destination_id)
    assert (client.dialog.video_format is not None) is initial_video
    before = client.dialog
    await asyncio.to_thread(registered_phones[1].command, "/atransferexec")
    async with asyncio.timeout(6):
        while True:
            for peer in registered_phones:
                peer.read()
            sessions = tuple(lab.calls.sessions.values())
            if len(sessions) == 1 and sessions[0].call_id not in {original, consultation.call_id}:
                break
            assert "transfer failed" not in registered_phones[1].output.lower(), registered_phones[1].output
            await asyncio.sleep(.01)
    replacement = sessions[0]
    assert replacement.live and replacement.answer_committed
    assert lab.calls.resource_for(replacement.call_id, "relay") is relay
    assert lab.calls.sip_client_for(destination_id) is client
    assert client.dialog is not before
    assert (client.dialog.video_format is not None) is final_video
    assert (relay.video_relay is not None) is final_video
    left, right = relay.left_tx_packets, relay.right_tx_packets
    async with asyncio.timeout(4):
        while relay.left_tx_packets < left + 20 or relay.right_tx_packets < right + 20:
            for peer in registered_phones:
                peer.read()
            await asyncio.sleep(.01)
    if final_video:
        baseline = {side: len(captured[(id(relay.video_relay), side)]) for side in ("left", "right")}
        async with asyncio.timeout(4):
            while any(len(captured[(id(relay.video_relay), side)]) < baseline[side] + 15 for side in baseline):
                for peer in registered_phones:
                    peer.read()
                await asyncio.sleep(.01)
        for side in baseline:
            await asyncio.to_thread(assert_h264_color_bars, captured[(id(relay.video_relay), side)], tmp_path / f"{side}.h264")
    await asyncio.to_thread(registered_phones[0].hangup)
    async with asyncio.timeout(3):
        while lab.calls.sessions:
            for peer in registered_phones:
                peer.read()
            await asyncio.sleep(.01)
    assert not list(lab.calls.resource_items("replacement_grant"))

    for peer in registered_phones:
        await asyncio.to_thread(peer.close)
    assert_peer_audio_tones(tmp_path, (("transfer_a", 650), ("transfer_c", 450)))


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"video": ("transfer_b", "transfer_c")}], indirect=True)
async def test_video_replacement_handoff_failure_compensates_remote_media(registered_phones, trunks, monkeypatch):
    from dataclasses import replace
    from custom_components.voip_stack.call_replaces import prepare_replacement, prepare_replacement_grant
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.core.sip_transfer import SipReplaces, SipReferTarget

    lab = trunks
    original_id, consultation = await establish_consultation(lab, registered_phones)
    original = lab.calls.get_session(original_id)
    manager = lab.calls.component("udp_listener")
    dialog = next(endpoint.active_dialogs[consultation.call_id] for endpoint in manager._dialog_endpoints()
                  if consultation.call_id in endpoint.active_dialogs)
    key = SipReplaces(consultation.call_id, dialog.to_tag, sip.extract_tag(dialog.request.header("From")))
    _, source_id = lab.calls.bridge_for(original_id)
    _, destination_id = lab.calls.bridge_for(consultation.call_id)
    client = lab.calls.sip_client_for(destination_id)
    relay = lab.calls.resource_for(consultation.call_id, "relay")
    video = relay.video_relay
    assert video is not None and client.dialog.video_format is not None
    audio_sdp = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=Replacement\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                 f"m=audio {dialog.invite.remote_rtp_port} RTP/AVP 0\r\na=rtpmap:0 PCMU/8000\r\na=ptime:20\r\na=sendrecv\r\n").encode()
    invite = replace(dialog.invite, call_id="video-rollback-replacement", replaces=key,
                     caller="transfer_a", identified_source_endpoint_id="transfer_a",
                     remote_sdp=audio_sdp, video_format=None, local_video_format=None)
    grant = prepare_replacement_grant(lab.runtime, original_id,
        SipReferTarget("sip:transfer_c@127.0.0.1", key), lab.calls.sip_client_for(source_id))
    register = lab.calls.register_bridge

    def failed_handoff(**kwargs):
        register(**kwargs)
        if kwargs["source_call_id"] == invite.call_id:
            raise RuntimeError("injected handoff failure after dialog registration")

    try:
        result = await prepare_replacement(lab.hass, invite, "127.0.0.1")
        assert result.status == 200
        monkeypatch.setattr(lab.calls, "register_bridge", failed_handoff)
        with pytest.raises(RuntimeError, match="injected handoff failure"):
            await result.commit()
        await result.rollback()
        assert lab.calls.get_session(original_id) is original and original.live
        assert lab.calls.get_session(consultation.call_id) is consultation and consultation.live
        assert lab.calls.get_session(invite.call_id) is None
        assert lab.calls.sip_client_for(destination_id) is client
        assert client.dialog.video_format is not None
        assert relay.video_relay is video
        assert len(video._transports) == 4
        left, right = video.left_tx_packets, video.right_tx_packets
        async with asyncio.timeout(4):
            while video.left_tx_packets < left + 3 or video.right_tx_packets < right + 3:
                for peer in registered_phones:
                    peer.read()
                await asyncio.sleep(.01)
        assert not list(lab.calls.resource_items("replacement_grant"))
    finally:
        grant.release()


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_long_lived_transfer_runtime_releases_each_generation(registered_phones, trunks):
    import json
    import os
    from pathlib import Path

    lab = trunks
    cycles = int(os.environ.get("VOIP_CONSOLIDATION_SOAK_CYCLES", "2"))
    duration = float(os.environ.get("VOIP_CONSOLIDATION_SOAK_SECONDS", "0"))
    report = os.environ.get("VOIP_CONSOLIDATION_SOAK_REPORT", "")
    started = asyncio.get_running_loop().time()

    async def wait_until(predicate):
        async with asyncio.timeout(6):
            while not predicate():
                for peer in registered_phones:
                    peer.read()
                await asyncio.sleep(.01)

    for cycle in range(cycles):
        await asyncio.to_thread(registered_phones[0].command, "/dial transfer_b")
        await wait_until(lambda: len(lab.calls.sessions) == 1 and all(
            session.state == "in_call" and lab.calls.resource_for(session.call_id, "relay") is not None
            for session in lab.calls.sessions.values()))
        original = next(iter(lab.calls.sessions))
        await asyncio.to_thread(registered_phones[1].command,
            f'/atransferstart sip:transfer_c@127.0.0.1:{lab.runtime.transport_config["sip_port"]}')
        await wait_until(lambda: len(lab.calls.sessions) == 2 and all(
            session.state == "in_call" and lab.calls.resource_for(session.call_id, "relay") is not None
            for session in lab.calls.sessions.values()))
        prior = set(lab.calls.sessions)
        assert original in prior
        await asyncio.to_thread(registered_phones[1].command, "/atransferexec")
        await wait_until(lambda: len(lab.calls.sessions) == 1 and not set(lab.calls.sessions) & prior)
        session = next(iter(lab.calls.sessions.values()))
        relay = lab.calls.resource_for(session.call_id, "relay")
        left, right = relay.left_tx_packets, relay.right_tx_packets
        await wait_until(lambda: relay.left_tx_packets >= left + 10 and relay.right_tx_packets >= right + 10)
        await asyncio.to_thread(registered_phones[2].hangup)
        await wait_until(lambda: not lab.calls.sessions)
        assert not lab.runtime.rtp_port_pool.get("used")
        assert not list(lab.calls.resource_items("relay"))
        assert not list(lab.calls.resource_items("replacement_grant"))
        assert all(not lab.runtime.endpoints.get(name).active_call_id for name in ("transfer_a", "transfer_b", "transfer_c"))
        application = lab.calls.component("sip_application")
        if application is not None:
            snapshot = application.snapshot()
            assert snapshot["task_failures"] == 0
            assert snapshot["subscriptions"] == 0
            assert snapshot["notifiers"] == 0
            assert snapshot["signaling_leases"] == 0
            assert snapshot["cached_responses"] <= 512
        else:
            snapshot = {}
        if report:
            with Path(report).open("a") as stream:
                stream.write(json.dumps({"cycle": cycle + 1, "elapsed": asyncio.get_running_loop().time() - started,
                    "sessions": len(lab.calls.sessions), "ports": len(lab.runtime.rtp_port_pool.get("used", ())),
                    "application": snapshot, "fds": len(list(Path("/proc/self/fd").iterdir())),
                    "tasks": len(asyncio.all_tasks())}) + "\n")
        deadline = started + duration * (cycle + 1) / cycles
        while (remaining := deadline - asyncio.get_running_loop().time()) > 0:
            await asyncio.sleep(min(remaining, 30))
            for peer in registered_phones:
                peer.read()


def assert_h264_color_bars(datagrams, evidence_path):
    """Decode real forwarded RFC 6184 packets with independent FFmpeg."""
    import subprocess
    from custom_components.voip_stack.core import rtp
    elementary = bytearray()
    fragment = bytearray()
    for raw in datagrams:
        if len(raw) >= 20 and raw[:2] == b"\x00\x11" and raw[4:8] == b"\x21\x12\xa4\x42":
            continue  # STUN Binding Indication maintains the negotiated media path.
        payload = rtp.parse_packet(raw).payload
        if not payload:
            continue
        nal_type = payload[0] & 31
        if 1 <= nal_type <= 23:
            elementary += b"\x00\x00\x00\x01" + payload
        elif nal_type == 24:
            offset = 1
            while offset + 2 <= len(payload):
                length = int.from_bytes(payload[offset:offset+2], "big")
                offset += 2
                assert length and offset + length <= len(payload)
                elementary += b"\x00\x00\x00\x01" + payload[offset:offset+length]
                offset += length
        elif nal_type == 28 and len(payload) >= 2:
            if payload[1] & 0x80:
                fragment = bytearray([(payload[0] & 0xE0) | (payload[1] & 31)])
            fragment += payload[2:]
            if payload[1] & 0x40:
                elementary += b"\x00\x00\x00\x01" + fragment
                fragment.clear()
    evidence_path.write_bytes(elementary)
    decoded = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "h264", "-i", "pipe:0",
        "-vf", "scale=160:120", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1"],
        input=bytes(elementary), capture_output=True, timeout=5, check=True).stdout
    frame_bytes = 160 * 120 * 3
    assert len(decoded) >= frame_bytes * 2
    last = decoded[-frame_bytes:]
    for x, channel in ((26, 0), (80, 1), (133, 2)):
        pixel = last[(60 * 160 + x) * 3:(60 * 160 + x) * 3 + 3]
        assert pixel[channel] > 120 and pixel[channel] > max(pixel[i] for i in range(3) if i != channel) + 80


def assert_peer_audio_tones(directory, expected_tones):
    """Check independently decoded peer audio after their WAV writers close."""
    for name, expected in expected_tones:
        found = False
        for path in (directory / name).glob("*-dec.wav"):
            with wave.open(str(path), "rb") as audio:
                assert audio.getsampwidth() == 2 and audio.getnchannels() == 1
                rate = audio.getframerate()
                samples = struct.unpack(f"<{audio.getnframes()}h", audio.readframes(audio.getnframes()))[-1600:]
            if len(samples) < 800:
                continue
            powers = {frequency: abs(sum(value * complex(
                math.cos(2 * math.pi * frequency * index / rate),
                math.sin(2 * math.pi * frequency * index / rate),
            ) for index, value in enumerate(samples))) for frequency in (450, 550, 650)}
            found |= max(powers, key=powers.get) == expected and powers[expected] > 10000
        assert found, f"{name} did not receive the other phone's recorded tone"


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("cancel_wait", [False, True])
async def test_replacement_waits_for_destination_remote_media_commit(registered_phones, trunks, monkeypatch, cancel_wait):
    from dataclasses import replace
    from custom_components.voip_stack.call_replaces import prepare_replacement, prepare_replacement_grant
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.core.sip_transfer import SipReplaces, SipReferTarget

    lab = trunks
    original, consultation = await establish_consultation(lab, registered_phones)
    manager = lab.calls.component("udp_listener")
    dialog = next(endpoint.active_dialogs[consultation.call_id] for endpoint in manager._dialog_endpoints()
                  if consultation.call_id in endpoint.active_dialogs)
    key = SipReplaces(consultation.call_id, dialog.to_tag, sip.extract_tag(dialog.request.header("From")))
    invite = replace(dialog.invite, call_id="destination-media-replacement", replaces=key,
                     caller="transfer_a", identified_source_endpoint_id="transfer_a")
    _, referring_id = lab.calls.bridge_for(original)
    _, destination_id = lab.calls.bridge_for(consultation.call_id)
    client = lab.calls.sip_client_for(destination_id)
    entered, release = asyncio.Event(), asyncio.Event()
    update = client.on_media_update

    async def blocked_update(*args):
        entered.set()
        await release.wait()
        return await update(*args)

    monkeypatch.setattr(client, "on_media_update", blocked_update)
    grant = prepare_replacement_grant(lab.runtime, original, SipReferTarget("sip:transfer_c@127.0.0.1", key),
                                      lab.calls.sip_client_for(referring_id))
    pending = None
    result = None
    try:
        await asyncio.to_thread(registered_phones[2].command, "/hold")
        await asyncio.wait_for(entered.wait(), 2)
        pending = asyncio.create_task(prepare_replacement(lab.hass, invite, "127.0.0.1"))
        done, _ = await asyncio.wait({pending}, timeout=.05)
        assert not done, "replacement overtook the destination's pending media commit"
        if cancel_wait:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            assert not list(lab.calls.resource_items("replacement_grant"))
            assert lab.calls.get_session(invite.call_id) is None
            assert consultation.live and lab.calls.get_session(original).live
            return
        release.set()
        result = await asyncio.wait_for(pending, 2)
        assert result.status == 200
        await result.rollback()
        assert consultation.live and lab.calls.get_session(original).live
        assert lab.calls.sip_client_for(destination_id) is client and client.dialog is not None
        assert lab.calls.get_session(invite.call_id) is None
    finally:
        release.set()
        if pending is not None:
            if not pending.done():
                pending.cancel()
            settled = await asyncio.gather(pending, return_exceptions=True)
            if result is None and not isinstance(settled[0], BaseException):
                result = settled[0]
        if result is not None and result.rollback is not None:
            await result.rollback()
        grant.release()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("transferor_index", [0, 1])
@pytest.mark.parametrize("retained_kind", [EndpointKind.SIP_ACCOUNT, EndpointKind.ESPHOME])
@pytest.mark.parametrize("first_rejected", [False, True])
@pytest.mark.parametrize("use_leg_id", [False, True])
async def test_blind_transfer_service_retains_opposite_phone(
    registered_phones, trunks, transferor_index, retained_kind, first_rejected, use_leg_id,
):
    """The retained SIP peer can establish its referred dialog before BYE."""
    from homeassistant.core import ServiceCall
    from custom_components.voip_stack import _handle_sip_transfer_service

    lab = trunks
    caller, callee, target = registered_phones
    await asyncio.to_thread(caller.dial, "transfer_b")
    await asyncio.to_thread(callee.wait_for, "Call established", 5)
    original_id = lab.runtime.endpoints.get("transfer_a").active_call_id
    original = lab.calls.get_session(original_id)
    assert original is not None and original.live
    retained_index = 1 - transferor_index
    retained_id = ("transfer_a", "transfer_b")[retained_index]
    transferor_id = ("transfer_a", "transfer_b")[transferor_index]
    # bareSIP supplies independent real SIP transactions. The ESP endpoint kind
    # reproduces the admission policy used by firmware staging a blind REFER.
    lab.runtime.endpoints.update(retained_id, kind=retained_kind)
    request_call_id = lab.calls.bridge_link_for(original_id) if use_leg_id else original_id
    request = ServiceCall(
        lab.hass, "voip_stack", "transfer",
        {"device_id": transferor_id, "call_id": request_call_id, "destination": "transfer_c"},
    )
    if first_rejected:
        lab.runtime.endpoints.update("transfer_c", dnd=True)
        rejected = await _handle_sip_transfer_service(request)
        assert not rejected["success"] and rejected["status"] == 486, rejected
        assert original.live and set(original.endpoint_claims) == {"transfer_a", "transfer_b"}
        assert lab.runtime.endpoints.get(retained_id).active_call_id == original_id
        original_relay = lab.calls.resource_for(original_id, "relay")
        left, right = original_relay.left_tx_packets, original_relay.right_tx_packets
        async with asyncio.timeout(3):
            while original_relay.left_tx_packets <= left + 10 or original_relay.right_tx_packets <= right + 10:
                await asyncio.sleep(0.01)
        lab.runtime.endpoints.update("transfer_c", dnd=False)
    result = await _handle_sip_transfer_service(request)
    assert result["success"], result
    assert result["call_id"] == original_id
    async with asyncio.timeout(5):
        while True:
            for peer in registered_phones:
                peer.read()
            sessions = tuple(lab.calls.sessions.values())
            if len(sessions) == 1 and sessions[0].call_id != original_id:
                replacement = sessions[0]
                break
            await asyncio.sleep(0.01)
    assert set(replacement.endpoint_claims) == {retained_id, "transfer_c"}
    assert not lab.runtime.endpoints.get(transferor_id).active_call_id
    assert lab.runtime.endpoints.get(retained_id).active_call_id == replacement.call_id
    relay = lab.calls.resource_for(replacement.call_id, "relay")
    left, right = relay.left_tx_packets, relay.right_tx_packets
    async with asyncio.timeout(3):
        while relay.left_tx_packets <= left + 10 or relay.right_tx_packets <= right + 10:
            await asyncio.sleep(0.01)
    assert target.read().count("Call established") == 1
    await asyncio.to_thread(registered_phones[retained_index].command, "/hangupall")
    async with asyncio.timeout(4):
        while lab.calls.sessions:
            await asyncio.sleep(0.01)
    assert not lab.calls.endpoint_claims_snapshot()
    assert not lab.runtime.rtp_port_pool.get("used")


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_legacy_esp_refer_rejection_keeps_original_busy(registered_phones, trunks, monkeypatch):
    """A peer without REFER retains its original call and destination claim."""
    from homeassistant.core import ServiceCall
    from custom_components.voip_stack import _handle_sip_transfer_service
    from custom_components.voip_stack.core import sip
    from custom_components.voip_stack.roster import RosterEntry

    lab = trunks
    caller, second, _target = registered_phones
    legacy = lab.phones[0]
    lab.runtime.endpoints.update("sip:bedroom0", kind=EndpointKind.ESPHOME, availability="available")
    port = legacy.transport.get_extra_info("sockname")[1]
    lab.roster.append(RosterEntry(
        "bedroom0", sip_uri=f"sip:bedroom0@127.0.0.1:{port}",
        metadata={"endpoint_id": "sip:bedroom0", "endpoint_kind": "esphome"},
    ))
    original_dispatch = legacy.datagram_received
    refers = []

    def dispatch(raw, address):
        request = sip.parse_message(raw)
        if request.method != "REFER":
            original_dispatch(raw, address)
            return
        refers.append(request)
        headers = [(name, request.header(name)) for name in ("Via", "From", "To", "Call-ID", "CSeq")]
        legacy.transport.sendto(sip.build_response(501, "Not Implemented", headers), address)

    monkeypatch.setattr(legacy, "datagram_received", dispatch)
    await asyncio.to_thread(caller.dial, "bedroom0")
    original_id = lab.runtime.endpoints.get("transfer_a").active_call_id
    original = lab.calls.get_session(original_id)
    result = await _handle_sip_transfer_service(ServiceCall(
        lab.hass, "voip_stack", "transfer",
        {"device_id": "transfer_a", "call_id": original_id, "destination": "transfer_c"},
    ))
    assert not result["success"] and result["status"] == 501, result
    assert len(refers) == 1
    assert original.live and original.state == "in_call"
    assert set(original.endpoint_claims) == {"transfer_a", "sip:bedroom0"}
    assert not legacy.bye.is_set()
    await asyncio.to_thread(second.command, "/dial bedroom0")
    await asyncio.to_thread(second.wait_for, "486 Busy Here", 5)
    assert len(legacy.invites) == 1
    assert lab.runtime.endpoints.get("sip:bedroom0").active_call_id == original_id
    await asyncio.to_thread(caller.hangup)
    await asyncio.wait_for(original.terminated.wait(), 3)
