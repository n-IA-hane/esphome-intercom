"""RFC 3515 REFER and RFC 3891 Replaces value objects."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from email.parser import BytesParser
from email.policy import default as email_policy
from urllib.parse import quote, urlencode

from . import sip


@dataclass(frozen=True, slots=True)
class SipTransferResult:
    """Final outcome reported by a REFER subscription."""

    accepted: bool
    status: int
    state: str
    retained_call: bool = False


class ReferSubscription:
    """One bounded transfer outcome, independent of dialog direction."""

    def __init__(self) -> None:
        self.result: asyncio.Future[SipTransferResult] = asyncio.get_running_loop().create_future()
        self.refer_cseq: int | None = None
        self.allow_missing_id = False
        self._expiry: asyncio.TimerHandle | None = None
        self.expires_at: float | None = None
        self.dialog = None

    def bind(self, cseq: int, *, first: bool) -> None:
        """Bind before sending, including a new CSeq after a digest challenge."""
        self.refer_cseq = cseq
        self.allow_missing_id = first

    def notify(self, request: sip.SipMessage) -> tuple[int, str]:
        if len(request.header_values("Event")) != 1:
            return 400, "Bad Request"
        event = request.header("Event").split(";")
        if event[0].strip().casefold() != "refer":
            return 489, "Bad Event"
        ids = [part.split("=", 1)[1].strip() if "=" in part else ""
               for part in event[1:] if part.split("=", 1)[0].strip().casefold() == "id"]
        if len(ids) > 1:
            return 400, "Bad Request"
        if (self.refer_cseq is None or self.result.done()
                or (ids and ids[0] != str(self.refer_cseq))
                or (not ids and not self.allow_missing_id)):
            return 481, "Subscription Does Not Exist"
        if len(request.header_values("Subscription-State")) != 1:
            return 400, "Bad Request"
        state_parts = request.header("Subscription-State").split(";")
        state = state_parts[0].strip().casefold()
        if state not in {"active", "pending", "terminated"}:
            return 400, "Bad Request"
        if request.header("Content-Type").split(";", 1)[0].strip().casefold() != "message/sipfrag":
            return 415, "Unsupported Media Type"
        try:
            status = parse_sipfrag_status(request.body)
        except (TypeError, ValueError, sip.SipError):
            return 400, "Bad Request"
        terminated = state == "terminated"
        if not terminated:
            expirations = [part.partition("=")[2].strip() for part in state_parts[1:]
                           if part.partition("=")[0].strip().casefold() == "expires"]
            if len(expirations) > 1:
                return 400, "Bad Request"
            if expirations:
                value = expirations[0]
                if not value.isascii() or not value.isdigit() or len(value) > 10:
                    return 400, "Bad Request"
                if self._expiry is not None:
                    self._expiry.cancel()
                loop = asyncio.get_running_loop()
                self.expires_at = loop.time() + int(value)
                self._expiry = loop.call_at(self.expires_at, self.close)
        if not self.result.done():
            if status >= 200:
                self.result.set_result(SipTransferResult(
                    200 <= status < 300, status,
                    "completed" if status < 300 else "failed",
                ))
            elif terminated:
                self.result.set_result(SipTransferResult(False, status, "terminated"))
        if self.result.done() and self._expiry is not None:
            self._expiry.cancel()
        return 200, "OK"

    def receive_dialog_notify(self, request: sip.SipMessage) -> tuple[int, str]:
        """Match a notification on its own dialog usage before changing state."""
        from .sip_dialog import DialogKey
        state = self.dialog
        if state is None:
            return 481, "Subscription Does Not Exist"
        local_tag = sip.extract_tag(request.header("To"))
        remote_tag = sip.extract_tag(request.header("From"))
        if (request.header("Call-ID") != state.key.call_id or local_tag != state.key.local_tag
                or not remote_tag or (state.key.remote_tag and remote_tag != state.key.remote_tag)):
            return 481, "Subscription Does Not Exist"
        try:
            cseq = sip.parse_cseq(request.header("CSeq"))
            contact = sip.contact_target_uri(request)
        except sip.SipError:
            return 400, "Bad Request"
        if cseq.method != "NOTIFY":
            return 400, "Bad Request"
        if cseq.number <= state.remote_cseq:
            return 500, "Server Internal Error"
        status, reason = self.notify(request)
        if status == 200:
            if not state.key.remote_tag:
                state.key = DialogKey(state.key.call_id, local_tag, remote_tag)
                state.route_set = tuple(request.header_values("Record-Route"))
            if contact:
                state.remote_target_uri = contact
            state.remote_cseq = cseq.number
        return status, reason

    def close(self) -> None:
        if self._expiry is not None:
            self._expiry.cancel()
        if not self.result.done():
            self.result.set_result(SipTransferResult(False, 0, "terminated"))


@dataclass(frozen=True, slots=True)
class SipReplaces:
    """One dialog identifier carried by a Replaces header."""

    call_id: str
    to_tag: str
    from_tag: str
    early_only: bool = False

    def __str__(self) -> str:
        parts = [self.call_id, f"to-tag={self.to_tag}", f"from-tag={self.from_tag}"]
        if self.early_only:
            parts.append("early-only")
        return ";".join(parts)


@dataclass(frozen=True, slots=True)
class SipReferTarget:
    """Normalized target of one REFER request."""

    uri: str
    replaces: SipReplaces | None = None
    referred_by: str = ""
    referral_content_type: str = ""
    referral_body: bytes = b""
    referral_body_headers: tuple[tuple[str, str], ...] = ()

    def as_header(self) -> str:
        query = ""
        if self.replaces is not None:
            query = "?" + urlencode(
                {"Replaces": str(self.replaces)},
                quote_via=quote,
                safe="",
            )
        return f"<{self.uri}{query}>"


def parse_replaces(value: str) -> SipReplaces:
    """Parse a Replaces value with mandatory dialog tags."""

    # A SIP header is already decoded. URI decoding belongs to Refer-To only;
    # decoding again changes valid percent sequences in the dialog identity.
    parts = [part.strip() for part in (value or "").split(";")]
    call_id = parts[0] if parts else ""
    params: dict[str, str] = {}
    flags: set[str] = set()
    for part in parts[1:]:
        if not part:
            continue
        if "=" in part:
            key, item = part.split("=", 1)
            key, item = key.strip().lower(), item.strip()
            if key in params or key in flags:
                raise sip.SipError("duplicate Replaces parameter")
            params[key] = item
        else:
            key = part.lower()
            if key in params or key in flags:
                raise sip.SipError("duplicate Replaces parameter")
            flags.add(key)
    if not call_id or any(char.isspace() for char in call_id) or not all(
        sip.is_sip_token(params.get(key, "")) for key in ("to-tag", "from-tag")
    ) or "early-only" in params:
        raise sip.SipError("Replaces requires call-id, to-tag and from-tag")
    return SipReplaces(
        call_id=call_id,
        to_tag=params["to-tag"],
        from_tag=params["from-tag"],
        early_only="early-only" in flags,
    )


def request_replaces(request: sip.SipMessage) -> SipReplaces | None:
    """Read the single INVITE-only RFC 3891 header without URI decoding."""
    values = request.header_values("Replaces")
    if not values:
        return None
    if request.method != "INVITE" or len(values) != 1:
        raise sip.SipError("Replaces is allowed once, on INVITE only")
    return parse_replaces(values[0])


def parse_refer_to(value: str) -> SipReferTarget:
    """Parse one Refer-To name-address and its optional Replaces header."""

    uri_text, headers = sip.split_sip_uri_headers(str(value or ""))
    uri = str(sip.parse_sip_uri(uri_text))
    replaces: SipReplaces | None = None
    for key, item in headers:
        if key.casefold() == "replaces":
            if replaces is not None:
                raise sip.SipError("Refer-To contains duplicate Replaces")
            replaces = parse_replaces(item)
    return SipReferTarget(uri=uri, replaces=replaces)


def request_refer_to(request: sip.SipMessage) -> SipReferTarget:
    """Enforce Refer-To cardinality after compact-header canonicalization."""
    values = request.header_values("Refer-To")
    if len(values) != 1:
        raise sip.SipError("REFER requires exactly one Refer-To")
    target = parse_refer_to(values[0])
    referrers = request.header_values("Referred-By")
    if len(referrers) > 1:
        raise sip.SipError("REFER contains duplicate Referred-By")
    if not referrers:
        return target
    referred_by = referrers[0]
    sip.parse_sip_uri(sip.name_addr_uri(referred_by))
    body_headers = tuple((key, value) for key, value in request.headers
                         if key.casefold() in {"content-id", "content-transfer-encoding", "content-disposition"})
    # Inspect MIME structure for the referenced entity, but forward the original
    # bytes: decoding and re-encoding a signed token can invalidate its signature.
    suffix = referred_by[referred_by.rfind(">") + 1:] if ">" in referred_by else referred_by
    _head, parameters = sip._split_semicolon_params(suffix)
    cid = dict(parameters).get("cid")
    if cid:
        content_type = request.header("Content-Type")
        raw_headers = [("Content-Type", content_type), *body_headers]
        entity = BytesParser(policy=email_policy).parsebytes(
            ("\r\n".join(f"{key}: {value}" for key, value in raw_headers) + "\r\n\r\n").encode() + request.body)
        wanted = cid.strip('"<>')
        if not request.body or not any(str(part.get("Content-ID", "")).strip("<>") == wanted for part in entity.walk()):
            raise sip.SipError("Referred-By token is missing")
        return replace(target, referred_by=referred_by, referral_content_type=content_type,
                       referral_body=request.body, referral_body_headers=body_headers)
    return replace(target, referred_by=referred_by)


def parse_sipfrag_status(body: bytes | str) -> int:
    """Return the status code from a message/sipfrag status line."""

    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
    first_line = text.replace("\r\n", "\n").split("\n", 1)[0].strip()
    parts = first_line.split(None, 2)
    if len(parts) < 2 or parts[0].upper() != "SIP/2.0":
        raise sip.SipError("invalid message/sipfrag status line")
    try:
        status = int(parts[1])
    except ValueError as err:
        raise sip.SipError("invalid message/sipfrag status code") from err
    if not 100 <= status <= 699:
        raise sip.SipError("message/sipfrag status is outside SIP range")
    return status


@dataclass(frozen=True, slots=True)
class SipTargetDialog:
    """RFC 4538 identifiers expressed from the recipient's perspective."""

    call_id: str
    local_tag: str
    remote_tag: str

    def __str__(self):
        return f"{self.call_id};local-tag={self.local_tag};remote-tag={self.remote_tag}"


