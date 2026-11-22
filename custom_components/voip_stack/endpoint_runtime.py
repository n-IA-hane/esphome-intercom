"""Construct, bind and publish the Home Assistant SIP endpoint."""

from __future__ import annotations

from functools import partial
import logging
import time
import uuid

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_call_later

from .assist_endpoint import AssistEndpoint
from .config_entry_runtime import async_refresh_and_push_phonebook as _refresh_and_push_phonebook
from .codec_settings import effective_codec_policy
from .const import CONF_REGISTRAR_ENABLED, CONF_SIP_VIDEO, CONF_VIDEO_CAMERA_SEND, CONF_VIDEO_TRANSCODING
from .core.audio_format import HA_SIP_PCM_FORMATS, HA_SIP_PCM_RX_FORMATS, HA_SIP_PCM_TX_FORMATS
from .dtmf_events import handle_sip_info
from .endpoint_dialing import EndpointDialer
from .endpoint_lifecycle import async_stop_sip_endpoint, call_registry as _call_registry, create_runtime_task
from .endpoint_routing import EndpointRouteResolver
from .endpoint_termination import EndpointTerminationHandler
from .runtime_data import endpoint_directory, require_runtime_data, sip_endpoint_manager
from .sip_ingress import SipIngressPolicy, classify_trunk_invite
from .store import sip_accounts as _sip_accounts

_LOGGER = logging.getLogger(__name__)
_classify_trunk_invite = classify_trunk_invite


class _RegistrarExpiryScheduler:
    """Own the single timer for the nearest SIP Contact expiry."""

    def __init__(self, hass: HomeAssistant, registrar) -> None:
        self._hass = hass
        self._registrar = registrar
        self._cancel = None

    def reschedule(self) -> None:
        self.close()
        expires_at = self._registrar.next_expiration_at()
        if expires_at is None:
            return
        self._cancel = async_call_later(
            self._hass,
            max(0.0, expires_at - time.time()),
            self._expire,
        )

    @callback
    def _expire(self, _now) -> None:
        self._cancel = None
        self._registrar.expire()
        self.reschedule()

    def close(self) -> None:
        cancel = self._cancel
        self._cancel = None
        if cancel is not None:
            cancel()


