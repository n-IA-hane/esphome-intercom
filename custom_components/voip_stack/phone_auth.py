"""Private LDAP authorization over the existing configured SIP account owner."""

from dataclasses import dataclass, field
import hmac

from .phone_endpoint import EndpointAvailability, EndpointKind
from .runtime_data import runtime_data, sip_registrar


@dataclass(frozen=True, slots=True)
class PhonePrincipal:
    username: str
    endpoint_id: str
    generation: str = field(repr=False)


class PhoneAccountAuthenticator:
    """No credential cache: the registrar owns account generations and secrets."""

    def __init__(self, hass, entry):
        self.hass, self.entry = hass, entry
        self.runtime = runtime_data(hass)
        self.registrar = sip_registrar(hass)

    def _current(self):
        return (self.runtime is not None and runtime_data(self.hass) is self.runtime
                and self.registrar is not None and sip_registrar(self.hass) is self.registrar)

    def principal(self, username):
        if not self._current():
            return None
        account = self.registrar.accounts.get(str(username).casefold())
        if account is None or not account.enabled:
            return None
        endpoint = self.runtime.endpoints.by_username(account.username)
        if (endpoint is None or endpoint.availability is EndpointAvailability.UNAVAILABLE
                or endpoint.kind not in {EndpointKind.ESPHOME, EndpointKind.SIP_ACCOUNT}):
            return None
        from .store import sip_accounts
        configured = next((item for item in sip_accounts(self.hass)
                           if item.username.casefold() == account.username.casefold() and item.enabled), None)
        if configured is None or not hmac.compare_digest(configured.password.encode(), account.password.encode()):
            return None
        if endpoint.kind is EndpointKind.ESPHOME:
            from .managed_phones import managed_records
            phone = managed_records(self.entry).get(endpoint.device_id)
            controller = self.runtime.phone_provisioning
            if (phone is None or phone.endpoint_id != endpoint.endpoint_id or not phone.active
                    or controller is None or controller.is_removed(phone.origin_entry_id, phone.device_id)):
                return None
        generation = self.registrar.authentication_generation(account.username)
        return PhonePrincipal(account.username.casefold(), endpoint.endpoint_id, generation) if generation else None

    def valid(self, principal):
        return isinstance(principal, PhonePrincipal) and self.principal(principal.username) == principal

    async def authenticate(self, username, password):
        principal = self.principal(username)
        account = self.registrar.accounts.get(str(username).casefold()) if self._current() else None
        return principal if (principal is not None and account is not None
                             and hmac.compare_digest(account.password.encode(), password)) else None
