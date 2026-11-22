"""Native HA persistence for still-valid UDP registrar leases.

The registrar remains authoritative. This adapter restores only datagram
leases, never TCP connections, Digest nonces or capability-probe results.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import hmac
import logging
import time

from homeassistant.helpers.storage import Store

from .core import sip
from .core.sip_gruu import GruuTokens
from .sip_registrar import SipRegistration

_LOGGER = logging.getLogger(__name__)


class RegistrarLeaseStore:
    """Flush before signaling shutdown; coalesce ordinary Contact updates."""

    def __init__(self, hass, registrar, entry_id):
        self.registrar = registrar
        self.store = Store(hass, 1, f"voip_stack.registrar.{entry_id}", private=True)
        self._unsubscribe = None
        self._account_unsubscribe = None
        self._closed = False

    @staticmethod
    def _stamp(account):
        return hashlib.sha256((account.username + "\0" + account.password).encode()).hexdigest()

    def snapshot(self):
        rows = []
        now = time.time()
        for binding in self.registrar.registrations.values():
            account = self.registrar.accounts.get(binding.username.casefold())
            if (binding.transport.upper() != "UDP" or binding.expires_at <= now
                    or account is None or not account.enabled):
                continue
            row = asdict(binding)
            row.pop("capabilities", None)
            row.pop("capability_generation", None)
            rows.append({"credential_stamp": self._stamp(account), "binding": row})
        tokens = self.registrar._gruu_tokens
        return {"bindings": rows, "gruu_key": tokens.persistence_key.hex() if tokens is not None else ""}

    async def async_restore(self):
        data = await self.store.async_load()
        if isinstance(data, dict):
            key = data.get("gruu_key", "")
            if key:
                try:
                    self.registrar._gruu_tokens = GruuTokens(bytes.fromhex(key))
                except (TypeError, ValueError):
                    _LOGGER.warning("Stored SIP instance-routing key is invalid")
            restored = 0
            records = data.get("bindings", ())
            if not isinstance(records, (list, tuple)):
                _LOGGER.warning("Stored SIP leases are invalid")
                records = ()
            for record in records:
                try:
                    values = dict(record["binding"])
                    account = self.registrar.accounts.get(str(values["username"]).casefold())
                    if (account is None or not account.enabled
                            or not hmac.compare_digest(str(record.get("credential_stamp", "")), self._stamp(account))):
                        continue
                    binding = SipRegistration(**values)
                    sip.parse_sip_uri(binding.contact_uri)
                    if (binding.transport.upper() != "UDP" or binding.expires_at <= time.time()
                            or not 0 < binding.source_port <= 65535):
                        continue
                    identity = self.registrar._new_binding_key(self.registrar.registrations, account.username)
                    self.registrar.registrations[identity] = binding
                    restored += 1
                except (KeyError, TypeError, ValueError):
                    _LOGGER.warning("Ignoring an invalid stored SIP lease")
            if restored:
                _LOGGER.info("Restored %s unexpired SIP UDP registrations", restored)
        self._unsubscribe = self.registrar.subscribe_contact_changes(self._changed)
        self._account_unsubscribe = self.registrar.subscribe_account_changes(self._changed)

    def _changed(self, _usernames):
        if not self._closed:
            self.store.async_delay_save(self.snapshot, 1.0)

    async def async_close(self):
        if self._closed:
            return
        self._closed = True
        for remove in (self._unsubscribe, self._account_unsubscribe):
            if remove is not None:
                remove()
        self._unsubscribe = self._account_unsubscribe = None
        await self.store.async_save(self.snapshot())
