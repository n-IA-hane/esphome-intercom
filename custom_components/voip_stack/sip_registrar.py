"""Local SIP registrar for standard SIP endpoints registered to Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import StrEnum
import hmac
import logging
import math
from secrets import token_hex, token_urlsafe
import time
import uuid
from collections.abc import Callable
from typing import Any

from .core import sip
from .core.codec_capabilities import supports_dahua_pcm
from .core.sip_auth import parse_digest_challenge, verify_digest_authorization
from .roster import RosterEntry


_LOGGER = logging.getLogger(__name__)
REALM = "voip_stack"
NONCE_TTL = 600.0
MAX_ACTIVE_NONCES = 256
MAX_NONCE_USE_RECORDS = 2048


@dataclass(frozen=True, slots=True)
class SipAccount:
    username: str
    display_name: str
    password: str
    enabled: bool = True
    extension: str = ""
    conference_group: str = ""
    conference_ring: bool = False
    ring_group: str = ""

    @property
    def roster_name(self) -> str:
        return self.display_name or self.username


@dataclass(slots=True)
class SipRegistration:
    username: str
    contact_uri: str
    source_host: str
    source_port: int
    transport: str
    expires_at: float
    advertised_contact_uri: str = ""
    user_agent: str = ""
    call_id: str = ""
    cseq: int = 0
    q: float = 1.0
    instance_id: str = ""
    reg_id: int = 0
    outbound: bool = False

    def snapshot(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "contact_uri": self.contact_uri,
            "advertised_contact_uri": self.advertised_contact_uri,
            "source_host": self.source_host,
            "source_port": self.source_port,
            "transport": self.transport.lower(),
            "expires_at": self.expires_at,
            "user_agent": self.user_agent,
            "call_id": self.call_id,
            "cseq": self.cseq,
            "q": self.q,
            "outbound": self.outbound,
            "reg_id": self.reg_id,
        }


@dataclass(frozen=True, slots=True)
class _PreparedContact:
    advertised_uri: str
    expires: int
    contact_uri: str
    q: float
    instance_id: str = ""
    reg_id: int = 0
    outbound: bool = False


@dataclass(frozen=True, slots=True)
class SipRegisterResult:
    status: int
    reason: str
    headers: tuple[tuple[str, str], ...] = ()


class _AuthorizationStatus(StrEnum):
    VALID = "valid"
    INVALID = "invalid"
    STALE = "stale"


def generate_password() -> str:
    return token_urlsafe(18)


def normalize_username(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError("username is required")
    if not all(ch.isalnum() or ch in {"_", "-", "."} for ch in cleaned):
        raise ValueError("username must contain only letters, numbers, _, - or .")
    return cleaned


def account_from_mapping(raw: dict[str, Any]) -> SipAccount:
    username = normalize_username(str(raw.get("username") or raw.get("id") or ""))
    return SipAccount(
        username=username,
        display_name=str(raw.get("display_name") or raw.get("name") or username).strip(),
        password=str(raw.get("password") or ""),
        enabled=bool(raw.get("enabled", True)),
        extension=str(raw.get("extension") or "").strip(),
        conference_group=str(raw.get("conference_group") or "").strip(),
        conference_ring=bool(raw.get("conference_ring", False)),
        ring_group=str(raw.get("ring_group") or "").strip(),
    )


def dump_account(account: SipAccount) -> dict[str, Any]:
    return asdict(account)


def _extract_uri(header: str) -> str:
    value = (header or "").strip()
    if "<" in value and ">" in value:
        value = value[value.index("<") + 1:value.index(">")]
    return value.strip()


def _extract_register_username(request: sip.SipMessage) -> str:
    """Return the address-of-record user from the mandatory To header."""

    uri = sip.parse_sip_uri(_extract_uri(request.header("To")))
    return normalize_username(uri.user)


def _header_param(header: str, name: str) -> str:
    wanted = name.lower()
    for part in (header or "").split(";")[1:]:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip().lower() == wanted:
            return value.strip()
    return ""


def _parse_expires(raw: str, default: int = 3600) -> int:
    try:
        return max(0, min(int(raw), 86400))
    except (TypeError, ValueError):
        return default


def _register_contacts(request: sip.SipMessage) -> list[tuple[str, int, str]]:
    default_expires = _parse_expires(request.header("Expires") or "3600")
    contacts: list[tuple[str, int, str]] = []
    for raw in request.header_values("Contact"):
        header = raw.strip()
        if not header:
            continue
        if header == "*":
            contacts.append(("*", 0, header))
            continue
        uri = _extract_uri(header)
        if not uri:
            continue
        try:
            uri = str(sip.parse_sip_uri(uri))
        except (TypeError, ValueError, sip.SipError):
            continue
        contact_expires = _header_param(header, "expires")
        expires = _parse_expires(contact_expires, default_expires) if contact_expires else default_expires
        contacts.append((uri, expires, header))
    return contacts


def _same_contact(left: str, right: str) -> bool:
    return _extract_uri(left).strip().lower() == _extract_uri(right).strip().lower()


def _register_cseq(request: sip.SipMessage) -> int:
    parts = request.header("CSeq").split()
    if len(parts) != 2 or parts[1].upper() != "REGISTER":
        raise ValueError("invalid REGISTER CSeq")
    value = int(parts[0])
    if value < 0:
        raise ValueError("invalid REGISTER CSeq")
    return value


def _contact_q(raw_contact: str) -> float:
    raw = _header_param(raw_contact, "q")
    if not raw:
        return 1.0
    value = float(raw)
    if not 0.0 <= value <= 1.0:
        raise ValueError("invalid Contact q value")
    return value


def _outbound_contact(
    request: sip.SipMessage,
    raw_contact: str,
) -> tuple[str, int, bool]:
    instance_id = _header_param(raw_contact, "+sip.instance").strip('"<>')
    raw_reg_id = _header_param(raw_contact, "reg-id")
    supports_outbound = "outbound" in sip.option_tags(request, "Supported")
    if not supports_outbound:
        # +sip.instance is also used independently by GRUU-aware user agents.
        # It becomes an RFC 5626 flow identity only when the UA explicitly
        # negotiates outbound and supplies the paired reg-id.
        return "", 0, False
    if not instance_id or not raw_reg_id:
        raise ValueError("incomplete RFC 5626 Contact")
    reg_id = int(raw_reg_id)
    if not 1 <= reg_id <= 2_147_483_647:
        raise ValueError("invalid RFC 5626 reg-id")
    if not instance_id.lower().startswith("urn:uuid:"):
        raise ValueError("invalid RFC 5626 instance ID")
    instance_id = f"urn:uuid:{uuid.UUID(instance_id[9:])}"
    return instance_id, reg_id, True


def _registration_matches_contact(
    registration: SipRegistration,
    contact: _PreparedContact,
) -> bool:
    if contact.outbound:
        return (
            registration.outbound
            and registration.instance_id == contact.instance_id
            and registration.reg_id == contact.reg_id
        )
    return _same_contact(
        contact.advertised_uri,
        registration.advertised_contact_uri or registration.contact_uri,
    )


def _contact_for_source_flow(
    contact_uri: str,
    addr: tuple[str, int],
    transport: str,
) -> str:
    """Pin a REGISTER binding to the authenticated signaling flow.

    The Contact user and non-transport URI parameters remain intact, while an
    endpoint cannot turn its authenticated account into an arbitrary network
    target.  This is also the NAT-friendly behavior expected for a registrar
    that receives REGISTER directly rather than through a trusted edge proxy.
    """

    advertised = sip.parse_sip_uri(contact_uri)
    source_host = str(addr[0] or "").strip()
    if ":" in source_host and not source_host.startswith("["):
        source_host = f"[{source_host}]"
    source_port = int(addr[1])
    if not source_host or not 1 <= source_port <= 65535:
        raise sip.SipError("REGISTER source flow is invalid")
    actual_transport = str(transport or "UDP").strip().lower()
    had_transport = any(
        key.lower() == "transport" for key, _value in advertised.params
    )
    params = tuple(
        (key, value)
        for key, value in advertised.params
        if key.lower() != "transport"
    )
    if had_transport or actual_transport != "udp":
        params += (("transport", actual_transport),)
    return str(
        sip.SipUri(
            user=advertised.user,
            host=source_host,
            port=source_port,
            params=params,
        )
    )


class SipRegistrar:
    def __init__(
        self,
        *,
        enabled: bool,
        accounts: list[SipAccount],
        local_ip: str,
        local_sip_port: int,
        on_registration_change: Callable[[str, bool], None] | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.local_ip = local_ip
        self.local_sip_port = int(local_sip_port)
        self.accounts = {account.username.lower(): account for account in accounts}
        self.registrations: dict[str, SipRegistration] = {}
        self.nonces: dict[str, float] = {}
        self.nonce_uses: dict[
            tuple[str, str, str],
            tuple[int, tuple[Any, ...]],
        ] = {}
        self.source_nonces: dict[str, str] = {}
        self.last_sip_event = ""
        self.last_sip_status_code = 0
        self.last_sip_reason = ""
        self.on_registration_change = on_registration_change

    def _notify_registration_change(self, username: str, registered: bool) -> None:
        if self.on_registration_change is None:
            return
        try:
            self.on_registration_change(str(username), bool(registered))
        except Exception:  # pragma: no cover - runtime observer isolation
            _LOGGER.exception(
                "SIP registrar registration observer failed user=%s registered=%s",
                username,
                registered,
            )

    def update_accounts(self, accounts: list[SipAccount]) -> None:
        previous_accounts = self.accounts
        self.accounts = {account.username.lower(): account for account in accounts}
        retained: dict[str, list[SipRegistration]] = {}
        retained_usernames: set[str] = set()
        for registration in self.registrations.values():
            key = registration.username.lower()
            account = self.accounts.get(key)
            previous = previous_accounts.get(key)
            if (
                account is None
                or not account.enabled
                or previous is None
                or not hmac.compare_digest(previous.password, account.password)
            ):
                continue
            registration.username = account.username
            retained.setdefault(account.username.lower(), []).append(registration)
            retained_usernames.add(account.username.lower())
        removed = {
            registration.username
            for registration in self.registrations.values()
            if registration.username.lower() not in retained_usernames
        }
        registrations: dict[str, SipRegistration] = {}
        for wanted, bindings in retained.items():
            account = self.accounts[wanted]
            for index, registration in enumerate(bindings):
                key = account.username if index == 0 else f"{account.username}#{index + 1}"
                registrations[key] = registration
        self.registrations = registrations
        for username in removed:
            self._notify_registration_change(username, False)

    def _registration(self, username: str) -> SipRegistration | None:
        registrations = self._registrations(username)
        return registrations[0] if registrations else None

    def _registrations(self, username: str) -> list[SipRegistration]:
        wanted = str(username or "").lower()
        return [
            registration
            for key, registration in self.registrations.items()
            if key.lower() == wanted or registration.username.lower() == wanted
        ]

    def registered_contacts(self, username: str) -> tuple[SipRegistration, ...]:
        """Return every live Contact for one logical SIP account."""

        self.expire()
        return tuple(
            sorted(
                self._registrations(username),
                key=lambda registration: (-registration.q, registration.contact_uri),
            )
        )

    @staticmethod
    def _new_binding_key(
        registrations: dict[str, SipRegistration],
        username: str,
    ) -> str:
        if username not in registrations:
            return username
        index = 2
        while f"{username}#{index}" in registrations:
            index += 1
        return f"{username}#{index}"

    def remove_registration(self, username: str) -> None:
        wanted = str(username or "").lower()
        removed_username = ""
        for key in list(self.registrations):
            registration = self.registrations[key]
            if key.lower() == wanted or registration.username.lower() == wanted:
                self.registrations.pop(key, None)
                removed_username = registration.username
        if removed_username:
            self._notify_registration_change(removed_username, False)

    def registration_matches_source(
        self,
        username: str,
        host: str,
        port: int,
        transport: str,
    ) -> bool:
        """Authenticate an in-dialog origin against its live REGISTER flow."""

        self.expire()
        return any(
            registration.source_host == str(host or "")
            and int(registration.source_port) == int(port)
            and registration.transport.upper() == str(transport or "").upper()
            for registration in self._registrations(username)
        )

    def _prune_nonces(self) -> None:
        now = time.time()
        self.nonces = {key: exp for key, exp in self.nonces.items() if exp > now}
        active = set(self.nonces)
        self.nonce_uses = {
            key: value for key, value in self.nonce_uses.items() if key[0] in active
        }
        self.source_nonces = {
            source: nonce
            for source, nonce in self.source_nonces.items()
            if nonce in active
        }

    def _challenge(
        self,
        source: str = "",
        *,
        force_new: bool = False,
        stale: bool = False,
    ) -> tuple[str, str]:
        self._prune_nonces()
        cached_nonce = self.source_nonces.get(source) if source else None
        if not force_new and cached_nonce and cached_nonce in self.nonces:
            return cached_nonce, (
                f'Digest realm="{REALM}", nonce="{cached_nonce}", '
                f'algorithm=MD5, qop="auth"'
                f'{", stale=true" if stale else ""}'
            )
        while len(self.nonces) >= MAX_ACTIVE_NONCES:
            expired_nonce = next(iter(self.nonces))
            self.nonces.pop(expired_nonce)
            self.nonce_uses = {
                key: value
                for key, value in self.nonce_uses.items()
                if key[0] != expired_nonce
            }
            self.source_nonces = {
                key: value
                for key, value in self.source_nonces.items()
                if value != expired_nonce
            }
        nonce = token_hex(16)
        self.nonces[nonce] = time.time() + NONCE_TTL
        if source:
            self.source_nonces[source] = nonce
        return nonce, (
            f'Digest realm="{REALM}", nonce="{nonce}", algorithm=MD5, qop="auth"'
            f'{", stale=true" if stale else ""}'
        )

    def _valid_nonce(self, nonce: str) -> bool:
        self._prune_nonces()
        return bool(nonce and nonce in self.nonces)

    @staticmethod
    def _modern_challenges(challenge: str) -> tuple[str, ...]:
        """Offer RFC 8760 SHA-256 while retaining legacy MD5 interoperability."""

        return (challenge.replace("algorithm=MD5", "algorithm=SHA-256"), challenge)

    @staticmethod
    def _register_fingerprint(
        request: sip.SipMessage,
        addr: tuple[str, int],
        transport: str,
    ) -> tuple[Any, ...]:
        return (
            request.method,
            request.uri,
            request.header("Call-ID"),
            request.header("CSeq"),
            request.header_values("Via")[:1],
            request.header_values("Contact"),
            request.header("Expires"),
            str(addr[0]),
            int(addr[1]),
            str(transport or "").upper(),
        )

    def _check_authorization(
        self,
        request: sip.SipMessage,
        account: SipAccount,
        addr: tuple[str, int],
        transport: str,
    ) -> _AuthorizationStatus:
        params = parse_digest_challenge(request.header("Authorization"))
        nonce = params.get("nonce", "")
        if not self._valid_nonce(nonce):
            return (
                _AuthorizationStatus.STALE
                if nonce
                else _AuthorizationStatus.INVALID
            )
        try:
            _algorithm, cnonce, nonce_count = verify_digest_authorization(
                authorization_header=request.header("Authorization"),
                username=account.username,
                password=account.password,
                method="REGISTER",
                uri=request.uri,
                realm=REALM,
                nonce=nonce,
                body=request.body,
            )
        except ValueError:
            return _AuthorizationStatus.INVALID
        use_key = (nonce, account.username.lower(), cnonce)
        fingerprint = self._register_fingerprint(request, addr, transport)
        previous = self.nonce_uses.get(use_key)
        if previous is not None:
            previous_count, previous_fingerprint = previous
            if nonce_count < previous_count:
                return _AuthorizationStatus.STALE
            if nonce_count == previous_count:
                return (
                    _AuthorizationStatus.VALID
                    if hmac.compare_digest(
                        repr(fingerprint),
                        repr(previous_fingerprint),
                    )
                    else _AuthorizationStatus.STALE
                )
        if use_key not in self.nonce_uses:
            while len(self.nonce_uses) >= MAX_NONCE_USE_RECORDS:
                self.nonce_uses.pop(next(iter(self.nonce_uses)))
        self.nonce_uses[use_key] = (nonce_count, fingerprint)
        return _AuthorizationStatus.VALID

    def account_for_source(
        self,
        addr: tuple[str, int],
        transport: str,
    ) -> SipAccount | None:
        """Resolve an authenticated live registration by signaling flow."""

        self.expire()
        wanted_transport = str(transport or "").upper()
        registration = next(
            (
                item
                for item in self.registrations.values()
                if item.source_host == str(addr[0])
                and item.source_port == int(addr[1])
                and item.transport.upper() == wanted_transport
            ),
            None,
        )
        return (
            self.accounts.get(registration.username.lower())
            if registration is not None
            else None
        )

    async def handle_register(self, request: sip.SipMessage, addr: tuple[str, int], transport: str) -> SipRegisterResult:
        self.last_sip_event = "REGISTER"
        if not self.enabled:
            return self._result(405, "Method Not Allowed")
        try:
            sip.parse_sip_uri(request.uri)
            username = _extract_register_username(request)
        except Exception:
            return self._result(400, "Bad Request")
        account = self.accounts.get(username.lower())
        source_key = f"{str(transport or '').upper()}:{addr[0]}"
        authorization_status = (
            _AuthorizationStatus.INVALID
            if account is None or not account.enabled
            else self._check_authorization(request, account, addr, transport)
        )
        if (
            account is None
            or not account.enabled
            or authorization_status is not _AuthorizationStatus.VALID
        ):
            stale = authorization_status is _AuthorizationStatus.STALE
            _nonce, challenge = self._challenge(
                source_key,
                force_new=stale,
                stale=stale,
            )
            return self._result(
                401,
                "Unauthorized",
                tuple(
                    ("WWW-Authenticate", candidate)
                    for candidate in self._modern_challenges(challenge)
                ),
            )

        try:
            cseq = _register_cseq(request)
        except (TypeError, ValueError):
            return self._result(400, "Bad Request")
        call_id = request.header("Call-ID").strip()
        if not call_id:
            return self._result(400, "Bad Request")

        self.expire()
        raw_contacts = [
            value.strip() for value in request.header_values("Contact") if value.strip()
        ]
        if not raw_contacts:
            return self._result(
                200,
                "OK",
                self._contact_response_headers(account.username),
            )

        contacts = _register_contacts(request)
        # Contact parsing is atomic at the registrar boundary. The public
        # helper remains tolerant for phonebook parsing, but a REGISTER cannot
        # partially apply only its syntactically valid bindings.
        if not contacts or len(contacts) != len(raw_contacts):
            return self._result(400, "Bad Request")
        wildcard = [contact for contact in contacts if contact[0] == "*"]
        if wildcard and (
            len(contacts) != 1
            or request.header("Expires").strip() != "0"
        ):
            return self._result(400, "Bad Request")

        prepared: list[_PreparedContact] = []
        seen_contacts: set[str] = set()
        for advertised_contact_uri, expires, raw_contact in contacts:
            if advertised_contact_uri == "*":
                prepared.append(_PreparedContact("*", 0, "", 1.0))
                continue
            try:
                q = _contact_q(raw_contact)
                instance_id, reg_id, outbound = _outbound_contact(
                    request,
                    raw_contact,
                )
                contact_uri = (
                    _contact_for_source_flow(
                        advertised_contact_uri,
                        addr,
                        transport,
                    )
                    if expires > 0
                    else ""
                )
            except (TypeError, ValueError, sip.SipError):
                return self._result(400, "Bad Request")
            identity = (
                f"{instance_id.casefold()}:{reg_id}"
                if outbound
                else advertised_contact_uri.casefold()
            )
            if identity in seen_contacts:
                return self._result(400, "Bad Request")
            seen_contacts.add(identity)
            prepared.append(
                _PreparedContact(
                    advertised_contact_uri,
                    expires,
                    contact_uri,
                    q,
                    instance_id,
                    reg_id,
                    outbound,
                )
            )

        current_bindings = self._registrations(account.username)
        for contact in prepared:
            if contact.advertised_uri == "*":
                continue
            current = next(
                (
                    registration
                    for registration in current_bindings
                    if _registration_matches_contact(registration, contact)
                ),
                None,
            )
            if current is None or current.call_id != call_id:
                continue
            if cseq < current.cseq:
                return self._result(
                    500,
                    "Server Internal Error",
                    (("Retry-After", "0"),),
                )
            if cseq == current.cseq:
                # An authenticated retransmission returns the current complete
                # binding set without extending expiration or re-notifying HA.
                return self._result(
                    200,
                    "OK",
                    self._contact_response_headers(account.username),
                )

        was_registered = bool(current_bindings)
        next_registrations = dict(self.registrations)
        wanted = account.username.lower()
        if wildcard:
            next_registrations = {
                key: registration
                for key, registration in next_registrations.items()
                if registration.username.lower() != wanted
            }
        else:
            now = time.time()
            for contact in prepared:
                key = next(
                    (
                        candidate_key
                        for candidate_key, registration in next_registrations.items()
                        if registration.username.lower() == wanted
                        and _registration_matches_contact(registration, contact)
                    ),
                    None,
                )
                if contact.expires <= 0:
                    if key is not None:
                        next_registrations.pop(key, None)
                    continue
                if key is None:
                    key = self._new_binding_key(
                        next_registrations,
                        account.username,
                    )
                next_registrations[key] = SipRegistration(
                    username=account.username,
                    contact_uri=contact.contact_uri,
                    source_host=addr[0],
                    source_port=int(addr[1]),
                    transport=transport,
                    expires_at=now + contact.expires,
                    advertised_contact_uri=contact.advertised_uri,
                    user_agent=request.header("User-Agent"),
                    call_id=call_id,
                    cseq=cseq,
                    q=contact.q,
                    instance_id=contact.instance_id,
                    reg_id=contact.reg_id,
                    outbound=contact.outbound,
                )

        self.registrations = next_registrations
        is_registered = bool(self._registrations(account.username))
        if was_registered != is_registered:
            self._notify_registration_change(account.username, is_registered)
        if is_registered:
            _LOGGER.info(
                "SIP registrar updated user=%s transport=%s active_contacts=%s request_contacts=%s",
                account.username,
                transport.upper(),
                len(self._registrations(account.username)),
                len(prepared),
            )
        else:
            _LOGGER.info(
                "SIP registrar unregistered final contact user=%s transport=%s",
                account.username,
                transport.upper(),
            )
        return self._result(
            200,
            "OK",
            self._contact_response_headers(account.username),
        )

    def _contact_response_headers(
        self,
        username: str,
    ) -> tuple[tuple[str, str], ...]:
        now = time.time()
        registrations = self.registered_contacts(username)
        headers = [
            (
                "Contact",
                f"<{registration.advertised_contact_uri or registration.contact_uri}>"
                f";expires={max(0, math.ceil(registration.expires_at - now))}"
                f";q={registration.q:g}",
            )
            for registration in registrations
        ]
        for index, registration in enumerate(registrations):
            if not registration.outbound:
                continue
            key, value = headers[index]
            headers[index] = (
                key,
                f'{value};+sip.instance="<{registration.instance_id}>"'
                f";reg-id={registration.reg_id}",
            )
        if any(registration.outbound for registration in registrations):
            headers.extend(
                (("Require", "outbound"), ("Supported", "outbound"), ("Flow-Timer", "25"))
            )
        return tuple(headers)

    def _result(self, status: int, reason: str, headers: tuple[tuple[str, str], ...] = ()) -> SipRegisterResult:
        self.last_sip_status_code = int(status)
        self.last_sip_reason = reason
        return SipRegisterResult(status, reason, headers)

    def expire(self) -> bool:
        now = time.time()
        previously_registered = {
            registration.username.lower(): registration.username
            for registration in self.registrations.values()
        }
        old = set(self.registrations)
        self.registrations = {key: reg for key, reg in self.registrations.items() if reg.expires_at > now}
        expired = old - set(self.registrations)
        still_registered = {
            registration.username.lower() for registration in self.registrations.values()
        }
        for wanted, username in previously_registered.items():
            if wanted not in still_registered:
                _LOGGER.info("SIP registrar expired final binding user=%s", username)
                self._notify_registration_change(username, False)
        return bool(expired)

    def roster_entries(self) -> list[RosterEntry]:
        return self.registered_roster_entries()

    def registered_roster_entries(self) -> list[RosterEntry]:
        self.expire()
        entries: list[RosterEntry] = []
        registered_usernames = sorted(
            {registration.username.lower() for registration in self.registrations.values()}
        )
        for username in registered_usernames:
            account = self.accounts.get(username)
            if account is None or not account.enabled:
                continue
            registrations = self.registered_contacts(account.username)
            if not registrations:
                continue
            registration = registrations[0]
            entries.append(
                RosterEntry(
                    id=account.username,
                    name=account.roster_name,
                    sip_uri=registration.contact_uri,
                    extension=account.extension,
                    metadata={
                        "sip_transport": registration.transport.lower(),
                        "registered": True,
                        "user_agent": registration.user_agent,
                        "sip_profile": (
                            "dahua"
                            if supports_dahua_pcm(registration.user_agent)
                            else ""
                        ),
                        "conference_group": account.conference_group,
                        "conference_ring": bool(account.conference_ring),
                        "ring_group": account.ring_group,
                        "sip_contacts": [
                            {
                                "uri": binding.contact_uri,
                                "transport": binding.transport.lower(),
                                "q": binding.q,
                                "user_agent": binding.user_agent,
                                "sip_profile": (
                                    "dahua"
                                    if supports_dahua_pcm(binding.user_agent)
                                    else ""
                                ),
                            }
                            for binding in registrations
                        ],
                    },
                )
            )
        return entries

    def snapshot(self) -> dict[str, Any]:
        self.expire()
        registered_users = {
            registration.username.lower() for registration in self.registrations.values()
        }
        return {
            "registrar_enabled": self.enabled,
            "registrar_accounts": len(self.accounts),
            "registrar_registered": len(registered_users),
            "registrar_binding_count": len(self.registrations),
            "registrar_bindings": [reg.snapshot() for reg in self.registrations.values()],
            "registrar_last_sip_event": self.last_sip_event,
            "registrar_last_sip_status_code": self.last_sip_status_code,
            "registrar_last_sip_reason": self.last_sip_reason,
        }
