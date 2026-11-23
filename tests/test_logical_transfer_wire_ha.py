"""Public transfer uses the logical PBX URI while preserving duplex/failure tests."""

import pytest

from custom_components.voip_stack.core import sip
from .support.sip_observer import sip_messages as sip_messages
from .test_card_transfer_ha import test_browser_transfer_service_moves_peer_to_registered_phone as exercise_transfer
from .test_sip_phone_transfers_ha import registered_phones as registered_phones
from .test_trunk_calls_ha import trunks as trunks, lab as lab

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("trunks", [True], indirect=True)
@pytest.mark.parametrize("registered_phones", [{"manual": "transfer_a"}], indirect=True)
@pytest.mark.parametrize("reject", [False, True])
@pytest.mark.parametrize("incoming", [False, True])
@pytest.mark.parametrize("sensor_absent", [False, True])
async def test_public_transfer_addresses_logical_phone_and_preserves_failure_conversation(
    registered_phones, trunks, monkeypatch, tmp_path, reject, incoming, sensor_absent, sip_messages,
):
    if sensor_absent:
        from custom_components.voip_stack import softphone_originate
        from custom_components.voip_stack.phone_directory import PhoneDirectory

        trunks.runtime.directory = PhoneDirectory(trunks.roster)
        monkeypatch.setattr(softphone_originate, "phonebook_state", lambda _hass: None, raising=False)
    # The permanent public Dashboard scenario proves duplex media, ownership,
    # rejected-DND conversation survival, and complete teardown independently.
    await exercise_transfer(registered_phones, trunks, monkeypatch, tmp_path, reject, incoming)
    references = [message for message, _remote, _tcp, outgoing in sip_messages
                  if outgoing and message.method == "REFER"]
    assert references == [], "Local dashboard transfers remain owned by the PBX"
    invitations = [(message, remote) for message, remote, _tcp, outgoing in sip_messages
        if outgoing and message.method == "INVITE" and sip.parse_sip_uri(message.uri).user == "transfer_b"]
    if reject:
        assert not invitations
    else:
        assert invitations
        for message, remote in invitations:
            target = sip.parse_sip_uri(message.uri)
            assert sip.parse_sip_uri(message.header("To")).user == "transfer_b"
            assert target.host == "127.0.0.1" and target.port == remote[1]
            assert target.port != trunks.runtime.transport_config["sip_port"]
