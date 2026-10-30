"""Optional Pymap server projecting the canonical voicemail archive as INBOX."""

from __future__ import annotations

from argparse import Namespace
import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import ssl
from uuid import uuid4

from proxyprotocol.sock import SocketInfo
from pymap.backend.mailbox import MailboxDataInterface, MailboxSetInterface
from pymap.backend.session import BaseSession
from pymap.concurrent import Subsystem
from pymap.config import BackendCapability, IMAPConfig
from pymap.context import current_command, subsystem
from pymap.exceptions import AuthorizationFailure, InvalidAuth, NotAllowedError, NotSupportedError, TemporaryFailure
from pymap.imap import IMAPServer
from pymap.listtree import ListTree
from pymap.mailbox import MailboxSnapshot
from pymap.message import BaseLoadedMessage, BaseMessage
from pymap.parsing.specials import FetchRequirement, Flag, ObjectId, SequenceSet
from pymap.parsing.specials.flag import Recent, Seen
from pymap.parsing.command.select import FetchCommand
from pymap.selected import SelectedSet
from pymap.token import AllTokens
from pymap.user import UserMetadata
from pysasl import SASLAuth
from pysasl.creds.plain import PlainCredentials
from pysasl.hashing import Cleartext
from pysasl.identity import ClearIdentity
from pysasl.mechanism.login import LoginMechanism
from pysasl.mechanism.plain import PlainMechanism

from .core.media_archive import ArchiveError
from .imap_message import LoadedVoicemail, MimeCache

_LOGGER = logging.getLogger(__name__)
_CURRENT_CLIENT = ContextVar("voip_imap_client", default=None)
AuthenticateVerifier = Callable[[str, Callable[[str], bool]], Awaitable[str | None]]


class _Config(IMAPConfig):
    def __init__(self):
        owner = Subsystem.for_asyncio()
        super().__init__(Namespace(passlib_cfg=None, proxy_protocol=None), host=None, port=0,
            tls_enabled=False, secure_auth=False, preauth_credentials=None, invalid_user_sleep=0,
            reject_dnsbl=False, hash_context=Cleartext(), subsystem=owner, cpu_subsystem=owner,
            ssl_context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER), max_append_len=0)

    @property
    def backend_capability(self):
        return BackendCapability(idle=True, object_id=False, multi_append=False)

    @property
    def tls_auth(self):
        return SASLAuth([PlainMechanism(), LoginMechanism()])

    @property
    def greeting(self):
        return b"VoIP Stack voicemail ready"

    @property
    def login_capability(self):
        return [b"UIDPLUS", b"CHILDREN", b"IDLE", b"APPENDLIMIT=0"]


class _BoundedReader:
    """Enforce input budgets through public StreamReader operations."""

    def __init__(self, reader):
        self._reader = reader

    def __getattr__(self, name):
        return getattr(self._reader, name)

    async def readexactly(self, size):
        if size > 65536:
            raise ValueError("IMAP literal exceeds its byte budget")
        return await self._reader.readexactly(size)


@dataclass(slots=True)
class _Client:
    writer: asyncio.StreamWriter
    server: object
    task: asyncio.Task | None = None
    username: str = ""
    endpoint_id: str = ""
    content: object | None = None
    closed: bool = False


class _ProtocolPrivacyFilter(logging.Filter):
    """Pymap's wire debugging includes LOGIN, AUTHENTICATE and message data."""

    def __init__(self, server):
        super().__init__()
        self.server = server

    def filter(self, record):
        client = _CURRENT_CLIENT.get()
        return not (record.levelno <= logging.DEBUG and client is not None
                    and client.server is self.server)


