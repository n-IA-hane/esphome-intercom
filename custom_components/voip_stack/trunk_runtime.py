"""SIP trunk lifecycle for VoIP Stack."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant

from .config import normalized_trunk_config, transport_config, trunk_config, trunk_enabled
from .const import (
    CONF_TRUNK_REGISTER,
    CONF_TRUNK_ALLOWED_IPS,
    CONF_TRUNK_AUTH_USERNAME,
    CONF_TRUNK_DOMAIN,
    CONF_TRUNK_EXPIRES,
    CONF_TRUNK_OUTBOUND_PROXY,
    CONF_TRUNK_PASSWORD,
    CONF_TRUNK_PORT,
    CONF_TRUNK_SERVER,
    CONF_TRUNK_TRANSPORT,
    CONF_TRUNK_USERNAME,
)
from .session_cleanup import async_wait_for_cleanup
from .runtime_data import sip_endpoint_manager, sip_endpoint_runtime, sip_trunks, runtime_data

_LOGGER = logging.getLogger(__name__)


def trunk_client_config(cfg: dict):
    """Normalize the signaling settings which require a new client."""
    from .sip_trunk import SipTrunkConfig

    return SipTrunkConfig(
        enabled=True,
        register=bool(cfg[CONF_TRUNK_REGISTER]),
        allowed_ips=tuple(cfg[CONF_TRUNK_ALLOWED_IPS]),
        transport=str(cfg[CONF_TRUNK_TRANSPORT]),
        server=str(cfg[CONF_TRUNK_SERVER]),
        port=int(cfg[CONF_TRUNK_PORT]),
        domain=str(cfg[CONF_TRUNK_DOMAIN]),
        username=str(cfg[CONF_TRUNK_USERNAME]),
        auth_username=str(cfg[CONF_TRUNK_AUTH_USERNAME]),
        password=str(cfg[CONF_TRUNK_PASSWORD]),
        expires=int(cfg[CONF_TRUNK_EXPIRES]),
        outbound_proxy=str(cfg[CONF_TRUNK_OUTBOUND_PROXY]),
        registration_id=int(cfg.get("trunk_registration_id", 1)),
        outbound_media_address=str(cfg.get("trunk_outbound_media_address") or ""),
        media_nat_mode=str(cfg.get("trunk_media_nat") or ""),
        audio_codecs=tuple(cfg["trunk_audio_codecs"]) if "trunk_audio_codecs" in cfg else None,
        video_codecs=tuple(cfg["trunk_video_codecs"]) if "trunk_video_codecs" in cfg else None,
    )


def create_trunk_client(hass: HomeAssistant, cfg: dict, *, local_ip: str, trunk_id: str = ""):
    """Construct one client for startup or validation; the caller owns adoption."""
    from .sip_trunk import SipTrunkClient

    from homeassistant.helpers.dispatcher import async_dispatcher_send
    from .runtime_data import sip_trunk
    from .trunk_config import TRUNK_UPDATE_SIGNAL

    client = SipTrunkClient(
        config=trunk_client_config(cfg), local_ip=local_ip,
        local_sip_port=int(transport_config(hass)["sip_port"]), trunk_id=trunk_id,
    )

    def changed():
        if sip_trunk(hass, trunk_id or None) is client:
            async_dispatcher_send(hass, TRUNK_UPDATE_SIGNAL, trunk_id)

    client.on_state_change = changed
    def media_stun_urls():
        from homeassistant.components.web_rtc import async_get_ice_servers
        urls = []
        for server in async_get_ice_servers(hass):
            values = server.urls
            urls.extend([values] if isinstance(values, str) else values)
        return tuple(url for url in urls if url.startswith("stun:"))

    client.media_stun_urls = media_stun_urls
    return client


async def async_start_sip_trunk(hass: HomeAssistant, *, local_ip: str) -> bool:
    runtime = runtime_data(hass)
    from .store import config_entry
    from .trunk_config import clear_removed_trunk_default
    from .repairs import async_sync_trunk_default_issue
    entry = config_entry(hass)
    if runtime is not None and entry is not None:
        clear_removed_trunk_default(hass, entry, runtime.trunk_configs)
        if runtime.default_trunk_id and runtime.default_trunk_id not in runtime.trunk_configs:
            runtime.default_trunk_id = ""
            runtime.trunk_config = normalized_trunk_config({})
        async_sync_trunk_default_issue(hass, runtime.trunk_configs, runtime.default_trunk_id)
    configs = runtime.trunk_configs if runtime is not None and runtime.trunk_configs else {"": trunk_config(hass)}
    enabled = {identity: cfg for identity, cfg in configs.items() if trunk_enabled(cfg)}
    if not enabled:
        return True
    if not local_ip:
        _LOGGER.warning("SIP trunks disabled: HA advertise IP is unknown")
        return False
    endpoint = sip_endpoint_manager(hass)
    pbx_runtime = sip_endpoint_runtime(hass)
    if pbx_runtime is None:
        return False
    for identity, cfg in enabled.items():
        trunk = create_trunk_client(hass, cfg, local_ip=local_ip, trunk_id=identity)
        if endpoint is not None:
            trunk.attach_endpoint_manager(endpoint)
        pbx_runtime.adopt_component(f"trunk:{identity}" if identity else "trunk", trunk, closer=trunk.stop)
        try:
            await trunk.start()
        except Exception as err:
            _LOGGER.warning("SIP trunk startup failed id=%s: %s", identity, err)
    return True


async def async_stop_sip_trunk(hass: HomeAssistant) -> None:
    pbx_runtime = sip_endpoint_runtime(hass)
    if pbx_runtime is None:
        return
    task = pbx_runtime.trunk_stop_task
    if not isinstance(task, asyncio.Task) or task.done():
        task = asyncio.create_task(
            _async_stop_sip_trunk(hass),
            name="voip-sip-trunk-runtime-stop",
        )
        pbx_runtime.trunk_stop_task = task
    try:
        await async_wait_for_cleanup(task)
    finally:
        if task.done() and pbx_runtime.trunk_stop_task is task:
            pbx_runtime.trunk_stop_task = None


def trunk_has_calls(hass: HomeAssistant, identity: str, client) -> bool:
    """Use existing dialogs/session metadata before replacing signaling settings."""
    runtime = sip_endpoint_runtime(hass)
    return bool(
        getattr(client, "_outbound_dialogs", {})
        or getattr(getattr(client, "inbound_endpoint", None), "active_dialogs", {})
        or (runtime is not None and any(
            session.live and (session.metadata.get("inbound_trunk_id") == identity
                              or any(leg.trunk_id == identity for leg in session.legs.values()))
            for session in runtime.sessions.values()
        ))
    )


async def async_reconcile_trunks(hass: HomeAssistant, entry) -> None:
    """Adopt validated replacements without reloading other phones or trunks."""
    from .peer_snapshot import async_advertise_host
    from .trunk_config import entry_trunk_configs, entry_default_trunk, sync_trunk_devices, clear_removed_trunk_default

    runtime = runtime_data(hass)
    if runtime is None or runtime.sip is None:
        return
    async with runtime.trunk_update_lock:
        configs = entry_trunk_configs(entry)
        clear_removed_trunk_default(hass, entry, configs)
        default = entry_default_trunk(entry, configs)
        from .repairs import async_sync_trunk_default_issue
        async_sync_trunk_default_issue(hass, configs, default)
        sync_trunk_devices(hass, entry)
        if configs == runtime.trunk_configs and default == runtime.default_trunk_id:
            return
        local_ip = await async_advertise_host(hass)
        owner = runtime.sip
        clients = sip_trunks(hass)
        planned = []
        prepared = []
        try:
            for identity, cfg in configs.items():
                old = clients.pop(identity, None)
                if old is not None and trunk_enabled(cfg) and old.config == trunk_client_config(cfg):
                    continue
                pending_name = f"pending_trunk:{identity}"
                candidate = owner.component(pending_name)
                if candidate is not None and (not trunk_enabled(cfg) or candidate.config != trunk_client_config(cfg)):
                    await candidate.stop()
                    owner.release_component(pending_name, candidate)
                    candidate = None
                if trunk_enabled(cfg) and candidate is None:
                    candidate = create_trunk_client(hass, cfg, local_ip=local_ip, trunk_id=identity)
                    owner.adopt_component(pending_name, candidate, closer=candidate.stop)
                    prepared.append((pending_name, candidate))
                    await candidate.start()
                elif candidate is not None:
                    prepared.append((pending_name, candidate))
                planned.append((f"trunk:{identity}", old, pending_name, candidate))
            if runtime.sip is not owner or owner.phase.value != "active":
                raise RuntimeError("SIP runtime stopped while preparing trunk changes")
            endpoint = sip_endpoint_manager(hass)
            if endpoint is not None:
                for _name, candidate in prepared:
                    candidate.attach_endpoint_manager(endpoint)
        except BaseException:
            async def discard():
                results = await asyncio.gather(*(client.stop() for _name, client in prepared), return_exceptions=True)
                for (name, client), result in zip(prepared, results, strict=True):
                    if not isinstance(result, BaseException):
                        owner.release_component(name, client)
            cleanup = asyncio.create_task(discard(), name="voip-trunk-update-rollback")
            await async_wait_for_cleanup(cleanup)
            raise
        planned.extend((f"trunk:{identity}" if identity else "trunk", client, "", None)
                       for identity, client in clients.items())
        retired = []
        # No await during the ownership/configuration cutover.
        for name, old, pending_name, candidate in planned:
            if old is not None:
                owner.release_component(name, old)
                retired_name = f"retired_{name}:{old.call_id}"
                owner.adopt_component(retired_name, old, closer=old.stop)
                retired.append((retired_name, old))
            if candidate is not None:
                owner.release_component(pending_name, candidate)
                owner.adopt_component(name, candidate, closer=candidate.stop)
        runtime.trunk_configs = configs
        runtime.default_trunk_id = default
        runtime.trunk_config = configs.get(default, normalized_trunk_config({}))
        from homeassistant.helpers.dispatcher import async_dispatcher_send
        from .trunk_config import TRUNK_UPDATE_SIGNAL
        async_dispatcher_send(hass, TRUNK_UPDATE_SIGNAL, "")
        for name, client in retired:
            await client.stop()
            owner.release_component(name, client)


async def _async_stop_sip_trunk(hass: HomeAssistant) -> None:
    pbx_runtime = sip_endpoint_runtime(hass)
    for identity, trunk in sip_trunks(hass).items():
        try:
            await trunk.stop()
        except Exception:
            _LOGGER.debug("Ignoring SIP trunk stop error id=%s", identity, exc_info=True)
            continue
        if pbx_runtime is not None:
            pbx_runtime.release_component(f"trunk:{identity}" if identity else "trunk", trunk)
