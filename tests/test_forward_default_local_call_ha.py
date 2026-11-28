"""Forward selects the current local call on a canonical SIP account."""

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import Context
from homeassistant.exceptions import ServiceValidationError, Unauthorized

from .test_automation_originate_ha import lab as lab

pytestmark = pytest.mark.ha


@pytest.mark.parametrize("case", ["current", "stale_generation", "foreign_call", "ambiguous_pending", "denied"])
async def test_forward_default_local_call_uses_shared_owned_command(lab, hass_read_only_user, case):
    from custom_components.voip_stack.account_runtime import recompute_account_availability
    from custom_components.voip_stack.directory_runtime import compose_directory_entries
    from custom_components.voip_stack.endpoint_session import TerminationIntent
    from custom_components.voip_stack.endpoint_device import async_ensure_endpoint_device
    from custom_components.voip_stack.ha_bindings import HaCardBindings
    from custom_components.voip_stack.local_softphone_runtime import (
        async_setup_local_softphone_bridge, async_shutdown_local_softphone_bridge,
        start_local_softphone_call,
    )
    from custom_components.voip_stack.phone_config import sync_registry_from_entry
    from custom_components.voip_stack.phone_endpoint import EndpointKind
    from custom_components.voip_stack.store import config_entry

    entry = config_entry(lab.hass)
    for name in ("default-c", "default-b", "default-a", "parallel-left", "parallel-right"):
        lab.hass.config_entries.async_add_subentry(entry, ConfigSubentry(
            subentry_type="phone", title=name, unique_id="phone:" + name,
            data={"kind": "sip_account", "name": name, "username": name,
                  "password": "isolated-test-only", "username_source": "user",
                  "ha_bindings": [] if name == "default-c" else [{"source": "migration", "enabled": True}]}))
    sync_registry_from_entry(lab.hass, entry)
    for endpoint in tuple(lab.runtime.endpoints.endpoints):
        async_ensure_endpoint_device(lab.hass, entry, endpoint, lab.runtime.endpoints)
    bindings = HaCardBindings(lab.hass, entry, lab.runtime,
        lambda identities: recompute_account_availability(lab.hass, identities))
    lab.runtime.ha_bindings = bindings
    await bindings.async_start()
    recompute_account_availability(lab.hass)
    lab.roster[:] = compose_directory_entries(lab.hass, [], lab.calls.component("registrar").registered_roster_entries())
    lab.runtime.directory.replace(lab.roster)
    bridge = async_setup_local_softphone_bridge(lab.hass)
    original = start_local_softphone_call(lab.hass, "default-c", "default-b", caller_owner_id="caller-owner")
    parallel = start_local_softphone_call(lab.hass, "parallel-left", "parallel-right", caller_owner_id="parallel-owner")
    session = lab.calls.get_session(original.call_id)
    token = session.token
    phone = lab.runtime.endpoints.require("default-b")
    assert phone.kind is EndpointKind.SIP_ACCOUNT
    data = {"device_id": phone.ha_device_id, "destination": "default-a"}
    if case == "stale_generation":
        data["expected_generation"] = token.generation + 1
    elif case == "foreign_call":
        data["call_id"] = parallel.call_id
    elif case == "ambiguous_pending":
        for identity in ("pending-one", "pending-two"):
            lab.calls.upsert(identity, state="connecting", owner="router", endpoint_id=phone.endpoint_id)
            lab.calls.set_pending_route(identity, {"future": lab.hass.loop.create_future()})
    context = Context(user_id=hass_read_only_user.id) if case == "denied" else Context()
    try:
        action = lab.hass.services.async_call("voip_stack", "forward", data, blocking=True, context=context)
        if case == "current":
            await action
            redirected = bridge.get_call(original.call_id)
            assert redirected.callee_endpoint_id == "default-a" and redirected.callee_state.value == "ringing"
            assert not lab.runtime.endpoints.require("default-b").active_call_id
        else:
            error, message = {
                "stale_generation": (ServiceValidationError, "generation"),
                "foreign_call": (ServiceValidationError, "own|belongs"),
                "ambiguous_pending": (ServiceValidationError, "More than one"),
                "denied": (Unauthorized, None),
            }[case]
            with pytest.raises(error, match=message):
                await action
            assert bridge.get_call(original.call_id).callee_endpoint_id == "default-b"
        assert lab.calls.get_session(token.call_id, generation=token.generation) is session and session.live
        assert bridge.get_call(parallel.call_id).callee_endpoint_id == "parallel-right"
    finally:
        for current in tuple(lab.calls.sessions.values()):
            await lab.calls.request_termination(current.call_id, TerminationIntent("test_done"), generation=current.generation)
        async_shutdown_local_softphone_bridge(lab.hass)
        await bindings.async_close()
        lab.runtime.ha_bindings = None
