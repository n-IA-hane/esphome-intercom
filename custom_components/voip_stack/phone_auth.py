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
        # The registrar owns the current account generation. Resolve only this
        # subentry instead of reconstructing every account for each LDAP frame.
        configured = self.entry.subentries.get(self.runtime.endpoints.subentry_ids.get(endpoint.endpoint_id))
        if configured is None or (
                not configured.data.get("enabled", True)
                or configured.data.get("username") != account.username
                or not hmac.compare_digest(str(configured.data.get("password", "")).encode(), account.password.encode())):
            return None
        generation = self.registrar.authentication_generation(account.username)
        return PhonePrincipal(account.username, endpoint.endpoint_id, generation) if generation else None

    def valid(self, principal):
        return isinstance(principal, PhonePrincipal) and self.principal(principal.username) == principal

    async def authenticate(self, username, password):
        principal = self.principal(username)
        account = self.registrar.accounts.get(str(username).casefold()) if self._current() else None
        return principal if (principal is not None and account is not None
                             and hmac.compare_digest(account.password.encode(), password)) else None
