"""Application-level SIP methods owned by the integration runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import secrets
import time
from xml.etree import ElementTree

from homeassistant.core import HomeAssistant

from .const import EVENT_SIP_MESSAGE, EVENT_SIP_PRESENCE
from .core import sip
from .sip_listener import SipFollowUpRequest, SipRequestResult
from .sip_registrar import SipRegistrar


@dataclass(slots=True)
class SipApplicationMethods:
    """Handle non-call requests without adding another SIP state machine."""

    hass: HomeAssistant
    registrar: SipRegistrar
    publications: dict[str, "SipPresencePublication"] | None = None
    subscriptions: dict[str, "SipPresenceSubscription"] | None = None
    tasks: set[asyncio.Task[object]] | None = None
    publication_timers: dict[str, asyncio.Task[object]] | None = None
    subscription_timers: dict[str, asyncio.Task[object]] | None = None

    def __post_init__(self) -> None:
        self.publications = {}
        self.subscriptions = {}
        self.tasks = set()
        self.publication_timers = {}
        self.subscription_timers = {}

    async def handle(
        self,
        request: sip.SipMessage,
        addr: tuple[str, int],
        transport: str,
        follow_up_sender: "SipFollowUpSender | None" = None,
    ) -> SipRequestResult:
        account = self.registrar.account_for_source(addr, transport)
        if account is None:
            return SipRequestResult(403, "Forbidden")
        if request.method == "MESSAGE":
            return self._message(request, account.username)
        if request.method == "PUBLISH":
            return self._publish(request, account.username)
        if request.method == "SUBSCRIBE":
            return self._subscribe(
                request,
                addr,
                account.username,
                follow_up_sender,
            )
        return SipRequestResult(405, "Method Not Allowed")

    def _message(self, request: sip.SipMessage, sender: str) -> SipRequestResult:
        """Publish one authenticated text message to Home Assistant."""

        content_type = request.header("Content-Type").split(";", 1)[0].lower()
        if content_type != "text/plain":
            return SipRequestResult(415, "Unsupported Media Type")
        try:
            message = request.body.decode("utf-8")
            recipient = sip.parse_sip_uri(request.uri).user
        except (UnicodeDecodeError, ValueError, sip.SipError):
            return SipRequestResult(400, "Bad Request")
        self.hass.bus.async_fire(
            EVENT_SIP_MESSAGE,
            {
                "sender": sender,
                "recipient": recipient,
                "content_type": content_type,
                "message": message,
            },
        )
        return SipRequestResult()

    def _publish(self, request: sip.SipMessage, sender: str) -> SipRequestResult:
        """Apply one RFC 3903 presence publication atomically."""

        if request.header("Event").strip().lower() != "presence":
            return SipRequestResult(489, "Bad Event")
        try:
            target = sip.parse_sip_uri(request.uri).user
            expires = _publication_expires(request.header("Expires"))
        except (TypeError, ValueError, sip.SipError):
            return SipRequestResult(400, "Bad Request")
        if target.lower() != sender.lower():
            return SipRequestResult(403, "Forbidden")

        publications = self.publications
        assert publications is not None
        current = publications.get(target.lower())
        if current is not None and current.expires_at <= time.time():
            publications.pop(target.lower(), None)
            current = None
        match = request.header("SIP-If-Match").strip()
        if match and (current is None or match != current.entity_tag):
            return SipRequestResult(412, "Conditional Request Failed")
        if current is not None and not match:
            return SipRequestResult(412, "Conditional Request Failed")

        body = request.body
        if body:
            if request.header("Content-Type").split(";", 1)[0].lower() != "application/pidf+xml":
                return SipRequestResult(415, "Unsupported Media Type")
            if not _valid_pidf(body, target):
                return SipRequestResult(400, "Bad Request")
        elif not match:
            return SipRequestResult(400, "Bad Request")

        if expires == 0:
            publications.pop(target.lower(), None)
            self._cancel_timer(self.publication_timers, target.lower())
            entity_tag = current.entity_tag if current is not None else secrets.token_hex(8)
        else:
            entity_tag = current.entity_tag if current is not None else secrets.token_hex(8)
            publications[target.lower()] = SipPresencePublication(
                entity_tag=entity_tag,
                body=body or (current.body if current is not None else b""),
                expires_at=time.time() + expires,
            )
            self._replace_timer(
                self.publication_timers,
                target.lower(),
                self._expire_publication(target, entity_tag, expires),
                f"voip-sip-publish-expiry-{target}",
            )
        self.hass.bus.async_fire(
            EVENT_SIP_PRESENCE,
            {"account": sender, "published": bool(expires), "expires": expires},
        )
        self._notify_subscribers(target)
        return SipRequestResult(
            headers=(("SIP-ETag", entity_tag), ("Expires", str(expires)))
        )

    def _subscribe(
        self,
        request: sip.SipMessage,
        addr: tuple[str, int],
        subscriber: str,
        follow_up_sender: "SipFollowUpSender | None",
    ) -> SipRequestResult:
        """Create, refresh or terminate one RFC 6665 presence subscription."""

        if request.header("Event").strip().lower() != "presence":
            return SipRequestResult(489, "Bad Event")
        if follow_up_sender is None or not request.header("Contact"):
            return SipRequestResult(400, "Bad Request")
        try:
            target = sip.parse_sip_uri(request.uri).user
            from_user = sip.parse_sip_uri(
                _uri_from_header(request.header("From"))
            ).user
            expires = _publication_expires(request.header("Expires"))
        except (TypeError, ValueError, sip.SipError):
            return SipRequestResult(400, "Bad Request")
        if from_user.lower() != subscriber.lower():
            return SipRequestResult(403, "Forbidden")
        target_account = self.registrar.accounts.get(target.lower())
        if target_account is None or not target_account.enabled:
            return SipRequestResult(404, "Not Found")

        key = f"{request.header('Call-ID')}:{sip.extract_tag(request.header('From'))}"
        subscriptions = self.subscriptions
        assert subscriptions is not None
        current = subscriptions.get(key)
        request_to_tag = sip.extract_tag(request.header("To"))
        if current is not None and request_to_tag != current.to_tag:
            return SipRequestResult(481, "Call/Transaction Does Not Exist")
        to_tag = current.to_tag if current is not None else secrets.token_hex(8)
        subscription = SipPresenceSubscription(
            key=key,
            target=target,
            subscriber=subscriber,
            to_tag=to_tag,
            request=request,
            addr=addr,
            expires_at=time.time() + expires,
            sender=follow_up_sender,
            next_cseq=current.next_cseq if current is not None else 1,
            notify_lock=current.notify_lock if current is not None else asyncio.Lock(),
        )
        if expires:
            subscriptions[key] = subscription
            self._replace_timer(
                self.subscription_timers,
                key,
                self._expire_subscription(key, to_tag, expires),
                f"voip-sip-subscription-expiry-{key}",
            )
        else:
            subscriptions.pop(key, None)
            self._cancel_timer(self.subscription_timers, key)
        return self._notification_result(
            subscription,
            expires=expires,
            terminated=not expires,
        )

    def _notification_result(
        self,
        subscription: "SipPresenceSubscription",
        *,
        expires: int,
        terminated: bool = False,
    ) -> SipRequestResult:
        publication = self._publication(subscription.target)
        body = publication.body if publication is not None else _closed_pidf(
            subscription.target
        )
        state = "terminated;reason=timeout" if terminated else f"active;expires={expires}"
        cseq = subscription.next_cseq
        subscription.next_cseq += 1
        return SipRequestResult(
            headers=(("Expires", str(expires)),),
            to_tag=subscription.to_tag,
            follow_up=SipFollowUpRequest(
                method="NOTIFY",
                headers=(("Event", "presence"), ("Subscription-State", state)),
                body=body,
                content_type="application/pidf+xml",
                cseq=cseq,
            ),
            follow_up_lock=subscription.notify_lock,
        )

    def _publication(self, target: str) -> "SipPresencePublication | None":
        publications = self.publications
        assert publications is not None
        publication = publications.get(target.lower())
        if publication is not None and publication.expires_at <= time.time():
            publications.pop(target.lower(), None)
            self._cancel_timer(self.publication_timers, target.lower())
            return None
        return publication

    def _notify_subscribers(self, target: str) -> None:
        subscriptions = self.subscriptions
        assert subscriptions is not None
        now = time.time()
        for key, subscription in tuple(subscriptions.items()):
            if subscription.expires_at <= now:
                subscriptions.pop(key, None)
                self._cancel_timer(self.subscription_timers, key)
                continue
            if subscription.target.lower() != target.lower():
                continue
            result = self._notification_result(
                subscription,
                expires=max(0, int(subscription.expires_at - now)),
            )
            self._track(
                subscription.sender(subscription.request, subscription.addr, result),
                f"voip-sip-notify-{subscription.key}",
            )

    async def _expire_publication(
        self, target: str, entity_tag: str, expires: int
    ) -> None:
        await asyncio.sleep(expires)
        publications = self.publications
        if publications is None:
            return
        current = publications.get(target.lower())
        if current is None or current.entity_tag != entity_tag:
            return
        if current.expires_at > time.time():
            return
        publications.pop(target.lower(), None)
        self._notify_subscribers(target)

    async def _expire_subscription(
        self, key: str, to_tag: str, expires: int
    ) -> None:
        await asyncio.sleep(expires)
        subscriptions = self.subscriptions
        if subscriptions is None:
            return
        current = subscriptions.get(key)
        if current is None or current.to_tag != to_tag:
            return
        if current.expires_at > time.time():
            return
        subscriptions.pop(key, None)
        result = self._notification_result(current, expires=0, terminated=True)
        await current.sender(current.request, current.addr, result)

    def _track(self, awaitable: Awaitable[object], name: str) -> None:
        tasks = self.tasks
        assert tasks is not None
        task = asyncio.create_task(awaitable, name=name)
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def _replace_timer(
        self,
        store: dict[str, asyncio.Task[object]] | None,
        key: str,
        awaitable: Awaitable[object],
        name: str,
    ) -> None:
        assert store is not None
        self._cancel_timer(store, key)
        task = asyncio.create_task(awaitable, name=name)
        store[key] = task
        tasks = self.tasks
        assert tasks is not None
        tasks.add(task)

        def _done(completed: asyncio.Task[object]) -> None:
            tasks.discard(completed)
            if store.get(key) is completed:
                store.pop(key, None)

        task.add_done_callback(_done)

    def _cancel_timer(
        self,
        store: dict[str, asyncio.Task[object]] | None, key: str
    ) -> None:
        if store is None:
            return
        task = store.pop(key, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            if self.tasks is not None:
                self.tasks.discard(task)

    async def stop(self) -> None:
        """Cancel application transactions and discard expiring state."""

        tasks = self.tasks
        if tasks:
            for task in tuple(tasks):
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            tasks.clear()
        if self.subscriptions is not None:
            self.subscriptions.clear()
        if self.publications is not None:
            self.publications.clear()
        if self.publication_timers is not None:
            self.publication_timers.clear()
        if self.subscription_timers is not None:
            self.subscription_timers.clear()


@dataclass(frozen=True, slots=True)
class SipPresencePublication:
    """One bounded presence publication owned by the application surface."""

    entity_tag: str
    body: bytes
    expires_at: float


SipFollowUpSender = Callable[
    [sip.SipMessage, tuple[str, int], SipRequestResult],
    Awaitable[sip.SipMessage | None],
]


@dataclass(slots=True)
class SipPresenceSubscription:
    """One authenticated presence dialog and its notification transport."""

    key: str
    target: str
    subscriber: str
    to_tag: str
    request: sip.SipMessage
    addr: tuple[str, int]
    expires_at: float
    sender: SipFollowUpSender
    next_cseq: int
    notify_lock: asyncio.Lock


def _publication_expires(value: str) -> int:
    expires = 3600 if not value.strip() else int(value)
    if not 0 <= expires <= 3600:
        raise ValueError("invalid presence expiry")
    return expires


def _valid_pidf(body: bytes, target: str) -> bool:
    if len(body) > 64 * 1024:
        return False
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return False
    if root.tag != "{urn:ietf:params:xml:ns:pidf}presence":
        return False
    try:
        entity = sip.parse_sip_uri(root.attrib.get("entity", ""))
    except (ValueError, sip.SipError):
        return False
    return entity.user.lower() == target.lower()


def _uri_from_header(value: str) -> str:
    start = value.find("<")
    end = value.find(">", start + 1)
    if start >= 0 and end > start:
        return value[start + 1 : end].strip()
    return value.split(";", 1)[0].strip()


def _closed_pidf(target: str) -> bytes:
    return (
        '<presence xmlns="urn:ietf:params:xml:ns:pidf" '
        f'entity="sip:{target}@localhost"><tuple id="phone">'
        "<status><basic>closed</basic></status></tuple></presence>"
    ).encode()
