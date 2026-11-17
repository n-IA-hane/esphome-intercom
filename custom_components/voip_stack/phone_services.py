"""Entry-owned standard LDAP directory, independent of host OS services."""

import asyncio
import ssl
import logging
import uuid

from ldap3.core.exceptions import LDAPInvalidDnError
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir

from .const import DOMAIN
from .directory_runtime import async_refresh_directory, directory_uri
from .endpoint_lifecycle import create_runtime_task
from .ldap_server import LdapDirectoryServer
from .ldap_server import _dn_key
from .phone_auth import PhoneAccountAuthenticator
from .runtime_data import runtime_data, sip_registrar

_LOGGER = logging.getLogger(__name__)
CONF_PHONE_SERVICES = "phone_services"


def settings(entry):
    values = entry.data.get(CONF_PHONE_SERVICES, {}) if entry is not None else {}
    return dict(enabled=bool(values.get("enabled", True)),
        listen_host=str(values.get("listen_host") or "0.0.0.0"),
        ldap_port=int(values.get("ldap_port", 1389)),
        base_dn=str(values.get("base_dn") or "ou=phonebook,dc=voip"),
        allow_anonymous=bool(values.get("allow_anonymous", False)),
        tls=bool(values.get("tls", False)), certificate=str(values.get("certificate") or ""),
        private_key=str(values.get("private_key") or ""))


def tls_context(config):
    if not (config["tls"] or config["certificate"] or config["private_key"]):
        return None
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(config["certificate"], config["private_key"])
    return context


def validate_settings(config):
    """Validate the same public configuration at the form and runtime boundary."""
    if not 0 <= config["ldap_port"] <= 65535 or not config["listen_host"].strip():
        raise ValueError("phone_service_configuration")
    try:
        if not _dn_key(config["base_dn"]):
            raise ValueError
    except (ValueError, LDAPInvalidDnError):
        raise ValueError("phone_service_base_dn_invalid") from None
    if ((config["tls"] and not (config["certificate"] and config["private_key"]))
            or bool(config["certificate"]) != bool(config["private_key"])):
        raise ValueError("phone_service_certificate_required")