class _Message(BaseMessage):
    def __init__(self, mailbox, uid, record, *, expunged=False):
        flags = {Flag(value.encode()) for value in record.get("imap_flags", ())}
        if record["read"]:
            flags.add(Seen)
        super().__init__(uid, datetime.fromtimestamp(record["created_at"], timezone.utc), flags,
            expunged=expunged, email_id=ObjectId.new_email_id(record["id"].encode()),
            thread_id=ObjectId.new_thread_id(record["id"].encode()))
        self.mailbox, self.record = mailbox, record

    async def load_content(self, requirement):
        command = current_command.get(None)
        if isinstance(command, FetchCommand):
            requirements = []
            for attribute in command.attributes:
                name, section = attribute.attribute, attribute.section
                if name in {b"RFC822.SIZE", b"BODYSTRUCTURE"}:
                    requirements.append(FetchRequirement.HEADER)
                elif name == b"BODY.PEEK" and section is not None and not section.parts and section.specifier in {
                        b"HEADER", b"HEADER.FIELDS", b"HEADER.FIELDS.NOT"}:
                    # Pymap conservatively marks BODY.PEEK as full content;
                    # its public command context retains the parsed section.
                    requirements.append(FetchRequirement.HEADER)
                else:
                    requirements.append(attribute.requirement)
            requirement = FetchRequirement.reduce(requirements)
        if self.expunged or not requirement & (FetchRequirement.HEADER | FetchRequirement.BODY):
            return BaseLoadedMessage(self, requirement, None)
        try:
            # The archived message, not the protocol cache, authorizes every
            # read and supplies its immutable content metadata.
            record = self.mailbox.archive.message(self.record["id"], self.mailbox.endpoint_id)
        except ArchiveError:
            return BaseLoadedMessage(self, requirement, None)
        layout = self.mailbox.server.content.layout(record)
        value = None
        if requirement & FetchRequirement.BODY:
            try:
                value = await self.mailbox.server.content.get(self.mailbox.archive, self.mailbox.endpoint_id, record)
            except (ArchiveError, OSError, ValueError):
                raise TemporaryFailure("Voicemail content is unavailable.") from None
            client = _CURRENT_CLIENT.get()
            if client is not None:
                client.content = value  # One active immutable lease per client.
        return LoadedVoicemail(self, requirement, layout, value)


class _Mailbox(MailboxDataInterface[_Message]):
    def __init__(self, server, archive, endpoint_id, snapshot):
        self.server, self.archive, self.endpoint_id = server, archive, endpoint_id
        self._uid_validity = snapshot.uid_validity
        self._mailbox_id = ObjectId.new_mailbox_id(f"{endpoint_id}:{snapshot.uid_validity}".encode())
        self._selected = SelectedSet()
        self._updated = subsystem.get().new_event()
        self._generation = 0

    @property
    def mailbox_id(self):
        return self._mailbox_id

    @property
    def readonly(self):
        return False

    @property
    def uid_validity(self):
        return self._uid_validity

    @property
    def selected_set(self):
        return self._selected

    def changed(self):
        self._generation += 1
        self._updated.set()

    async def _snapshot(self, *, claim_recent=False, subscribed=None):
        try:
            return await self.archive.imap_snapshot(self.endpoint_id, claim_recent=claim_recent, subscribed=subscribed)
        except (ArchiveError, OSError):
            raise TemporaryFailure("Voicemail metadata is unavailable.") from None

    async def update_selected(self, selected, *, wait_on=None):
        if wait_on is not None and selected.mod_sequence == self._generation:
            self._updated.clear()
            if selected.mod_sequence == self._generation:
                await wait_on.or_event(self._updated).wait()
        generation = self._generation
        snapshot = await self._snapshot(claim_recent=not selected.readonly)
        for uid in snapshot.recent:
            selected.session_flags.add_recent(uid)
        messages = [_Message(self, uid, record) for uid, record in snapshot.messages]
        previous = {uid for _, uid in selected.messages.get_uids(SequenceSet.all(uid=True))}
        current = {message.uid for message in messages}
        selected.add_updates(messages, previous - current)
        selected.mod_sequence = generation
        return selected

    async def get(self, uid, cached_msg):
        if not isinstance(cached_msg, _Message):
            raise TypeError("Unexpected IMAP message identity")
        try:
            record = self.archive.message(cached_msg.record["id"], self.endpoint_id)
        except ArchiveError:
            return _Message(self, uid, cached_msg.record, expunged=True)
        return _Message(self, uid, record)

    async def update(self, uid, cached_msg, flag_set, mode):
        message = await self.get(uid, cached_msg)
        if message.expunged:
            return message
        flags = mode.apply(message.permanent_flags, flag_set)
        extra = {flag.value.decode("ascii") for flag in flags if flag not in {Seen, Recent}}
        record = await self.archive.set_imap_flags(message.record["id"], self.endpoint_id, seen=Seen in flags, flags=extra)
        if record != message.record:
            self.server.changed(self.endpoint_id)
        return _Message(self, uid, record)

    async def delete(self, uids):
        snapshot = await self._snapshot()
        wanted = set(uids)
        for uid, record in snapshot.messages:
            if uid in wanted:
                await self.archive.delete(record["id"], self.endpoint_id)
        if wanted:
            self.server.changed(self.endpoint_id)

    async def claim_recent(self, selected):
        snapshot = await self._snapshot(claim_recent=True)
        for uid in snapshot.recent:
            selected.session_flags.add_recent(uid)

    async def cleanup(self):
        return None

    async def snapshot(self):
        state = await self._snapshot()
        unseen = [index for index, (_, record) in enumerate(state.messages, 1) if not record["read"]]
        return MailboxSnapshot(self.mailbox_id, False, self.uid_validity, self.permanent_flags,
            self.session_flags, len(state.messages), len(state.recent), len(unseen),
            unseen[0] if unseen else None, state.next_uid)

    async def append(self, append_msg, *, recent=False):
        raise NotSupportedError("Messages are deposited through the PBX voicemail application.")

    async def copy(self, uid, destination, *, recent=False):
        raise NotSupportedError("Only the phone's own voicemail INBOX is available.")

    async def move(self, uid, destination, *, recent=False):
        raise NotSupportedError("Only the phone's own voicemail INBOX is available.")


