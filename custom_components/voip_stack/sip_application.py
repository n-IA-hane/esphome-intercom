"""Application-level SIP methods owned by the integration runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace, field
import secrets
import logging
import re
from copy import deepcopy
import time
from weakref import WeakValueDictionary
from xml.etree import ElementTree

from homeassistant.core import HomeAssistant, callback

from .const import EVENT_SIP_MESSAGE, EVENT_SIP_PRESENCE
from .core import sip, sip_transfer
from .core.sip_dialog import SubscriptionDialog
from .core.sip_auth import SipDigestCredentials
from .sip_listener import SipFollowUpRequest, SipRequestResult
from .sip_registrar import SipRegistrar


_LOGGER = logging.getLogger(__name__)

# Sixty-four dialog/presence pairs plus four account MWI usages. The global
# owner remains bounded while preserving the former aggregate watch capacity.
MAX_EVENT_USAGES_PER_SUBSCRIBER = 132
MAX_EVENT_USAGES = 512


@dataclass(slots=True)
class SipApplicationMethods:
    """Handle non-call requests without adding another SIP state machine."""

    hass: HomeAssistant
    registrar: SipRegistrar
    publications: dict[str, "SipPresencePublication"] | None = None
    subscriptions: dict[str, "SipEventSubscription"] | None = None
    tasks: set[asyncio.Task[object]] | None = None
    publication_timers: dict[str, asyncio.Task[object]] | None = None
    subscription_timers: dict[str, asyncio.Task[object]] | None = None
    mailbox_archive: object | None = None
    mailbox_unsubscribe: Callable[[], None] | None = None
    dialog_unsubscribe: Callable[[], None] | None = None
    endpoint_unsubscribe: Callable[[], None] | None = None
    parking_unsubscribe: Callable[[], None] | None = None
    notifiers: dict[int, "SipEventSubscription"] = field(default_factory=dict)
    coalesced_notifications: int = 0
    rejected_subscriptions: int = 0
    task_failures: int = 0
    peak_notifiers: int = 0
    signaling_leases: dict[int, asyncio.Task] = field(default_factory=dict)
    event_clients: WeakValueDictionary = field(default_factory=WeakValueDictionary)
    response_cache: dict[tuple, tuple[float, object]] = field(default_factory=dict)
    response_cache_timer: asyncio.TimerHandle | None = None
    stopping: bool = False
    gruu_id: str = ""

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
        *, flow_trunk_id: str = "",
    ) -> SipRequestResult:
        account = self.registrar.account_for_source(addr, transport)
        if request.method == "REFER":
            return self._refer(request, addr, transport, account, follow_up_sender, flow_trunk_id)
        if request.method == "SUBSCRIBE" and request.header("Event").partition(";")[0].strip().casefold() == "refer":
            for subscription in self.subscriptions.values():
                notifier = subscription.refer_notifier
                if notifier is not None and notifier.dialog.key.call_id == request.header("Call-ID"):
                    status, reason, headers = notifier.refresh(request)
                    return SipRequestResult(status, reason, headers=headers, to_tag=subscription.to_tag)
            return SipRequestResult(481, "Subscription Does Not Exist")
        if account is None:
            return SipRequestResult(403, "Forbidden")
        if request.method == "MESSAGE":
            return self._message(request, account.username)
        if request.method == "PUBLISH":
            return self._publish(request, account.username)
        if request.method == "SUBSCRIBE":
            mailbox_id = ""
            dialog_endpoint_id = ""
            event = request.header("Event").partition(";")[0].strip().lower()
            if event == "dialog":
                from .runtime_data import endpoint_directory
                from .parking import parking_address

                try:
                    target = sip.parse_sip_uri(request.uri).user
                    endpoint = endpoint_directory(self.hass).resolve(target)
                    parking = parking_address(self.hass, target)
                except (ValueError, sip.SipError):
                    return SipRequestResult(400, "Bad Request")
                if parking is not None and parking[1]:
                    dialog_endpoint_id = "parking:" + parking[1]
                elif endpoint is not None:
                    dialog_endpoint_id = endpoint.endpoint_id
                else:
                    return SipRequestResult(404, "Not Found")
                if request.body:
                    return SipRequestResult(415, "Unsupported Media Type")
            elif event == "presence":
                from .runtime_data import runtime_data

                try:
                    target = sip.parse_sip_uri(request.uri).user
                    runtime = runtime_data(self.hass)
                    registry = runtime.endpoints if runtime is not None else None
                    endpoint = registry.resolve(target) if registry is not None else None
                except (ValueError, sip.SipError):
                    return SipRequestResult(400, "Bad Request")
                if endpoint is not None:
                    dialog_endpoint_id = endpoint.endpoint_id
            if event == "message-summary":
                from .mailbox import get_archive, mailbox_settings, EVENT_VOICEMAIL_CHANGED
                from .runtime_data import endpoint_directory

                try:
                    target = sip.parse_sip_uri(request.uri).user
                except (ValueError, sip.SipError):
                    return SipRequestResult(400, "Bad Request")
                if target.casefold() != account.username.casefold():
                    return SipRequestResult(403, "Forbidden")
                endpoint = endpoint_directory(self.hass).by_username(account.username)
                if (endpoint is None or not mailbox_settings(self.hass, endpoint.endpoint_id).get("enabled")):
                    return SipRequestResult(404, "Not Found")
                self.mailbox_archive = await get_archive(self.hass)
                mailbox_id = endpoint.endpoint_id
                if self.mailbox_unsubscribe is None:
                    self.mailbox_unsubscribe = self.hass.bus.async_listen(EVENT_VOICEMAIL_CHANGED, self._mailbox_changed)
            result = self._subscribe(
                request,
                addr,
                account.username,
                follow_up_sender,
                mailbox_id=mailbox_id,
                dialog_endpoint_id=dialog_endpoint_id,
            )
            if (event in {"dialog", "presence"} and result.status == 200 and self.dialog_unsubscribe is None
                    and any(item.event in {"dialog", "presence"} and item.dialog_endpoint_id
                            for item in self.subscriptions.values())):
                from .phone_state import phone_state_observer

                self.dialog_unsubscribe = phone_state_observer(self.hass).subscribe(
                    lambda dialogs: self._notify_dialog_changes(dialogs=dialogs), kind="dialog")
            return result
        return SipRequestResult(405, "Method Not Allowed")

    def _refer(self, request, addr, transport, account, sender, flow_trunk_id):
        """Authorize Target-Dialog against the existing leg's stable principal."""
        from .runtime_data import require_runtime_data, sip_trunks
        from .call_transfer import _inbound_server
        from .trunk_policy import identify_inbound_trunk

        try:
            target_dialog = sip_transfer.request_target_dialog(request)
            target = sip_transfer.request_refer_to(request)
            sequence = sip.parse_cseq(request.header("CSeq"))
            if target_dialog is None or sender is None or sip.extract_tag(request.header("To")):
                return SipRequestResult(403, "Forbidden")
            runtime = require_runtime_data(self.hass)
            calls = runtime.sip
            if calls is None:
                return SipRequestResult(481, "Call/Transaction Does Not Exist")
            session = calls.get_session(calls.resolve_session_id(target_dialog.call_id))
            if session is None or not session.live:
                return SipRequestResult(481, "Call/Transaction Does Not Exist")
            client = calls.sip_client_for(target_dialog.call_id)
            if client is not None:
                ids = client.dialog_ids
                matches = (client.dialog is not None and ids.local_tag == target_dialog.local_tag
                           and ids.remote_tag == target_dialog.remote_tag)
                leg = session.legs.get(target_dialog.call_id)
                endpoint_id = (leg.endpoint_id if leg is not None else "") or session.metadata.get("dest_endpoint_id", "")
                trunk_id = (leg.trunk_id if leg is not None else "") or session.metadata.get("outbound_trunk_id", "")
            else:
                server, _uri = _inbound_server(runtime, target_dialog.call_id)
                key = server.dialog_key(target_dialog.call_id) if server is not None else None
                matches = key is not None and key.local_tag == target_dialog.local_tag and key.remote_tag == target_dialog.remote_tag
                endpoint_id = session.metadata.get("source_endpoint_id", "")
                trunk_id = session.metadata.get("inbound_trunk_id", "")
            if not matches:
                return SipRequestResult(481, "Call/Transaction Does Not Exist")
            principal = runtime.endpoints.by_username(account.username) if account is not None else None
            try:
                trusted_trunk = identify_inbound_trunk(sip_trunks(self.hass), addr[0], addr[1], transport,
                                                      flow_trunk_id=flow_trunk_id) if trunk_id else ""
            except ValueError:
                return SipRequestResult(403, "Forbidden")
            if not ((principal is not None and principal.endpoint_id == endpoint_id)
                    or (trunk_id and trusted_trunk == trunk_id)):
                return SipRequestResult(403, "Forbidden")
            subscriber = account.username if account is not None else f"trunk:{trusted_trunk}"
            owned = {id(item): item for item in self.subscriptions.values()}
            owned.update(self.notifiers)
            if (len(owned) >= MAX_EVENT_USAGES or
                    sum(item.subscriber == subscriber for item in owned.values()) >= MAX_EVENT_USAGES_PER_SUBSCRIBER):
                self.rejected_subscriptions += 1
                return SipRequestResult(503, "Service Unavailable", headers=(("Retry-After", "1"),))
            tag = secrets.token_hex(8)
            state = SubscriptionDialog.from_request(request, tag)
            key = f"{state.key.call_id}:{state.key.remote_tag}:refer:{sequence.number}"
            if key in self.subscriptions:
                return SipRequestResult(491, "Request Pending")
            subscription = SipEventSubscription(key=key, dialog=state, event="refer", target=target.uri,
                subscriber=subscriber, to_tag=tag, request=request, addr=addr, expires_at=time.time() + 60,
                sender=sender, notify_lock=asyncio.Lock())
        except (ValueError, sip.SipError):
            return SipRequestResult(400, "Bad Request")
        self.subscriptions[key] = subscription

        async def action():
            return await calls.refer_call(target_dialog.call_id, target, source_client=client)

        async def send(headers, body):
            result = SipRequestResult(to_tag=tag, follow_up_dialog=state,
                follow_up_credentials=self._subscriber_credentials(subscriber),
                follow_up=SipFollowUpRequest(method="NOTIFY", cseq=state.next_cseq(),
                    headers=headers, body=body, content_type="message/sipfrag"))
            return await sender(request, addr, result)

        notifier = sip_transfer.ReferNotifier(state, sequence.number, send)
        subscription.refer_notifier = notifier

        async def run():
            try:
                await sip_transfer.run_refer_action(action, notifier.report)
            finally:
                await notifier.close()
                if self.subscriptions.get(key) is subscription:
                    self.subscriptions.pop(key, None)

        def start():
            subscription.notify_task = self._track(run(), f"voip-refer-operation-{state.key.call_id}")

        return SipRequestResult(to_tag=tag, follow_up_scheduler=start)

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
        match = request.header("SIP-If-Match").strip()
        current = publications.get(match) if match else None
        if current is not None and current.expires_at <= time.time():
            publications.pop(match, None)
            self._cancel_timer(self.publication_timers, match)
            current = None
        if match and (current is None or current.target.casefold() != target.casefold()):
            return SipRequestResult(412, "Conditional Request Failed")
        if not match and (len(publications) >= 256 or
                          sum(item.target.casefold() == target.casefold() for item in publications.values()) >= 64):
            return SipRequestResult(503, "Service Unavailable", headers=(("Retry-After", "1"),))

        body = request.body
        if body:
            if request.header("Content-Type").split(";", 1)[0].lower() != "application/pidf+xml":
                return SipRequestResult(415, "Unsupported Media Type")
            if not _valid_pidf(body, target):
                return SipRequestResult(400, "Bad Request")
        elif not match:
            return SipRequestResult(400, "Bad Request")

        entity_tag = secrets.token_hex(16)
        if current is not None:
            publications.pop(match, None)
            self._cancel_timer(self.publication_timers, match)
        if expires:
            publications[entity_tag] = SipPresencePublication(
                entity_tag=entity_tag,
                body=body or current.body,
                expires_at=time.time() + expires,
                target=target,
                publication_id=current.publication_id if current else secrets.token_hex(8),
            )
            self._replace_timer(
                self.publication_timers, entity_tag,
                self._expire_publication(target, entity_tag, expires),
                f"voip-sip-publish-expiry-{entity_tag}",
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
        *, mailbox_id: str = "", dialog_endpoint_id: str = "",
    ) -> SipRequestResult:
        """Create, refresh or terminate one RFC 6665 presence subscription."""

        event = request.header("Event").partition(";")[0].strip().lower()
        if event not in {"presence", "message-summary", "dialog"}:
            return SipRequestResult(489, "Bad Event")
        content_type = {"presence": "application/pidf+xml", "message-summary": "application/simple-message-summary",
                        "dialog": "application/dialog-info+xml"}[event]
        accepted = request.header("Accept")
        if accepted and not any(item.split(";", 1)[0].strip().lower() in {content_type, "application/*", "*/*"}
                                for item in accepted.split(",")):
            return SipRequestResult(406, "Not Acceptable", headers=(("Accept", content_type),))
        if follow_up_sender is None or not request.header("Contact"):
            return SipRequestResult(400, "Bad Request")
        try:
            target = sip.parse_sip_uri(request.uri).user
            from_user = sip.parse_sip_uri(
                _uri_from_header(request.header("From"))
            ).user
            expires = _publication_expires(request.header("Expires"))
            from .dialog_info import subscription_filter

            dialog_filter = subscription_filter(request.header("Event")) if event == "dialog" else ("", "", "")
        except (TypeError, ValueError, sip.SipError):
            return SipRequestResult(400, "Bad Request")
        if from_user.lower() != subscriber.lower():
            return SipRequestResult(403, "Forbidden")
        target_account = self.registrar.accounts.get(target.lower())
        if event != "dialog" and (target_account is None or not target_account.enabled):
            if (event != "presence" or not dialog_endpoint_id
                    or (target_account is not None and not target_account.enabled)):
                return SipRequestResult(404, "Not Found")

        _package, parameters = sip._split_semicolon_params(request.header("Event"))
        event_id = dict(parameters).get("id") or ""
        key = f"{request.header('Call-ID')}:{sip.extract_tag(request.header('From'))}:{event}:{event_id}"
        subscriptions = self.subscriptions
        assert subscriptions is not None
        current = subscriptions.get(key)
        request_to_tag = sip.extract_tag(request.header("To"))
        if (current is not None and request_to_tag != current.to_tag) or (current is None and request_to_tag):
            return SipRequestResult(481, "Call/Transaction Does Not Exist")
        owned = {id(item): item for item in subscriptions.values()}
        owned.update(self.notifiers)
        if current is None and (len(owned) >= MAX_EVENT_USAGES or
                sum(item.subscriber == subscriber for item in owned.values()) >= MAX_EVENT_USAGES_PER_SUBSCRIBER):
            self.rejected_subscriptions += 1
            return SipRequestResult(503, "Service Unavailable", headers=(("Retry-After", "1"),))
        to_tag = current.to_tag if current is not None else secrets.token_hex(8)
        try:
            if current is not None:
                if current.target != target or current.subscriber != subscriber:
                    return SipRequestResult(403, "Forbidden")
                current.dialog.refresh(request)
                dialog_state = current.dialog
            else:
                dialog_state = SubscriptionDialog.from_request(request, to_tag)
        except sip.SipError:
            return SipRequestResult(400, "Bad Request")
        except ValueError:
            return SipRequestResult(500, "Server Internal Error", headers=(("Retry-After", "1"),))
        subscription = SipEventSubscription(
            key=key,
            dialog=dialog_state,
            event=event,
            mailbox_id=mailbox_id,
            dialog_endpoint_id=dialog_endpoint_id,
            dialog_filter=dialog_filter,
            dialog_version=current.dialog_version if current is not None else 0,
            target=target,
            subscriber=subscriber,
            to_tag=to_tag,
            request=request,
            addr=addr,
            expires_at=time.time() + expires,
            sender=follow_up_sender,
            notify_lock=current.notify_lock if current is not None else asyncio.Lock(),
        )
        if current is not None:
            current.request = request
            current.addr = addr
            current.expires_at = subscription.expires_at
            current.sender = follow_up_sender
            subscription = current
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
            self._release_dialog_observers_if_unused()
        result = self._notification_result(subscription, expires=expires, terminated=not expires)
        return replace(result, follow_up_scheduler=lambda: self._schedule_notification(subscription, result))

    def _notification_result(
        self,
        subscription: "SipEventSubscription",
        *,
        expires: int,
        terminated: bool = False,
        termination_reason: str = "timeout",
        dialog_states: tuple | None = None,
    ) -> SipRequestResult:
        if subscription.event == "dialog":
            from .dialog_info import document, snapshot
            from .runtime_data import require_runtime_data

            states = (snapshot(require_runtime_data(self.hass), subscription.dialog_endpoint_id, subscription.dialog_filter)
                      if dialog_states is None else dialog_states)
            body = document(subscription.request.uri, subscription.dialog_version, states)
            subscription.dialog_snapshot = states
            content_type = "application/dialog-info+xml"
        elif subscription.event == "message-summary":
            from dataclasses import replace

            new, old = self.mailbox_archive.counts(subscription.mailbox_id)
            account_uri = replace(sip.parse_sip_uri(subscription.request.uri), user=f"mailbox.{subscription.target}")
            body = (f"Messages-Waiting: {'yes' if new else 'no'}\r\n"
                    f"Message-Account: {account_uri}\r\n"
                    f"Voice-Message: {min(new, 4294967295)}/{min(old, 4294967295)} (0/0)\r\n").encode()
            content_type = "application/simple-message-summary"
        else:
            publication = self._publication(subscription.target)
            body = publication.body if publication is not None else _closed_pidf(subscription.target)
            if subscription.dialog_endpoint_id:
                from .phone_endpoint import EndpointAvailability
                from .runtime_data import runtime_data

                runtime = runtime_data(self.hass)
                endpoint = runtime.endpoints.get(subscription.dialog_endpoint_id) if runtime is not None else None
                available = endpoint is not None and endpoint.availability is EndpointAvailability.AVAILABLE
                subscription.dialog_snapshot = (available,)
                body = _endpoint_pidf(_uri_from_header(subscription.request.header("To")), available,
                                      publication.body if publication is not None else None)
            content_type = "application/pidf+xml"
        state = f"terminated;reason={termination_reason}" if terminated else f"active;expires={expires}"
        cseq = subscription.dialog.local_cseq
        return SipRequestResult(
            headers=(("Expires", str(expires)),),
            to_tag=subscription.to_tag,
            follow_up=SipFollowUpRequest(
                method="NOTIFY",
                headers=(("Event", subscription.request.header("Event")), ("Subscription-State", state)),
                body=body,
                content_type=content_type,
                cseq=cseq,
            ),
            follow_up_lock=subscription.notify_lock,
            follow_up_dialog=subscription.dialog,
            follow_up_credentials=self._subscriber_credentials(subscription.subscriber),
        )

    def dialog_credentials(self, call_id, address, transport):
        account = self.registrar.account_for_source(address, transport)
        if account is not None:
            return SipDigestCredentials(account.username, account.password)
        from .runtime_data import require_runtime_data
        runtime = require_runtime_data(self.hass)
        session = runtime.sip.get_session(runtime.sip.resolve_session_id(call_id)) if runtime.sip is not None else None
        identity = session.metadata.get("inbound_trunk_id", "") if session is not None else ""
        return self._subscriber_credentials("trunk:" + identity) if identity else None

    def _subscriber_credentials(self, subscriber):
        if subscriber.startswith("trunk:"):
            from .runtime_data import sip_trunks
            trunk = sip_trunks(self.hass).get(subscriber.removeprefix("trunk:"))
            config = trunk.config if trunk is not None else None
            return (SipDigestCredentials(config.username, config.password, config.auth_username)
                    if config is not None and config.username else None)
        account = self.registrar.accounts.get(subscriber.casefold())
        return SipDigestCredentials(account.username, account.password) if account is not None and account.enabled else None

    def _end_subscription(self, subscription, *, reason="timeout", notify=True):
        """Remove an event usage once, retaining its terminal sender until done."""
        if self.subscriptions.get(subscription.key) is not subscription:
            return
        self.subscriptions.pop(subscription.key)
        self._cancel_timer(self.subscription_timers, subscription.key)
        if notify:
            self._schedule_notification(subscription, self._notification_result(
                subscription, expires=0, terminated=True, termination_reason=reason))
        self._release_dialog_observers_if_unused()

    async def async_revoke_account(self, username):
        """Release this subscriber's usages when its owning phone is deleted."""
        wanted = username.casefold()
        usages = {id(item): item for item in (self.subscriptions or {}).values()}
        usages.update(self.notifiers)
        pending = []
        for usage in usages.values():
            if usage.subscriber.casefold() != wanted:
                continue
            self._end_subscription(usage, notify=False)
            usage.pending_notification = None
            usage.notify_terminated = True
            if usage.notify_task is not None and usage.notify_task is not asyncio.current_task():
                usage.notify_task.cancel()
                pending.append(usage.notify_task)
            if usage.refer_notifier is not None:
                await usage.refer_notifier.close()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for key, publication in tuple((self.publications or {}).items()):
            if publication.target.casefold() == wanted:
                self.publications.pop(key)
                self._cancel_timer(self.publication_timers, key)
        self._notify_subscribers(username)
        self._release_dialog_observers_if_unused()

    def _publication(self, target: str) -> "SipPresencePublication | None":
        publications = self.publications
        assert publications is not None
        active = []
        for tag, publication in tuple(publications.items()):
            if publication.expires_at <= time.time():
                publications.pop(tag, None)
                self._cancel_timer(self.publication_timers, tag)
            elif publication.target.casefold() == target.casefold():
                active.append(publication)
        if not active:
            return None
        root = ElementTree.fromstring(active[0].body)
        merged = ElementTree.Element(root.tag, root.attrib)
        for publication in sorted(active, key=lambda item: item.publication_id):
            for child in ElementTree.fromstring(publication.body):
                child = deepcopy(child)
                for element in child.iter():
                    if "id" in element.attrib:
                        element.set("id", f"p{publication.publication_id}-{element.get('id')}")
                merged.append(child)
        return SipPresencePublication("", ElementTree.tostring(merged), min(item.expires_at for item in active), target)

    @callback
    def _dialog_changed(self, _event) -> None:
        self._notify_dialog_changes()

    @callback
    def _dialog_endpoint_changed(self, event) -> None:
        self._notify_dialog_changes(event.endpoint.endpoint_id)

    def _notify_dialog_changes(self, endpoint_id="", *, dialogs=None):
        if self.dialog_unsubscribe is None or not self.subscriptions:
            return
        from .dialog_info import snapshot_all, filter_dialogs
        from .runtime_data import require_runtime_data

        runtime = require_runtime_data(self.hass)
        dialogs = snapshot_all(runtime) if dialogs is None else dialogs
        for subscription in tuple((self.subscriptions or {}).values()):
            if (subscription.event not in {"dialog", "presence"} or not subscription.dialog_endpoint_id
                    or (endpoint_id and endpoint_id != subscription.dialog_endpoint_id)):
                continue
            removed = runtime.endpoints.get(subscription.dialog_endpoint_id) is None
            if subscription.event == "dialog" and subscription.dialog_endpoint_id.startswith("parking:"):
                from .parking import parking_address
                removed = parking_address(self.hass, subscription.dialog_endpoint_id.removeprefix("parking:")) is None
            if subscription.event == "presence":
                from .phone_endpoint import EndpointAvailability

                endpoint = runtime.endpoints.get(subscription.dialog_endpoint_id)
                states = (endpoint is not None and endpoint.availability is EndpointAvailability.AVAILABLE,)
            else:
                states = filter_dialogs(dialogs.get(subscription.dialog_endpoint_id, ()), subscription.dialog_filter)
            if not removed and states == subscription.dialog_snapshot:
                continue
            if removed:
                self.subscriptions.pop(subscription.key, None)
                self._cancel_timer(self.subscription_timers, subscription.key)
            result = self._notification_result(
                subscription, expires=0 if removed else max(0, int(subscription.expires_at - time.time())),
                terminated=removed, termination_reason="noresource", dialog_states=states,
            )
            self._schedule_notification(subscription, result)
        self._release_dialog_observers_if_unused()

    def _release_dialog_observers_if_unused(self):
        if any(item.event in {"dialog", "presence"} and item.dialog_endpoint_id
               for item in (self.subscriptions or {}).values()):
            return
        for name in ("dialog_unsubscribe", "endpoint_unsubscribe", "parking_unsubscribe"):
            if (unsubscribe := getattr(self, name)) is not None:
                unsubscribe()
                setattr(self, name, None)

    @callback
    def _mailbox_changed(self, event) -> None:
        if self.mailbox_archive is None:
            return
        for subscription in tuple((self.subscriptions or {}).values()):
            if subscription.event == "message-summary" and subscription.mailbox_id == event.data.get("endpoint_id"):
                if event.data.get("removed"):
                    self.subscriptions.pop(subscription.key, None)
                    self._cancel_timer(self.subscription_timers, subscription.key)
                    result = self._notification_result(
                        subscription, expires=0, terminated=True, termination_reason="noresource",
                    )
                    self._schedule_notification(subscription, result)
                    continue
                self._notify_subscribers(subscription.target, event="message-summary")
                break

    def _notify_subscribers(self, target: str, *, event="presence") -> None:
        subscriptions = self.subscriptions
        assert subscriptions is not None
        now = time.time()
        for key, subscription in tuple(subscriptions.items()):
            if subscription.expires_at <= now:
                self._end_subscription(subscription)
                continue
            if subscription.event != event or subscription.target.lower() != target.lower():
                continue
            result = self._notification_result(
                subscription,
                expires=max(0, int(subscription.expires_at - now)),
            )
            self._schedule_notification(subscription, result)

    async def _expire_publication(
        self, target: str, entity_tag: str, expires: int
    ) -> None:
        await asyncio.sleep(expires)
        publications = self.publications
        if publications is None:
            return
        current = publications.get(entity_tag)
        if current is None or current.entity_tag != entity_tag:
            return
        if current.expires_at > time.time():
            return
        publications.pop(entity_tag, None)
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
        self._end_subscription(current)
        if current.notify_task is not None:
            await asyncio.shield(current.notify_task)

    def _schedule_notification(self, subscription, result):
        """Keep one sender and one latest state; terminal updates cannot be lost."""
        terminal = dict(result.follow_up.headers).get("Subscription-State", "").startswith("terminated")
        if subscription.notify_terminated:
            return
        subscription.notify_terminated = terminal
        if subscription.pending_notification is not None:
            self.coalesced_notifications += 1
        subscription.pending_notification = result
        if subscription.notify_task is not None:
            return

        self.notifiers[id(subscription)] = subscription
        self.peak_notifiers = max(self.peak_notifiers, len(self.notifiers))

        async def send():
            try:
                while subscription.pending_notification is not None:
                    pending = subscription.pending_notification
                    subscription.pending_notification = None
                    follow_up = replace(pending.follow_up, cseq=subscription.dialog.next_cseq())
                    if subscription.event == "dialog":
                        body = re.sub(rb'version="[0-9]+"',
                                      f'version="{subscription.dialog_version}"'.encode(), follow_up.body, count=1)
                        subscription.dialog_version = (subscription.dialog_version + 1) & 0xFFFFFFFF
                        follow_up = replace(follow_up, body=body)
                    pending = replace(pending, follow_up=follow_up, follow_up_scheduler=None)
                    response = await subscription.sender(subscription.request, subscription.addr, pending)
                    if response is None or int(response.status_code or 0) in {401, 403, 404, 405, 407, 410, 416, 480, 481, 482, 483, 484, 485, 489, 501, 604}:
                        self._end_subscription(subscription, notify=False)
                        subscription.pending_notification = None
                        subscription.notify_terminated = True
            except Exception:
                self._end_subscription(subscription, notify=False)
                subscription.pending_notification = None
                subscription.notify_terminated = True
                raise
            finally:
                subscription.notify_task = None
                self.notifiers.pop(id(subscription), None)
                self._release_dialog_observers_if_unused()

        subscription.notify_task = self._track(send(), f"voip-sip-notify-{subscription.key}")

    def resolve_local_target(self, uri: sip.SipUri) -> sip.SipUri:
        """Use the authoritative registration binding for a locally issued GRUU."""
        return self.registrar.local_gruu_target(uri) or uri

    def local_contact_uri(self, request, host, port, transport, *, flow_trunk_id=""):
        from .runtime_data import require_runtime_data, sip_trunks
        runtime = require_runtime_data(self.hass)
        session_id = request.header("Call-ID")
        try:
            target = sip_transfer.request_target_dialog(request)
            if target is not None:
                session_id = target.call_id
        except sip.SipError:
            pass
        session = runtime.sip.get_session(runtime.sip.resolve_session_id(session_id)) if runtime.sip is not None else None
        trunk_id = flow_trunk_id or ((session.metadata.get("inbound_trunk_id") or "") if session is not None else "")
        if trunk_id:
            trunk = sip_trunks(self.hass).get(trunk_id)
            if trunk is not None and trunk.registered and trunk.public_gruu:
                return trunk.public_gruu
        user = sip.parse_sip_uri(request.uri).user or "voip"
        params = (("transport", transport.lower()),)
        if self.gruu_id and not trunk_id:
            params += (("gr", self.gruu_id),)
        return str(sip.SipUri(user, host, int(port or 5060), params=params))

    def observe_event_owner(self, owner):
        self.event_clients[id(owner)] = owner

    def release_event_owner(self, owner):
        self.archive_responses(owner.completed_responses())
        self.event_clients.pop(id(owner), None)

    @staticmethod
    def _response_key(request):
        from .core.sip_transaction import transaction_key
        return (request.header("Call-ID"), sip.extract_tag(request.header("From")),
                sip.extract_tag(request.header("To")), transaction_key(request))

    def archive_responses(self, responses):
        if self.stopping:
            return
        from .core.sip_transaction import SIP_TIMER_F
        expiry = asyncio.get_running_loop().time() + SIP_TIMER_F
        for response in responses:
            if response.request.method not in {"REFER", "NOTIFY", "SUBSCRIBE", "BYE"}:
                continue
            key = self._response_key(response.request)
            self.response_cache.pop(key, None)
            self.response_cache[key] = expiry, response
            while len(self.response_cache) > 512:
                self.response_cache.pop(next(iter(self.response_cache)))
        self._expire_responses()

    def _expire_responses(self):
        if self.response_cache_timer is not None:
            self.response_cache_timer.cancel()
            self.response_cache_timer = None
        loop = asyncio.get_running_loop()
        for key, (expiry, _response) in tuple(self.response_cache.items()):
            if expiry <= loop.time():
                self.response_cache.pop(key, None)
        if self.response_cache:
            self.response_cache_timer = loop.call_at(min(item[0] for item in self.response_cache.values()), self._expire_responses)

    def known_control(self, request, addr, transport):
        if request.method not in {"ACK", "BYE", "CANCEL", "UPDATE", "NOTIFY", "PRACK", "SUBSCRIBE"}:
            return False
        from .runtime_data import require_runtime_data
        runtime = require_runtime_data(self.hass)
        owner = runtime.sip.sip_client_for(request.header("Call-ID")) if runtime.sip is not None else None
        if owner is not None and owner.knows_control_request(request, addr):
            return True
        if any(client.knows_control_request(request, addr) for client in self.event_clients.values()):
            return True
        if request.method == "SUBSCRIBE":
            return any(item.dialog.key.call_id == request.header("Call-ID")
                       and item.dialog.key.local_tag == sip.extract_tag(request.header("To"))
                       and item.dialog.key.remote_tag == sip.extract_tag(request.header("From"))
                       for item in self.subscriptions.values())
        return False

    async def route_owned_request(self, request, addr, transport, reply):
        """Select the canonical client even when a new flow receives its request."""
        from .runtime_data import require_runtime_data
        cached = self.response_cache.get(self._response_key(request))
        if cached is not None and cached[0] > asyncio.get_running_loop().time():
            result = cached[1]
            reply(sip.build_uas_response(request, result.status, result.reason, to_tag=result.to_tag,
                contact_uri=result.contact_uri, extra_headers=result.extra_headers, body=result.body))
            return True
        runtime = require_runtime_data(self.hass)
        owner = runtime.sip.sip_client_for(request.header("Call-ID")) if runtime.sip is not None else None
        if owner is not None and await owner.accept_owned_request(request, addr, reply):
            return True
        for client in tuple(self.event_clients.values()):
            if client is not owner and await client.accept_owned_request(request, addr, reply):
                return True
        return False

    def retain_signaling(self, owner, awaitable):
        if self.stopping:
            awaitable.close()
            return None
        key = id(owner)
        if key in self.signaling_leases:
            awaitable.close()
            raise RuntimeError("signaling flow already has an event owner")
        started = False
        async def drain():
            nonlocal started
            started = True
            try:
                return await awaitable
            finally:
                self.signaling_leases.pop(key, None)
        def finished(task):
            if not started:
                awaitable.close()
                self.signaling_leases.pop(key, None)
        task = self._track(drain(), "voip-event-signaling-lease")
        task.add_done_callback(finished)
        self.signaling_leases[key] = task
        return task

    def snapshot(self):
        """Expose bounded ownership counts without subscriber identities."""
        return {
            "subscriptions": len(self.subscriptions or {}),
            "signaling_leases": len(self.signaling_leases),
            "routed_clients": len(self.event_clients),
            "cached_responses": len(self.response_cache),
            "publications": len(self.publications or {}),
            "notifiers": len(self.notifiers),
            "pending_notifications": sum(item.pending_notification is not None for item in self.notifiers.values()),
            "peak_notifiers": self.peak_notifiers,
            "coalesced_notifications": self.coalesced_notifications,
            "rejected_subscriptions": self.rejected_subscriptions,
            "task_failures": self.task_failures,
        }

    def _task_done(self, task):
        self.tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            self.task_failures += 1
            _LOGGER.error("SIP application task failed task=%s", task.get_name(), exc_info=(type(error), error, error.__traceback__))

    def _track(self, awaitable: Awaitable[object], name: str):
        tasks = self.tasks
        assert tasks is not None
        task = asyncio.create_task(awaitable, name=name)
        tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

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
            self._task_done(completed)
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
        self.stopping = True
        retained = tuple(owner for key, owner in self.event_clients.items() if key in self.signaling_leases)
        if self.response_cache_timer is not None:
            self.response_cache_timer.cancel()
            self.response_cache_timer = None
        self.response_cache.clear()

        if self.mailbox_unsubscribe is not None:
            self.mailbox_unsubscribe()
            self.mailbox_unsubscribe = None
        self.mailbox_archive = None
        for name in ("dialog_unsubscribe", "endpoint_unsubscribe", "parking_unsubscribe"):
            if (unsubscribe := getattr(self, name)) is not None:
                unsubscribe()
                setattr(self, name, None)
        tasks = self.tasks
        if tasks:
            for task in tuple(tasks):
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            tasks.clear()
        for owner in retained:
            await owner.close_event_signaling()
        self.signaling_leases.clear()
        self.event_clients.clear()
        self.notifiers.clear()
        if self.subscriptions is not None:
            for subscription in tuple(self.subscriptions.values()):
                if subscription.refer_notifier is not None:
                    await subscription.refer_notifier.close()
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
    target: str = ""
    publication_id: str = ""


