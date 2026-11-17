"""A real registered baresip telephone uses the common voicemail IVR and MWI."""

import asyncio
from pathlib import Path
import shutil
import struct
import wave

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import callback

from custom_components.voip_stack.core.audio_format import AudioFormat
from custom_components.voip_stack.mailbox import get_archive
from custom_components.voip_stack.phone_endpoint import PhoneEndpoint, EndpointKind
from custom_components.voip_stack.roster import RosterEntry
from custom_components.voip_stack.sip_registrar import SipAccount
from custom_components.voip_stack.store import config_entry
from .test_trunk_calls_ha import trunks as trunks, lab as lab

pytestmark = [pytest.mark.ha, pytest.mark.skipif(shutil.which("baresip") is None, reason="baresip is not installed")]


@pytest.mark.parametrize("trunks", [True], indirect=True)
async def test_baresip_registers_gets_mwi_plays_and_deletes_message(trunks, tmp_path, monkeypatch):
    from homeassistant.components import tts
    from tools.ha_softphone_matrix import BareSip

    lab = trunks
    from custom_components.voip_stack.trunk_runtime import async_stop_sip_trunk
    await async_stop_sip_trunk(lab.hass)
    lab.runtime.trunk_configs = {}
    lab.runtime.trunk_config = {}
    lab.runtime.default_trunk_id = ""
    lab.hass.config.config_dir = str(tmp_path / "ha")
    lab.hass.config.media_dirs = {"local": str(tmp_path / "media")}
    entry = config_entry(lab.hass)
    lab.hass.config_entries.async_update_entry(entry, data={"voicemail": {
        "enabled": True, "tts_entity_id": "tts.test", "language": "en",
    }})
    lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(
        subentry_type="phone", title="Baresip", unique_id="phone:baresip",
        data={"endpoint_id": "baresip", "name": "Baresip", "kind": "sip_account",
              "username": "baresip", "extension": "101", "voicemail": {"enabled": True}},
    ))
    lab.runtime.endpoints.register(PhoneEndpoint(endpoint_id="baresip", name="Baresip", username="baresip",
                                               extension="101", kind=EndpointKind.SIP_ACCOUNT))
    lab.roster.append(RosterEntry("baresip", address="127.0.0.2", metadata={"endpoint_id": "baresip"}))
    registrar = lab.calls.component("registrar")
    registrar.enabled = True
    registrar.update_accounts([SipAccount("baresip", "Baresip", "voicemail-lab-only", extension="101")])
    class Speech:
        def async_set_message(self, _text):
            pass
        async def async_stream_result(self):
            yield bytes(640)
    monkeypatch.setattr(tts, "async_create_stream", lambda *_args: Speech())
    from custom_components.voip_stack import voicemail_config
    from types import SimpleNamespace
    monkeypatch.setattr(voicemail_config, "voice_engine", lambda *_: SimpleNamespace(available=True))
    archive = await get_archive(lab.hass)
    writer = await archive.begin("baresip", caller="Lab delivery")
    for _ in range(25):
        assert writer.offer(struct.pack("<3200h", *([2200] * 3200)), AudioFormat(16000, frame_ms=20))
    message = await writer.finish()
    config = tmp_path / "baresip"
    config.mkdir()
    wav = tmp_path / "received.wav"
    (config / "config").write_text(f"""net_interface 127.0.0.1
sip_listen 127.0.0.1:0
sip_trans_def udp
call_max_calls 1
audio_player aubridge,nil
snd_path {tmp_path}
audio_source ausine,550
audio_alert aubridge,nil
module_path /usr/lib/baresip/modules
module stdio.so
module menu.so
module g711.so
module auconv.so
module auresamp.so
module ausine.so
module sndfile.so
module aubridge.so
module_app account.so
module_app mwi.so
""")
    account = (
        f'<sip:baresip@127.0.0.1:{lab.runtime.transport_config["sip_port"]}>'
        ';auth_user=baresip;auth_pass=voicemail-lab-only;regint=60;mwi=yes;dtmfmode=rtpevent'
    )
    (config / "accounts").write_text("")
    (config / "accounts").chmod(0o600)
    peer = None
    deleted = asyncio.Event()
    @callback
    def operation(event):
        if event.data.get("message_id") == message["id"]:
            deleted.set()
    unsubscribe = lab.hass.bus.async_listen("voip_stack_voicemail_operation", operation)
    try:
        # Register after the peer's modules are ready. The pinned mwi module's
        # startup timer appends subscriptions to the list it is iterating if
        # REGISTER_OK arrives first. Keep that peer initialization race out of
        # the protocol test without changing its SIP, MWI or IVR exchanges.
        peer = await asyncio.to_thread(BareSip, config, wait_for_registration=False)
        await asyncio.to_thread(peer.command, "/uanew " + account)
        await asyncio.to_thread(peer.wait_for, "registered successfully", 5)
        await asyncio.to_thread(peer.wait_for, "Voice-Message: 1/0", 5)
        assert peer.output.count("mwi: subscribing to messages") == 1
        await asyncio.to_thread(peer.dial, "*97")
        await asyncio.to_thread(peer.digits, "01#2", 0.3)
        await asyncio.wait_for(deleted.wait(), 5)
        await asyncio.to_thread(peer.wait_for, "Voice-Message: 0/0", 5)
        assert not archive.list("baresip")
        session = next(iter(lab.calls.sessions.values()))
        await asyncio.to_thread(peer.hangup)
        await asyncio.wait_for(session.termination_started.wait(), 3)
        barrier = lab.calls.request_termination(session.call_id, session.termination_intent)
        if barrier is not None:
            await barrier
        await asyncio.to_thread(peer.close)
        assert not lab.calls.sessions
        decoded_files = list(tmp_path.glob("*-dec.wav"))
        assert len(decoded_files) == 1
        wav = decoded_files[0]
        with wave.open(str(wav), "rb") as audio:
            samples = struct.unpack(f"<{audio.getnframes() * audio.getnchannels()}h", audio.readframes(audio.getnframes()))
            assert any(2000 <= abs(sample) <= 2400 for sample in samples)
        evidence = Path(__file__).parents[1] / "test_captures/ha-pbx/baresip-voicemail"
        evidence.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(wav, evidence / "received.wav")
        (evidence / "baresip.log").write_text(peer.output)
    finally:
        unsubscribe()
        if peer is not None:
            evidence = Path(__file__).parents[1] / "test_captures/ha-pbx/baresip-voicemail"
            evidence.mkdir(parents=True, exist_ok=True)
            (evidence / "baresip.log").write_text(peer.output)
        if peer is not None and peer.proc.poll() is None:
            await asyncio.to_thread(peer.close)
