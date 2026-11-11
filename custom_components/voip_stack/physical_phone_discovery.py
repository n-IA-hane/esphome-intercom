"""SIP ua-profile discovery on the existing PBX owner, not a second PBX."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import ipaddress
import re
import secrets
import socket
import time
from uuid import uuid4

from .core import sip
from .physical_phone import physical_phone_records

PNP_ADDRESS = "224.0.1.75"
PNP_PORT = 5060


@dataclass(slots=True)
class PhoneCandidate:
    phone_id: str
    mac: str
    host: str
    vendor: str = ""
    model: str = ""
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32), repr=False)
    seen: float = field(default_factory=time.monotonic)
    flow_id: str = ""

    def public(self):
        return {"phone_id": self.phone_id, "mac": self.mac, "host": self.host,
                "vendor": self.vendor, "model": self.model}

    def setup_data(self):
        return {**self.public(), "provisioning_token": self.token}


def pnp_identity(request, addr):
    """An unauthenticated announcement is only an inventory candidate."""
    if (request.method != "SUBSCRIBE" or request.header("Event").partition(";")[0].strip().lower() != "ua-profile"
            or not ipaddress.ip_address(addr[0]).is_private):
        raise ValueError("invalid_phone_discovery")
    _package, pairs = sip._split_semicolon_params(request.header("Event"))
    params = dict(pairs)
    strings = [request.uri, request.header("From"), request.header("Contact"),
               params.get("mac", ""), params.get("macaddress", "")]
    mac = ""
    for value in strings:
        match = re.search(r"(?i)(?:^|[^0-9a-f])([0-9a-f]{12}|(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2})(?:[^0-9a-f]|$)", value)
        if match:
            mac = re.sub(r"[:-]", "", match[1]).lower()
            break
    if not mac:
        raise ValueError("phone_discovery_mac_missing")
    return mac, str(params.get("vendor", "")).strip('"')[:64], str(params.get("model", "")).strip('"')[:80]


class PhoneDiscovery:
    def __init__(self, owner):
        self.owner = owner
        self.candidates = {}
        self._membership = None
        self._transport = None
        self._tasks = set()
        self._closed = False

    async def async_start(self):
        if self._closed:
            return
        from homeassistant.components import network
        manager = self.owner.runtime.sip.component("udp_listener") if self.owner.runtime.sip else None
        udp = getattr(manager, "udp_server", None)
        transport = getattr(udp, "transport", None)
        interface = await network.async_get_source_ip(self.owner.hass, target_ip=PNP_ADDRESS)
        if self._closed:
            return
        membership = socket.inet_aton(PNP_ADDRESS) + socket.inet_aton(interface)
        if manager is not None and manager.port == PNP_PORT and transport is not None:
            sock = transport.get_extra_info("socket")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            self._membership = (sock, membership)
            return
        # A non-default main SIP port still needs only a ua-profile transport
        # adapter on the standardized discovery port. No call state lives here.
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", PNP_PORT))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
            sock.setblocking(False)
            transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
                lambda: _PnpTransport(self, interface), sock=sock)
            if self._closed:
                transport.close()
                return
            self._transport = transport
        except BaseException:
            sock.close()
            raise

    async def _start_flow(self, candidate):
        from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
        from .const import DOMAIN
        if self._closed:
            return
        try:
            if candidate.flow_id:
                self.owner.hass.config_entries.flow.async_get(candidate.flow_id)
                return
        except Exception:
            candidate.flow_id = ""
        result = await self.owner.hass.config_entries.flow.async_init(DOMAIN,
            context={"source": SOURCE_INTEGRATION_DISCOVERY,
                     "title_placeholders": {"name": (candidate.vendor + " " + candidate.model).strip() or candidate.mac}},
            data={"discovery_candidate": candidate.public()})
        if result.get("type") != "abort":
            candidate.flow_id = result["flow_id"]

    async def handle(self, request, addr, transport):
        from .sip_listener import SipFollowUpRequest, SipRequestResult
        from .physical_phone_provisioning import phone_provisioning_url
        if self._closed or not self.owner.owned():
            return SipRequestResult(489, "Bad Event")
        try:
            mac, vendor, model = pnp_identity(request, addr)
        except (ValueError, sip.SipError):
            return SipRequestResult(400, "Bad Request")
        self.candidates = {key: value for key, value in self.candidates.items()
                           if time.monotonic() - value.seen < 86400}
        phone = next((item for item in physical_phone_records(self.owner.entry).values() if item["mac"] == mac), None)
        candidate = self.candidates.get(mac)
        if phone is not None and (candidate is None or candidate.host != addr[0]
                or not secrets.compare_digest(candidate.token, phone["provisioning_token"])):
            # A MAC is an inventory hint, never a credential. Existing URLs
            # cannot be recovered by an unauthenticated announcement after a
            # restart, token rotation, or from another source.
            return SipRequestResult(200, headers=(("Expires", "0"),), to_tag=secrets.token_hex(8))
        if candidate is not None and candidate.host != addr[0]:
            return SipRequestResult(403, "Forbidden")
        if candidate is None:
            if len(self.candidates) >= 64:
                return SipRequestResult(503, "Service Unavailable")
            candidate = self.candidates[mac] = PhoneCandidate(uuid4().hex, mac, addr[0], vendor, model)
        candidate.seen, candidate.host = time.monotonic(), addr[0]
        if phone is None:
            task = self.owner.hass.async_create_task(self._start_flow(candidate), "voip-phone-discovery")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            phone = {"phone_id": candidate.phone_id, "provisioning_token": candidate.token}
        else:
            self.owner.schedule({phone["phone_id"]})
        try:
            url = phone_provisioning_url(self.owner.hass, self.owner.entry, phone)
        except Exception:
            # Discovery is still useful while the administrator supplies an
            # HTTPS provisioning origin. No credentials are sent as a fallback.
            return SipRequestResult(200, headers=(("Expires", "0"),), to_tag=secrets.token_hex(8))
        notify = SipFollowUpRequest("NOTIFY", headers=(("Event", request.header("Event")),
            ("Subscription-State", "terminated;reason=timeout")), body=url.encode(), content_type="application/url")
        return SipRequestResult(200, headers=(("Expires", "0"),), to_tag=secrets.token_hex(8), follow_up=notify)

    async def async_close(self):
        self._closed = True
        if self._membership is not None:
            sock, membership = self._membership
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, membership)
            except OSError:
                pass
            self._membership = None
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        self.candidates.clear()


class _PnpTransport(asyncio.DatagramProtocol):
    """Only discovery framing; the shared SIP parser remains authoritative."""
    def __init__(self, owner, interface):
        self.owner, self.interface, self.transport = owner, interface, None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, raw, addr):
        try:
            request = sip.parse_message(raw)
        except sip.SipError:
            return
        if request.is_response or request.method != "SUBSCRIBE":
            return
        task = self.owner.owner.hass.async_create_task(self._respond(request, addr), "voip-pnp-response")
        self.owner._tasks.add(task)
        task.add_done_callback(self.owner._tasks.discard)

    async def _respond(self, request, addr):
        result = await self.owner.handle(request, addr, "UDP")
        to = request.header("To") + (";tag=" + result.to_tag if result.to_tag else "")
        headers = [(key, request.header(key)) for key in ("Via", "From", "Call-ID", "CSeq")]
        headers.extend((("To", to), *result.headers))
        self.transport.sendto(sip.build_response(result.status, result.reason, headers), addr)
        if result.follow_up is None:
            return
        notify = result.follow_up
        headers = [("Via", f"SIP/2.0/UDP {self.interface}:5060;branch=z9hG4bK{secrets.token_hex(8)};rport"),
            ("From", to), ("To", request.header("From")), ("Call-ID", request.header("Call-ID")),
            ("CSeq", "1 NOTIFY"), ("Contact", f"<sip:provision@{self.interface}:5060>"),
            ("Content-Type", notify.content_type), *notify.headers]
        target = sip.name_addr_uri(request.header("Contact")) or request.uri
        self.transport.sendto(sip.build_request("NOTIFY", target, headers, notify.body), addr)