class _MailboxSet(MailboxSetInterface[_Mailbox]):
    def __init__(self, mailbox):
        self.mailbox = mailbox

    @property
    def delimiter(self):
        return "/"

    async def set_subscribed(self, name, subscribed):
        if name.upper() != "INBOX":
            raise KeyError(name)
        await self.mailbox._snapshot(subscribed=subscribed)

    async def list_subscribed(self):
        snapshot = await self.mailbox._snapshot()
        tree = ListTree(self.delimiter)
        if snapshot.subscribed:
            tree.update("INBOX")
        return tree

    async def list_mailboxes(self):
        tree = ListTree(self.delimiter)
        tree.update("INBOX")
        return tree

    async def get_mailbox(self, name):
        if name.upper() != "INBOX":
            raise KeyError(name)
        return self.mailbox

    async def add_mailbox(self, name):
        raise NotSupportedError("Only the phone's own voicemail INBOX is available.")

    async def delete_mailbox(self, name):
        raise NotSupportedError("The voicemail INBOX cannot be removed through IMAP.")

    async def rename_mailbox(self, before, after):
        raise NotSupportedError("The voicemail INBOX has a fixed name.")


class _Session(BaseSession[_Message]):
    def __init__(self, username, config, mailbox):
        super().__init__(username)
        self._config = config
        self._mailboxes = _MailboxSet(mailbox)

    @property
    def config(self):
        return self._config

    @property
    def mailbox_set(self):
        return self._mailboxes


class _Identity:
    def __init__(self, server, client):
        self.server, self.client = server, client

    @property
    def name(self):
        return self.client.username

    @property
    def roles(self):
        return frozenset()

    async def get(self):
        return UserMetadata(self.server.config, self.name)

    @asynccontextmanager
    async def new_session(self):
        if self.client.closed:
            raise InvalidAuth()
        try:
            archive = await self.server.archive_provider()
            snapshot = await archive.imap_snapshot(self.client.endpoint_id)
        except Exception:
            raise TemporaryFailure("Voicemail metadata is unavailable.") from None
        self.server.archive = archive
        endpoint = self.client.endpoint_id
        mailbox = self.server._mailboxes.get(endpoint)
        if mailbox is None:
            mailbox = _Mailbox(self.server, archive, endpoint, snapshot)
            self.server._mailboxes[endpoint] = mailbox
        if self.client.closed:
            raise InvalidAuth()
        yield _Session(self.name, self.server.config, mailbox)

    async def new_token(self, *, expiration=None):
        return None

    async def set(self, metadata):
        raise NotAllowedError("Accounts are managed by VoIP Stack.")

    async def delete(self):
        raise NotAllowedError("Accounts are managed by VoIP Stack.")


class _Login:
    def __init__(self, server, client):
        self.server, self.client = server, client
        self._tokens = AllTokens(server.config)

    @property
    def tokens(self):
        return self._tokens

    async def authenticate(self, credentials):
        if not isinstance(credentials, PlainCredentials):
            raise InvalidAuth()
        self.client.username = credentials.authcid
        def verify(secret):
            return credentials.verify(ClearIdentity(credentials.authcid, secret))
        try:
            async with asyncio.timeout(5):
                endpoint = await self.server.authenticate(credentials.authcid, verify)
        except Exception:
            raise TemporaryFailure("Account verification is unavailable.") from None
        if not endpoint or self.client.closed:
            raise InvalidAuth()
        self.client.endpoint_id = endpoint
        return _Identity(self.server, self.client)

    async def authorize(self, authenticated, authzid):
        if authzid.casefold() != authenticated.name.casefold():
            raise AuthorizationFailure()
        return authenticated


