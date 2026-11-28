"""Request bytes and ordered next hops for one outbound initial INVITE."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Self

from .core import sdp, sip
from .core.sip_transport import SIP_UDP_SAFE_REQUEST_BYTES, udp_fallback_allowed_before_send


if TYPE_CHECKING:
    from .core.sip_resolution import SipServerResolver


_LOGGER = logging.getLogger(f"{__package__}.sip_client")


@dataclass(slots=True)
class InviteExchange:
    """Transient request data; the SIP client owns the transport, transaction and dialog."""

    target: str
    host: str
    port: int
    request_uri: str
    endpoints: tuple[tuple[str, int, str], ...] = ()
    next_index: int = 0
    selection_errors: list[OSError | RuntimeError] = field(default_factory=list)
    request: bytes = b""
    udp_fallback_used: bool = False

    @classmethod
    def for_target(
        cls, target: str, host: str, port: int, request_uri: str, transport: str
    ) -> Self:
        uri = request_uri or str(
            sip.SipUri(
                target, host, int(port), params=(("transport", transport.lower()),)
            )
        )
        return cls(target, host, int(port), uri)

    @staticmethod
    def compose_request(
        *, request_uri: str, route_set: Iterable[str], local_uri: str,
        remote_uri: str, dialog: sip.SipDialogIds, contact_uri: str,
        body: bytes, transport: str, via_sent_by: tuple[str, int],
        local_name: str, target_display_name: str, answer_mode: str = "",
        max_forwards: int = 70,
        authorization_headers: Iterable[tuple[str, str]] = (),
    ) -> bytes:
        """Compose current transaction facts without changing client or dialog state."""
        routing = sip.dialog_request_routing(request_uri, route_set)
        headers = sip.dialog_headers(
            request_uri=routing.request_uri,
            local_uri=local_uri,
            remote_uri=remote_uri,
            dialog=dialog,
            method="INVITE",
            contact_uri=contact_uri or local_uri,
            content_type="application/sdp" if body else None,
            transport=transport,
            via_sent_by=via_sent_by,
            local_display_name=local_name,
            remote_display_name=target_display_name,
            max_forwards=max_forwards,
        )
        headers.extend(("Route", value) for value in routing.route_headers)
        if answer_mode:
            headers.append(("Answer-Mode", answer_mode))
            if answer_mode == "Auto":
                # Explicit intercom requests also support legacy SIP phones.
                headers.extend([
                    ("Call-Info", f"<{local_uri}>;answer-after=0"),
                    ("Alert-Info", "<http://127.0.0.1>;info=alert-autoanswer;delay=0"),
                ])
        headers.extend(authorization_headers)
        return sip.build_request("INVITE", routing.request_uri, headers, body)

    def next_hop(self, outbound_proxy: str, *, transport: str = "") -> sip.SipUri:
        """Resolve the signaling route separately from the called address."""
        logical = sip.parse_sip_uri(self.request_uri)
        if outbound_proxy:
            proxy = str(outbound_proxy).strip()
            route = sip.parse_sip_uri(
                proxy if proxy.lower().startswith(("sip:", "sips:")) else f"sip:{proxy}"
            )
        elif self.host != logical.host:
            route = sip.SipUri("", self.host, self.port)
        else:
            route = logical
        if not transport:
            return route
        params = tuple(
            (key, value)
            for key, value in route.params
            if key.strip().lower() != "transport"
        )
        return replace(route, params=(*params, ("transport", transport.lower())))

    def replace_endpoints(self, endpoints: tuple[tuple[str, int, str], ...]) -> None:
        self.endpoints = endpoints
        self.next_index = 0

    async def resolve(
        self, resolver: SipServerResolver, route: sip.SipUri, transport: str
    ) -> None:
        targets = await resolver.resolve(route, transport=transport)
        self.replace_endpoints(
            tuple(endpoint for target in targets for endpoint in target.endpoints())
        )

    def needs_tcp(self, transport: str) -> bool:
        return (
            transport == "UDP"
            and len(self.request) > SIP_UDP_SAFE_REQUEST_BYTES
            and not self.udp_fallback_used
        )

    def tcp_setup_allows_udp_fallback(self) -> bool:
        return bool(self.selection_errors) and all(
            udp_fallback_allowed_before_send(error) for error in self.selection_errors
        )

    def selection_error(self, transport: str) -> OSError | RuntimeError:
        return (
            self.selection_errors[-1]
            if self.selection_errors
            else OSError(f"no SIP {transport} target")
        )

    async def select_next(
        self,
        transport: str,
        call_id: str,
        connect: Callable[[str, int], Awaitable[int]],
    ) -> bool:
        """Try each remaining matching address through the client's existing transport."""
        self.selection_errors.clear()
        while self.next_index < len(self.endpoints):
            selected_host, selected_port, selected_transport = self.endpoints[
                self.next_index
            ]
            self.next_index += 1
            if selected_transport != transport:
                continue
            started = asyncio.get_running_loop().time()
            _LOGGER.debug(
                "SIP transport attempt call_id=%s transport=%s remote=%s:%s",
                call_id,
                selected_transport,
                selected_host,
                selected_port,
            )
            try:
                local_port = await connect(selected_host, selected_port)
                _LOGGER.debug(
                    "SIP transport ready call_id=%s transport=%s remote=%s:%s local_port=%s elapsed_ms=%.1f",
                    call_id,
                    selected_transport,
                    selected_host,
                    selected_port,
                    local_port,
                    (asyncio.get_running_loop().time() - started) * 1000,
                )
                return True
            except (ConnectionError, OSError, RuntimeError) as err:
                self.selection_errors.append(err)
                _LOGGER.info(
                    "SIP initial target unavailable %s:%s transport=%s call_id=%s "
                    "error_type=%s errno=%s elapsed_ms=%.1f error=%s",
                    selected_host,
                    selected_port,
                    selected_transport,
                    call_id,
                    type(err).__name__,
                    getattr(err, "errno", None),
                    (asyncio.get_running_loop().time() - started) * 1000,
                    err,
                )
        return False

    def log_sent(self, body: bytes, transport: str) -> None:
        offered_audio = []
        offered_dtmf = []
        offered_video = []
        if body.strip():
            offered_audio = [
                item
                for item in sdp.offered_media_descriptions(body)
                if "TELEPHONE-EVENT" not in item
            ]
            offered_dtmf = [
                f"pt={item.payload_type}:TELEPHONE-EVENT/{item.sample_rate}"
                for item in sdp.offered_dtmf_formats(body)
            ]
            offered_video = [
                item.wire_token() for item in sdp.offered_video_formats(body)
            ]
        _LOGGER.info(
            "SIP TX INVITE %s@%s:%s bytes=%s transport=%s "
            "audio=[%s] dtmf=[%s] video=[%s]",
            self.target,
            self.host,
            self.port,
            len(self.request),
            transport,
            ", ".join(offered_audio) if body else "delayed",
            ", ".join(offered_dtmf) if body else "delayed",
            ", ".join(offered_video) if body else "delayed",
        )
