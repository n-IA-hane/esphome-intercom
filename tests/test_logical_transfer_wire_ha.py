"""Public transfer uses the logical PBX URI while preserving duplex/failure tests."""

import pytest

from custom_components.voip_stack.core import sip
from custom_components.voip_stack import sip_client, sip_listener
from .test_card_transfer_ha import test_browser_transfer_service_moves_peer_to_registered_phone as exercise_transfer
from .test_sip_phone_transfers_ha import registered_phones as registered_phones
from .test_trunk_calls_ha import trunks as trunks, lab as lab

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_a"}], indirect=True)
@pytest.mark.parametrize("reject", [False, True])
@pytest.mark.parametrize("incoming", [False, True])
async def test_public_transfer_addresses_logical_phone_and_preserves_failure_conversation(
    registered_phones, trunks, monkeypatch, tmp_path, reject, incoming,
):
    references = []
    native_send = sip_client.send_sip_datagram

    def observe(transport, raw, address):
        message = sip.parse_message(raw)
        if message.method == "REFER":
            references.append((message, address))
        return native_send(transport, raw, address)

    monkeypatch.setattr(sip_client, "send_sip_datagram", observe)
    monkeypatch.setattr(sip_listener, "send_sip_datagram", observe)
    # The permanent public Dashboard scenario proves duplex media, ownership,
    # rejected-DND conversation survival, and complete teardown independently.
    await exercise_transfer(registered_phones, trunks, monkeypatch, tmp_path, reject, incoming)
    if not reject:
        assert references
    for message, _address in references:
        target = sip.parse_sip_uri(message.header("Refer-To"))
        assert target.user == "transfer_b"
        assert target.host == "127.0.0.1"
        assert target.port == trunks.runtime.transport_config["sip_port"]
