"""Reference model for ESP-side routing used only by contract tests."""

from __future__ import annotations

from typing import Any


def resolve_esp_origin(
    router: Any,
    target: str,
    entries: list[Any],
    ha_uri: str,
) -> Any:
    """Model the routing decision implemented by ESP firmware."""

    target = (target or "").strip()
    target_class = router.classify_target(target)
    if target_class in {router.TargetClass.SIP_URI, router.TargetClass.NAME_AT_HOST}:
        return router.RouteDecision(
            router.RouteAction.DIRECT,
            target=target,
            sip_uri=router.to_sip_uri(target),
            reason=router.RouteReason.DIRECT_URI,
        )
    if target_class is router.TargetClass.NUMERIC:
        return router.RouteDecision(
            router.RouteAction.BRIDGE,
            target=target,
            sip_uri=router.ha_uri_for(target, entries, ha_uri),
            reason=router.RouteReason.NUMBER_VIA_HA,
        )

    entry = router.find_entry(entries, target, include_number=False)
    if entry is None:
        return router.RouteDecision(
            router.RouteAction.BRIDGE,
            target=target,
            sip_uri=ha_uri,
            reason=router.RouteReason.NAME_VIA_HA,
        )
    if not entry.enabled:
        return router.RouteDecision(
            router.RouteAction.REJECT,
            target=target,
            status=403,
            reason=router.RouteReason.TARGET_DISABLED,
            entry=entry,
        )
    transport = router._entry_transport(entry)
    direct_uri = entry.sip_uri or (
        router._uri(entry.id, entry.address, router._entry_port(entry), transport)
        if entry.address
        else ""
    )
    if direct_uri and not entry.ha_bridge:
        return router.RouteDecision(
            router.RouteAction.DIRECT,
            target=entry.id,
            sip_uri=direct_uri,
            source="phonebook",
            entry=entry,
        )
    bridge_target = entry.extension or entry.id
    return router.RouteDecision(
        router.RouteAction.BRIDGE,
        target=bridge_target,
        sip_uri=router.ha_uri_for(bridge_target, entries, ha_uri),
        reason=router.RouteReason.NAME_VIA_HA,
        source="phonebook",
        entry=entry,
    )
