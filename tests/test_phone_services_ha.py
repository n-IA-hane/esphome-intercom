"""Standard listeners share the native account and own their entire lifecycle."""

import asyncio
import imaplib

import pytest
from homeassistant.helpers import issue_registry as ir

from custom_components.voip_stack import phone_services
from custom_components.voip_stack.managed_phones import managed_records
from .test_esphome_provisioning_ha import phone_lab as phone_lab

pytestmark = pytest.mark.ha


async def test_standard_listeners_accept_same_native_account_and_close(phone_lab, tmp_path, socket_enabled):
    lab = phone_lab
    lab.hass.config.media_dirs = {"local": str(tmp_path)}
    lab.hass.config_entries.async_update_entry(lab.entry, data={
        "phone_services": {"listen_host": "127.0.0.1", "ldap_port": 0, "imap_port": 0},
    })
    await lab.controller.async_close()
    await phone_services.async_setup_phone_services(lab.hass, lab.entry)
    controller = lab.runtime.phone_provisioning
    try:
        await controller.async_sync()
        phone = managed_records(lab.entry)[lab.device.id]
        assert controller.directory_enabled and controller.imap_enabled
        assert await controller.authenticate(phone.username, phone.password.encode()) == phone.endpoint_id
        ldap_port = lab.runtime.ldap_server.port
        imap_port = lab.runtime.imap_server.port
        assert ldap_port and imap_port and ldap_port != imap_port

        def inbox():
            with imaplib.IMAP4("127.0.0.1", imap_port, timeout=3) as client:
                assert client.login(phone.username, phone.password)[0] == "OK"
                assert client.select("INBOX")[0] == "OK"
                assert client.uid("SEARCH", None, "ALL")[0] == "OK"
        await lab.hass.async_add_executor_job(inbox)
    finally:
        await phone_services.async_close_phone_services(lab.hass)
        archive = lab.runtime.sip.component("media_archive")
        if archive is not None:
            await archive.close()
    assert lab.runtime.imap_server is None and lab.runtime.ldap_server is None
    for port in (ldap_port, imap_port):
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", port)
    assert lab.runtime.sip.phase.value == "active"


async def test_invalid_tls_creates_repair_and_preserves_call_engine(phone_lab):
    lab = phone_lab
    lab.hass.config_entries.async_update_entry(lab.entry, data={"phone_services": {
        "tls": True, "certificate": "/invalid-test-certificate", "private_key": "/invalid-test-key",
    }})
    await lab.controller.async_close()
    await phone_services.async_setup_phone_services(lab.hass, lab.entry)
    try:
        assert not lab.runtime.phone_provisioning.directory_enabled
        assert not lab.runtime.phone_provisioning.imap_enabled
        assert lab.runtime.ldap_server is None and lab.runtime.imap_server is None
        assert lab.runtime.sip.phase.value == "active"
        assert ("voip_stack", "phone_service_tls") in ir.async_get(lab.hass).issues
    finally:
        await phone_services.async_close_phone_services(lab.hass)
