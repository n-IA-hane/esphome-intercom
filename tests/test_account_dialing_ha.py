"""An account offers its existing SIP contacts and HA transport together."""

from types import SimpleNamespace

import pytest

from custom_components.voip_stack.phone_endpoint import EndpointAvailability, EndpointKind, PhoneEndpoint
from custom_components.voip_stack.roster import RosterEntry

pytestmark = pytest.mark.ha


async def test_account_fork_requires_saved_binding_and_real_contact(monkeypatch, hass):
    from custom_components.voip_stack import account_dialing
    from custom_components.voip_stack.duration_config import initialize_duration_config
    from pytest_homeassistant.common import MockConfigEntry
    configured = MockConfigEntry(domain="voip_stack", version=7, minor_version=7,
        data=initialize_duration_config({"service_timeouts": {"contact_step_timeout": 23}}))
    configured.add_to_hass(hass)

    endpoint = PhoneEndpoint(endpoint_id="sip:shared", name="Shared", username="shared",
                             kind=EndpointKind.SIP_ACCOUNT, availability=EndpointAvailability.AVAILABLE)
    entry = RosterEntry(id="shared", name="Shared", metadata={"sip_contacts": [
        {"uri": "sip:shared@127.0.0.1:25060", "transport": "udp"}]})
    monkeypatch.setattr(account_dialing, "account_has_ha_binding", lambda *_: False)
    assert account_dialing.account_fork_entry(hass, endpoint, entry) is None
    monkeypatch.setattr(account_dialing, "account_has_ha_binding", lambda *_: True)
    assert account_dialing.account_fork_entry(hass, endpoint, RosterEntry(id="shared")) is None
    fork = account_dialing.account_fork_entry(hass, endpoint, entry)
    assert fork.metadata["account_fork"] == endpoint.endpoint_id
    assert fork.metadata["members"] == [entry.id]
    assert fork.metadata["route_kind"] == "direct"
    assert entry.metadata.get("account_fork") is None
    assert account_dialing.account_call_plan(fork).ring_policy.step_timeout == 23


@pytest.mark.asyncio
async def test_saved_ha_offer_does_not_hide_sip_contact_candidates():
    from custom_components.voip_stack.group_candidates import (
        GroupCandidateRuntime, GroupCandidates, async_prepare_group_candidates,
    )
    from custom_components.voip_stack.dial_plan import RingPolicy
    from custom_components.voip_stack.outbound_attempts import BrowserLeg

    endpoint = PhoneEndpoint(endpoint_id="sip:shared", name="Shared", username="shared",
                             kind=EndpointKind.SIP_ACCOUNT, availability=EndpointAvailability.AVAILABLE)
    claimed = []
    registry = SimpleNamespace(claim_endpoint=lambda *args, **kwargs: claimed.append(args))
    directory = SimpleNamespace(get=lambda _: endpoint)
    prepared = []

    def prepare(**kwargs):
        prepared.append(kwargs)
        return SimpleNamespace(endpoint_id=endpoint.endpoint_id, **{
            key: kwargs[key] for key in ("candidate_id", "tier", "order")})

    row = RosterEntry(id="shared", name="Shared", metadata={
        "endpoint_id": endpoint.endpoint_id,
        "sip_contacts": [{"uri": "sip:shared@127.0.0.1:25060", "transport": "udp"}],
    })
    result = GroupCandidates()
    await async_prepare_group_candidates(result, GroupCandidateRuntime(
        registry, directory,
        lambda *_: BrowserLeg("shared", endpoint.endpoint_id, "Shared", ""), prepare,
        lambda *_: endpoint,
    ), invite=SimpleNamespace(call_id="one-call", caller="caller", source_host="127.0.0.2"),
        members=["shared"], peers=[], roster_entries=[row], local_name="Caller",
        ring_policy=RingPolicy())
    assert len(result.browser_legs) == 1
    assert len(result.attempts) == 1
    assert prepared[0]["uri_override"] == "sip:shared@127.0.0.1:25060"
    assert {args[1] for args in claimed} == {endpoint.endpoint_id}