SipFollowUpSender = Callable[
    [sip.SipMessage, tuple[str, int], SipRequestResult],
    Awaitable[sip.SipMessage | None],
]


@dataclass(slots=True)
class SipEventSubscription:
    """One event package using a shared dialog and bounded notification owner."""

    key: str
    dialog: SubscriptionDialog
    target: str
    subscriber: str
    to_tag: str
    request: sip.SipMessage
    addr: tuple[str, int]
    expires_at: float
    sender: SipFollowUpSender
    notify_lock: asyncio.Lock
    event: str = "presence"
    mailbox_id: str = ""
    dialog_endpoint_id: str = ""
    dialog_filter: tuple[str, str, str] = ("", "", "")
    dialog_version: int = 0
    dialog_snapshot: tuple = ()
    notify_task: asyncio.Task | None = None
    pending_notification: SipRequestResult | None = None
    notify_terminated: bool = False
    refer_notifier: sip_transfer.ReferNotifier | None = None


def _publication_expires(value: str) -> int:
    expires = 3600 if not value.strip() else int(value)
    if expires < 0:
        raise ValueError("invalid presence expiry")
    return min(expires, 3600)


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


def _endpoint_pidf(uri: str, available: bool, publication: bytes | None = None) -> bytes:
    """Compose SIP reachability without modifying an explicit publication lease."""
    if publication is not None and available:
        return publication
    namespace = "{urn:ietf:params:xml:ns:pidf}"
    if publication is None:
        document = ElementTree.Element(namespace + "presence", {"entity": uri})
        part = ElementTree.SubElement(document, namespace + "tuple", {"id": "phone"})
        status = ElementTree.SubElement(part, namespace + "status")
        ElementTree.SubElement(status, namespace + "basic").text = "open" if available else "closed"
        ElementTree.SubElement(part, namespace + "contact").text = uri
    else:
        document = ElementTree.fromstring(publication)
        statuses = document.findall(f"{namespace}tuple/{namespace}status")
        if not statuses:
            used = {part.get("id") for part in document.findall(namespace + "tuple")}
            identity = "reachability"
            while identity in used:
                identity += "-1"
            part = ElementTree.SubElement(document, namespace + "tuple", {"id": identity})
            statuses = [ElementTree.SubElement(part, namespace + "status")]
        for status in statuses:
            basics = status.findall(namespace + "basic") or [ElementTree.SubElement(status, namespace + "basic")]
            for basic in basics:
                basic.text = "closed"
    return ElementTree.tostring(document, encoding="utf-8")
