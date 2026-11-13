"""An account offers its existing SIP contacts and HA transport together."""

from types import SimpleNamespace

import pytest

from custom_components.voip_stack.phone_endpoint import EndpointAvailability, EndpointKind, PhoneEndpoint
from custom_components.voip_stack.roster import RosterEntry

pytestmark = pytest.mark.ha


def test_account_fork_requires_saved_binding_and_real_contact(monkeypatch):
    from custom_components.voip_stack import account_dialing

    endpoint = PhoneEndpoint(endpoint_id="sip:shared", name="Shared", username="shared",
                             kind=EndpointKind.SIP_ACCOUNT, availability=EndpointAvailability.AVAILABLE)
    entry = RosterEntry(id="shared", name="Shared", metadata={"sip_contacts": [
        {"uri": "sip:shared@127.0.0.1:25060", "transport": "udp"}]})
    monkeypatch.setattr(account_dialing, "account_has_ha_binding", lambda *_: False)
    assert account_dialing.account_fork_entry(None, endpoint, entry) is None
    monkeypatch.setattr(account_dialing, "account_has_ha_binding", lambda *_: True)
    assert account_dialing.account_fork_entry(None, endpoint, RosterEntry(id="shared")) is None
    fork = account_dialing.account_fork_entry(None, endpoint, entry)
    assert fork.metadata["account_fork"] == endpoint.endpoint_id
    assert fork.metadata["members"] == [entry.id]
    assert fork.metadata["route_kind"] == "direct"
    assert entry.metadata.get("account_fork") is None


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