class ImapMailboxServer:
    """Pymap owns IMAP semantics; archive and account owners remain authoritative."""

    def __init__(self, archive_provider, authenticate_callback: AuthenticateVerifier, *, changed_callback=None, max_connections=32):
        self.archive_provider, self.authenticate = archive_provider, authenticate_callback
        self.changed_callback = changed_callback
        self.max_connections = max_connections
        self.config = _Config()
        self.content = MimeCache()
        self.archive = None
        self._listener: asyncio.Server | None = None
        self._clients: list[_Client] = []
        self._mailboxes: dict[str, _Mailbox] = {}
        self._lifecycle = asyncio.Lock()
        self._closing = True
        self._privacy_filter = _ProtocolPrivacyFilter(self)

    @property
    def port(self):
        return self._listener.sockets[0].getsockname()[1] if self._listener and self._listener.sockets else None

    def notify_changed(self, endpoint_id):
        mailbox = self._mailboxes.get(endpoint_id)
        if mailbox is not None:
            mailbox.changed()
        if self.archive is not None:
            self.content.invalidate_deleted(self.archive, endpoint_id)
            for client in self._clients:
                if client.endpoint_id == endpoint_id and client.content is not None:
                    try:
                        self.archive.message(client.content.key[1], endpoint_id)
                    except ArchiveError:
                        client.content = None

    def changed(self, endpoint_id):
        self.notify_changed(endpoint_id)
        if self.changed_callback is not None:
            self.changed_callback(endpoint_id)

    async def async_start(self, host, port, *, ssl_context=None):
        async with self._lifecycle:
            if self._listener is not None:
                return
            options = {"ssl": ssl_context, "limit": 65536}
            if ssl_context is not None:
                options["ssl_handshake_timeout"] = 5
            self._listener = await asyncio.start_server(self._connected, host, port, **options)
            logging.getLogger("pymap.imap").addFilter(self._privacy_filter)
            self._closing = False

    def _connected(self, reader, writer):
        if self._closing or len(self._clients) >= self.max_connections:
            writer.close()
            return
        client = _Client(writer, self)
        self._clients.append(client)
        client.task = asyncio.create_task(self._run(client, reader), name="voip-imap-client")
        client.task.add_done_callback(lambda task: self._finished(client, task))

    def _finished(self, client, task):
        if client in self._clients:
            self._clients.remove(client)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.warning("IMAP connection stopped (%s)", type(task.exception()).__name__)

    async def _run(self, client, reader):
        token = _CURRENT_CLIENT.set(client)
        try:
            self.config.apply_context()
            protocol = IMAPServer(_Login(self, client), self.config)
            info = SocketInfo.get(client.writer, None, unique_id=uuid4().bytes)
            await protocol(_BoundedReader(reader), client.writer, info)
        except (ValueError, OSError, ConnectionError, TimeoutError):
            pass
        finally:
            client.closed = True
            client.content = None
            client.writer.close()
            with suppress(OSError, ConnectionError, TimeoutError):
                async with asyncio.timeout(5):
                    await client.writer.wait_closed()
            _CURRENT_CLIENT.reset(token)

    async def _close_clients(self, clients):
        current = asyncio.current_task()
        tasks = []
        for client in clients:
            client.closed = True
            client.content = None
            client.writer.close()
            if client.task is not None and client.task is not current:
                # EOF makes Pymap finish and await its IDLE update task.
                # Before authentication there is no IDLE task, and account
                # revocation must interrupt a pending credential lookup.
                if not client.endpoint_id:
                    client.task.cancel()
                tasks.append(client.task)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=5)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    async def async_revoke_endpoint(self, endpoint_id, *, username=""):
        clients = [client for client in self._clients if client.endpoint_id == endpoint_id
                   or (username and client.username.casefold() == username.casefold())]
        await self._close_clients(clients)
        self._mailboxes.pop(endpoint_id, None)
        await self.content.forget(endpoint_id)

    async def async_close(self):
        async with self._lifecycle:
            self._closing = True
            listener, self._listener = self._listener, None
            if listener is not None:
                listener.close()
            await self._close_clients(tuple(self._clients))
            if listener is not None:
                await listener.wait_closed()
            await self.content.forget()
            self._mailboxes.clear()
            logging.getLogger("pymap.imap").removeFilter(self._privacy_filter)