def request_target_dialog(request: sip.SipMessage) -> SipTargetDialog | None:
    values = request.header_values("Target-Dialog")
    if not values:
        return None
    if len(values) != 1:
        raise sip.SipError("duplicate Target-Dialog")
    call_id, parameters = sip._split_semicolon_params(values[0])
    names = [key for key, _ in parameters]
    params = dict(parameters)
    if (not call_id or any(char.isspace() for char in call_id) or len(names) != len(set(names))
            or not all(sip.is_sip_token(params.get(key) or "") for key in ("local-tag", "remote-tag"))):
        raise sip.SipError("invalid Target-Dialog")
    return SipTargetDialog(call_id, params["local-tag"], params["remote-tag"])


def refer_event_headers(cseq: int, *, terminal: bool) -> tuple[tuple[str, str], ...]:
    return (("Event", f"refer;id={cseq}"),
            ("Subscription-State", "terminated;reason=noresource" if terminal else "active;expires=60"))


async def run_refer_action(action, report, *, timeout: float | None = None) -> int:
    """Let the owned route supply its deadline independently of progress reporting."""
    progress = asyncio.create_task(report(100, False), name="voip-refer-progress")
    try:
        try:
            async with asyncio.timeout(timeout):
                status = int(await action())
        except TimeoutError:
            status = 408
        except asyncio.CancelledError:
            raise
        except Exception:
            import logging
            logging.getLogger(__name__).exception("SIP REFER operation failed")
            status = 500
        status = status if 200 <= status <= 699 else 500
        try:
            response = await progress
            if response is not None and 200 <= int(response.status_code or 0) < 300:
                await report(status, True)
        except (ConnectionError, OSError, TimeoutError):
            pass
        return status
    finally:
        if not progress.done():
            progress.cancel()
        await asyncio.gather(progress, return_exceptions=True)


