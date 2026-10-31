"""Optional read-only LDAP listener over the canonical HA phone directory."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
import logging
import re
import ssl
import unicodedata

from ldap3.core.exceptions import LDAPInvalidDnError
from ldap3.utils.dn import escape_rdn, parse_dn

from . import ldap_codec as codec
from .phone_directory import PhoneDirectory
from .roster import RosterEntry

_LOGGER = logging.getLogger(__name__)
DEFAULT_BASE_DN = "ou=phonebook,dc=voip"
_CATEGORIES = ("esphome", "sip_account", "browser", "contact", "automation", "group", "service")
_USER = re.compile(r"[\w.-]+\Z")
_ALIASES = {"commonname": "cn", "surname": "sn", "userid": "uid", "2.5.4.3": "cn", "2.5.4.20": "telephonenumber"}
_KNOWN = {"objectclass", "uid", "cn", "sn", "displayname", "telephonenumber", "mobile", "homephone",
          "labeleduri", "ou", "description", "supportedldapversion", "namingcontexts", "supportedcontrol",
          "supportedextension", "supportedsaslmechanisms"}
_OPERATIONAL = {"supportedldapversion", "namingcontexts", "supportedcontrol", "supportedextension", "supportedsaslmechanisms"}
_RESPONSES = {"modifyRequest": "modifyResponse", "addRequest": "addResponse", "delRequest": "delResponse",
              "modDNRequest": "modDNResponse", "compareRequest": "compareResponse", "extendedReq": "extendedResp"}
Authenticate = Callable[[str, bytes], Awaitable[str | None]]


@dataclass(frozen=True, slots=True)
class Limits:
    connections: int = 32
    searches_per_connection: int = 4
    results: int = 256
    bind_seconds: float = 5.0
    frame_seconds: float = 10.0
    search_seconds: float = 3.0
    write_seconds: float = 5.0


def _unescape(value: str) -> str:
    decoded = bytearray()
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\":
            index += 1
            if index >= len(value):
                raise ValueError("Incomplete DN escape")
            if index + 1 < len(value) and all(c in "0123456789abcdefABCDEF" for c in value[index:index + 2]):
                decoded.append(int(value[index:index + 2], 16))
                index += 2
                continue
            char = value[index]
        decoded.extend(char.encode("utf-8"))
        index += 1
    return decoded.decode("utf-8")


def _dn(value: str):
    if not value:
        return ()
    groups, current = [], []
    for name, content, separator in parse_dn(value, strip=True):
        current.append((name.casefold(), _unescape(content)))
        if separator != "+":
            groups.append(tuple(sorted(current)))
            current = []
    return tuple(groups)


def _dn_key(value: str):
    return tuple(tuple((name, content.casefold()) for name, content in group) for group in _dn(value))


def _attribute(name: str) -> str:
    name = name.casefold()
    return _ALIASES.get(name, name)


def _normal(value: bytes, attribute: str, exact: bool = False) -> str | None:
    try:
        text = unicodedata.normalize("NFKC", value.decode("utf-8"))
    except UnicodeDecodeError:
        return None
    if exact or attribute == "labeleduri":
        return text
    text = " ".join(text.casefold().split())
    if attribute in {"telephonenumber", "mobile", "homephone"}:
        text = text.replace(" ", "").replace("-", "")
    return text


def matches(filter: codec.Filter, attributes: dict[str, tuple[bytes, ...]]) -> bool | None:
    """Evaluate LDAP's TRUE/FALSE/Undefined semantics without regex backtracking."""
    operation = filter.operation
    if operation in {"and", "or"}:
        values = [matches(child, attributes) for child in filter.children]
        if operation == "and":
            return False if False in values else None if None in values else True
        return True if True in values else None if None in values else False
    if operation == "not":
        result = matches(filter.children[0], attributes)
        return None if result is None else not result
    attribute = _attribute(filter.attribute)
    if attribute not in _KNOWN:
        return None
    values = attributes.get(attribute, ())
    if operation == "present":
        return bool(values)
    exact = False
    if operation == "extensibleMatch":
        if filter.dn_attributes or filter.matching_rule not in {"", "caseIgnoreMatch", "2.5.13.2", "caseExactMatch", "2.5.13.5", "telephoneNumberMatch", "2.5.13.20"}:
            return None
        exact = filter.matching_rule in {"caseExactMatch", "2.5.13.5"}
        operation = "equalityMatch"
    if operation in {"greaterOrEqual", "lessOrEqual"}:
        # These directory attributes advertise equality/substring matching,
        # not an ordering rule. LDAP specifies Undefined for such assertions.
        return None
    if operation == "substringFilter":
        parts = [(name, _normal(value, attribute)) for name, value in filter.substrings]
        if any(value is None for _, value in parts):
            return None
        for value in values:
            text = _normal(value, attribute)
            if text is None:
                continue
            position, accepted = 0, True
            for kind, part in parts:
                if kind == "initial":
                    accepted = text.startswith(part)
                    position = len(part)
                elif kind == "final":
                    accepted = text.endswith(part) and len(text) - len(part) >= position
                else:
                    index = text.find(part, position)
                    accepted = index >= 0
                    position = index + len(part)
                if not accepted:
                    break
            if accepted:
                return True
        return False
    assertion = _normal(filter.value, attribute, exact)
    if assertion is None:
        return None
    return any(_normal(value, attribute, exact) == assertion for value in values)


