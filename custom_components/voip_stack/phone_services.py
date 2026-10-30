"""Entry-owned standard phone services, independent from host OS packages."""

from __future__ import annotations

import logging
import ssl

from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .directory_runtime import async_refresh_directory, directory_uri
from .esphome_provisioning import EspHomeProvisioner
from .ldap_server import LdapDirectoryServer
from .imap_server import ImapMailboxServer
from .mailbox import EVENT_VOICEMAIL_CHANGED, get_archive
from .phone_directory import PhoneDirectory
from .runtime_data import require_runtime_data

_LOGGER = logging.getLogger(__name__)
CONF_PHONE_SERVICES = "phone_services"


def settings(entry):
    values = entry.data.get(CONF_PHONE_SERVICES, {})
    return {
        "listen_host": str(values.get("listen_host") or "0.0.0.0"),
        "ldap_port": int(values.get("ldap_port", 1389)),
        "base_dn": str(values.get("base_dn") or "ou=phonebook,dc=voip"),
        "allow_anonymous": bool(values.get("allow_anonymous", False)),
        "imap_port": int(values.get("imap_port", 1143)),
        "tls": bool(values.get("tls", False)),
        "certificate": str(values.get("certificate") or ""),
        "private_key": str(values.get("private_key") or ""),
    }


def tls_context(config):
    if not config["tls"]:
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(config["certificate"], config["private_key"])
    return context


async def async_setup_phone_services(hass, entry):
    runtime = require_runtime_data(hass)
    config = settings(entry)
    runtime.directory = runtime.directory or PhoneDirectory()
    controller = EspHomeProvisioner(hass, entry, ldap_port=config["ldap_port"],
        base_dn=config["base_dn"], imap_port=config["imap_port"])
    runtime.phone_provisioning = controller
    controller.tls = config["tls"]
    controller.directory_enabled = False
    controller.imap_enabled = False
    await async_refresh_directory(hass)
    try:
        context = await hass.async_add_executor_job(tls_context, config)
    except (OSError, ValueError, ssl.SSLError):
        _LOGGER.error("Phone services TLS configuration could not be loaded")
        ir.async_create_issue(hass, DOMAIN, "phone_service_tls", is_fixable=False,
            severity=ir.IssueSeverity.ERROR, translation_key="phone_service_unavailable",
            translation_placeholders={"service": "TLS", "port": "LDAP / IMAP"})
        controller.start()
        return
    ir.async_delete_issue(hass, DOMAIN, "phone_service_tls")
    server = LdapDirectoryServer(runtime.directory, controller.authenticate,
        base_dn=config["base_dn"], allow_anonymous=config["allow_anonymous"],
        uri_for=lambda row: directory_uri(hass, row))
    try:
        await server.async_start(config["listen_host"], config["ldap_port"], ssl_context=context)
    except (OSError, ValueError):
        # A failed ancillary listener must not tear down an otherwise healthy
        # call engine; the repair gives its exact service and configured port.
        await server.async_close()
        _LOGGER.warning("LDAP phone directory could not start on port %s", config["ldap_port"])
        ir.async_create_issue(hass, DOMAIN, "phone_service_ldap", is_fixable=False,
            severity=ir.IssueSeverity.ERROR, translation_key="phone_service_unavailable",
            translation_placeholders={"service": "LDAP", "port": str(config["ldap_port"])})
    else:
        runtime.ldap_server = server
        controller.directory_enabled = True
        ir.async_delete_issue(hass, DOMAIN, "phone_service_ldap")
    def mailbox_changed(endpoint_id):
        hass.bus.async_fire(EVENT_VOICEMAIL_CHANGED, {"endpoint_id": endpoint_id})

    imap = ImapMailboxServer(lambda: get_archive(hass), controller.authenticate_verifier,
        changed_callback=mailbox_changed)
    try:
        await imap.async_start(config["listen_host"], config["imap_port"], ssl_context=context)
    except (OSError, ValueError):
        await imap.async_close()
        _LOGGER.warning("IMAP phone mailboxes could not start on port %s", config["imap_port"])
        ir.async_create_issue(hass, DOMAIN, "phone_service_imap", is_fixable=False,
            severity=ir.IssueSeverity.ERROR, translation_key="phone_service_unavailable",
            translation_placeholders={"service": "IMAP", "port": str(config["imap_port"])})
    else:
        runtime.imap_server = imap
        controller.imap_enabled = True
        ir.async_delete_issue(hass, DOMAIN, "phone_service_imap")
    controller.start()


async def async_close_phone_services(hass):
    runtime = require_runtime_data(hass)
    if runtime.phone_provisioning is not None:
        await runtime.phone_provisioning.async_close()
        runtime.phone_provisioning = None
    # Close protocol readers before SIP shutdown releases the audio archive.
    for name in ("imap_server", "ldap_server"):
        server = getattr(runtime, name)
        if server is not None:
            await server.async_close()
            setattr(runtime, name, None)
