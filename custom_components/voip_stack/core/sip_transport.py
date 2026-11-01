"""Transport helpers shared by SIP clients and trunks."""

from __future__ import annotations

import asyncio
from inspect import isawaitable
import ssl
import logging


async def default_tls_context() -> ssl.SSLContext:
    """Load the system trust store without blocking the event loop."""

    return await asyncio.to_thread(ssl.create_default_context)


def bind_flow_usage(routes, parent_key, usage_key):
    """Route an independent dialog usage to an existing flow's sole reader."""
    queue = routes.get(parent_key)
    if queue is None:
        raise ConnectionError("SIP flow owner is no longer registered")
    if usage_key in routes:
        raise RuntimeError("SIP dialog usage already has a reader")
    routes[usage_key] = queue

    def release():
        if routes.get(usage_key) is queue:
            routes.pop(usage_key, None)

    return release


class SipResponseMailbox:
    """Bounded responses for one transaction, registered before its first send."""

    def __init__(self, matches, *, flow=None):
        self.matches = matches
        self.flow = flow
        self.dialog_scoped = True
        self.queue = asyncio.Queue(maxsize=8)

    async def receive(self, timeout):
        try:
            item = await asyncio.wait_for(self.queue.get(), timeout)
        except TimeoutError:
            return None
        if isinstance(item, Exception):
            raise item
        return item

    def fail(self, error):
        """Wake this transaction on an observed failure of its owned flow."""
        if self.queue.full():
            self.queue.get_nowait()
        self.queue.put_nowait(error)

    def deliver(self, item):
        if self.queue.full():
            # Keep the final response deliverable when provisional responses
            # outpace the transaction consumer.
            if item is not None and int(item[0].status_code or 0) < 200:
                return
            self.queue.get_nowait()
        self.queue.put_nowait(item)


class SipMessageDispatcher:
    """One transport reader with transaction mailboxes and bounded requests."""

    def __init__(self, read, on_request, on_unmatched, on_failure, reject_request, is_control):
        self.read = read
        self.on_request = on_request
        self.on_unmatched = on_unmatched
        self.on_failure = on_failure
        self.reject_request = reject_request
        self.is_control = is_control
        self.mailboxes = []
        self.requests = set()
        self.reader = None
        self.closed = False

    def start(self):
        if self.reader is None:
            self.reader = asyncio.create_task(self._run(), name="voip-sip-dispatcher")

    def register(self, matches):
        if self.closed:
            raise ConnectionError("SIP dispatcher is closed")
        mailbox = SipResponseMailbox(matches)
        self.mailboxes.append(mailbox)
        self.start()
        return mailbox

    def unregister(self, mailbox):
        if mailbox in self.mailboxes:
            self.mailboxes.remove(mailbox)

    def _done(self, task):
        self.requests.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logging.getLogger(__name__).error("SIP request handler failed", exc_info=(type(error), error, error.__traceback__))

    async def _run(self):
        try:
            while not self.closed:
                item = await self.read(None)
                if item is None:
                    raise ConnectionError("SIP transport closed")
                message, addr = item
                if message.is_response:
                    for mailbox in tuple(self.mailboxes):
                        if mailbox.matches(message):
                            mailbox.deliver(item)
                            break
                    else:
                        preparation = self.on_unmatched(message, addr)
                        if isawaitable(preparation):
                            await preparation
                elif len(self.requests) >= (32 if self.is_control(message, addr) else 24):
                    self.reject_request(message, addr)
                else:
                    task = asyncio.create_task(self.on_request(message, addr), name="voip-sip-dispatch-request")
                    self.requests.add(task)
                    task.add_done_callback(self._done)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self.on_failure(error)
        finally:
            self.closed = True
            for mailbox in self.mailboxes:
                mailbox.deliver(None)

    async def close(self):
        self.closed = True
        current = asyncio.current_task()
        tasks = tuple(task for task in (self.reader, *self.requests)
                      if task is not None and task is not current)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for mailbox in self.mailboxes:
            mailbox.deliver(None)
        self.mailboxes.clear()
