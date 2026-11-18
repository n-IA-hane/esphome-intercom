"""SIP ua-profile discovery on the existing PBX owner, not a second PBX."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import ipaddress
import logging
import re
import secrets
import socket
import time

from homeassistant.exceptions import HomeAssistantError

from .core import sip
from .physical_phone import (
    discovery_enabled, draft_physical_subentry, phone_candidate_name, physical_phone_id,
    physical_phone_records, provisioning_enabled,
)
from .runtime_data import runtime_data

PNP_ADDRESS = "224.0.1.75"
PNP_PORT = 5060
_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class PhoneCandidate:
    phone_id: str
    mac: str
    host: str
    vendor: str = ""
    model: str = ""
    token: str = field(default="", repr=False)
    seen: float = field(default_factory=time.monotonic)
    flow_id: str = ""
    native_subentry: object | None = field(default=None, repr=False)
    pnp_host: str = field(default="", repr=False)
    _flow_task: asyncio.Task | None = field(default=None, repr=False)
    _flow_context: dict | None = field(default=None, repr=False)

    def public(self):
        return {"mac": self.mac, "host": self.host, "vendor": self.vendor, "model": self.model,
                **({"phone_id": self.phone_id} if self.phone_id else {})}

    def setup_data(self):
        """Allocate the native draft only when the user chooses management."""
        if self.native_subentry is None:
            data = {"mac": self.mac, "host": self.host}
            if self.token:
                data["provisioning_token"] = self.token
            self.native_subentry = draft_physical_subentry(data, title=phone_candidate_name(self.vendor, self.model))
            self.phone_id = physical_phone_id(self.native_subentry)
            self.token = self.native_subentry.data["provisioning_token"]
        return {**self.public(), "provisioning_token": self.token}


def pnp_identity(request, addr):
    """An unauthenticated announcement is only an inventory candidate."""
    if (request.method != "SUBSCRIBE" or request.header("Event").partition(";")[0].strip().lower() != "ua-profile"
            or not ipaddress.ip_address(addr[0]).is_private):
        raise ValueError("invalid_phone_discovery")
    _package, pairs = sip._split_semicolon_params(request.header("Event"))
    params = dict(pairs)
    # The shared SIP parser decodes URI user escapes once. Snom's documented
    # MAC%3a<address> therefore becomes MAC:<address>. Inspect only the whole
    # user or an explicit MAC parameter, never display names, hosts or tags.
    users = [sip.parse_sip_uri(sip.name_addr_uri(value)).user
             for value in (request.uri, request.header("From"), request.header("Contact")) if value]
    identities = set()
    for value in [*users, params.get("mac"), params.get("macaddress")]:
        value = str(value or "").strip().strip('"')
        if value[:4].casefold() == "mac:":
            value = value[4:]
        if re.fullmatch(r"(?i)([0-9a-f]{12}|(?:[0-9a-f]{2}:){5}[0-9a-f]{2}|(?:[0-9a-f]{2}-){5}[0-9a-f]{2})", value):
            identities.add(re.sub(r"[:-]", "", value).lower())
    if not identities:
        raise ValueError("phone_discovery_mac_missing")
    if len(identities) != 1:
        raise ValueError("phone_discovery_mac_conflict")
    return identities.pop(), str(params.get("vendor") or "").strip('"')[:64], str(params.get("model") or "").strip('"')[:80]


def dhcp_identity(info):
    """Recognize only the declared Snom360 DHCP shape, without contacting it."""
    if not all(isinstance(getattr(info, key, None), str) for key in ("macaddress", "ip", "hostname")):
        raise ValueError("invalid_phone_discovery")
    mac = re.sub(r"[:-]", "", str(info.macaddress)).lower()
    host = ipaddress.ip_address(info.ip)
    if (not re.fullmatch(r"000413[0-9a-f]{6}", mac)
            or not re.fullmatch(r"snom360(?:-[a-z0-9][a-z0-9._-]*)?", info.hostname.casefold())
            or host.version != 4 or not host.is_private or host.is_loopback
            or host.is_link_local or host.is_unspecified or host.is_multicast):
        raise ValueError("invalid_phone_discovery")
    return mac, host.compressed, "snom", "snom360"


class PhoneDiscovery:
    def __init__(self, hass, entry):
        self.hass, self.entry, self.runtime = hass, entry, entry.runtime_data
        self.candidates = {}
        self._membership = None
        self._transport = None
        self._tasks = set()
        self._closed = False
        self._start_lock = asyncio.Lock()
        self._dhcp_backfilled = False

    def owned(self):
        return (not self._closed and runtime_data(self.hass) is self.runtime
                and self.runtime.phone_discovery is self and discovery_enabled(self.entry))

    async def async_start(self):
        async with self._start_lock:
            dhcp_available = "dhcp" in self.hass.config.components
            if self.owned() and dhcp_available and not self._dhcp_backfilled:
                from homeassistant.components.dhcp import async_discovered_service_info

                known = async_discovered_service_info(self.hass)
                self._dhcp_backfilled = True
                for info in known:
                    self.observe_dhcp(info)
            try:
                await self._async_start_owned()
            except (OSError, HomeAssistantError) as error:
                self._close_transport()
                if not dhcp_available:
                    raise
                _LOGGER.warning("SIP phone discovery unavailable; native DHCP remains active (%s)", type(error).__name__)

    def _prune_candidates(self):
        for key, expired in tuple(self.candidates.items()):
            if time.monotonic() - expired.seen >= 86400:
                self._abort_candidate_flow(expired)
                if expired._flow_task is not None:
                    expired._flow_task.cancel()
                del self.candidates[key]

    def _schedule_flow(self, candidate):
        if candidate._flow_task is None or candidate._flow_task.done():
            task = candidate._flow_task = self.hass.async_create_task(self._start_flow(candidate), "voip-phone-discovery")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def observe_dhcp(self, info, *, start_flow=True):
        """Cache a native discovery hint, never a PnP delivery authorization."""
        if not self.owned():
            return None
        try:
            mac, host, vendor, model = dhcp_identity(info)
        except ValueError:
            return None
        if any(phone["mac"] == mac for phone in physical_phone_records(self.entry).values()):
            return None
        self._prune_candidates()
        candidate = self.candidates.get(mac)
        if candidate is None:
            if len(self.candidates) >= 64:
                return None
            candidate = self.candidates[mac] = PhoneCandidate("", mac, host, vendor, model)
        candidate.host, candidate.seen = host, time.monotonic()
        if start_flow:
            self._schedule_flow(candidate)
        return candidate

    async def _async_start_owned(self):
        if not self.owned():
            return
        from homeassistant.components import network
        manager = self.runtime.sip.component("udp_listener") if self.runtime.sip else None
        udp = getattr(manager, "udp_server", None)
        transport = getattr(udp, "transport", None)
        # Reconcile again when the existing SIP engine is ready. In particular,
        # never occupy its standard port before its own listener starts.
        if manager is None or transport is None:
            self._close_transport()
            return
        listener_transport, listener_port = transport, manager.port
        current_socket = transport.get_extra_info("socket")
        if current_socket is None:
            self._close_transport()
            return
        if listener_port == PNP_PORT and self._membership is not None and self._membership[0] is current_socket:
            return
        if listener_port != PNP_PORT and self._transport is not None and not self._transport.is_closing():
            return
        interface = await network.async_get_source_ip(self.hass, target_ip=PNP_ADDRESS)
        current_manager = self.runtime.sip.component("udp_listener") if self.runtime.sip else None
        if (not self.owned() or current_manager is not manager
                or current_manager.port != listener_port
                or getattr(getattr(current_manager, "udp_server", None), "transport", None) is not listener_transport):
            return
        self._close_transport()
        membership = socket.inet_aton(PNP_ADDRESS) + socket.inet_aton(interface)
        if listener_port == PNP_PORT:
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
            current_manager = self.runtime.sip.component("udp_listener") if self.runtime.sip else None
            if (not self.owned() or current_manager is not manager
                    or current_manager.port != listener_port
                    or getattr(getattr(current_manager, "udp_server", None), "transport", None) is not listener_transport):
                transport.close()
                return
            self._transport = transport
        except BaseException:
            sock.close()
            raise

    async def _start_flow(self, candidate):
        from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
        from homeassistant.data_entry_flow import UnknownFlow
        from .const import DOMAIN
        if not self.owned() or self.candidates.get(candidate.mac) is not candidate:
            return
        try:
            if candidate.flow_id:
                self.hass.config_entries.flow.async_get(candidate.flow_id)
                return
        except UnknownFlow:
            candidate.flow_id = ""
        context = candidate._flow_context = {"source": SOURCE_INTEGRATION_DISCOVERY,
            "title_placeholders": {"name": phone_candidate_name(candidate.vendor, candidate.model) or candidate.mac}}
        try:
            result = await self.hass.config_entries.flow.async_init(DOMAIN, context=context,
                data={"discovery_candidate": candidate.public()})
        except BaseException:
            self._abort_candidate_flow(candidate)
            raise
        if result.get("type") not in {"abort", "create_entry"}:
            candidate.flow_id = result["flow_id"]
        if not self.owned() or self.candidates.get(candidate.mac) is not candidate:
            self._abort_candidate_flow(candidate)

    def _abort_candidate_flow(self, candidate):
        """Abort only this collector's pending flow, including an init race."""
        if not candidate.flow_id and candidate._flow_context is None:
            return
        from homeassistant.data_entry_flow import UnknownFlow
        from .const import DOMAIN

        manager = self.hass.config_entries.flow
        flow_ids = {candidate.flow_id} if candidate.flow_id else set()
        if candidate._flow_context is not None:
            flow_ids.update(item["flow_id"] for item in manager.async_progress_by_handler(
                DOMAIN, include_uninitialized=True) if item.get("context") is candidate._flow_context)
        for flow_id in flow_ids:
            try:
                manager.async_abort(flow_id)
            except UnknownFlow:
                pass
        candidate.flow_id = ""

    async def handle(self, request, addr, transport):
        from .sip_listener import SipFollowUpRequest, SipRequestResult
        if not self.owned():
            return SipRequestResult(489, "Bad Event")
        try:
            mac, vendor, model = pnp_identity(request, addr)
        except (ValueError, sip.SipError):
            return SipRequestResult(400, "Bad Request")
        self._prune_candidates()
        phone = next((item for item in physical_phone_records(self.entry).values() if item["mac"] == mac), None)
        candidate = self.candidates.get(mac)
        if phone is not None and (candidate is None or candidate.pnp_host != addr[0] or candidate.host != addr[0]
                or candidate.phone_id != phone["phone_id"]
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
            candidate = self.candidates[mac] = PhoneCandidate("", mac, addr[0], vendor, model)
        candidate.seen, candidate.host = time.monotonic(), addr[0]
        if phone is None:
            # Only an actual PnP request before adoption can bind delivery.
            # DHCP can update the inventory hint without granting this right.
            candidate.pnp_host = addr[0]
            if vendor:
                candidate.vendor = vendor
            if model:
                candidate.model = model
            self._schedule_flow(candidate)
            # Admission is not adoption. Neither an enabled provisioner nor a
            # guessed MAC authorizes sending this private URL to an unknown UA.
            return SipRequestResult(200, headers=(("Expires", "0"),), to_tag=secrets.token_hex(8))
        manager = self.runtime.physical_phones
        if (not provisioning_enabled(self.entry) or manager is None or not manager.owned()
                or not phone.get("enabled", True)):
            return SipRequestResult(200, headers=(("Expires", "0"),), to_tag=secrets.token_hex(8))
        manager.schedule({phone["phone_id"]})
        from .physical_phone_provisioning import phone_provisioning_url

        try:
            url = phone_provisioning_url(self.hass, self.entry, phone)
        except Exception:
            # Discovery is still useful while the administrator supplies an
            # HTTPS provisioning origin. No credentials are sent as a fallback.
            return SipRequestResult(200, headers=(("Expires", "0"),), to_tag=secrets.token_hex(8))
        notify = SipFollowUpRequest("NOTIFY", headers=(("Event", request.header("Event")),
            ("Subscription-State", "terminated;reason=timeout")), body=url.encode(), content_type="application/url")
        return SipRequestResult(200, headers=(("Expires", "0"),), to_tag=secrets.token_hex(8), follow_up=notify)

    def _close_transport(self):
        """Release only discovery I/O, preserving candidate adoption bindings."""
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

    async def async_close(self):
        self._closed = True
        self._close_transport()
        for candidate in self.candidates.values():
            self._abort_candidate_flow(candidate)
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        for candidate in self.candidates.values():
            self._abort_candidate_flow(candidate)
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
        task = self.owner.hass.async_create_task(self._respond(request, addr), "voip-pnp-response")
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
