"""Standard LDAP composition through public HA setup and private native RPC."""

from unittest.mock import AsyncMock

import pytest
from aioesphomeapi import UserService, UserServiceArg, UserServiceArgType

from .test_managed_phones_ha import (
    custom_integration_path as custom_integration_path,
    phone_lab as phone_lab,
    managed as managed,
    phone_record as phone_record,
)

pytestmark = pytest.mark.ha


def directory_actions(native):
    fields = [(name, UserServiceArgType.INT if name == "port" else UserServiceArgType.BOOL
               if name in {"tls", "enabled"} else UserServiceArgType.STRING)
              for name in ("server", "port", "base_dn", "bind_dn", "account_id", "tls", "enabled")]
    native.services = {**native.services,
        10: UserService(name="configure_directory", key=10,
                        args=[UserServiceArg(name=name, type=kind) for name, kind in fields]),
        11: UserService(name="refresh_ldap_directory", key=11, args=[])}


async def test_public_entry_setup_starts_ldap_and_privately_configures_native(phone_lab, monkeypatch, socket_enabled):
    from custom_components import voip_stack
    from custom_components.voip_stack import mailbox
    lab = phone_lab
    directory_actions(lab.native.runtime_data)
    lab.hass.config_entries.async_update_entry(lab.entry, data={**lab.entry.data,
        "phone_services": {"listen_host": "127.0.0.1", "ldap_port": 0}})
    for name in ("_async_setup_shared", "_async_apply_assist_intents", "_async_start_sip_trunk",
                 "_deferred_phonebook_sync"):
        monkeypatch.setattr(voip_stack, name, AsyncMock())
    monkeypatch.setattr(mailbox, "async_setup_mailboxes", AsyncMock())
    monkeypatch.setattr(lab.hass.config_entries, "async_forward_entry_setups", AsyncMock())

    async def sip(_hass):
        lab.entry.runtime_data.sip = lab.runtime.sip
        return True

    monkeypatch.setattr(voip_stack, "_async_start_sip_endpoint", sip)
    try:
        assert await voip_stack.async_setup_entry(lab.hass, lab.entry)
        await lab.hass.async_block_till_done(wait_background_tasks=True)
        server = getattr(lab.entry.runtime_data, "ldap_server", None)
        assert server is not None and server.port
        configure = [data for action, data in lab.sent if action == "configure_directory"]
        assert configure and configure[-1] == dict(server="127.0.0.1", port=server.port,
            base_dn="ou=phonebook,dc=voip", bind_dn="uid={username},ou=accounts,ou=phonebook,dc=voip",
            account_id="ha", tls=False, enabled=True)
        assert any(action == "refresh_ldap_directory" for action, _data in lab.sent)
        assert not lab.events
    finally:
        owner = getattr(lab.entry.runtime_data, "phone_services", None)
        if owner is not None:
            await owner.async_close()
