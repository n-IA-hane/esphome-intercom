"""SIP trunk lifecycle for VoIP Stack."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant

from .config import transport_config, trunk_config, trunk_enabled
from .const import (
    CONF_TRUNK_AUTH_USERNAME,
    CONF_TRUNK_DOMAIN,
    CONF_TRUNK_EXPIRES,
    CONF_TRUNK_OUTBOUND_PROXY,
    CONF_TRUNK_PASSWORD,
    CONF_TRUNK_PORT,
    CONF_TRUNK_SERVER,
    CONF_TRUNK_TRANSPORT,
    CONF_TRUNK_USERNAME,
    DOMAIN,
)
from .session_cleanup import async_wait_for_cleanup
from .runtime_data import sip_endpoint_manager, sip_endpoint_runtime, sip_trunk

_LOGGER = logging.getLogger(__name__)


async def async_start_sip_trunk(hass: HomeAssistant, *, local_ip: str) -> bool:
    cfg = trunk_config(hass)
    if not trunk_enabled(cfg):
        hass.data.setdefault(DOMAIN, {}).pop("sip_trunk", None)
        return True
    if not local_ip:
        _LOGGER.warning("SIP trunk disabled: HA advertise IP is unknown")
        return False

    from .sip_trunk import SipTrunkClient, SipTrunkConfig

    trunk = SipTrunkClient(
        config=SipTrunkConfig(
            enabled=True,
            transport=str(cfg[CONF_TRUNK_TRANSPORT]),
            server=str(cfg[CONF_TRUNK_SERVER]),
            port=int(cfg[CONF_TRUNK_PORT]),
            domain=str(cfg[CONF_TRUNK_DOMAIN]),
            username=str(cfg[CONF_TRUNK_USERNAME]),
            auth_username=str(cfg[CONF_TRUNK_AUTH_USERNAME]),
            password=str(cfg[CONF_TRUNK_PASSWORD]),
            expires=int(cfg[CONF_TRUNK_EXPIRES]),
            outbound_proxy=str(cfg[CONF_TRUNK_OUTBOUND_PROXY]),
        ),
        local_ip=local_ip,
        local_sip_port=int(transport_config(hass)["sip_port"]),
    )
    endpoint = sip_endpoint_manager(hass)
    if endpoint is not None:
        trunk.attach_endpoint_manager(endpoint)
    pbx_runtime = sip_endpoint_runtime(hass)
    if pbx_runtime is None:
        return False
    pbx_runtime.adopt_component("trunk", trunk, closer=trunk.stop)
    try:
        await trunk.start()
    except Exception as err:
        _LOGGER.warning("SIP trunk registration failed: %s", err)
    return True


async def async_stop_sip_trunk(hass: HomeAssistant) -> None:
    bucket = hass.data.setdefault(DOMAIN, {})
    task = bucket.get("sip_trunk_stop_task")
    if not isinstance(task, asyncio.Task) or task.done():
        task = asyncio.create_task(
            _async_stop_sip_trunk(hass),
            name="voip-sip-trunk-runtime-stop",
        )
        bucket["sip_trunk_stop_task"] = task
    try:
        await async_wait_for_cleanup(task)
    finally:
        if task.done() and bucket.get("sip_trunk_stop_task") is task:
            bucket.pop("sip_trunk_stop_task", None)


async def _async_stop_sip_trunk(hass: HomeAssistant) -> None:
    trunk = sip_trunk(hass)
    if trunk is None:
        return
    try:
        await trunk.stop()
    except Exception:
        _LOGGER.debug("Ignoring SIP trunk stop error", exc_info=True)
        return
    pbx_runtime = sip_endpoint_runtime(hass)
    if pbx_runtime is not None:
        pbx_runtime.release_component("trunk", trunk)
