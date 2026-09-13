"""Native HA trigger execution keeps concurrent call contexts isolated."""

import asyncio
from datetime import timedelta

import pytest
from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.trigger import TriggerConfig

from custom_components.voip_stack import automation_context, trigger
from custom_components.voip_stack.pbx_runtime import SipEndpointRuntime

pytestmark = pytest.mark.ha


@pytest.fixture
def registry(monkeypatch):
    registry = SipEndpointRuntime(allow_dark_sessions=True)
    registry.activate()
    monkeypatch.setattr(automation_context, "call_registry", lambda _: registry)
    monkeypatch.setattr(trigger, "call_registry", lambda _: registry)
    return registry


async def test_native_runner_binds_each_call_across_await(hass, registry):
    sessions = [
        registry.upsert(name, state="ringing", owner="automation")
        for name in ("one", "two")
    ]
    ready = asyncio.Event()
    attached = asyncio.Event()
    seen = []
    tasks = []

    async def action():
        await ready.wait()
        call = automation_context.bind_call_action(
            ServiceCall(hass, "voip_stack", "tts_say", {})
        )
        seen.append((call.data["call_id"], call.data["expected_generation"]))
        return object()

    def runner(payload, description, context):
        assert payload["call"]["callee"] == "Prova"
        task = asyncio.create_task(action())
        tasks.append(task)
        if len(tasks) == 2:
            attached.set()
        return task

    subject = trigger.VoipCallTrigger(
        hass,
        TriggerConfig("voip_stack.call_received", options={"destination": "Prova"}),
    )
    detach = await subject.async_attach_runner(runner)
    try:
        for session in sessions:
            hass.bus.async_fire(
                trigger.CALL_EVENT,
                {
                    "event_type": "automation_requested",
                    "call_id": session.call_id,
                    "generation": session.generation,
                    "callee": "Prova",
                },
            )
        async with asyncio.timeout(2):
            await attached.wait()
        ready.set()
        await asyncio.gather(*tasks)
        await hass.async_block_till_done()
        assert sorted(seen) == [
            (session.call_id, session.generation) for session in sessions
        ]
        assert automation_context.current_execution.get() is None
    finally:
        ready.set()
        detach()


async def test_delayed_action_does_not_select_replacement_call(hass, registry):
    session = registry.upsert("old", state="ringing", owner="automation")
    execution = automation_context.CallExecution(session.token, {})
    binding = automation_context.current_execution.set(execution)
    try:
        execution.active = False
        registry.upsert("new", state="ringing", owner="automation")
        with pytest.raises(ServiceValidationError, match="ended"):
            automation_context.bind_call_action(
                ServiceCall(hass, "voip_stack", "hangup", {})
            )
    finally:
        automation_context.current_execution.reset(binding)


def test_filters_use_snapshot_and_allow_optional_destination():
    snapshot = {
        "caller": "Alice",
        "callee": "Prova",
        "called_extension": "666",
        "ingress": "extension",
    }
    assert trigger.matches_call(snapshot, {})
    assert trigger.matches_call(snapshot, {"destination": "  prova "})
    assert trigger.matches_call(snapshot, {"destination": "666"})
    assert not trigger.matches_call(snapshot, {"caller": "Bob"})
    assert not trigger.matches_call(snapshot, {"ingress": "trunk"})


def test_legacy_explicit_action_is_unchanged_without_context(hass):
    call = ServiceCall(
        hass,
        "voip_stack",
        "forward",
        {"call_id": "legacy", "destination": "Casa"},
        context=Context(),
    )
    assert automation_context.bind_call_action(call) is call


async def test_connection_projections_start_one_automation(hass, registry):
    session = registry.upsert("connected", state="in_call", owner="bridge")
    seen = []

    def runner(payload, *_args):
        seen.append(payload)
        return asyncio.create_task(asyncio.sleep(0))

    subject = trigger.VoipCallTrigger(hass, TriggerConfig("voip_stack.call_connected"))
    detach = await subject.async_attach_runner(runner)
    try:
        for scope in ("sip_bridge", "session", "session", "session"):
            hass.bus.async_fire(
                trigger.CALL_EVENT,
                {
                    "call_id": session.call_id,
                    "generation": session.generation,
                    "sequence": 3,
                    "state": "in_call",
                    "scope": scope,
                    "event_type": "answered" if scope == "sip_bridge" else "connected",
                },
            )
        await hass.async_block_till_done()
        assert len(seen) == 1
    finally:
        detach()


