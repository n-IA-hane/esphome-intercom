"""Adversarial public-policy and unknown-peer checks for shared-account codecs."""

import asyncio
from dataclasses import replace
import json
import re

import pytest

from custom_components.voip_stack.codec_settings import effective_codec_policy
from custom_components.voip_stack.core import sip
from custom_components.voip_stack.endpoint_session import TerminationIntent
from custom_components.voip_stack.store import config_entry
from tests.test_canonical_account_calls_ha import (
    canonical_browser as canonical_browser,
    logical_browser as logical_browser,
    registered_phones as registered_phones,
    trunks as trunks,
    lab as lab,
)
from tests.test_browser_logical_availability_ha import resources
from tests.support.shared_codec_peer import CASES, CodecPeer
from tests.test_shared_account_codec_media_ha import until

pytestmark = pytest.mark.ha


class NoOptionsPeer(CodecPeer):
    """Capture an ordinary unknown UA without publishing discovery capabilities."""

    def __init__(self, case):
        super().__init__(case, "uncached", "unused-isolated-peer")
        self.offered = asyncio.Event()

    def datagram_received(self, raw, address):
        message = sip.parse_message(raw)
        if message.method == "OPTIONS":
            self.signalling.append(raw)
            return
        if message.method == "INVITE":
            self.signalling.append(raw)
            self.offers.append(message.body)
            self.current = message, address
            self.remote_sdp = message.body
            self.call_id = message.header("Call-ID")
            self.answer(message, address, status=180)
        else:
            super().datagram_received(raw, address)
        if message.method == "INVITE":
            self.offered.set()


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("logical_browser", [{"ringing_timeout": 30}], indirect=True)
async def test_public_dialer_offers_explicit_l24_to_unknown_peer_without_options(
    canonical_browser,
    tmp_path,
    socket_enabled,
):
    lab = canonical_browser
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            "audio_codecs": [{"codec": "L24", "enabled": True}],
            "video_codecs": [],
        },
    )
    policy = effective_codec_policy(lab.hass, endpoint_id="casa")
    assert policy.audio == ("L24",)
    assert "audio_codecs" not in lab.browser_subentry.data
    peer = NoOptionsPeer(next(case for case in CASES if case.name == "L24"))
    action = None
    evidence = {
        "config_entry_audio_codecs": entry.data["audio_codecs"],
        "account_override": "absent/inherited",
        "effective_audio": policy.audio,
        "peer_registered": False,
        "peer_options_answers": 0,
    }
    try:
        await lab.hass.loop.create_datagram_endpoint(
            lambda: peer, local_addr=("127.0.0.1", 0)
        )
        assert not lab.calls.component("registrar").registered_contacts("uncached")
        target = (
            f"sip:uncached@127.0.0.1:{peer.transport.get_extra_info('sockname')[1]}"
        )
        action = asyncio.create_task(
            lab.hass.services.async_call(
                "voip_stack",
                "call",
                {"device_id": "browser-casa", "destination": target},
                blocking=True,
                return_response=True,
            )
        )
        await until(lambda: action.done() or peer.offered.is_set())
        if peer.offered.is_set():
            mapping = re.search(rb"a=rtpmap:\d+ L24/(\d+)(?:/(\d+))?", peer.offers[0])
            if mapping:
                # The unknown UA chooses an actual offered mapping. The
                # enabled-codec contract imposes no private RTP geometry.
                peer.case = replace(
                    peer.case,
                    clock=int(mapping[1]),
                    pcm_rate=int(mapping[1]),
                    channels=int(mapping[2] or b"1"),
                )
                await peer.accept()
            else:
                invite, address = peer.current
                peer.answer(invite, address, status=488)
        try:
            evidence["service_result"] = await asyncio.wait_for(action, 5)
            evidence["service_error"] = None
        except Exception as error:
            evidence["service_error"] = {
                "type": type(error).__name__,
                "message": str(error),
            }
        evidence["wire_offers"] = [body.decode() for body in peer.offers]
        evidence["wire_request_methods"] = [
            raw.split(b" ", 1)[0].decode()
            for raw in peer.signalling
            if not raw.startswith(b"SIP/2.0")
        ]
        assert evidence["service_error"] is None, evidence
        assert len(peer.offers) == 1 and b"L24/" in peer.offers[0], evidence
        await asyncio.wait_for(peer.acked.wait(), 3)
    finally:
        (tmp_path / "unknown-l24.json").write_text(
            json.dumps(evidence, indent=2, default=repr) + "\n"
        )
        if action is not None and not action.done():
            action.cancel()
            await asyncio.gather(action, return_exceptions=True)
        for session in tuple(lab.calls.sessions.values()):
            barrier = lab.calls.request_termination(
                session.call_id, TerminationIntent("test_done")
            )
            if barrier is not None:
                await barrier
        await peer.close()
        assert resources(lab)["call_scoped_quiescent"]


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("logical_browser", [{"ringing_timeout": 30}], indirect=True)
@pytest.mark.parametrize("policy_source", ["global", "account"])
@pytest.mark.browser_real
async def test_unknown_l24_public_card_call_has_independent_duplex_media(
    canonical_browser,
    hass_ws_client,
    hass_admin_user,
    tmp_path,
    socket_enabled,
    policy_source,
):
    from tests.support.shared_codec_browser import Card
    from tests.support.shared_codec_peer import (
        check_wire,
        decode_named_rtp,
        measure_tone,
    )
    from tests.test_shared_account_codec_media_ha import (
        received_seconds,
        measure_browser,
    )
    from tests.test_browser_logical_availability_ha import public_phone

    lab = canonical_browser
    entry = config_entry(lab.hass)
    rows = [{"codec": "L24", "enabled": True}]
    if policy_source == "global":
        lab.hass.config_entries.async_update_entry(
            entry, data={**entry.data, "audio_codecs": rows, "video_codecs": []}
        )
    else:
        lab.hass.config_entries.async_update_subentry(
            entry,
            lab.browser_subentry,
            data={
                **lab.browser_subentry.data,
                "audio_codecs": rows,
                "video_codecs": [],
            },
        )
    policy = effective_codec_policy(lab.hass, endpoint_id="casa")
    assert policy.audio == ("L24",) and policy.audio_configured
    peer = NoOptionsPeer(next(case for case in CASES if case.name == "L24"))
    card = Card()
    evidence = {
        "policy_source": policy_source,
        "effective_audio": policy.audio,
        "audio_configured": policy.audio_configured,
    }
    try:
        await lab.hass.loop.create_datagram_endpoint(
            lambda: peer, local_addr=("127.0.0.1", 0)
        )
        await card.start(lab, hass_ws_client, tmp_path / "chromium", hass_admin_user)
        destination = (
            f"sip:uncached@127.0.0.1:{peer.transport.get_extra_info('sockname')[1]}"
        )
        await card.command("dial", destination=destination)
        await asyncio.wait_for(peer.offered.wait(), 5)
        body = peer.offers[-1]
        assert re.search(rb"a=rtpmap:\d+ L24/16000/1", body), body.decode()
        await peer.accept()
        await until(lambda: next(iter(lab.calls.sessions.values())).state == "in_call")
        phone = await public_phone(lab)
        call_id = phone["calls"][0]["call_id"]
        await peer.start_media(750)
        await until(lambda: received_seconds(peer) >= 0.85)
        await peer.stop_media()
        stats = await card.command("stats")
        assert stats["errors"] == []
        assert stats["card"]["state"] == "in_call" and stats["card"]["audioReady"]
        evidence.update(
            offer=body.decode(),
            answer=peer.receive_sdp.decode(),
            browser=stats,
            wire_to_peer=check_wire(peer.case, peer.received),
            card_receive_tone=measure_browser(stats, call_id, "rx", 750),
            card_microphone_tone=measure_browser(stats, call_id, "tx", 650),
        )
        decoded = await decode_named_rtp(
            peer.case, peer.received, peer.receive_sdp, tmp_path / "peer-receive"
        )
        evidence["peer_tone"] = measure_tone(decoded, 16000, 650)
        assert evidence["wire_to_peer"]["payload_bytes"] == [960]
        assert evidence["wire_to_peer"]["actual_packet_ms"] == [20]
        assert not any(raw.startswith(b"OPTIONS ") for raw in peer.signalling)
        await card.command("hangup")
        await asyncio.wait_for(peer.ended.wait(), 5)
        await until(lambda: resources(lab)["call_scoped_quiescent"])
        evidence["final_resources"] = resources(lab)
    finally:
        (tmp_path / "unknown-l24-media.json").write_text(
            json.dumps(evidence, indent=2, default=repr) + "\n"
        )
        for session in tuple(lab.calls.sessions.values()):
            barrier = lab.calls.request_termination(
                session.call_id, TerminationIntent("test_done")
            )
            if barrier is not None:
                await barrier
        await card.close()
        await peer.close()
        assert resources(lab)["call_scoped_quiescent"]