async def async_start_sip_endpoint(hass: HomeAssistant) -> bool:
    """Bind the enabled SIP signaling listeners for HA softphone and bridge calls."""
    from .config import transport_config
    from .core.sip import parse_sip_uri
    from .endpoint_call_routing import compose_call_routing
    from .peer_snapshot import async_advertise_host
    from .sip_application import SipApplicationMethods
    from .sip_endpoint import SipEndpointManager
    from .sip_registrar import SipRegistrar
    from .sip_runtime import enable_reused_tcp_connection, uri_transport
    from .softphone_termination import async_terminate_sip_bridge_session
    from .store import config_entry

    if sip_endpoint_manager(hass) is not None:
        _LOGGER.debug("Stopping existing SIP endpoint before rebinding listeners")
        await async_stop_sip_endpoint(hass)

    cfg = transport_config(hass)
    local_ip = await async_advertise_host(hass)
    if not local_ip:
        _LOGGER.error("Cannot start SIP endpoint: HA announce IP is unknown")
        return False

    def _on_registration_change(username: str, registered: bool) -> None:
        from .account_runtime import recompute_account_availability

        endpoint_registry = endpoint_directory(hass)
        endpoint = endpoint_registry.by_username(username)
        if endpoint is not None:
            recompute_account_availability(hass, endpoint.endpoint_id)
        create_runtime_task(hass, _refresh_and_push_phonebook(hass))

    accounts = _sip_accounts(hass)
    entry = config_entry(hass)
    registrar = SipRegistrar(
        enabled=bool(cfg.get(CONF_REGISTRAR_ENABLED, False)),
        accounts=accounts,
        local_ip=local_ip,
        local_sip_port=int(cfg["sip_port"]),
        on_registration_change=_on_registration_change,
    )
    runtime = require_runtime_data(hass)
    # Call control and its HA projection share one runtime owner.  It remains
    # dark until every listener component has transferred into it.
    pbx_runtime = _call_registry(hass)
    pbx_runtime.attach_component("registrar", registrar)
    if entry is not None:
        from .registrar_storage import RegistrarLeaseStore

        leases = RegistrarLeaseStore(hass, registrar, entry.entry_id)
        await leases.async_restore()
        pbx_runtime.attach_component("registrar_leases", leases, closer=leases.async_close)
    registrar_expiry = _RegistrarExpiryScheduler(hass, registrar)
    registrar_expiry.reschedule()
    pbx_runtime.attach_component(
        "registrar_expiry",
        registrar_expiry,
        closer=registrar_expiry.close,
    )
    route_resolver = EndpointRouteResolver(
        hass=hass,
        local_ip=local_ip,
        sip_port=int(cfg["sip_port"]),
    )

    async def _on_register(request, addr, transport):
        result = await registrar.handle_register(request, addr, transport)
        if 200 <= int(result.status) < 300:
            registrar_expiry.reschedule()
            # Admit the SIP transaction independently of directory and device I/O.
            coordinator = runtime.phonebook
            if coordinator is not None:
                coordinator.request_refresh()
            from .sip_capabilities import schedule_capability_probe

            try:
                username = parse_sip_uri(request.header("To")).user
            except ValueError:
                username = ""
            if username:
                schedule_capability_probe(hass, username)
        return result

    def _on_flow_closed(host: str, port: int, transport: str) -> None:
        if not registrar.remove_flow(host, port, transport):
            return
        registrar_expiry.reschedule()
        create_runtime_task(hass, _refresh_and_push_phonebook(hass))

    _on_info = partial(handle_sip_info, hass)

    application_methods = SipApplicationMethods(hass, registrar)
    if entry is not None:
        application_methods.gruu_id = uuid.uuid5(uuid.NAMESPACE_URL, f"voip-stack:{entry.entry_id}").hex
    pbx_runtime.attach_component(
        "sip_application",
        application_methods,
        closer=application_methods.stop,
    )

    assist_endpoint = AssistEndpoint(
        hass=hass,
        terminate_sip_bridge=async_terminate_sip_bridge_session,
    )

    endpoint_dialer = EndpointDialer(
        hass=hass,
        local_ip=local_ip,
        config=cfg,
        route_resolver=route_resolver,
        sip_uri_transport=uri_transport,
        enable_reused_tcp_connection=enable_reused_tcp_connection,
    )
    pbx_runtime.adopt_component("endpoint_dialer", endpoint_dialer)

    ingress = SipIngressPolicy(hass, cfg, local_ip, registrar)
    routing = compose_call_routing(
        dialer=endpoint_dialer, ingress=ingress, assist=assist_endpoint, calls=pbx_runtime, integration=runtime,
    )
    pbx_runtime.ring_conference_members_from_ha = routing.groups.ring_conference_from_ha
    pbx_runtime.start_ring_group_from_ha = routing.groups.start_ring_group_from_ha

    endpoint_termination = EndpointTerminationHandler(hass)
    supported_formats = list(HA_SIP_PCM_FORMATS)
    endpoint = SipEndpointManager(
        host="0.0.0.0",
        port=int(cfg["sip_port"]),
        local_ip=local_ip,
        local_rtp_port=int(cfg["rtp_port"]),
        supported_formats=supported_formats,
        supported_send_formats=list(HA_SIP_PCM_TX_FORMATS),
        supported_recv_formats=list(HA_SIP_PCM_RX_FORMATS),
        on_invite=routing.on_invite,
        on_offerless_invite=routing.on_offerless_invite,
        codec_policy_resolver=ingress.codec_policy_for_invite,
        codec_policy=effective_codec_policy(hass),
        invite_identity_resolver=ingress.identity_for_invite,
        invite_authorizer=ingress.authorize_initial_invite,
        on_terminated=endpoint_termination.handle,
        on_register=_on_register,
        on_info=_on_info,
        on_media_update=routing.on_media_update,
        on_refer=routing.on_refer,
        on_request=application_methods.handle,
        request_router=application_methods,
        on_flow_closed=_on_flow_closed,
        udp_enabled=True,
        tcp_enabled=True,
        enable_video=bool(cfg.get(CONF_SIP_VIDEO, False)),
        enable_video_transcoding=bool(cfg.get(CONF_VIDEO_TRANSCODING, False)),
        prefer_browser_video_send=bool(cfg.get(CONF_VIDEO_CAMERA_SEND, False)),
    )
    # Atomic ownership cutover: the runtime and registry are authoritative
    # before either listener can dispatch its first INVITE.  The two component
    # names expose both transports while only one closer stops their shared
    # SipEndpointManager instance.
    pbx_runtime.attach_component("tcp_listener", endpoint)
    pbx_runtime.attach_component("udp_listener", endpoint, closer=endpoint.stop)
    pbx_runtime.refer_call = routing.on_refer
    pbx_runtime.activate()
    runtime.sip = pbx_runtime
    try:
        started = await endpoint.start()
    except BaseException:
        await pbx_runtime.shutdown()
        if runtime.sip is pbx_runtime:
            runtime.sip = None
        raise
    if not started:
        await pbx_runtime.shutdown()
        if runtime.sip is pbx_runtime:
            runtime.sip = None
        return False
    pbx_runtime.forward_call = routing.forward_call
    _LOGGER.info(
        "SIP endpoint enabled on UDP+TCP/%s (RTP base %s)",
        cfg["sip_port"],
        cfg["rtp_port"],
    )
    return True