async def test_no_answer_action_cannot_move_a_call_answered_while_it_waited(
    hass, registry
):
    session = registry.upsert("race", state="ringing", owner="router", callee="Casa")
    started, release = asyncio.Event(), asyncio.Event()
    tasks = []

    async def action():
        started.set()
        await release.wait()
        automation_context.bind_call_action(
            ServiceCall(hass, "voip_stack", "forward", {"destination": "Reception"})
        )

    def runner(*_args):
        task = asyncio.create_task(action())
        tasks.append(task)
        return task

    subject = trigger.VoipCallTrigger(
        hass,
        TriggerConfig(
            "voip_stack.call_unanswered",
            options={"for": timedelta(milliseconds=1)},
        ),
    )
    detach = await subject.async_attach_runner(runner)
    try:
        hass.bus.async_fire(
            trigger.CALL_EVENT,
            {
                "event_type": "ringing",
                "call_id": session.call_id,
                "generation": session.generation,
                "callee": "Casa",
            },
        )
        async with asyncio.timeout(2):
            await started.wait()
        registry.upsert(session.call_id, state="in_call", owner="bridge")
        release.set()
        with pytest.raises(ServiceValidationError, match="changed"):
            await tasks[0]
        await hass.async_block_till_done()
    finally:
        release.set()
        detach()


@pytest.mark.parametrize("answered", [False, True])
async def test_no_answer_deadline_is_cancelled_on_answer(hass, registry, answered):
    session = registry.upsert("ringing", state="ringing", owner="router", callee="Casa")
    fired = asyncio.Event()

    def runner(*_args):
        fired.set()
        return asyncio.create_task(asyncio.sleep(0))

    subject = trigger.VoipCallTrigger(
        hass,
        TriggerConfig(
            "voip_stack.call_unanswered",
            options={"destination": "Casa", "for": timedelta(milliseconds=20)},
        ),
    )
    detach = await subject.async_attach_runner(runner)
    payload = {
        "call_id": session.call_id,
        "generation": session.generation,
        "callee": "Casa",
        "event_type": "ringing",
    }
    try:
        hass.bus.async_fire(trigger.CALL_EVENT, payload)
        await asyncio.sleep(0)
        if answered:
            registry.upsert(session.call_id, state="in_call", owner="bridge")
            hass.bus.async_fire(
                trigger.CALL_EVENT, {**payload, "event_type": "connected"}
            )
        await asyncio.sleep(0.04)
        await hass.async_block_till_done()
        assert fired.is_set() is not answered
        assert not any(
            resource.name.startswith("deadline:") for resource in session.resources
        )
    finally:
        detach()


@pytest.mark.parametrize("mode", ["parallel", "queued", "single", "restart"])
async def test_ha_script_modes_and_nested_script_keep_call_identity(
    hass, registry, mode
):
    """Exercise HA's real script runner, including a synchronous child script."""
    from homeassistant.helpers.script import Script
    from homeassistant.setup import async_setup_component

    entered = {name: asyncio.Event() for name in ("first", "second")}
    release = {name: asyncio.Event() for name in entered}
    seen = []

    async def gate(call):
        execution = automation_context.current_execution.get()
        name = execution.token.call_id
        entered[name].set()
        await release[name].wait()

    async def capture(call):
        bound = automation_context.bind_call_action(call)
        seen.append(bound.data["call_id"])

    hass.services.async_register("test", "gate", gate)
    hass.services.async_register("voip_stack", "hangup", capture)
    assert await async_setup_component(
        hass,
        "script",
        {
            "script": {
                "call_child": {
                    "mode": "parallel",
                    "sequence": [
                        {"action": "voip_stack.hangup"},
                    ],
                }
            },
        },
    )
    script = Script(
        hass,
        [{"action": "test.gate"}, {"action": "script.call_child"}],
        "Call mode test",
        "automation",
        script_mode=mode,
    )
    tasks = []

    def runner(payload, description, context):
        task = hass.async_create_task(script.async_run({"trigger": payload}, context))
        tasks.append(task)
        return task

    subject = trigger.VoipCallTrigger(hass, TriggerConfig("voip_stack.call_received"))
    detach = await subject.async_attach_runner(runner)
    try:
        for name in entered:
            session = registry.upsert(name, state="ringing", owner="router")
            hass.bus.async_fire(
                trigger.CALL_EVENT,
                {
                    "event_type": "ringing",
                    "call_id": name,
                    "generation": session.generation,
                    "callee": "Reception",
                },
            )
            if name == "first":
                async with asyncio.timeout(2):
                    await entered[name].wait()
        if mode in {"parallel", "restart"}:
            async with asyncio.timeout(2):
                await entered["second"].wait()
        else:
            async with asyncio.timeout(2):
                while len(tasks) < 2 or (mode == "queued" and script.runs != 2):
                    await asyncio.sleep(0)
                if mode == "single":
                    await tasks[1]
            assert not entered["second"].is_set()
        release["first"].set()
        if mode == "queued":
            async with asyncio.timeout(2):
                await entered["second"].wait()
        release["second"].set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        if mode == "restart":
            assert isinstance(results[0], asyncio.CancelledError)
            assert not isinstance(results[1], BaseException)
        else:
            assert not any(isinstance(result, BaseException) for result in results)
        await hass.async_block_till_done()
        expected = {"single": ["first"], "restart": ["second"]}.get(
            mode, ["first", "second"]
        )
        assert sorted(seen) == sorted(expected)
        assert automation_context.current_execution.get() is None
    finally:
        for event in release.values():
            event.set()
        await script.async_stop()
        detach()
