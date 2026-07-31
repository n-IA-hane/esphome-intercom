"""Behavioral tests for forwarded ring group candidate preparation."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types
import unittest
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
MODULE = (
    ROOT
    / "custom_components"
    / "voip_stack"
    / "forward_group_candidates.py"
)
PACKAGE = "voip_stack_forward_group_candidates_test"


class _BusyError(ValueError):
    pass


def _load_module():
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(MODULE.parent)]
    sys.modules[PACKAGE] = package

    call_registry = types.ModuleType(f"{PACKAGE}.call_registry")
    call_registry.CallRegistry = object
    sys.modules[call_registry.__name__] = call_registry

    endpoint_registry = types.ModuleType(f"{PACKAGE}.endpoint_registry")
    endpoint_registry.EndpointBusyError = _BusyError
    endpoint_registry.EndpointRegistry = object
    sys.modules[endpoint_registry.__name__] = endpoint_registry

    outbound_attempts = types.ModuleType(f"{PACKAGE}.outbound_attempts")
    outbound_attempts.BrowserLeg = object
    outbound_attempts.OutboundLeg = object
    sys.modules[outbound_attempts.__name__] = outbound_attempts

    pbx_routing = types.ModuleType(f"{PACKAGE}.pbx_routing")
    pbx_routing.browser_endpoint_can_ring = lambda endpoint: bool(
        endpoint is None or not getattr(endpoint, "blocked", False)
    )
    pbx_routing.caller_matches_group_member = (
        lambda caller, _source_host, member, _peers: caller == member
    )
    sys.modules[pbx_routing.__name__] = pbx_routing

    module_name = f"{PACKAGE}.forward_group_candidates"
    spec = importlib.util.spec_from_file_location(module_name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


forward_candidates = _load_module()


class _Registry:
    def __init__(self, *, busy: set[str] | None = None) -> None:
        self.busy = busy or set()
        self.claims: list[tuple[str, str, str]] = []

    def claim_endpoint(
        self,
        call_id: str,
        endpoint_id: str,
        *,
        role: str,
    ) -> None:
        if endpoint_id in self.busy:
            raise _BusyError
        self.claims.append((call_id, endpoint_id, role))


class _EndpointRegistry:
    def __init__(self, endpoints: dict[str, object]) -> None:
        self.endpoints = endpoints

    def get(self, endpoint_id: str):
        return self.endpoints.get(endpoint_id)


class ForwardGroupCandidatesTest(unittest.TestCase):
    def _prepare(
        self,
        *,
        initial_selection: bool,
        browser_legs: dict[str, object],
        endpoints: dict[str, object] | None = None,
        busy: set[str] | None = None,
    ):
        registry = _Registry(busy=busy)
        prepared: list[dict] = []

        def prepare_outbound_leg(**kwargs):
            prepared.append(dict(kwargs))
            return SimpleNamespace(candidate_id="", order=-1)

        result = forward_candidates.ForwardGroupCandidates()
        forward_candidates.prepare_forward_group_candidates(
            result,
            forward_candidates.ForwardGroupCandidateRuntime(
                registry=registry,
                endpoint_registry=_EndpointRegistry(endpoints or {}),
                browser_leg_for_member=(
                    lambda member, _peers, _entries: browser_legs.get(member)
                ),
                prepare_outbound_leg=prepare_outbound_leg,
            ),
            invite=SimpleNamespace(
                call_id="source-call",
                caller="Caller",
                source_host="caller.local",
            ),
            members=["Browser", "Caller", "Desk"],
            peers=[],
            roster_entries=[],
            local_name="Caller",
            initial_selection=initial_selection,
        )
        return result, registry, prepared

    def test_initial_selection_keeps_browser_and_sip_candidates(self) -> None:
        browser = SimpleNamespace(endpoint_id="browser")
        result, registry, prepared = self._prepare(
            initial_selection=True,
            browser_legs={"Browser": browser},
        )

        self.assertEqual(result.browser_legs, [browser])
        self.assertEqual(
            registry.claims,
            [("source-call", "browser", "group_candidate")],
        )
        self.assertEqual([item["member"] for item in prepared], ["Desk"])
        self.assertEqual(result.attempts[0].candidate_id, "forward:2")
        self.assertEqual(result.attempts[0].order, 2)
        self.assertEqual(prepared[0]["local_rtp_port_index"], 1)

    def test_later_forward_does_not_ring_browser_again(self) -> None:
        browser = SimpleNamespace(endpoint_id="browser")
        result, registry, prepared = self._prepare(
            initial_selection=False,
            browser_legs={"Browser": browser},
        )

        self.assertEqual(result.browser_legs, [])
        self.assertEqual(registry.claims, [])
        self.assertEqual([item["member"] for item in prepared], ["Desk"])

    def test_blocked_and_busy_browser_candidates_are_excluded(self) -> None:
        blocked = SimpleNamespace(endpoint_id="blocked")
        busy = SimpleNamespace(endpoint_id="busy")
        registry = _Registry(busy={"busy"})
        result = forward_candidates.ForwardGroupCandidates()

        forward_candidates.prepare_forward_group_candidates(
            result,
            forward_candidates.ForwardGroupCandidateRuntime(
                registry=registry,
                endpoint_registry=_EndpointRegistry(
                    {
                        "blocked": SimpleNamespace(blocked=True),
                        "busy": SimpleNamespace(blocked=False),
                    }
                ),
                browser_leg_for_member=(
                    lambda member, _peers, _entries: {
                        "Blocked": blocked,
                        "Busy": busy,
                    }.get(member)
                ),
                prepare_outbound_leg=lambda **_kwargs: None,
            ),
            invite=SimpleNamespace(
                call_id="source-call",
                caller="Caller",
                source_host="caller.local",
            ),
            members=["Blocked", "Busy"],
            peers=[],
            roster_entries=[],
            local_name="Caller",
            initial_selection=True,
        )

        self.assertEqual(result.browser_legs, [])
        self.assertEqual(result.attempts, [])
        self.assertEqual(registry.claims, [])


if __name__ == "__main__":
    unittest.main()