class PhoneServices:
    """One listener coordinator sharing the existing directory and account owners."""

    def __init__(self, hass, entry):
        from .esphome_provisioning import EspHomeProvisioner

        self.hass, self.entry, self.runtime = hass, entry, entry.runtime_data
        if self.runtime.phone_provisioning is None or self.runtime.phone_provisioning._closed:
            self.runtime.phone_provisioning = EspHomeProvisioner(hass, entry)
        self.controller = self.runtime.phone_provisioning
        self.auth = PhoneAccountAuthenticator(hass, entry)
        self._lock = asyncio.Lock()
        self._revision = 0
        self._closed = False
        self._config = None
        self._failed = False
        self._directory_unsubscribe = None
        self._account_unsubscribe = None

    def _owned(self, revision=None):
        return (not self._closed and runtime_data(self.hass) is self.runtime
                and self.runtime.phone_services is self
                and (revision is None or revision == self._revision))

    def _issue(self, name, port):
        ir.async_create_issue(self.hass, DOMAIN, "phone_service_" + name, is_fixable=False,
            is_persistent=False, severity=ir.IssueSeverity.ERROR,
            translation_key="phone_service_unavailable",
            translation_placeholders={"service": name.upper(), "port": str(port)})

    @callback
    def _directory_changed(self):
        if self._owned() and self.runtime.ldap_server is not None:
            self.controller.schedule()

    @callback
    def _account_changed(self, usernames):
        server = self.runtime.ldap_server
        if self._owned() and server is not None:
            for username in usernames:
                create_runtime_task(self.hass, server.async_revoke_endpoint("", username=username))

    async def async_reconcile(self, *, force=False):
        try:
            config = settings(self.entry)
            validate_settings(config)
        except (TypeError, ValueError):
            self._revision += 1
            revision = self._revision
            async with self._lock:
                if self._owned(revision):
                    self._failed = True
                    await self._close_listener()
                    if self._owned(revision):
                        self._issue("ldap", "configuration")
                        self.controller.configure_directory_service(None)
            return
        if not force and not self._failed and config == self._config and self.auth.registrar is sip_registrar(self.hass):
            return
        self._revision += 1
        revision = self._revision
        async with self._lock:
            if not self._owned(revision):
                return
            self._config = config
            self._failed = True
            if self._account_unsubscribe is not None:
                self._account_unsubscribe()
            self.auth = PhoneAccountAuthenticator(self.hass, self.entry)
            if self.auth.registrar is not None:
                self._account_unsubscribe = self.auth.registrar.subscribe_account_changes(self._account_changed)
            if not config["enabled"]:
                await self._close_listener()
                if self._owned(revision):
                    self.controller.configure_directory_service({**config, "ldap_port": config["ldap_port"] or 1389})
                    for name in ("tls", "ldap"):
                        ir.async_delete_issue(self.hass, DOMAIN, "phone_service_" + name)
                    self.controller.start()
                    self._failed = False
                return
            if await async_refresh_directory(self.hass) is None or not self._owned(revision):
                return
            if self._directory_unsubscribe is None:
                self._directory_unsubscribe = self.runtime.directory.subscribe(self._directory_changed)
            try:
                context = await self.hass.async_add_executor_job(tls_context, config)
            except (OSError, ValueError, ssl.SSLError):
                if self._owned(revision):
                    # A requested TLS configuration must not leave the previous
                    # plaintext listener serving. Native keeps its last snapshot.
                    await self._close_listener()
                if self._owned(revision):
                    self._issue("tls", config["ldap_port"])
                    self.controller.configure_directory_service(None)
                    self.controller.start()
                return
            if not self._owned(revision):
                return
            ir.async_delete_issue(self.hass, DOMAIN, "phone_service_tls")
            try:
                server = LdapDirectoryServer(self.runtime.directory, self.auth.authenticate,
                    base_dn=config["base_dn"], allow_anonymous=config["allow_anonymous"],
                    authorize_callback=self.auth.valid,
                    owner_callback=self._owned,
                    uri_for=lambda row: directory_uri(self.hass, row))
                await self._close_listener()
                if not self._owned(revision):
                    return
                await server.async_start(config["listen_host"], config["ldap_port"],
                                         ssl_context=context if config["tls"] else None,
                                         starttls_context=None if config["tls"] else context)
            except (OSError, ValueError):
                if "server" in locals():
                    await server.async_close()
                if self._owned(revision):
                    self._issue("ldap", config["ldap_port"])
                    self.controller.configure_directory_service(None)
                    self.controller.start()
                return
            if not self._owned(revision):
                await server.async_close()
                return
            self.runtime.ldap_server = server
            ir.async_delete_issue(self.hass, DOMAIN, "phone_service_ldap")
            self.controller.configure_directory_service({**config, "ldap_port": server.port}, epoch=uuid.uuid4().hex)
            self.controller.start()
            self._failed = False

    async def _close_listener(self):
        server, self.runtime.ldap_server = self.runtime.ldap_server, None
        if server is not None:
            await server.async_close()

    async def async_close(self):
        self._closed = True
        self._revision += 1
        for unsubscribe in (self._directory_unsubscribe, self._account_unsubscribe):
            if unsubscribe is not None:
                unsubscribe()
        self._directory_unsubscribe = self._account_unsubscribe = None
        async with self._lock:
            await self._close_listener()


async def async_setup_phone_services(hass, entry):
    runtime = entry.runtime_data
    if runtime.phone_services is None:
        runtime.phone_services = PhoneServices(hass, entry)
    await runtime.phone_services.async_reconcile(force=True)


async def async_reconcile_phone_services(hass, entry):
    if entry.runtime_data.phone_services is not None:
        await entry.runtime_data.phone_services.async_reconcile()


async def async_close_phone_services(hass):
    runtime = runtime_data(hass)
    if runtime is not None and runtime.phone_services is not None:
        await runtime.phone_services.async_close()
        runtime.phone_services = None