@pytest.mark.parametrize("ingress", [True, False], ids=["real-ingress-wrapper", "application-session"])
async def test_ingress_and_application_share_contact_admission(monkeypatch, ingress):
    from custom_components.voip_stack import group_candidates
    from custom_components.voip_stack.dial_plan import RingPolicy
    from custom_components.voip_stack.outbound_attempts import BrowserLeg

    endpoint = PhoneEndpoint(endpoint_id="sip:shared", name="Shared", username="shared",
                             kind=EndpointKind.SIP_ACCOUNT, availability=EndpointAvailability.AVAILABLE)
    row = RosterEntry(id="shared", metadata={"endpoint_id": endpoint.endpoint_id, "sip_contacts": [
        {"uri": "sip:shared@127.0.0.1:25060", "transport": "udp"},
        {"uri": "sip:shared@127.0.0.1:25061;transport=tcp", "transport": "tcp"},
    ]})
    claims, prepared, shared_calls = [], [], []
    registry = SimpleNamespace(claim_endpoint=lambda *args, **kwargs: claims.append(args))
    def prepare(**kwargs):
        prepared.append(kwargs)
        return SimpleNamespace(endpoint_id=endpoint.endpoint_id, **{
            key: kwargs[key] for key in ("candidate_id", "tier", "order")})
    runtime = group_candidates.GroupCandidateRuntime(registry, SimpleNamespace(get=lambda _: endpoint),
        lambda *_: BrowserLeg("shared", endpoint.endpoint_id, "Shared", ""), prepare, lambda *_: endpoint)
    implementation = group_candidates.async_prepare_account_candidates
    async def observe(*args, **kwargs):
        shared_calls.append(kwargs)
        return await implementation(*args, **kwargs)
    monkeypatch.setattr(group_candidates, "async_prepare_account_candidates", observe)
    result = group_candidates.GroupCandidates()
    arguments = dict(members=["shared"], peers=[], roster_entries=[row], local_name="Caller",
                     ring_policy=RingPolicy())
    if ingress:
        invite = SimpleNamespace(call_id="one-call", caller="caller", source_host="127.0.0.2")
        await group_candidates.async_prepare_group_candidates(result, runtime, invite=invite, **arguments)
        assert all(value["invite"] is invite for value in prepared)
    else:
        await group_candidates.async_prepare_account_candidates(result, runtime,
            call_id="one-call", outbound_context={"local_rtp_port_index": 0}, **arguments)
        assert all("invite" not in value for value in prepared)
        assert all(value["local_rtp_port_index"] == 0 for value in prepared)
    assert len(shared_calls) == 1
    assert len(result.browser_legs) == 1 and len(result.attempts) == 2
    assert {value["uri_override"] for value in prepared} == {
        "sip:shared@127.0.0.1:25060", "sip:shared@127.0.0.1:25061;transport=tcp"}
    assert {args[:2] for args in claims} == {("one-call", endpoint.endpoint_id)}