class ReferNotifier:
    """One implicit REFER usage; unsubscribing never cancels its operation."""

    def __init__(self, dialog, refer_cseq, send, *, allow_missing_id=True):
        self.dialog = dialog
        self.refer_cseq = refer_cseq
        self.send = send
        self.allow_missing_id = allow_missing_id
        self.active = True
        self.status = 100
        self.expires_at = asyncio.get_running_loop().time() + 60
        self.lock = asyncio.Lock()
        self.timer = asyncio.get_running_loop().call_later(60, self._expire)
        self.pending = None
        self.task = None

    def _expire(self):
        self.active = False
        self._schedule(True, "timeout")

    def _schedule(self, terminal=False, reason="noresource"):
        if self.pending is not None and self.pending[0]:
            return
        self.pending = (terminal, reason)
        if self.task is None:
            self.task = asyncio.create_task(
                self._drain(), name="voip-refer-notify-refresh"
            )

    async def _drain(self):
        try:
            while self.pending is not None:
                terminal, reason = self.pending
                self.pending = None
                async with self.lock:
                    if self.active or terminal:
                        await self._send(self.status, terminal, reason)
        except Exception:
            import logging

            self.active = False
            logging.getLogger(__name__).exception(
                "REFER subscription notification failed"
            )
        finally:
            self.task = None

    async def _send(self, status, terminal, reason="noresource"):
        remaining = max(0, int(self.expires_at - asyncio.get_running_loop().time()))
        state = (
            f"terminated;reason={reason}" if terminal else f"active;expires={remaining}"
        )
        headers = (
            ("Event", f"refer;id={self.refer_cseq}"),
            ("Subscription-State", state),
        )
        phrase = (
            "Trying" if status < 200 else "OK" if status < 300 else "Transfer Failed"
        )
        response = await self.send(headers, f"SIP/2.0 {status} {phrase}\r\n".encode())
        if response is None or not 200 <= int(response.status_code or 0) < 300:
            self.active = False
        return response

    async def report(self, status, terminal):
        async with self.lock:
            self.status = status
            if not self.active:
                return sip.SipMessage(status_code=200, reason="OK")
            if terminal:
                self.active = False
                self.timer.cancel()
            return await self._send(status, terminal)

    def refresh(self, request):
        from .sip_dialog import DialogKey

        if DialogKey.from_uas_request(request) != self.dialog.key or not self.active:
            return 481, "Subscription Does Not Exist", ()
        events = request.header_values("Event")
        if len(events) != 1:
            return 400, "Bad Request", ()
        package, pairs = sip._split_semicolon_params(events[0])
        ids = [value for key, value in pairs if key == "id"]
        if package.casefold() != "refer":
            return 489, "Bad Event", ()
        if len(ids) > 1:
            return 400, "Bad Request", ()
        if (ids and ids[0] != str(self.refer_cseq)) or (
            not ids and not self.allow_missing_id
        ):
            return 481, "Subscription Does Not Exist", ()
        value = request.header("Expires").strip() or "60"
        if (
            not value.isascii()
            or not value.isdigit()
            or len(value) > 10
            or request.body
        ):
            return 400, "Bad Request", ()
        try:
            self.dialog.refresh(request)
        except sip.SipError:
            return 400, "Bad Request", ()
        except ValueError:
            return 500, "Server Internal Error", (("Retry-After", "1"),)
        expires = min(int(value), 60)
        self.timer.cancel()
        self.expires_at = asyncio.get_running_loop().time() + expires
        if expires:
            self.timer = asyncio.get_running_loop().call_later(expires, self._expire)
        else:
            self.active = False
        self._schedule(not expires, "timeout")
        return 200, "OK", (("Expires", str(expires)),)

    def cancel(self):
        self.active = False
        self.timer.cancel()
        if self.task is not None:
            self.task.cancel()

    async def close(self):
        self.cancel()
        task = self.task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