class LdapDirectoryServer:
    """One optional listener, with no credential store, polling or media owner."""

    def __init__(self, directory: PhoneDirectory, authenticate_callback: Authenticate, *,
                 base_dn: str = DEFAULT_BASE_DN, allow_anonymous: bool = False,
                 uri_for: Callable[[RosterEntry], str] | None = None, limits: Limits | None = None) -> None:
        self.directory = directory
        self.authenticate = authenticate_callback
        self.base_dn = base_dn
        self._base_key = _dn_key(base_dn)
        if not self._base_key:
            raise ValueError("A directory base DN is required")
        self.allow_anonymous = allow_anonymous
        self.uri_for = uri_for
        self.limits = limits or Limits()
        self._listener: asyncio.Server | None = None
        self._connections: set[_Connection] = set()
        self._lifecycle = asyncio.Lock()
        self._closing = True

    @property
    def port(self) -> int | None:
        return self._listener.sockets[0].getsockname()[1] if self._listener and self._listener.sockets else None

    async def async_start(self, host: str, port: int, *, ssl_context: ssl.SSLContext | None = None) -> None:
        async with self._lifecycle:
            if self._listener is not None:
                return
            options = {"ssl": ssl_context, "limit": codec.MAX_PDU_BYTES + 1}
            if ssl_context is not None:
                options["ssl_handshake_timeout"] = self.limits.bind_seconds
            self._listener = await asyncio.start_server(self._connected, host, port, **options)
            self._closing = False

    async def async_close(self) -> None:
        async with self._lifecycle:
            self._closing = True
            listener, self._listener = self._listener, None
            if listener is not None:
                listener.close()
            # Python 3.14's Server.wait_closed also waits for client transports.
            # Release our owned streams and tasks before awaiting that barrier.
            await asyncio.gather(*(connection.close() for connection in tuple(self._connections)))
            if listener is not None:
                await listener.wait_closed()

    async def async_revoke_endpoint(self, endpoint_id: str, *, username: str = "") -> None:
        """Revoke active and, when username is supplied, pending authentications."""
        targets = [connection for connection in self._connections
                   if connection.endpoint_id == endpoint_id or
                   (username and connection.bind_username.casefold() == username.casefold())]
        await asyncio.gather(*(connection.close() for connection in targets))

    def _connected(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closing or len(self._connections) >= self.limits.connections:
            writer.close()
            return
        connection = _Connection(self, reader, writer)
        self._connections.add(connection)
        connection._task = asyncio.create_task(connection.run(), name="voip-ldap-client")
        connection._task.add_done_callback(lambda task: self._finished_connection(connection, task))

    def _finished_connection(self, connection, task) -> None:
        self._connections.discard(connection)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.warning("LDAP connection stopped (%s)", type(task.exception()).__name__)

    def username(self, bind_dn: str) -> str:
        # Account-name shorthand remains useful to ordinary SIP phones. The
        # published Bind DN is uid=<SIP username>,ou=accounts,<directory base>.
        if _USER.fullmatch(bind_dn):
            return bind_dn
        groups = _dn(bind_dn)
        key = _dn_key(bind_dn)
        if (len(groups) != len(self._base_key) + 2 or len(groups[0]) != 1
                or groups[0][0][0] != "uid" or key[1] != (("ou", "accounts"),)
                or key[2:] != self._base_key):
            raise ValueError("Unknown Bind identity")
        return groups[0][0][1]

    def records(self) -> Iterable[tuple[str, dict[str, tuple[bytes, ...]]]]:
        yield self.base_dn, {"objectclass": (b"top", b"organizationalUnit"), "ou": (b"phonebook",)}
        for category in _CATEGORIES:
            yield f"ou={category},{self.base_dn}", {"objectclass": (b"top", b"organizationalUnit"), "ou": (category.encode(),)}
        for entry in self.directory.snapshot():
            if not entry.enabled:
                continue
            kind = str(entry.metadata.get("endpoint_kind") or "contact")
            virtual = str(entry.metadata.get("virtual_endpoint") or "")
            if virtual:
                kind = "automation" if virtual in {"automation", "assist_pipeline"} else "group" if virtual in {"ring_group", "conference"} else "service"
            elif entry.metadata.get("group_type") in {"ring", "conference"}:
                kind = "group"
            if kind not in _CATEGORIES:
                kind = "contact"
            identity = str(entry.metadata.get("endpoint_id") or entry.id)
            if not identity or any(ord(char) < 32 for char in identity):
                continue
            attributes = {"objectclass": (b"top", b"organizationalRole", b"uidObject", b"labeledURIObject"),
                          "uid": (identity.encode(),), "cn": (entry.display_name.encode(),), "ou": (kind.encode(),)}
            numbers = tuple(dict.fromkeys(value.encode() for value in (entry.extension, entry.number) if value))
            if numbers:
                attributes["telephonenumber"] = numbers
            uri = self.uri_for(entry) if self.uri_for is not None else entry.sip_uri
            if uri:
                attributes["labeleduri"] = (uri.encode(),)
            if any(len(value) > codec.MAX_VALUE_BYTES for values in attributes.values() for value in values):
                raise codec.LdapProtocolError("Directory value exceeds its byte budget")
            yield f"uid={escape_rdn(identity)},ou={kind},{self.base_dn}", attributes

    @staticmethod
    def select_attributes(attributes, requested):
        if not requested:
            requested = ("*",)
        chosen = {_attribute(name) for name in requested if name not in {"*", "+", "1.1"}}
        if "*" in requested:
            chosen |= attributes.keys() - _OPERATIONAL
        if "+" in requested:
            chosen |= attributes.keys() & _OPERATIONAL
        return {name: values for name, values in attributes.items() if name in chosen}


class _Connection:
    def __init__(self, server, reader, writer) -> None:
        self.server = server
        self.reader, self.writer = reader, writer
        self.endpoint_id = ""
        self.bind_username = ""
        self._generation = 0
        self._closed = False
        self._write_lock = asyncio.Lock()
        self._searches: dict[int, asyncio.Task] = {}
        self._task: asyncio.Task | None = None

    async def close(self) -> None:
        self._closed = True
        self._generation += 1
        current = asyncio.current_task()
        tasks = [task for task in (*self._searches.values(), self._task) if task and task is not current]
        for task in tasks:
            task.cancel()
        self.writer.close()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with suppress(OSError, TimeoutError, ConnectionError):
            async with asyncio.timeout(self.server.limits.write_seconds):
                await self.writer.wait_closed()

    async def _send(self, frame: bytes, generation: int | None = None) -> None:
        async with self._write_lock:
            if self._closed or (generation is not None and generation != self._generation):
                return
            self.writer.write(frame)
            async with asyncio.timeout(self.server.limits.write_seconds):
                await self.writer.drain()

    async def _read(self) -> bytes:
        first = await self.reader.readexactly(1)  # An idle connection owns no timer.
        async with asyncio.timeout(self.server.limits.frame_seconds):
            header = bytearray(first)
            while (length := codec.frame_size(header)) is None:
                header.extend(await self.reader.readexactly(1))
            return bytes(header) + await self.reader.readexactly(length - len(header))

    async def run(self) -> None:
        self._task = asyncio.current_task()
        try:
            while not self._closed:
                frame = await self._read()
                request = codec.decode_request(frame)
                await self._dispatch(request)
                # Do not retain a Bind password while waiting for the next PDU.
                del request, frame
        except asyncio.IncompleteReadError:
            pass
        except codec.LdapProtocolError:
            with suppress(OSError, TimeoutError, ConnectionError):
                await self._send(codec.encode_result(0, "extendedResp", 2, diagnostic="Invalid LDAP request", notice=True))
        except (OSError, TimeoutError, ConnectionError):
            pass
        finally:
            await self.close()

    async def _dispatch(self, request: codec.Request) -> None:
        if request.operation == "unbindRequest":
            self._closed = True
            return
        if request.operation == "abandonRequest":
            task = self._searches.get(request.abandon_id)
            if task is not None:
                task.cancel()
            return
        if request.message_id in self._searches:
            raise codec.LdapProtocolError("Message identity is already in use")
        if request.operation == "bindRequest":
            await self._bind(request)
        elif request.operation == "searchRequest":
            if len(self._searches) >= self.server.limits.searches_per_connection:
                await self._send(codec.encode_result(request.message_id, "searchResDone", 51, diagnostic="Search limit reached"))
                return
            task = asyncio.create_task(self._search(request, self._generation), name="voip-ldap-search")
            self._searches[request.message_id] = task
            task.add_done_callback(lambda finished, identity=request.message_id: self._completed(identity, finished))
        else:
            response = _RESPONSES.get(request.operation)
            if response is None:
                raise codec.LdapProtocolError("Expected a request")
            await self._send(codec.encode_result(request.message_id, response, 53, diagnostic="Directory is read only"))

    def _completed(self, identity, task) -> None:
        if self._searches.get(identity) is task:
            self._searches.pop(identity)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.warning("LDAP search stopped (%s)", type(task.exception()).__name__)
            self.writer.close()

    async def _bind(self, request) -> None:
        self._generation += 1
        self.endpoint_id = ""
        self.bind_username = ""
        tasks = tuple(self._searches.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        code = 0
        if request.version != 3:
            code = 2
        elif request.critical_control:
            code = 12
        elif request.authentication != "simple":
            code = 7
        elif request.bind_dn or request.password:
            if not request.bind_dn or not request.password:
                code = 49
            else:
                try:
                    self.bind_username = self.server.username(request.bind_dn)
                    async with asyncio.timeout(self.server.limits.bind_seconds):
                        identity = await self.server.authenticate(self.bind_username, request.password)
                    if not identity:
                        code = 49
                    elif not self._closed:
                        self.endpoint_id = identity
                except (ValueError, LDAPInvalidDnError):
                    code = 49
                except TimeoutError:
                    code = 52
                except Exception as error:
                    _LOGGER.warning("LDAP authentication unavailable (%s)", type(error).__name__)
                    code = 52
        await self._send(codec.encode_result(request.message_id, "bindResponse", code))

    async def _search(self, request, generation) -> None:
        code = 0
        try:
            if request.critical_control:
                code = 12
            elif not request.base_dn:
                if request.scope != 0:
                    code = 32
                else:
                    attributes = {"objectclass": (b"top",), "supportedldapversion": (b"3",),
                                  "namingcontexts": (self.server.base_dn.encode(),)}
                    if matches(request.filter, attributes) is True:
                        await self._send(codec.encode_entry(request.message_id, "",
                            self.server.select_attributes(attributes, request.attributes), types_only=request.types_only), generation)
            elif not self.endpoint_id and not self.server.allow_anonymous:
                code = 50
            else:
                base = _dn_key(request.base_dn)
                rows = tuple(self.server.records())
                if not any(_dn_key(dn) == base for dn, _ in rows):
                    code = 32
                else:
                    limit = min(request.size_limit or self.server.limits.results, self.server.limits.results)
                    seconds = min(request.time_limit or self.server.limits.search_seconds, self.server.limits.search_seconds)
                    deadline = asyncio.get_running_loop().time() + seconds
                    sent = 0
                    for index, (dn, attributes) in enumerate(rows):
                        if asyncio.get_running_loop().time() >= deadline:
                            code = 3
                            break
                        name = _dn_key(dn)
                        under = len(name) >= len(base) and name[-len(base):] == base
                        selected = name == base if request.scope == 0 else under and len(name) == len(base) + 1 if request.scope == 1 else under
                        if selected and matches(request.filter, attributes) is True:
                            if sent >= limit:
                                code = 4
                                break
                            await self._send(codec.encode_entry(request.message_id, dn,
                                self.server.select_attributes(attributes, request.attributes), types_only=request.types_only), generation)
                            sent += 1
                        if index % 16 == 15:
                            await asyncio.sleep(0)  # Yield work; no periodic worker.
        except codec.LdapProtocolError:
            code = 11
        except (ValueError, LDAPInvalidDnError):
            code = 34
        await self._send(codec.encode_result(request.message_id, "searchResDone", code), generation)