@pytest.mark.parametrize("retain_all", [False, True], ids=["normal-first", "paging-all"])
async def test_shared_offer_builder_preserves_selected_answer_contract(retain_all):
    import asyncio
    from custom_components.voip_stack.dial_fork import DialCandidate, DialDisposition, DialForkController, DialOutcome
    from custom_components.voip_stack.endpoint_session import EndpointCallSession
    from custom_components.voip_stack.outbound_attempts import BrowserLeg
    from custom_components.voip_stack.ring_group_fork import build_offer_candidates

    session = EndpointCallSession("one-account", 1)
    sip_ready, browser_committed = asyncio.Event(), asyncio.Event()
    closed, committed = [], []
    attempts = [SimpleNamespace(candidate_id=f"sip:{index}", endpoint_id="sip:shared", tier=0, order=index,
        client=SimpleNamespace(dialog_ids=SimpleNamespace(call_id=f"dialog-{index}"))) for index in range(2)]
    browser = BrowserLeg("shared", "sip:shared", "Shared", "")
    async def dial_sip(_attempt):
        await sip_ready.wait()
        return DialOutcome(DialDisposition.ANSWERED, 200)
    async def close_sip(attempt, mode):
        closed.append((attempt.candidate_id, mode))
    def browser_candidate(leg):
        async def ready():
            return DialOutcome(DialDisposition.ANSWERED, 200)
        async def close(mode):
            closed.append(("browser:shared", mode))
        return DialCandidate("browser:shared", ready, close, endpoint_id=leg.endpoint_id)
    candidates, payloads = build_offer_candidates(attempts=attempts, browser_legs=[browser],
        preflight_failures=[], dial_sip=dial_sip, close_sip=close_sip, browser_candidate=browser_candidate)
    assert {candidate.endpoint_id for candidate in candidates} == {"sip:shared"}
    assert payloads["browser:shared"] is browser
    def commit(candidate, _outcome):
        committed.append(candidate.candidate_id)
        if candidate.candidate_id == "browser:shared":
            browser_committed.set()
        return session.live
    controller = DialForkController(session, candidates, overall_timeout=2)
    task = asyncio.create_task(controller.run_all(commit) if retain_all else controller.run(commit))
    await asyncio.wait_for(browser_committed.wait(), 1)
    if retain_all:
        assert not task.done() and committed == ["browser:shared"] and not closed
        sip_ready.set()
        result = await task
        assert {candidate.candidate_id for candidate in result.answered} == {"sip:0", "sip:1", "browser:shared"}
        assert not closed
    else:
        result = await task
        assert result.winner.candidate_id == "browser:shared"
        assert {identity for identity, _mode in closed} == {"sip:0", "sip:1"}
        assert committed == ["browser:shared"]


async def test_normal_ring_group_adapts_into_the_same_offer_builder(monkeypatch):
    import asyncio
    from custom_components.voip_stack import ring_group_fork
    calls = []
    implementation = ring_group_fork.build_offer_candidates
    def observe(**kwargs):
        calls.append(kwargs)
        return implementation(**kwargs)
    monkeypatch.setattr(ring_group_fork, "build_offer_candidates", observe)
    future = asyncio.get_running_loop().create_future()
    candidates, _payloads, _decision = ring_group_fork.build_ring_group_fork(
        sip_port=5060, route_future=future, attempts=[], browser_legs=[], preflight_failures=[])
    assert len(calls) == 1
    assert len(candidates) == 1 and candidates[0].control
    await candidates[0].close(None)


def test_resolved_browser_endpoint_uses_the_shared_eligibility_without_roster(monkeypatch):
    from custom_components.voip_stack import account_runtime
    from custom_components.voip_stack.endpoint_dialing import EndpointDialer

    browser = PhoneEndpoint(endpoint_id="rowless-browser", name="Card", username="card",
                            kind=EndpointKind.BROWSER, availability=EndpointAvailability.AVAILABLE)
    resolver = SimpleNamespace(logical_endpoint=lambda *_: None, is_ha_target=lambda _: False)
    dialer = EndpointDialer(hass=None, local_ip="127.0.0.1", config={}, route_resolver=resolver,
                          sip_uri_transport=lambda _: "UDP", enable_reused_tcp_connection=lambda *_: False)
    monkeypatch.setattr(account_runtime, "account_has_ha_binding", lambda *_: False)
    assert dialer.browser_leg_for_member("card", [], []) is None
    resolved = dialer.browser_leg_for_endpoint(browser, "card")
    assert resolved.endpoint_id == browser.endpoint_id and resolved.member == "card"
    resolver.logical_endpoint = lambda *_: browser
    ordinary = dialer.browser_leg_for_member("card", [], [])
    assert ordinary == resolved
    assert dialer.browser_leg_for_endpoint(None, "missing") is None
    managed = PhoneEndpoint(endpoint_id="sip:managed", name="Account", username="managed",
                            kind=EndpointKind.SIP_ACCOUNT, availability=EndpointAvailability.AVAILABLE)
    assert dialer.browser_leg_for_endpoint(managed, "managed") is None
    monkeypatch.setattr(account_runtime, "account_has_ha_binding", lambda *_: True)
    assert dialer.browser_leg_for_endpoint(managed, "managed").endpoint_id == managed.endpoint_id
